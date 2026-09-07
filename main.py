"""
main.py

App entrypoint. Creates the pywebview window loading index.html, wires
up the Api bridge, and starts the event loop.

Usage:
    python main.py
"""

import webview

from api import Api


def main() -> None:
    """
    main()
    Usage: run directly (`python main.py`) to launch the desktop app.
    Not intended to be imported/called from elsewhere.
    """
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
    webview.start()


if __name__ == "__main__":
    main()
