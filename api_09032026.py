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

import json
import webbrowser
from typing import Optional
from urllib.parse import urlencode

import requests
import webview

import history
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


class Api:
    """
    Api
    Usage: see module docstring. Every method here is directly callable
    from index.html's JS via window.pywebview.api.*; keep signatures
    JSON-serializable in both directions.
    """

    def __init__(self) -> None:
        self._window = None
        self._overlay_window = None
        # (width, height) captured right before minimize_window() calls
        # self._window.minimize() — restore() alone isn't reliable
        # across every pywebview backend (some come back maximized
        # instead of at their prior size), so _on_overlay_closed()
        # follows it with an explicit resize() back to this.
        self._pre_minimize_size = None
        # Last (original, translated, is_final) pushed to updateSubtitle,
        # so open_caption_overlay() can replay it onto the overlay the
        # moment it opens — otherwise the overlay sits on its "Waiting
        # for speech…" placeholder until the next live result comes in,
        # even though the main stage already has text showing.
        self._last_subtitle = None
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
        self._last_subtitle = script
        if self._window is not None:
            self._window.evaluate_js(script)
        # overlay.html defines the same updateSubtitle hook (see its
        # module docstring), so the exact same call keeps it in sync
        # whenever it's open.
        if self._overlay_window is not None:
            self._overlay_window.evaluate_js(script)

    def _push_engine_change(self, mode: str) -> None:
        """
        _push_engine_change(mode)
        Usage: internal — the pipeline's on_engine_change callback. Lets
        the UI show a small "offline mode" indicator when Azure is
        unavailable and the app has fallen back automatically.
        """
        if self._window is None:
            return
        self._window.evaluate_js(f"window.setEngineBadge({json.dumps(mode)})")

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

    def deactivate_device(self, key_code: str):
        """
        deactivate_device(key_code)
        Usage (JS): window.pywebview.api.deactivate_device(status.key_code)
        Bound to the Settings page's "Deactivate this device" button.
        Frees the seat on the server (best-effort — still clears the
        local cache even if offline) and clears the cached token, so
        the license gate reappears on next launch.
        """
        deactivate_this_device(key_code)
        return {"ok": True}

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

    def stop_session(self):
        """
        stop_session()
        Usage (JS): window.pywebview.api.stop_session()
        Ends the session — releases the mic and closes the active engine.
        """
        self._pipeline.stop()
        return {"ok": True}

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

    def minimize_window(self, overlay_settings: Optional[dict] = None):
        """
        minimize_window(overlay_settings=None)
        Usage (JS): window.pywebview.api.minimize_window({position, displayMode, fontSize})
        Sends the main app window down to the OS taskbar/dock, then
        opens the small always-on-top captions overlay (see
        open_caption_overlay below) so translations stay visible while
        the main window is out of the way. Bound to the stage's
        bottom-right button (id="maximizeBtn" in index.html) in place
        of the old enterCompactMode() behavior. No-ops quietly if
        called before attach_window() has run.
        """
        if self._window is None:
            return
        self._pre_minimize_size = (self._window.width, self._window.height)
        self._window.minimize()
        self.open_caption_overlay(overlay_settings)

    def open_caption_overlay(self, overlay_settings: Optional[dict] = None):
        """
        open_caption_overlay(overlay_settings=None)
        Usage (JS): window.pywebview.api.open_caption_overlay({position, appearance, fontSize, displayMode})
        Creates the small always-on-top captions window (overlay.html)
        that stays visible while the main window is minimized.
        overlay_settings mirrors whatever Position/Appearance/Font
        Size/Display mode is currently active on the main stage, and is
        passed through as query-string params so overlay.html can style
        itself to match on load (see its own applyStyleFromParams()).
        No-ops if an overlay is already open — only one at a time.
        """
        if self._overlay_window is not None:
            return
        overlay_settings = overlay_settings or {}
        query = urlencode({
            "position": overlay_settings.get("position", "bottom"),
            "appearance": overlay_settings.get("appearance", "dark"),
            "fontSize": overlay_settings.get("fontSize", 24),
            "displayMode": overlay_settings.get("displayMode", "both"),
        })
        self._overlay_window = webview.create_window(
            "MithraVoice Captions",
            f"overlay.html?{query}",
            js_api=self,
            width=560,
            height=180,
            min_size=(300, 120),
            on_top=True,
            background_color="#0b0e14",
        )
        self._overlay_window.events.closed += self._on_overlay_closed
        self._overlay_window.events.loaded += self._push_last_subtitle_to_overlay

    def _push_last_subtitle_to_overlay(self):
        """
        _push_last_subtitle_to_overlay()
        Usage: internal — bound to the overlay window's `loaded` event
        (fires once its DOM/JS is ready), not called directly. Replays
        whatever was last shown on the main stage so the overlay opens
        already in sync instead of sitting on its "Waiting for
        speech…" placeholder until the next live result comes in.
        No-ops if nothing has been said yet this session.
        """
        if self._last_subtitle is not None and self._overlay_window is not None:
            self._overlay_window.evaluate_js(self._last_subtitle)

    def _on_overlay_closed(self):
        """
        _on_overlay_closed()
        Usage: internal — bound to the overlay window's `closed` event
        so the main window comes back no matter how the overlay went
        away: its own Close button (via close_caption_overlay below) or
        the OS window-chrome close button directly both end up here.

        restore() alone isn't enough: on GTK (and similarly on other
        backends) it's scheduled via glib.idle_add rather than applied
        immediately, so a resize() called right after restore() can
        land while the window is still iconified — the window manager
        then hands it back at full size once it does deiconify, which
        is what showed up as "the app maximizes". Waiting on the
        window's own `restored` event (set by every backend once the
        state change actually completes — see pywebview's window.py)
        before resizing avoids that race. This runs on the event
        dispatcher's own thread (pywebview's Event.set() spawns one),
        never on the GUI thread, so blocking here on wait() is safe.
        """
        self._overlay_window = None
        if self._window is not None:
            self._window.events.restored.clear()
            self._window.restore()
            self._window.events.restored.wait(2)
            if self._pre_minimize_size is not None:
                self._window.resize(*self._pre_minimize_size)

    def close_caption_overlay(self):
        """
        close_caption_overlay()
        Usage (JS, from overlay.html): window.pywebview.api.close_caption_overlay()
        Closes the captions overlay, which triggers its `closed` event
        and so also runs _on_overlay_closed above to restore the main
        window — this is what the overlay's own X button calls.
        """
        if self._overlay_window is not None:
            self._overlay_window.destroy()

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
