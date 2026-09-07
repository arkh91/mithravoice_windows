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
import traceback
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


# --- Translation window geometry ---------------------------------
# The "Full Screen" button's Translation window is not literally
# full-screen: it takes the BOTTOM THIRD of the display, full width,
# so whatever is behind it (slides, a document, a video call) stays
# visible in the upper two thirds while the captions run underneath.
#
# Nothing here is hardcoded to a resolution — the thirds are computed
# from whatever the display actually reports at the moment the window
# opens (see _bottom_strip_geometry), so 1366x768, 1920x1080 and
# 2560x1440 all get a proportional strip rather than a fixed pixel
# height that would be a sliver on one and half the screen on another.
TRANSLATION_SCREEN_DIVISIONS = 3  # 3 => bottom third. 4 => bottom quarter, etc.

# Measure the thirds against the WORK AREA (screen minus taskbar)
# rather than the raw screen bounds. With raw bounds, the bottom of
# the strip — which is where the translated line sits — lands behind
# the Windows taskbar and is simply not readable. Set False for a
# literal thirds-of-the-whole-screen split.
TRANSLATION_USE_WORK_AREA = True

# Normally None, meaning "ask Windows for the display scaling". Set a
# number here only to override that detection; see _display_scale().
TRANSLATION_DPI_SCALE_OVERRIDE = None

# Drop the OS title bar. The window's own X button and the Esc key
# (see translation.html) become the only way to close it, which is why
# both exist.
TRANSLATION_FRAMELESS = True

# Draw only the caption panel, letting whatever is behind show through
# the margin around it.
#
# HOW THIS ACTUALLY WORKS, because it is not obvious and the obvious
# version does not work. Three layers have to cooperate:
#
#   1. The PAGE must paint nothing (translation.html keeps html/body
#      transparent). Anything the page paints is drawn by WebView2.
#   2. WEBVIEW2 must have DefaultBackgroundColor = Transparent so it
#      composites nothing of its own. pywebview sets this when the
#      window is created with transparent=True, and this half works.
#   3. The FORM behind WebView2 paints TRANSLATION_KEY_COLOR, and that
#      colour is punched out of the window with
#      SetLayeredWindowAttributes.
#
# The key must be painted by the FORM, never by the page. WebView2
# renders through DirectComposition, on top of the layered window
# surface, and that content is NOT subject to the parent's colour key.
# Painting the key colour in CSS produces a window that is genuinely
# click-through — Windows keys the form underneath — while WebView2
# cheerfully paints the colour straight back over the hole, so it
# looks solid and behaves transparent. That exact symptom is what led
# here.
#
# pywebview is supposed to do step 3 too, but on this build it does
# not: the form's background measured (240,240,240), SystemColors
# .Control, i.e. never assigned. So step 3 is done here by hand, in
# _paint_form_background() and _apply_color_key().
TRANSLATION_TRANSPARENT = True

# The colour the FORM paints and Windows punches out. A near-black
# nothing else uses, rather than the conventional magenta or red:
#   - If any step above fails, the window falls back to showing this
#     colour flat. Near-black reads as the app's normal dark
#     background; magenta would be a screenful of eye-searing pink.
#   - The caption text is outlined in pure black (#000). Antialiased
#     pixels along that outline blend toward whatever is behind, and
#     blending black into near-black is invisible.
# Nothing in translation.html may paint this colour.
TRANSLATION_KEY_COLOR = (1, 2, 3)
TRANSLATION_KEY_COLOR_HEX = "#010203"

# Keep the strip above every other window while it is open, so the
# captions stay readable over whatever is being presented.
#
# Setting HWND_TOPMOST once is not enough on its own. Topmost is a
# z-order among topmost windows, not a lock: another application that
# raises itself the same way — a PowerPoint slideshow, a video player,
# a media overlay — lands above this one and stays there. So the
# position is re-asserted on a timer for as long as the window is
# open. See Api._start_topmost_watchdog().
TRANSLATION_ALWAYS_ON_TOP = True

