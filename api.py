"""
api.py

The bridge between the UI (index.html's JS) and the Python backend.
An instance of Api is passed to webview.create_window(..., js_api=...),
which makes every public method callable from JS as
`window.pywebview.api.<method_name>(...)`, returning a Promise.

Results flow the other direction (Python -> JS) via
`window.evaluate_js(...)`, calling the `updateSubtitle` and
`setEngineBadge` hooks added to index.html.

Usage:
    api = Api()
    window = webview.create_window("MithraCorp", "index.html", js_api=api)
    api.attach_window(window)
    webview.start()
"""

import functools
import json
import threading
import time
import webbrowser
from typing import Optional
from urllib.parse import urlencode

import requests
import webview

import history
import usage
from app_settings import load_settings, save_settings
from audio import list_input_devices
from config import settings
from engines.base import TranslationResult
from licensing import (
    ActivationResult,
    activate,
    clear_cached_token,
    deactivate_this_device,
    get_cached_license_status,
    get_cached_token,
)
from pipeline import TranslationPipeline
from updater import check_for_update as _check_for_update
from updater import get_current_version


def _log_click(func):
    """
    _log_click(func)
    Usage: decorator — put @_log_click above any Api method that's
    called directly from a UI button/control. Prints the method name
    and whatever args it was called with to the terminal running
    main.py the instant that happens, purely so you can watch along
    while clicking through the app and confirm exactly which handler
    fired (and with what values) for a given button. Doesn't change
    behavior or return value at all.
    """
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        arg_bits = [repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()]
        print(f"[BUTTON] {func.__name__}({', '.join(arg_bits)})", flush=True)
        return func(self, *args, **kwargs)
    return wrapper


