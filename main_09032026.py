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
        min_size=(1024, 680),
        background_color="#0b0e14",
    )
    api.attach_window(window)
    webview.start()


if __name__ == "__main__":
    main()