# How often to re-assert it. Short enough that being covered is a
# blink rather than a dead spot in the captions, long enough to be
# nothing on a CPU: this is one SetWindowPos call per tick.
TRANSLATION_TOPMOST_INTERVAL_SECONDS = 2.0

def _display_scale() -> float:
    """
    _display_scale()
    Usage: returns the display scaling factor as a plain float — 1.0
    at 100%, 1.5 at 150%. Called by _bottom_strip_geometry(); not
    useful on its own. Always returns 1.0 off Windows.

    This is needed because pywebview does not use one unit convention
    for its own geometry arguments on Windows. create_window() assigns
    the requested size to a WinForms Form that has
    AutoScaleMode.Dpi with AutoScaleDimensions of 96 DPI, so WinForms
    multiplies it by (current DPI / 96) before the window is shown —
    but the same call assigns the requested position to Form.Location
    untouched. A window asked for 2560x464 at (0, 928) on a 150%
    display therefore appears 3840x696 at (0, 928): right place, half
    again too big, hanging off the bottom of the screen.

    So the SIZE passed to create_window has to be pre-divided by this
    factor and the POSITION must not be. GetScaleFactorForDevice(0) is
    deliberately the same call pywebview itself uses internally to
    correct coordinates, so the two agree; device 0 is the primary
    display, which is the right answer here because pywebview only
    calls SetProcessDPIAware() (system DPI awareness, not per-monitor),
    meaning Windows reports every monitor in the primary's scale
    regardless of each monitor's own setting.
    """
    if TRANSLATION_DPI_SCALE_OVERRIDE is not None:
        return float(TRANSLATION_DPI_SCALE_OVERRIDE)

    import platform as _platform

    if _platform.system() != "Windows":
        return 1.0

    import ctypes

    try:
        scale = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
        if scale > 0:
            return scale
    except Exception:  # noqa: BLE001 - shcore is Windows 8.1+; older builds fall through
        pass
    try:
        # Windows 10 1607+. Falls back again to 1.0 below if missing,
        # which just means the window is sized as if at 100% scaling.
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:  # noqa: BLE001
        return 1.0


def _screen_rects():
    """
    _screen_rects()
    Usage: returns a list of (x, y, width, height) tuples, one per
    connected display, in the order pywebview reports them (index 0 is
    primary). Called by _work_area_for_window(); not useful alone.

    pywebview's Screen object exposes only .width/.height plus a
    platform-specific .frame — there is no portable .x/.y — so
    multi-monitor origins and the taskbar inset can only be read out
    of .frame, whose shape differs per backend: a WinForms
    WorkingArea Rectangle (.X/.Y/.Width/.Height) on Windows, a
    Gdk.Rectangle (.x/.y/.width/.height) on GTK. Both casings are
    tried; anything unrecognized falls back to .width/.height at
    origin (0, 0).

    NOTE: webview.screens must be read, never called. It is a
    proxy_tools Proxy, whose __call__ forwards to the proxied list —
    so callable() reports True but calling it raises
    "\'list\' object is not callable". A guard of the form
    `if callable(screens): screens = screens()` looks defensive and is
    in fact the bug. list() is safe: Proxy forwards __iter__.
    """
    try:
        screens = list(webview.screens)
    except Exception as exc:  # noqa: BLE001 - a display query must never block opening the window
        print(f"[TRANSLATION] could not query screens: {exc!r}", flush=True)
        return []

    rects = []
    for screen in screens:
        frame = getattr(screen, "frame", None)
        rect = None
        if TRANSLATION_USE_WORK_AREA and frame is not None:
            for xa, ya, wa, ha in (("X", "Y", "Width", "Height"), ("x", "y", "width", "height")):
                if all(hasattr(frame, a) for a in (xa, ya, wa, ha)):
                    rect = (int(getattr(frame, xa)), int(getattr(frame, ya)),
                            int(getattr(frame, wa)), int(getattr(frame, ha)))
                    break
        if rect is None:
            rect = (0, 0, int(screen.width), int(screen.height))
        rects.append(rect)
    return rects