class Api:
    """
    Api
    Usage: see module docstring. Every method here is directly callable
    from index.html's JS via window.pywebview.api.*; keep signatures
    JSON-serializable in both directions.
    """

    def __init__(self) -> None:
        self._window = None
        # The second, full-screen captions window opened by the stage's
        # "Full Screen" button (translation.html — see
        # open_translation_window). None whenever it isn't open, which
        # is also the "is it open?" check everywhere below; only one
        # can exist at a time.
        self._translation_window = None
        # The most recent window.updateSubtitle(...) script string
        # pushed to the main window, replayed into the Translation
        # window as soon as it finishes loading so it opens already in
        # sync instead of showing "Waiting for speech…" until the next
        # result arrives. None until the first result of this run.
        self._last_subtitle: Optional[str] = None
        self._capture_stream = None
        self._logged_first_push = False
        self._logged_no_window_warning = False
        self._current_from_lang = "English"
        self._current_to_lang = "Persian (Farsi)"
        self._pipeline = TranslationPipeline(
            on_result=self._push_result,
            on_engine_change=self._push_engine_change,
            on_level=self._push_audio_level,
        )
        # Prune old history on every launch according to whatever the
        # retention preference currently is, so files don't accumulate
        # forever even if the user never opens Settings.
        history.prune(load_settings().get("history_retention", "30_days"))

        # --- Online-engine usage tracking (see usage.py) ---
        # _online_started_at is a time.monotonic() timestamp for when
        # the ONLINE engine (not offline — that's unmetered) last
        # started being active; None while it isn't. monotonic() is
        # used rather than time.time() since it can't jump backwards
        # from a system clock change mid-session, which would corrupt
        # the elapsed-time calculation.
        self._online_started_at: Optional[float] = None
        self._usage_lock = threading.Lock()
        self._latest_usage_snapshot: Optional[usage.UsageSnapshot] = usage.get_cached_usage_snapshot()
        # Flushes periodically (not just on engine-change/stop) so a
        # long session that ends in a crash or force-quit doesn't lose
        # more than ~USAGE_FLUSH_INTERVAL_SECONDS of reportable usage.
        # Always running (daemon thread) but a no-op whenever
        # _online_started_at is None, so it costs nothing while the
        # offline engine or no session at all is active.
        threading.Thread(target=self._usage_flush_loop, daemon=True).start()

    def attach_window(self, window) -> None:
        """
        attach_window(window)
        Usage: called once from main.py right after webview.create_window,
        so this Api instance can push updates into that window via
        evaluate_js. Must happen before any translation session starts.
        """
        self._window = window

    def _push_result(self, result: TranslationResult) -> None:
        """
        _push_result(result)
        Usage: internal — the pipeline's on_result callback. Runs on a
        background thread, so it calls evaluate_js (thread-safe in
        pywebview) rather than touching any UI toolkit directly. Escapes
        text via json.dumps to safely pass through untrusted transcribed
        speech as a JS string literal. Also logs finalized (not interim)
        results to local history, unless the user has set history
        retention to "none".

        Mirrors the identical call into the Translation window whenever
        that's open (see open_translation_window). translation.html
        defines the same window.updateSubtitle hook index.html does
        precisely so this stays one script string sent to two windows,
        rather than two divergent formatting paths that could drift.
        """
        if result.is_final:
            retention = load_settings().get("history_retention", "30_days")
            if retention != "none":
                history.append_entry(result.original_text, result.translated_text, self._current_from_lang, self._current_to_lang)

        original = json.dumps(result.original_text)
        translated = json.dumps(result.translated_text)
        is_final = "true" if result.is_final else "false"
        script = f"window.updateSubtitle({original}, {translated}, {is_final})"
        self._last_subtitle = script
        if self._window is not None:
            self._window.evaluate_js(script)
        self._push_to_translation_window(script)

    def _push_engine_change(self, mode: str) -> None:
        """
        _push_engine_change(mode)
        Usage: internal — the pipeline's on_engine_change callback. Lets
        the UI show a small "offline mode" indicator when Azure is
        unavailable and the app has fallen back automatically. Also
        starts/stops online-usage tracking (see usage.py) — the offline
        engine is unmetered, so time only accumulates while mode is
        "online".
        """
        if mode == "online":
            self._start_online_tracking()
        else:
            self._flush_online_usage()
        if self._window is None:
            return
        self._window.evaluate_js(f"window.setEngineBadge({json.dumps(mode)})")

    def _start_online_tracking(self) -> None:
        """
        _start_online_tracking()
        Usage: internal — called whenever the pipeline reports the
        online engine became active (initial start, or the watchdog
        recovering back onto it). No-ops the clock-start if already
        tracking, so a redundant on_engine_change("online") call never
        resets it and under-counts. If there's no usage snapshot at
        all yet (very first online session this run), seeds one
        immediately with a 0-second report — otherwise the countdown
        pill would sit empty for up to USAGE_FLUSH_INTERVAL_SECONDS
        until the first periodic flush.
        """
        with self._usage_lock:
            already_tracking = self._online_started_at is not None
            if not already_tracking:
                self._online_started_at = time.monotonic()
        if already_tracking or self._latest_usage_snapshot is not None:
            return
        cached_status = get_cached_license_status()
        snapshot = usage.report_usage(get_cached_token(), 0, plan_code=cached_status.plan_code)
        self._latest_usage_snapshot = snapshot

    def _flush_online_usage(self) -> Optional[dict]:
        """
        _flush_online_usage()
        Usage: internal — reports however many seconds have elapsed
        since online tracking last started or was last flushed
        (whichever is more recent), then resets the clock rather than
        clearing it entirely, so a still-ongoing online session keeps
        accumulating correctly across multiple flushes rather than
        only ever reporting once at the very end. Called from three
        places: _push_engine_change() (engine switches away from
        online), stop_session() (session ends), and the periodic
        _usage_flush_loop() below (long-running safety net). Safe to
        call when nothing is being tracked (no-ops, returns None).
        Also handles enforcement: if the server reports the quota is
        now exceeded, stops the session and notifies the UI.
        """
        with self._usage_lock:
            if self._online_started_at is None:
                return None
            elapsed = time.monotonic() - self._online_started_at
            self._online_started_at = time.monotonic()

        cached_status = get_cached_license_status()
        snapshot = usage.report_usage(get_cached_token(), elapsed, plan_code=cached_status.plan_code)
        self._latest_usage_snapshot = snapshot
        if snapshot is not None and snapshot.exceeded:
            self._on_usage_exceeded()
        return self._usage_snapshot_to_dict(snapshot)

    def _on_usage_exceeded(self) -> None:
        """
        _on_usage_exceeded()
        Usage: internal — called the moment a usage report comes back
        exceeded=True. Ends the session outright (mirrors what a
        failed start_session() plan check already does — see
        toggleListening()'s handling of result.ok in index.html) and
        pushes a dedicated JS hook so the UI can show a clear message
        rather than just silently going quiet.
        """
        with self._usage_lock:
            self._online_started_at = None
        self._pipeline.stop()
        if self._window is not None:
            self._window.evaluate_js("window.onOnlineQuotaExceeded && window.onOnlineQuotaExceeded()")

    def _usage_flush_loop(self) -> None:
        """
        _usage_flush_loop()
        Usage: internal — runs for the lifetime of the app on a daemon
        thread. Every USAGE_FLUSH_INTERVAL_SECONDS, flushes whatever
        online usage has accumulated so far, so a crash or force-quit
        mid-session can't lose more than one interval's worth of
        reportable time. A no-op tick (via _flush_online_usage()'s own
        guard) whenever the online engine isn't currently active.
        """
        USAGE_FLUSH_INTERVAL_SECONDS = 20
        while True:
            time.sleep(USAGE_FLUSH_INTERVAL_SECONDS)
            try:
                self._flush_online_usage()
            except Exception as exc:
                print(f"[USAGE] periodic flush failed: {exc!r}", flush=True)

    @staticmethod
    def _usage_snapshot_to_dict(snapshot: Optional[usage.UsageSnapshot]) -> Optional[dict]:
        """
        _usage_snapshot_to_dict(snapshot)
        Usage: internal — JSON-serializable shape for get_usage_status()
        and the return value of _flush_online_usage(), factoring in
        pending_seconds (usage reported locally but not yet confirmed
        by the server, e.g. during a network blip) so the displayed
        countdown reflects the most current estimate available.
        """
        if snapshot is None:
            return None
        remaining = snapshot.seconds_remaining
        if remaining is not None:
            remaining = max(0, remaining)
        return {
            "seconds_used": snapshot.seconds_used,
            "seconds_included": snapshot.seconds_included,
            "seconds_remaining": remaining,
            "period_end": snapshot.period_end,
            "exceeded": snapshot.exceeded,
        }

    def get_usage_status(self):
        """
        get_usage_status()
        Usage (JS): window.pywebview.api.get_usage_status().then(status => ...)
        Polled periodically by the countdown pill in index.html.
        Returns the latest known snapshot (cached from disk on launch,
        refreshed by every _flush_online_usage() call) without itself
        triggering a network call or resetting the tracking clock —
        that only happens on an actual engine-mode change, session
        stop, or the periodic background flush. Returns None if this
        plan has no online usage tracked yet (e.g. never used the
        online engine, or an offline-only/pay-as-you-go plan with no
        monthly cap to show).
        """
        return self._usage_snapshot_to_dict(self._latest_usage_snapshot)

    def _push_audio_level(self, level: float) -> None:
        """
        _push_audio_level(level)
        Usage: internal — the pipeline's on_level callback, called once
        per captured audio chunk (~10 times/sec) with a 0.0-1.0 volume
        reading. Drives the UI's waveform bars with real microphone
        input instead of a decorative animation. Runs on the audio
        callback thread; evaluate_js is safe to call from any thread.
        """
        if self._window is None:
            return
        self._window.evaluate_js(f"window.updateAudioLevel({level:.3f})")

    # ---- Methods callable from JS ----

    def get_license_status(self):
        """
        get_license_status()
        Usage (JS): window.pywebview.api.get_license_status().then(status => ...)
        Called once on page load. Verifies any cached license token
        offline (no network call) and returns whether the app should
        show the license gate or go straight into Live Translation.
        Also called by the Settings page's Account section — key_code
        is included so the "Deactivate this device" button can use it.
        """
        status = get_cached_license_status()
        return {
            "valid": status.valid,
            "reason": status.reason,
            "plan_code": status.plan_code,
            "online_allowed": status.online_allowed,
            "offline_allowed": status.offline_allowed,
            "key_code": status.key_code,
        }

    @_log_click
    def deactivate_device(self, key_code: str):
        """
        deactivate_device(key_code)
        Usage (JS): window.pywebview.api.deactivate_device(status.key_code)
        Bound to the Settings page's "Deactivate this device" button.
        Frees the seat on the server (best-effort — still clears the
        local cache even if offline) and clears the cached token, so
        the app is no longer licensed on this device. index.html shows
        the license gate again immediately after this call succeeds
        (see its deactivateDeviceBtn click handler) rather than
        waiting for a relaunch.
        """
        deactivate_this_device(key_code)
        return {"ok": True}

    @_log_click
    def activate_license(self, key_code: str):
        """
        activate_license(key_code)
        Usage (JS): window.pywebview.api.activate_license("MVCE-7F3A-9K2Q-XPL4")
        Called from the license-gate form. Requires internet for this
        one call; on success the token is cached locally and future
        launches work via get_license_status() with no network needed.
        """
        result: ActivationResult = activate(key_code)
        return {"ok": result.ok, "error": result.error}

    def list_microphones(self) -> list:
        """
        list_microphones()
        Usage (JS): window.pywebview.api.list_microphones().then(devices => ...)
        Returns the list used to populate the Microphone selector.
        """
        return list_input_devices()

    @_log_click
    def start_session(self, from_lang: str, to_lang: str, device_index: Optional[int] = None, mode: str = "auto"):
        """
        start_session(from_lang, to_lang, device_index, mode)
        Usage (JS): window.pywebview.api.start_session("English", "Persian (Farsi)", null, "auto")
        Begins capturing the mic and streaming translations back via
        updateSubtitle(). mode is "auto" | "online" | "offline".

        This is the actual enforcement point for plan-based engine
        restriction — not just the Settings page UI. Before this existed,
        a user could pick "Online Only" in Settings regardless of their
        plan, and if Azure happened to be configured, they'd get it for
        free; an offline-restricted plan had literally nothing stopping
        online access. Now: an explicit "online"/"offline" request that
        the license doesn't include is rejected outright (returns
        ok=False), and "auto" is silently clamped to whichever single
        engine the plan actually grants — for an offline-only plan this
        also means the pipeline never even attempts Azure, not just that
        it falls back afterward.
        """
        status = get_cached_license_status()
        if not status.valid:
            return {"ok": False, "error": "not_licensed"}

        if mode == "online" and not status.online_allowed:
            return {"ok": False, "error": "online_not_in_plan"}
        if mode == "offline" and not status.offline_allowed:
            return {"ok": False, "error": "offline_not_in_plan"}
        if mode == "auto":
            if status.online_allowed and not status.offline_allowed:
                mode = "online"  # no offline entitlement — pipeline must not silently fall back to it
            elif status.offline_allowed and not status.online_allowed:
                mode = "offline"  # no online entitlement — never even attempt Azure
            elif not status.online_allowed and not status.offline_allowed:
                return {"ok": False, "error": "no_engine_in_plan"}
            # else: both allowed, "auto" proceeds as normal (try online, fall back to offline)

        self._current_from_lang, self._current_to_lang = from_lang, to_lang
        self._pipeline.start(from_lang, to_lang, device_index, mode)  # type: ignore[arg-type]
        return {"ok": True}

    @_log_click
    def stop_session(self):
        """
        stop_session()
        Usage (JS): window.pywebview.api.stop_session()
        Ends the session — releases the mic and closes the active
        engine, and flushes any accumulated online usage (see
        usage.py) so it isn't left to the next periodic flush.
        """
        self._pipeline.stop()
        self._flush_online_usage()
        return {"ok": True}

    @_log_click
    def set_paused(self, paused: bool):
        """
        set_paused(paused)
        Usage (JS): window.pywebview.api.set_paused(true)
        Bound to the Pause/Resume button; keeps the session warm.
        """
        self._pipeline.set_paused(paused)
        return {"ok": True}

    def get_app_settings(self):
        """
        get_app_settings()
        Usage (JS): window.pywebview.api.get_app_settings().then(settings => ...)
        Called when the Settings page loads. Returns the persisted
        preferences (history retention, engine mode, mic device) with
        defaults filled in for anything never explicitly set.
        """
        return load_settings()

    @_log_click
    def save_app_settings(self, patch: dict):
        """
        save_app_settings(patch)
        Usage (JS): window.pywebview.api.save_app_settings({engine_mode: "offline"})
        Called whenever the user changes something on the Settings page.
        Merges the patch onto existing saved settings and persists to
        disk immediately — no separate "Save" button, changes take
        effect right away (engine_mode and microphone_device_index are
        read by start_session on the NEXT session start, not
        retroactively for an already-running one). If history_retention
        changed, immediately re-prunes so a switch to "none" or a
        shorter window takes effect right away, not just on next launch.
        """
        result = save_settings(patch)
        if "history_retention" in patch:
            history.prune(result["history_retention"])
        return result

    def get_history_folder(self):
        """
        get_history_folder()
        Usage (JS): window.pywebview.api.get_history_folder().then(path => ...)
        Returns the local folder path where translation history is
        stored, for display on the Settings page next to the "Open
        History Folder" button.
        """
        return str(history.history_dir())

    @_log_click
    def open_history_folder(self):
        """
        open_history_folder()
        Usage (JS): window.pywebview.api.open_history_folder()
        Bound to the Settings page's "Open History Folder" button.
        Opens the folder directly in the OS file browser (Explorer on
        Windows) rather than making the user navigate there by hand.
        """
        import os
        import platform
        import subprocess

        path = history.history_dir()
        system = platform.system()
        if system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return {"ok": True}

    @_log_click
    def check_for_update(self):
        """
        check_for_update()
        Usage (JS): window.pywebview.api.check_for_update().then(result => ...)
        Called once after the license gate passes. Compares this
        build's bundled version against the license server's published
        latest (see updater.py, server's GET /v1/latest-version).
        Requires internet; silently reports no update available on any
        failure rather than erroring, since a failed check shouldn't
        interrupt using the app.
        """
        result = _check_for_update()
        return {
            "update_available": result.update_available,
            "current_version": result.current_version,
            "latest_version": result.latest_version,
            "download_url": result.download_url,
            "notes": result.notes,
        }

    def get_app_version(self):
        """
        get_app_version()
        Usage (JS): window.pywebview.api.get_app_version().then(result => ...)
        Called when the About page is opened. Unlike check_for_update()
        above, this needs no network at all — it just reads the VERSION
        file bundled into this build (see updater.py's
        get_current_version), so the About page always shows the
        actual running version even fully offline.
        """
        return {"version": get_current_version()}

    @_log_click
    def open_translation_window(self, view_settings: Optional[dict] = None):
        """
        open_translation_window(view_settings=None)
        Usage (JS): window.pywebview.api.open_translation_window({position, appearance, textColor, fontSize, displayMode})
        Bound to the stage's bottom-right "Full Screen" button
        (id="maximizeBtn" in index.html). Does two things, in this
        order:
          1. opens a second, full-screen window titled "Translation"
             (translation.html) that shows nothing but the live
             captions, mirrored from the same _push_result() that
             feeds the main stage;
          2. minimizes the main window down to the OS taskbar, so the
             control UI is out of the way while that full-screen
             caption display is up.

        Order matters: the new window is created BEFORE the main one
        is minimized. Minimizing first tends to hand focus to whatever
        was behind the app, and the new window can then open behind
        that instead of coming up front — which reads as "the button
        did nothing but minimize."

        view_settings mirrors whatever Caption Style / Display / Text
        Color is currently active on the main stage and is passed
        through as query-string params, so the full-screen window opens
        matching rather than resetting to defaults (see
        translation.html's applyStyleFromParams()).

        No-ops on the create step if a Translation window is already
        open — only one at a time. No-ops entirely if called before
        attach_window() has run.
        """
        if self._window is None:
            return {"ok": False, "error": "no_window"}

        if self._translation_window is None:
            view_settings = view_settings or {}
            query = urlencode({
                "position": view_settings.get("position", "bottom"),
                "appearance": view_settings.get("appearance", "dark"),
                "textColor": view_settings.get("textColor", "white"),
                "fontSize": view_settings.get("fontSize", 24),
                "displayMode": view_settings.get("displayMode", "both"),
            })
            self._translation_window = webview.create_window(
                "Translation",
                f"translation.html?{query}",
                js_api=self,
                width=1280,
                height=720,
                fullscreen=True,
                background_color="#0b0e14",
            )
            self._translation_window.events.closed += self._on_translation_closed
            self._translation_window.events.loaded += self._push_last_subtitle_to_translation

        self._window.minimize()
        return {"ok": True}

    def _push_to_translation_window(self, script: str) -> None:
        """
        _push_to_translation_window(script)
        Usage: internal — sends a JS string to the Translation window
        if one is open, swallowing the error if it isn't. The guard is
        not just a None check: this is called from _push_result() on a
        background engine thread, so the window can be destroyed by the
        user in between the check and the call, which surfaces as an
        exception from evaluate_js on a dead window. A caption update
        is never worth taking down the audio pipeline for.
        """
        window = self._translation_window
        if window is None:
            return
        try:
            window.evaluate_js(script)
        except Exception as exc:  # noqa: BLE001 - window closed mid-push; nothing to recover
            print(f"[TRANSLATION] push failed (window likely closed): {exc!r}", flush=True)

    def _push_last_subtitle_to_translation(self):
        """
        _push_last_subtitle_to_translation()
        Usage: internal — bound to the Translation window's `loaded`
        event (fires once its DOM/JS is ready), not called directly.
        Replays whatever was last shown on the main stage so the
        full-screen window opens already in sync instead of sitting on
        its "Waiting for speech…" placeholder until the next live
        result comes in. No-ops if nothing has been said yet this run.
        """
        if self._last_subtitle is not None:
            self._push_to_translation_window(self._last_subtitle)

    def _on_translation_closed(self):
        """
        _on_translation_closed()
        Usage: internal — bound to the Translation window's `closed`
        event, so the main window comes back no matter how that window
        went away: its own X button and the Esc key (both via
        close_translation_window below) and the OS window chrome all
        end up here.

        Clearing _translation_window first matters: restore() below can
        block briefly on some backends, and _push_result() is still
        running on the engine thread throughout — leaving a stale
        handle in place means it would keep pushing captions at a
        destroyed window for that whole window.
        """
        self._translation_window = None
        if self._window is not None:
            self._window.restore()

    def close_translation_window(self):
        """
        close_translation_window()
        Usage (JS, from translation.html): window.pywebview.api.close_translation_window()
        Closes the full-screen Translation window. Doesn't restore the
        main window itself — destroy() fires the window's `closed`
        event, and _on_translation_closed above is what does the
        restoring, so every dismissal path ends up in exactly one place.
        """
        if self._translation_window is not None:
            self._translation_window.destroy()
        return {"ok": True}

    @_log_click
    def exit_app(self):
        """
        exit_app()
        Usage (JS): window.pywebview.api.exit_app()
        Closes the app entirely. Bound to the sidebar's Exit button
        (id="exitAppBtn" in index.html), below About. Flushes any
        accumulated online usage first, best-effort, so quitting mid-
        session doesn't lose more than the periodic flush interval
        would have anyway.

        destroy() closes one window at a time, and webview.start()'s
        event loop only ends once EVERY window is gone — so the
        Translation window (if the "Full Screen" button opened one)
        has to be closed too, or Exit would minimize/close the main
        window while leaving a full-screen caption display stranded on
        the user's screen with no UI left to dismiss it. It's closed
        first so _on_translation_closed's restore() lands on a window
        that still exists.
        """
        try:
            self._flush_online_usage()
        except Exception:
            pass  # never block quitting the app over a usage-reporting hiccup
        if self._translation_window is not None:
            self._translation_window.destroy()
            self._translation_window = None
        if self._window is not None:
            self._window.destroy()

    @_log_click
    def open_external_link(self, url: str):
        """
        open_external_link(url)
        Usage (JS): window.pywebview.api.open_external_link("https://mithracorp.com/contact.html")
        Opens a URL in the user's default browser rather than navigating
        the app's own window to it — used for the update banner's
        download link and the license gate's renew/contact links.
        """
        webbrowser.open(url)
        return {"ok": True}

    @_log_click
    def verify_license_online(self):
        """
        verify_license_online()
        Usage (JS): window.pywebview.api.verify_license_online().then(status => ...)
        Called in the background shortly after get_license_status()
        passes offline — this is the "actually check if it's still
        active" step. get_license_status() only verifies the cached
        token's signature and expiry locally, which can't detect a
        revocation until the token's ~30-day validity window naturally
        ends. This calls the server's /v1/heartbeat instead, so a
        revoked key gets caught the next time the user has internet,
        not up to a month later. Requires internet; on failure (offline,
        server unreachable) returns valid=True so a temporarily
        disconnected user is never locked out — this is a best-effort
        early-warning check, not the source of truth get_license_status
        already is.
        """
        cached = get_cached_license_status()
        if not cached.valid or not cached.key_code:
            return {"valid": cached.valid, "reason": cached.reason}

        try:
            resp = requests.post(
                f"{settings.license_server_url}/v1/heartbeat",
                json={"token": get_cached_token()},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException:
            return {"valid": True, "reason": None}  # offline — don't punish the user for that

        if not data.get("valid", True):
            # Server says this key is no longer good (revoked/expired/
            # subscription inactive) — clear the local cache so the
            # gate reappears immediately instead of waiting for the
            # cached token to naturally expire.
            clear_cached_token()
            return {"valid": False, "reason": data.get("reason", "revoked")}

        return {"valid": True, "reason": None}


    def list_open_windows(self):
        """
        list_open_windows()
        Usage (JS): window.pywebview.api.list_open_windows().then(windows => ...)
        Populates the "Select App" picker with currently open windows
        (each as {id, title}), excluding MithraVoice's own window.
        Windows-only — returns an empty list with an "unsupported"
        flag on other platforms rather than raising, so the picker can
        show a clear message instead of crashing.
        """
        try:
            from window_capture import list_capturable_windows
        except ImportError:
            return {"supported": False, "windows": []}

        windows = list_capturable_windows(exclude_titles=["MithraVoice — Live Translation"])
        return {"supported": True, "windows": [{"id": w["hwnd"], "title": w["title"]} for w in windows]}

    @_log_click
    def start_app_capture(self, window_id: int):
        """
        start_app_capture(window_id)
        Usage (JS): window.pywebview.api.start_app_capture(12345)
        Begins live-capturing the chosen window's content (~2fps) and
        pushing frames into the main window's stage background,
        replacing the default scene graphic — this is what lets a
        spreadsheet or document be visible alongside the caption
        without a separate always-on-top OS window.
        """
        try:
            from window_capture import WindowCaptureStream
        except ImportError as exc:
            print(f"[capture] window_capture unavailable on this platform: {exc}")
            return {"ok": False, "error": "unsupported_platform"}

        print(f"[capture] starting capture for window id {window_id}")
        self._stop_capture_stream()
        self._logged_first_push = False
        self._capture_stream = WindowCaptureStream(window_id, on_frame=self._push_captured_frame)
        self._capture_stream.start()
        return {"ok": True}

    @_log_click
    def stop_app_capture(self):
        """
        stop_app_capture()
        Usage (JS): window.pywebview.api.stop_app_capture()
        Stops live capture and tells the UI to revert to the default
        scene background. Safe to call even if nothing is currently
        being captured.
        """
        self._stop_capture_stream()
        if self._window is not None:
            self._window.evaluate_js("window.clearCapturedFrame && window.clearCapturedFrame()")
        return {"ok": True}

    def _stop_capture_stream(self) -> None:
        """
        _stop_capture_stream()
        Usage: internal — stops and clears the active capture stream,
        if any. Shared by start_app_capture (replaces any previous
        capture before starting a new one) and stop_app_capture.
        """
        if self._capture_stream is not None:
            self._capture_stream.stop()
            self._capture_stream = None

    def _push_captured_frame(self, data_url: str) -> None:
        """
        _push_captured_frame(data_url)
        Usage: internal — WindowCaptureStream's on_frame callback, runs
        on its own background thread roughly twice a second. Pushes
        each frame straight into the main window; there's no separate
        overlay window anymore, so this only ever targets self._window.
        Logs only the first call (not every frame, to avoid flooding
        the console at 2fps) so it's visible whether evaluate_js is
        actually reaching the main window at all.
        """
        if self._window is None:
            if not self._logged_no_window_warning:
                print("[capture] got a frame but self._window is None — can't push it anywhere")
                self._logged_no_window_warning = True
            return
        if not self._logged_first_push:
            print(f"[capture] pushing first frame to main window via evaluate_js ({len(data_url)} chars)")
            self._logged_first_push = True
        self._window.evaluate_js(f"window.updateCapturedFrame({json.dumps(data_url)})")
