"""
main.py

App entrypoint. Creates the pywebview window loading index.html, wires
up the Api bridge, and starts the event loop.

Usage:
    python main.py
"""

import ctypes
import faulthandler
import os
import sys
from ctypes import wintypes
from pathlib import Path

import webview

from api import Api


def enable_crash_log() -> None:
    """
    enable_crash_log()
    Usage: called once at the very top of main(), before anything heavy
    is imported. Installs Python's faulthandler so a HARD crash leaves
    a trace behind.

    This exists because of a crash class that is otherwise completely
    undiagnosable from a log: the app disappears with no Python
    traceback at all. A traceback means an exception, which means
    Python was in control; its absence means the process died inside
    native code — a segfault or abort from one of the C extensions this
    app loads (ctranslate2 via faster-whisper, torch via stanza, the
    Azure Speech SDK, WebView2). Those never produce a Python
    traceback, so the console just stops mid-line and there is nothing
    to go on.

    faulthandler catches the fatal signal and dumps every thread's
    Python stack before the process goes, which turns "it crashed" into
    "it crashed inside this call, on this thread, while that other
    thread was in there too" — the difference between guessing and
    knowing. Writes to stderr and also to a file next to the app, since
    a crash frequently takes the console with it.
    """
    faulthandler.enable()
    try:
        log_dir = Path(os.environ.get("APPDATA", Path.home())) / "MithraCorp"
        log_dir.mkdir(parents=True, exist_ok=True)
        # Held open deliberately for the process's lifetime: faulthandler
        # writes to this descriptor from a signal handler, where opening
        # a file is not safe.
        handle = open(log_dir / "crash.log", "a", buffering=1, encoding="utf-8", errors="replace")
        faulthandler.enable(file=handle, all_threads=True)
        sys.modules[__name__]._crash_log_handle = handle  # keep a reference alive
        print(f"[main] hard-crash traces will be written to {log_dir / 'crash.log'}", flush=True)
    except Exception as exc:  # noqa: BLE001 - stderr-only faulthandler is still better than none
        print(f"[main] could not open a crash log file ({exc!r}); using stderr only", flush=True)

# DWMWA_CAPTION_COLOR: the DWM window-attribute id for title-bar color,
# per the Windows SDK's dwmapi.h. Only supported on Windows 11 build
# 22000+; DwmSetWindowAttribute just returns a (harmless, ignored)
# error code on older Windows, so no version check is needed here.
DWMWA_CAPTION_COLOR = 35
TITLEBAR_RGB = (224, 185, 63)


def set_titlebar_color(window: "webview.Window") -> None:
    """
    set_titlebar_color(window)
    Usage: hooked to window.events.shown (see main()) rather than
    called directly — DwmSetWindowAttribute needs a real HWND, which
    only exists once pywebview's winforms backend has actually created
    the native Form, i.e. after the window is shown.
    """
    try:
        r, g, b = TITLEBAR_RGB
        # COLORREF format is 0x00BBGGRR (blue/green/red byte order),
        # the reverse of the RGB tuple's order — this is a Windows API
        # convention, not a typo.
        colorref = wintypes.DWORD(r | (g << 8) | (b << 16))
        hwnd = wintypes.HWND(window.native.Handle.ToInt64())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_CAPTION_COLOR, ctypes.byref(colorref), ctypes.sizeof(colorref)
        )
    except Exception:
        # Non-Windows, older Windows without this DWM attribute, or
        # any other environment where this simply isn't supported —
        # the app should keep running with the default title bar
        # rather than crash over a cosmetic detail.
        pass


def main() -> None:
    """
    main()
    Usage: run directly (`python main.py`) to launch the desktop app.
    Not intended to be imported/called from elsewhere.
    """
    enable_crash_log()
    api = Api()
    window = webview.create_window(
        "MithraVoice — Live Translation",
        "index.html",
        js_api=api,
        width=1280,
        height=840,
        # NOT (1024, 680): min_size is a hard floor pywebview enforces
        # on every resize() call for the LIFETIME of the window (it
        # can't be changed after creation, and there's no per-call
        # override) — the Pin feature (api.py's pin_caption_window)
        # resizes down to as little as 700x160 for a thin always-on-
        # top caption strip. With the old (1024, 680) floor, that
        # resize was silently clamped back up to 680px tall, which
        # combined with Pin's near-bottom-of-screen y position pushed
        # most of the window off-screen — the confirmed cause of a
        # real "window disappeared" report. This value is below every
        # geometry Pin ever requests, while still preventing a
        # genuinely degenerate zero-sized window.
        min_size=(300, 140),
        background_color="#0b0e14",
    )
    api.attach_window(window)
    window.events.shown += lambda: set_titlebar_color(window)
    # icon: sets both the window's title-bar icon and the OS taskbar
    # icon (pywebview's winforms backend assigns this straight to the
    # window's .Icon — see webview/platforms/winforms.py). Needs an
    # actual .ico file, not the source .png, since System.Drawing.Icon
    # only loads the Windows icon-directory format.
    webview.start(icon="assets/mithracorp_logo.ico")


if __name__ == "__main__":
    main()