def _work_area_for_window(window) -> tuple:
    """
    _work_area_for_window(window)
    Usage: pass the MAIN app window to get the (x, y, width, height)
    usable area of the display it is currently sitting on, so the
    Translation window opens on the same monitor the user is working
    on rather than always on the primary.

    Which display that is has to be worked out by hit-testing the main
    window's own centre point against each screen rectangle: pywebview
    exposes no "which screen is this window on" call, and its Screen
    objects carry no coordinates of their own outside .frame. The
    CENTRE is tested rather than the top-left corner because a
    maximized or slightly off-screen window can have a corner sitting
    on a neighbouring display, or at negative coordinates, while its
    body is plainly on one monitor.

    Falls back to the primary display if the window position can\'t be
    read or lands outside every reported screen, and to a 1920x1080
    origin if pywebview reports no displays at all — so a geometry
    calculation can never divide by nothing.
    """
    rects = _screen_rects()
    if not rects:
        print("[TRANSLATION] no screens reported; falling back to 1920x1080", flush=True)
        return 0, 0, 1920, 1080

    try:
        cx = window.x + window.width // 2
        cy = window.y + window.height // 2
    except Exception as exc:  # noqa: BLE001 - position unreadable; primary is a fine default
        print(f"[TRANSLATION] could not read main window position ({exc!r}); using primary display", flush=True)
        return rects[0]

    for index, (rx, ry, rw, rh) in enumerate(rects):
        if rx <= cx < rx + rw and ry <= cy < ry + rh:
            if index != 0:
                print(f"[TRANSLATION] main window is on display {index + 1} of {len(rects)}", flush=True)
            return rx, ry, rw, rh

    print(
        f"[TRANSLATION] main window centre ({cx},{cy}) matched no display; using primary",
        flush=True,
    )
    return rects[0]


def _bottom_strip_geometry(window):
    """
    _bottom_strip_geometry(window)
    Usage: pass the MAIN app window; returns
    (x, y, width, height, create_width, create_height, scale) for
    the Translation window. Called by Api.open_translation_window().

    The first four are the REAL pixel geometry wanted on screen: full
    usable width of whichever display the main window is on,
    1/TRANSLATION_SCREEN_DIVISIONS of its usable height, pinned to the
    bottom edge. The last two are the same size pre-divided by the
    display scaling, because that is what has to be handed to
    create_window to actually get it — see _display_scale().

    Recomputed on every open rather than cached, so moving the app to
    a second monitor, unplugging a projector, or changing resolution
    between openings is picked up without relaunching.

    The height is floor-divided and y is derived by subtracting that
    height from the bottom edge rather than by multiplying
    (2/3 * height) — otherwise integer rounding leaves a 1-2px gap of
    desktop showing along the very bottom on heights that don\'t divide
    evenly by 3, e.g. 768 or 1050.
    """
    area_x, area_y, area_w, area_h = _work_area_for_window(window)

    width = area_w
    height = int(area_h / TRANSLATION_SCREEN_DIVISIONS)
    x = area_x
    y = area_y + area_h - height  # bottom edge of the usable area, minus the strip

    scale = _display_scale()
    create_width = int(round(width / scale))
    create_height = int(round(height / scale))

    print(
        f"[TRANSLATION] work area {area_w}x{area_h} at ({area_x},{area_y}), scaling {scale:g}x "
        f"-> bottom 1/{TRANSLATION_SCREEN_DIVISIONS} strip {width}x{height} at ({x},{y}) "
        f"(requesting {create_width}x{create_height})",
        flush=True,
    )
    return x, y, width, height, create_width, create_height, scale


# --- Exact window placement (Windows) ----------------------------
# Everything above computes WHERE the strip should go. Getting a
# window to actually land there is a separate problem, because
# pywebview's create_window does not document — and does not use —
# one consistent unit for geometry on Windows. Observed on a 2560x1440
# display at 150% scaling: a window asked for at (0, 912) sized
# 1707x304 came out the right SIZE but with its top edge at 1368,
# i.e. exactly on the bottom edge of the work area, entirely below the
# visible desktop. It existed, it rendered, its JavaScript ran and
# reported a correct width — it just wasn't on screen.
#
# So the position is not argued with; it is set afterwards with
# SetWindowPos, which takes real screen pixels and applies no scaling
# of its own. That is the one call in this whole path whose units are
# unambiguous. GetWindowRect then reads back where the window
# genuinely ended up, so "is it off-screen?" is never a guess again.
#
# Windows-only, and entirely optional: if any of it fails the window
# keeps whatever geometry create_window gave it, exactly as before.


