"""
window_capture.py

Lists open windows and captures a specific window's live content as a
PNG image, so it can be embedded directly inside MithraVoice's own
stage (see api.py's list_open_windows/start_app_capture) instead of
relying on a separate always-on-top OS window — which proved
unreliable (invisible despite correct geometry) on some Windows/
WebView2 setups. This sidesteps that whole class of bug: everything
happens inside the one window MithraVoice already reliably controls.

Windows-only (uses pywin32). Not available on macOS/Linux — api.py
should treat ImportError from this module as "feature unavailable on
this platform" rather than a hard crash.

Uses the PrintWindow API rather than a plain screen-region screenshot,
since PrintWindow can capture a window's content even when it's
partially covered by other windows; a screen-region grab would only
capture whatever's actually on top at that pixel location.

Usage:
    from window_capture import list_capturable_windows, WindowCaptureStream
    windows = list_capturable_windows(exclude_titles=["MithraVoice — Live Translation"])
    stream = WindowCaptureStream(windows[0]["hwnd"], on_frame=my_callback)
    stream.start()
    ...
    stream.stop()
"""

import base64
import ctypes
import io
import threading
import time
from ctypes import wintypes
from typing import Callable, List, Optional, TypedDict

import win32gui
import win32ui
from PIL import Image

# PrintWindow is NOT wrapped by pywin32's win32gui module (a wrong
# assumption in an earlier version of this file caused every single
# capture attempt to fail with AttributeError) — it has to be called
# directly against user32.dll via ctypes. Explicit argtypes/restype
# matter here: without them, ctypes' default 32-bit int marshaling can
# silently truncate the HWND/HDC handles on 64-bit Windows, corrupting
# the call instead of just failing loudly.
_user32 = ctypes.windll.user32
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_user32.PrintWindow.restype = wintypes.BOOL


class WindowInfo(TypedDict):
    hwnd: int
    title: str


def list_capturable_windows(exclude_titles: Optional[List[str]] = None) -> List[WindowInfo]:
    """
    list_capturable_windows(exclude_titles)
    Usage: call to populate the "Select App" picker. Returns visible,
    top-level windows with a non-empty title, excluding MithraVoice's
    own window (pass its title in exclude_titles) so it can't try to
    capture itself.

    Minimized windows are NOT filtered by client-area size — a
    minimized window often reports a zero/degenerate client rect even
    though it's a perfectly real, normally-capturable application once
    you know to look past that. Filtering on rect size unconditionally
    was very likely hiding real open apps from the picker; only
    non-minimized windows get that size check now, since for those a
    zero rect really does mean "not a real content window" (a hidden
    helper/tray window, etc.).
    """
    exclude_titles = set(exclude_titles or [])
    windows: List[WindowInfo] = []
    skipped: List[tuple] = []

    def _callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        if title in exclude_titles:
            skipped.append((title, "excluded (MithraVoice's own window)"))
            return True

        is_minimized = win32gui.IsIconic(hwnd)
        if not is_minimized:
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            if (right - left) <= 0 or (bottom - top) <= 0:
                skipped.append((title, f"zero client rect ({right - left}x{bottom - top})"))
                return True

        windows.append({"hwnd": hwnd, "title": title})
        return True

    win32gui.EnumWindows(_callback, None)

    print(f"[capture] found {len(windows)} capturable window(s); skipped {len(skipped)}:")
    for title, reason in skipped:
        print(f"[capture]   skipped {title!r}: {reason}")

    return windows


def capture_window(hwnd: int) -> Optional[str]:
    """
    capture_window(hwnd)
    Usage: captures the given window's current content and returns it
    as a base64-encoded PNG data URL, ready to drop straight into an
    <img src="...">. Returns None if the window no longer exists or
    capture fails for any reason (closed between listing and
    capturing, access denied, zero-size client area, etc.) — callers
    should treat None as "skip this frame," not a fatal error. Every
    None-returning path prints why, since silently swallowing the
    reason makes "it just doesn't work" impossible to diagnose.
    """
    if not win32gui.IsWindow(hwnd):
        print(f"[capture] hwnd {hwnd}: no longer a valid window")
        return None

    hwnd_dc = None
    mfc_dc = None
    save_dc = None
    bitmap = None
    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            print(f"[capture] hwnd {hwnd}: zero-size client rect ({width}x{height}), skipping frame")
            return None

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()

        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        # PW_RENDERFULLCONTENT (2) — needed for modern/Chromium-based
        # apps whose content the older PrintWindow flag (0) often misses.
        # Called via ctypes/user32 directly — see the module-level
        # comment on _user32.PrintWindow for why, not via win32gui.
        result = _user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        if not result:
            print(f"[capture] hwnd {hwnd}: PrintWindow returned falsy ({result}) — capture failed")
            return None

        bmp_info = bitmap.GetInfo()
        bmp_bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB",
            (bmp_info["bmWidth"], bmp_info["bmHeight"]),
            bmp_bits,
            "raw",
            "BGRX",
            0,
            1,
        )

        # A fully black frame from PrintWindow usually means the flag
        # didn't work for this specific window's rendering path (some
        # GPU-accelerated/DirectComposition apps resist PrintWindow
        # entirely) — worth knowing distinctly from a hard failure.
        extrema = image.convert("L").getextrema()
        if extrema == (0, 0):
            print(f"[capture] hwnd {hwnd}: captured frame is solid black (PrintWindow likely unsupported for this window)")

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception as exc:
        print(f"[capture] hwnd {hwnd}: exception during capture: {exc!r}")
        return None
    finally:
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
        if save_dc is not None:
            save_dc.DeleteDC()
        if mfc_dc is not None:
            mfc_dc.DeleteDC()
        if hwnd_dc is not None:
            win32gui.ReleaseDC(hwnd, hwnd_dc)


class WindowCaptureStream:
    """
    WindowCaptureStream(hwnd, on_frame, interval)
    Usage: same start()/stop() pattern as audio.py's MicrophoneStream.
    Runs a background thread that captures the target window every
    `interval` seconds and calls on_frame(data_url) with each new
    frame. interval defaults to 0.5s (2fps) — this is meant to keep a
    spreadsheet/document glanceable while talking, not provide smooth
    video; a shorter interval costs meaningfully more CPU for
    continuous PrintWindow calls with little practical benefit here.
    Stops itself automatically if the target window is closed.
    """

    def __init__(self, hwnd: int, on_frame: Callable[[str], None], interval: float = 0.5):
        self.hwnd = hwnd
        self.on_frame = on_frame
        self.interval = interval
        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """
        start()
        Usage: begins the capture loop on a background thread.
        """
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        """
        _loop()
        Usage: internal — the background thread's run function. Not
        called directly. Logs a one-line confirmation on the first
        successful frame (not every frame, to avoid flooding the
        console at 2fps) so it's obvious from the console whether
        frames are actually reaching on_frame at all.
        """
        logged_first_success = False
        while self._running.is_set():
            frame = capture_window(self.hwnd)
            if frame is not None:
                if not logged_first_success:
                    print(f"[capture] hwnd {self.hwnd}: first frame captured OK ({len(frame)} chars), calling on_frame")
                    logged_first_success = True
                self.on_frame(frame)
            elif not win32gui.IsWindow(self.hwnd):
                print(f"[capture] hwnd {self.hwnd}: window closed, stopping capture loop")
                break  # target window closed — stop trying to capture it
            time.sleep(self.interval)

    def stop(self) -> None:
        """
        stop()
        Usage: ends the capture loop. Safe to call even if start() was
        never called.
        """
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)
            self._thread = None
