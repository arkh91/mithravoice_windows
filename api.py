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

import ctypes
import functools
import json
import sys
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
        # (x, y, width, height) captured right before shrink_to_captions()
        # resizes/moves the window down to a small caption bar, so
        # restore_full_size() can put it back exactly where it was.
        self._pre_shrink_geometry = None
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
        """
        if result.is_final:
            retention = load_settings().get("history_retention", "30_days")
            if retention != "none":
                history.append_entry(result.original_text, result.translated_text, self._current_from_lang, self._current_to_lang)

        original = json.dumps(result.original_text)
        translated = json.dumps(result.translated_text)
        is_final = "true" if result.is_final else "false"
        script = f"window.updateSubtitle({original}, {translated}, {is_final})"
        if self._window is not None:
            self._window.evaluate_js(script)

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
    def shrink_to_captions(self, overlay_settings: Optional[dict] = None):
        """
        shrink_to_captions(overlay_settings=None)
        Usage (JS): window.pywebview.api.shrink_to_captions({position, displayMode, fontSize})
        Replaces the old minimize_window() + open_caption_overlay()
        flow, which opened a SECOND native WebView2 window to act as
        an always-on-top captions overlay while the main window sat
        minimized in the taskbar. That approach hit a confirmed,
        persistent rendering bug on a real Windows machine: the second
        window would report itself as created, shown, correctly
        positioned, and topmost via every pywebview/Win32 API checked,
        yet still not actually paint on screen in most tests — flaky
        in a way that resisted several rounds of fixes (a topmost
        z-order bug, an event-registration race, a maximize/restore
        workaround, a DPI-scale mismatch) without ever becoming fully
        reliable.
        This sidesteps that whole class of bug by never creating a
        second window at all: it resizes and moves THIS window down
        to a small caption-bar size/position, and index.html's own JS
        (see shrinkToCaptions() there) swaps to a caption-only layout
        via the '.compact-mode' CSS class (hiding the sidebar,
        settings panel, and control bar). Bound to the stage's
        bottom-right button (id="maximizeBtn" in index.html).
        Captions keep updating exactly as before via the same
        _push_result() -> evaluate_js() path — there's no second DOM
        to keep in sync, so none of the old "replay the last caption
        when the overlay opens" logic is needed anymore either.
        No-ops quietly if called before attach_window() has run.
        """
        if self._window is None:
            return
        self._pre_shrink_geometry = (
            self._window.x, self._window.y, self._window.width, self._window.height,
        )
        overlay_settings = overlay_settings or {}
        position = overlay_settings.get("position", "bottom")

        screen = self._find_main_window_monitor()
        if screen is None:
            screen = self._screen_for_point((self._window.x, self._window.y))

        if screen is not None:
            third = screen.height // 3
            if position == "top":
                x, y, width, height = screen.x, screen.y, screen.width, third
            elif position == "center":
                # A true centered box (60% of screen width, one third
                # of screen height) rather than a full-width strip —
                # matches "in the middle of the page" rather than just
                # vertically-middle.
                width = int(screen.width * 0.6)
                height = third
                x = screen.x + (screen.width - width) // 2
                y = screen.y + (screen.height - height) // 2
            else:  # "bottom" (and default)
                x, y, width, height = screen.x, screen.y + screen.height - third, screen.width, third
        else:
            # No screen info available for some reason — still shrink
            # the window, just without repositioning it.
            x = y = None
            width, height = 560, 200

        print(
            f"[SHRINK] chosen_screen={screen} -> geometry x={x} y={y} "
            f"width={width} height={height}",
            flush=True,
        )
        self._window.resize(width, height)
        if x is not None and y is not None:
            self._window.move(x, y)

    @_log_click
    def restore_full_size(self):
        """
        restore_full_size()
        Usage (JS): window.pywebview.api.restore_full_size()
        Undoes shrink_to_captions() above: resizes/moves this window
        back to exactly where and how big it was before shrinking.
        Bound to the Exit button that appears while '.compact-mode' is
        on #app (id="exitCompactBtn" in index.html), which is also
        responsible for removing that CSS class so the full UI
        reappears. No-ops quietly if called before a shrink happened.
        """
        if self._window is None or self._pre_shrink_geometry is None:
            return
        x, y, width, height = self._pre_shrink_geometry
        self._window.resize(width, height)
        self._window.move(x, y)
        self._pre_shrink_geometry = None

    @_log_click
    def exit_app(self):
        """
        exit_app()
        Usage (JS): window.pywebview.api.exit_app()
        Closes the app entirely. Bound to the sidebar's Exit button
        (id="exitAppBtn" in index.html), below About. Flushes any
        accumulated online usage first, best-effort, so quitting mid-
        session doesn't lose more than the periodic flush interval
        would have anyway. destroy() closes just this window; since
        main.py only ever creates the one (master) window, that's
        enough to end webview.start()'s event loop and let the process
        exit normally afterward.
        """
        try:
            self._flush_online_usage()
        except Exception:
            pass  # never block quitting the app over a usage-reporting hiccup
        if self._window is not None:
            self._window.destroy()

    def _find_main_window_monitor(self):
        """
        _find_main_window_monitor()
        Usage: internal — Windows-only. Asks Windows directly, via the
        Win32 API (MonitorFromWindow + GetMonitorInfo), which physical
        monitor the "MithraVoice — Live Translation" window's title
        bar is actually on right now, then matches that against
        webview.screens() by nearest starting X coordinate to return
        one of its entries.

        This replaced comparing self._window.x/self._window.y against
        webview.screens() bounds directly, which had two real
        problems on a multi-monitor machine: (1) once the main window
        is minimized, Windows reports its position as an off-screen
        sentinel value (e.g. -32000, -32000), which made that lookup
        pick the wrong monitor entirely, and (2) mixed-DPI monitors
        don't always report x/y in coordinate spaces that line up
        cleanly for a simple containment check. MonitorFromWindow
        instead asks Windows for the actual monitor a window handle
        is on — this works correctly even while minimized, since
        Windows still tracks a minimized window's last on-screen
        placement internally.

        Returns None (falls back to screens()[0]) on any non-Windows
        OS, if the window can't be found, or on any Win32-call
        failure — never raises.
        """
        if sys.platform != "win32":
            return None
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "MithraVoice \u2014 Live Translation")
            if not hwnd:
                return None
            MONITOR_DEFAULTTONEAREST = 2
            hmonitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)

            class _RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long),
                ]

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                    ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong),
                ]

            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                return None
            monitor_x = info.rcMonitor.left

            screens = webview.screens
            if not screens:
                return None
            # Nearest starting X, not exact equality — sidesteps small
            # scale-correction differences between the raw Win32
            # monitor rect and webview.screens()'s own values on
            # mixed-DPI setups; monitors are laid out left-to-right,
            # so "closest" reliably means "same one" in practice.
            return min(screens, key=lambda s: abs(s.x - monitor_x))
        except Exception as e:
            print(f"[OVERLAY] Win32 monitor lookup failed: {e!r}", flush=True)
            return None

    def _screen_for_point(self, point):
        """
        _screen_for_point((x, y))
        Usage: internal — fallback for _find_main_window_monitor()
        above (non-Windows OSes, or if that Win32 lookup fails).
        Picks the webview.screens() entry whose bounds actually
        contain the given point, falling back to the first available
        screen (or None) if that lookup comes up empty.
        """
        screens = webview.screens
        if not screens:
            return None
        if point is not None:
            x, y = point
            for screen in screens:
                if screen.x <= x < screen.x + screen.width and screen.y <= y < screen.y + screen.height:
                    return screen
        return screens[0]

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