def _reassert_topmost(hwnd) -> bool:
    """
    _reassert_topmost(hwnd)
    Usage: called on a timer by Api._start_topmost_watchdog(). Pushes
    the window back to the top of the topmost band without moving,
    resizing or focusing it. Returns False once the window no longer
    exists, which is the watchdog's signal to stop.

    IsWindow is checked first because the window can be destroyed
    between two ticks; calling SetWindowPos on a dead handle is at
    best a no-op and at worst hits a handle Windows has already
    recycled for something else.

    SWP_NOMOVE | SWP_NOSIZE means the x/y/width/height arguments are
    ignored, so this cannot fight the exact placement done earlier.
    SWP_NOACTIVATE means it cannot steal focus from whatever the user
    is typing in — without it this would yank the caret out of their
    spreadsheet every couple of seconds.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL

    if not user32.IsWindow(hwnd):
        return False

    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    HWND_TOPMOST = -1

    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    return True


def _paint_form_background(window, rgb) -> bool:
    """
    _paint_form_background(window, rgb)
    Usage: pass the Translation window object and an (r, g, b) tuple.
    Sets the underlying WinForms Form's BackColor to that colour so
    there is something for the colour key to punch out. Returns True
    if it was applied.

    This exists because pywebview does not reliably set it. Measured on
    the target machine, the form's background was exactly
    (240,240,240) — SystemColors.Control, the .NET default, meaning
    neither branch of pywebview's own transparency code had assigned
    it. Without this the colour key has nothing to match and the
    window shows flat grey.

    Reaches through to the Form via BrowserView.instances, which is
    pywebview internals rather than public API — hence the broad
    guard: if a future version renames any of it, transparency
    silently degrades to a flat near-black strip instead of raising.

    The assignment is marshalled with Form.Invoke because this is
    called from the placement thread, and touching a WinForms
    control's properties from a non-UI thread throws
    InvalidOperationException.
    """
    try:
        from webview.platforms.winforms import BrowserView
        from System import Action
        from System.Drawing import Color
    except Exception as exc:  # noqa: BLE001 - not Windows, or internals moved
        print(f"[TRANSLATION] cannot reach WinForms internals ({exc!r}); skipping form background", flush=True)
        return False

    form = BrowserView.instances.get(getattr(window, "uid", None))
    if form is None:
        print("[TRANSLATION] no BrowserView instance for the Translation window", flush=True)
        return False

    red, green, blue = rgb
    color = Color.FromArgb(255, red, green, blue)

    def _assign():
        form.BackColor = color

    try:
        if form.InvokeRequired:
            form.Invoke(Action(_assign))
        else:
            _assign()
        print(f"[TRANSLATION] form background painted rgb{tuple(rgb)}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001 - cosmetic; never take the app down for it
        print(f"[TRANSLATION] could not set form background: {exc!r}", flush=True)
        return False


def _apply_color_key(hwnd) -> bool:
    """
    _apply_color_key(hwnd)
    Usage: called from _force_window_rect() once the Translation
    window has been found, before it is positioned. Makes every pixel
    of TRANSLATION_KEY_COLOR fully transparent and click-through,
    leaving everything else fully opaque. Returns True on success.

    This replaces pywebview's transparent=True, which measurably did
    not work on the target machine. Doing it here is two calls: add
    WS_EX_LAYERED to the window's extended style, then
    SetLayeredWindowAttributes with LWA_COLORKEY. Windows then
    composites the window itself, with no dependency on what WebView2
    does or doesn't honour — WebView2 paints the key colour opaquely,
    exactly as it would any other colour, and the desktop window
    manager removes it afterwards. That is the whole reason this is
    more reliable than asking the browser engine for alpha.

    Note the COLORREF byte order: Win32 packs it as 0x00BBGGRR, the
    reverse of the RRGGBB the same colour is written as in CSS. Getting
    this backwards keys out a different colour than the page paints and
    the window looks completely unchanged — a silent failure, hence
    spelling it out.

    The alpha argument is 0 and is ignored: it applies only under
    LWA_ALPHA, which is deliberately not passed. Passing LWA_ALPHA too
    would make the WHOLE window uniformly translucent, caption text
    included, which is not what is wanted.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    LWA_COLORKEY = 0x00000001

    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.SetLayeredWindowAttributes.argtypes = [
        wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD
    ]
    user32.SetLayeredWindowAttributes.restype = wintypes.BOOL

    red, green, blue = TRANSLATION_KEY_COLOR
    colorref = red | (green << 8) | (blue << 16)  # 0x00BBGGRR, not RRGGBB

    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if not style & WS_EX_LAYERED:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)

    ok = bool(user32.SetLayeredWindowAttributes(hwnd, colorref, 0, LWA_COLORKEY))
    print(
        f"[TRANSLATION] colour key rgb{TRANSLATION_KEY_COLOR} "
        f"(COLORREF 0x{colorref:06x}): {'applied' if ok else 'FAILED — window will show a flat ' + TRANSLATION_KEY_COLOR_HEX}",
        flush=True,
    )
    return ok


def _force_window_rect(title: str, x: int, y: int, width: int, height: int):
    """
    _force_window_rect(title, x, y, width, height)
    Usage: called from Api._apply_translation_geometry() once the
    Translation window has loaded. Moves and resizes the window titled
    `title` belonging to THIS process to exactly the given screen
    pixels, and logs where it actually landed. Returns the window
    handle if it was found and positioned, otherwise None — the
    handle is what the caller needs to keep re-asserting topmost.

    Matching is by title AND process id: FindWindow on a title as
    generic as "Translation" could just as easily grab an unrelated
    application's window, and moving a stranger's window off to the
    bottom of the screen would be a genuinely bad bug. Comparing
    GetWindowThreadProcessId against os.getpid() makes that
    impossible.

    argtypes are declared explicitly for the same reason
    window_capture.py declares them for PrintWindow: without them,
    ctypes marshals handles as 32-bit ints, which silently truncates
    an HWND on 64-bit Windows and corrupts the call rather than
    failing loudly.
    """
    import ctypes
    import os
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

    own_pid = os.getpid()
    found = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _enum(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value != title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            found.append(hwnd)
            return False  # stop enumerating
        return True

    user32.EnumWindows(WNDENUMPROC(_enum), 0)

    if not found:
        print(f"[TRANSLATION] no window titled {title!r} owned by this process; leaving geometry alone", flush=True)
        return None

    hwnd = found[0]

    before = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(before))

    if TRANSLATION_TRANSPARENT:
        # Order matters only in that both must happen before the
        # window is next painted; the key is what makes the colour
        # disappear, the colour is what gives the key something to
        # match.
        _apply_color_key(hwnd)

    SWP_SHOWWINDOW = 0x0040
    SWP_FRAMECHANGED = 0x0020
    SWP_NOACTIVATE = 0x0010
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2

    # SWP_NOACTIVATE matters as much as the topmost flag here. Without
    # it, placing the window steals keyboard focus from whatever the
    # user is typing in — which for a caption strip that appears over
    # someone's spreadsheet would be worse than being hidden.
    insert_after = HWND_TOPMOST if TRANSLATION_ALWAYS_ON_TOP else HWND_NOTOPMOST
    user32.SetWindowPos(
        hwnd, insert_after, x, y, width, height,
        SWP_SHOWWINDOW | SWP_FRAMECHANGED | SWP_NOACTIVATE,
    )

    after = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(after))

    print(
        f"[TRANSLATION] placement: was ({before.left},{before.top})-({before.right},{before.bottom}) "
        f"-> now ({after.left},{after.top})-({after.right},{after.bottom}) "
        f"[wanted ({x},{y}) {width}x{height}]"
        + (", topmost" if TRANSLATION_ALWAYS_ON_TOP else ""),
        flush=True,
    )
    return hwnd


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
        # (x, y, width, height) in REAL pixels most recently wanted for
        # the Translation window, kept so report_translation_geometry()
        # can compare it against what the window actually became, and
        # correct it. None until the first open.
        self._target_translation_geometry: Optional[tuple] = None
        # Signals the always-on-top watchdog thread to stop. Set when
        # the Translation window closes; None whenever no watchdog is
        # running. See _start_topmost_watchdog().
        self._translation_topmost_stop: Optional[threading.Event] = None
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

        # device_index comes from settings persisted in an earlier
        # session. PortAudio/Windows device indices are NOT stable
        # across restarts — installing/enabling any new audio device
        # (a virtual speaker, a new headset, etc.) can silently
        # renumber every existing device, so an index saved last
        # session can now point at something else entirely, including
        # an output-only device. Re-validate it against the CURRENT
        # device list rather than trusting it blindly: if it no longer
        # names an input-capable device, fall back to the OS default
        # (None) instead of handing a wrong/invalid device straight to
        # sd.InputStream, where it would raise PortAudioError deep
        # inside the audio callback thread.
        if device_index is not None:
            current_devices = list_input_devices()
            if not any(d["index"] == device_index for d in current_devices):
                device_index = None

        self._current_from_lang, self._current_to_lang = from_lang, to_lang
        try:
            self._pipeline.start(from_lang, to_lang, device_index, mode)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - any mic/engine start failure must surface to the UI, never crash the bridge call
            # Print the full traceback to the console the app was
            # launched from — the UI only ever shows a short generic
            # message (see index.html's mic_start_failed), so this is
            # the only place the real cause (which device, which
            # PortAudio error code, etc.) is visible for debugging.
            traceback.print_exc()
            return {"ok": False, "error": "mic_start_failed", "detail": str(exc)}
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
          1. opens a second window titled "Translation", sized and
             positioned to the BOTTOM THIRD of the display (full
             width), showing nothing but the live captions, mirrored
             from the same _push_result() that feeds the main stage;
          2. minimizes the main window down to the OS taskbar, so the
             control UI is out of the way and whatever the user is
             actually presenting occupies the upper two thirds.

        The strip is measured from the display at open time, not
        hardcoded — see _bottom_strip_geometry() above.

        Order matters: the new window is created BEFORE the main one
        is minimized. Minimizing first tends to hand focus to whatever
        was behind the app, and the new window can then open behind
        that instead of coming up front — which reads as "the button
        did nothing but minimize."

        view_settings mirrors whatever Caption Style / Display / Text
        Color is currently active on the main stage and is passed
        through as query-string params, so the window opens matching
        rather than resetting to defaults (see translation.html's
        applyStyleFromParams()).

        No-ops on the create step if a Translation window is already
        open — only one at a time. No-ops entirely if called before
        attach_window() has run.
        """
        if self._window is None:
            return {"ok": False, "error": "no_window"}

        if self._translation_window is None:
            view_settings = view_settings or {}
            x, y, width, height, create_width, create_height, scale = _bottom_strip_geometry(self._window)
            self._target_translation_geometry = (x, y, width, height)
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
                # Every one of these is a best-effort STARTING point
                # only — the real geometry is forced with SetWindowPos
                # once the window loads (see
                # _apply_translation_geometry). Position is pre-scaled
                # the same way size is, because the evidence says
                # WinForms scales both: an unscaled y on a 150%
                # display put the window's top edge exactly on the
                # bottom of the work area, off-screen. Pre-scaling at
                # worst puts it somewhere visible and wrong for the
                # ~200ms before the correction lands, instead of
                # somewhere invisible.
                x=int(round(x / scale)),
                y=int(round(y / scale)),
                width=create_width,
                height=create_height,
                # Deliberately NOT fullscreen: the whole point of the
                # bottom third is that the upper two thirds stay
                # visible. Resizable so the strip can still be nudged
                # by hand on an odd display; min_size is well under
                # any third of a real screen so it never fights the
                # computed geometry (see main.py's note on min_size
                # being a permanent floor pywebview enforces).
                resizable=True,
                min_size=(320, 100),
                # No title bar. A caption strip sitting over someone's
                # slides shouldn't announce itself with window chrome,
                # and the ~30px the title bar occupied was a visible
                # slice out of a strip only a few hundred px tall.
                #
                # This removes the OS close button, so translation.html
                # has to carry its own way out — it does: the X in the
                # corner and the Esc key, both routed through
                # close_translation_window().
                frameless=TRANSLATION_FRAMELESS,
                # transparent=True is needed for ONE thing: it makes
                # pywebview set the WebView2 control's
                # DefaultBackgroundColor to Transparent, so the browser
                # paints nothing and the form behind it shows through.
                # That half demonstrably works. The other half — the
                # form's own BackColor and colour key — is not applied
                # on this build, so _prepare_translation_window() and
                # _apply_color_key() do it directly instead.
                transparent=TRANSLATION_TRANSPARENT,
                # pywebview's easy_drag attaches a mousedown handler to
                # the whole window and moves it on any subsequent mouse
                # movement. On a frameless window that is normally how
                # you drag it, but here it would fight the exact
                # placement _apply_translation_geometry() just did —
                # and because it fires on the close button too, a 1px
                # jitter while clicking X would shove the strip out of
                # position. The window is meant to be pinned; reopening
                # it recomputes the geometry anyway.
                easy_drag=False,
                background_color=TRANSLATION_KEY_COLOR_HEX,
            )
            self._translation_window.events.closed += self._on_translation_closed
            self._translation_window.events.loaded += self._on_translation_loaded

        self._window.minimize()
        return {"ok": True}

    def report_translation_geometry(self, css_width, css_height, device_pixel_ratio):
        """
        report_translation_geometry(css_width, css_height, device_pixel_ratio)
        Usage (JS, from translation.html): called once on load with
        window.innerWidth, window.innerHeight and
        window.devicePixelRatio. Pure logging — it corrects nothing.

        _apply_translation_geometry() is what actually enforces the
        window rectangle, and its SetWindowPos readback is the
        authoritative answer on where the window is. This is the view
        from the other side: what the WEB CONTENT thinks it has to
        draw in. The two together separate the two failure modes that
        otherwise look identical from a console log — a window that is
        the wrong size, versus a window that is the right size in the
        wrong place.

        Nothing here resizes, deliberately: this fires on a JS timer
        that can overlap the placement thread, and a corrective resize
        racing SetWindowPos would be a coin flip over which one wins.

        Compares WIDTH rather than height. That used to be because the
        title bar made height differ by ~30px even when correct; the
        window is frameless now, so both are clean, but width remains
        the better signal — it is the larger number, so a proportional
        scaling error shows up in it most clearly.
        """
        geometry = self._target_translation_geometry
        if not geometry:
            return {"ok": False}

        try:
            ratio = float(device_pixel_ratio) or 1.0
            actual_width = float(css_width) * ratio
        except (TypeError, ValueError):
            return {"ok": False}

        target_width = geometry[2]
        if not target_width:
            return {"ok": False}

        drift = actual_width / target_width
        verdict = "matches" if abs(drift - 1.0) <= 0.15 else "MISMATCH"
        print(
            f"[TRANSLATION] content size: {css_width}css x {ratio}dpr = {actual_width:.0f}px "
            f"vs {target_width}px wanted ({verdict}, ratio {drift:.2f})",
            flush=True,
        )
        return {"ok": True}

    def _on_translation_loaded(self):
        """
        _on_translation_loaded()
        Usage: internal — bound to the Translation window's `loaded`
        event, not called directly. Two jobs, in order: put the window
        exactly where it belongs, then replay the last caption into it.

        Placement happens here rather than at creation because the
        window has to exist and be visible before SetWindowPos can
        find it by title, and because create_window's own geometry
        arguments cannot be relied on (see _force_window_rect).
        Placement runs first so the window is never briefly readable
        in the wrong place.
        """
        self._apply_translation_geometry()
        self._push_last_subtitle_to_translation()

    def _apply_translation_geometry(self):
        """
        _apply_translation_geometry()
        Usage: internal — forces the Translation window to the exact
        screen rectangle _bottom_strip_geometry() worked out, using
        Win32 SetWindowPos rather than pywebview's own geometry calls.
        Called once per open from _on_translation_loaded().

        No-ops off Windows, where pywebview's create_window geometry is
        used as-is; the deliberate result is that a developer running
        this on macOS or Linux gets an approximately-right window
        rather than an exception.

        Retries briefly because `loaded` fires on browser navigation,
        which is not a guarantee that the OS-level window has been
        created and made visible yet — and _force_window_rect can only
        find a visible window. Runs on its own short-lived thread so a
        window that never appears costs a second of a background
        thread rather than blocking the event dispatcher.
        """
        import platform as _platform

        geometry = self._target_translation_geometry
        if geometry is None or _platform.system() != "Windows":
            return

        x, y, width, height = geometry

        if TRANSLATION_TRANSPARENT:
            # Give the colour key something to match. Done here rather
            # than at creation because it needs the Form to exist, and
            # before placement so the window is never painted grey
            # even briefly.
            _paint_form_background(self._translation_window, TRANSLATION_KEY_COLOR)

        def _place():
            for _ in range(10):
                try:
                    hwnd = _force_window_rect("Translation", x, y, width, height)
                    if hwnd:
                        if TRANSLATION_ALWAYS_ON_TOP:
                            self._start_topmost_watchdog(hwnd)
                        return
                except Exception as exc:  # noqa: BLE001 - placement is cosmetic; never take the app down for it
                    print(f"[TRANSLATION] placement failed: {exc!r}", flush=True)
                    return
                time.sleep(0.1)
            print("[TRANSLATION] gave up looking for the window to place", flush=True)

        threading.Thread(target=_place, daemon=True).start()

    def _start_topmost_watchdog(self, hwnd):
        """
        _start_topmost_watchdog(hwnd)
        Usage: internal — started once per open, from the placement
        thread, after the window has been found. Keeps the Translation
        strip above every other window for as long as it is open.

        A one-off HWND_TOPMOST is not enough. Topmost is a z-order
        band, not a lock: any other application that raises itself the
        same way — a PowerPoint slideshow, a video player, a media
        overlay — sits above this window and stays there, and the
        captions are silently lost behind it. Re-asserting on a timer
        means being covered lasts a tick instead of the rest of the
        talk.

        Stops on whichever comes first: the stop Event being set by
        _on_translation_closed(), or _reassert_topmost() reporting the
        window no longer exists. The second is the backstop for any
        path that destroys the window without firing `closed`, so a
        thread can't outlive its window.

        Any previous watchdog is stopped first. Without that, opening
        and closing the strip repeatedly would leave a thread per open
        all poking at stale handles.
        """
        self._stop_topmost_watchdog()

        stop = threading.Event()
        self._translation_topmost_stop = stop

        def _watch():
            # Event.wait() rather than sleep() so closing the window
            # ends the thread immediately instead of after up to a
            # full interval.
            while not stop.wait(TRANSLATION_TOPMOST_INTERVAL_SECONDS):
                try:
                    if not _reassert_topmost(hwnd):
                        return
                except Exception as exc:  # noqa: BLE001 - never take the app down over z-order
                    print(f"[TRANSLATION] topmost watchdog stopping: {exc!r}", flush=True)
                    return

        threading.Thread(target=_watch, daemon=True).start()
        print(
            f"[TRANSLATION] holding on top, re-asserted every "
            f"{TRANSLATION_TOPMOST_INTERVAL_SECONDS:g}s",
            flush=True,
        )

    def _stop_topmost_watchdog(self):
        """
        _stop_topmost_watchdog()
        Usage: internal — ends the always-on-top thread if one is
        running. Called when the Translation window closes and before
        starting a new watchdog. Safe to call when none is running.
        """
        if self._translation_topmost_stop is not None:
            self._translation_topmost_stop.set()
            self._translation_topmost_stop = None

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
        self._stop_topmost_watchdog()
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
