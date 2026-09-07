"""
app_settings.py

Persists user-configurable app preferences to a local JSON file so they
survive restarts. Separate from config.py's Settings (env-driven,
fixed per install — Azure keys, license server URL) since these are
things the user changes at runtime from the Settings page: how long to
keep history, which translation engine to prefer, which microphone to
use.

Usage:
    from app_settings import load_settings, save_settings
    settings = load_settings()               # -> dict with all keys, defaults filled in
    save_settings({"engine_mode": "offline"}) # merges and persists
"""

import json
import platform
from pathlib import Path
from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    "history_retention": "30_days",  # "forever" | "30_days" | "7_days" | "none"
    "engine_mode": "auto",  # "auto" | "online" | "offline"
    "microphone_device_index": None,  # int index from audio.list_input_devices(), or None for system default
}


def _settings_path() -> Path:
    """
    _settings_path()
    Usage: internal — same OS-appropriate location pattern as
    licensing.py's cached token, just a different filename, so both
    live side by side in the same per-user app data folder.
    """
    if platform.system() == "Windows":
        base = Path.home() / "AppData" / "Roaming" / "MithraCorp"
    else:
        base = Path.home() / ".config" / "mithracorp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "app_settings.json"


def load_settings() -> Dict[str, Any]:
    """
    load_settings()
    Usage: call on app startup (or whenever the Settings page loads) to
    get the current preferences. Always returns every key in DEFAULTS,
    even if the file is missing, corrupt, or from an older version that
    didn't have a given key yet.
    """
    path = _settings_path()
    if not path.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    save_settings(patch)
    Usage: pass only the keys that changed, e.g.
    save_settings({"engine_mode": "offline"}) — merges onto the existing
    saved settings (not onto DEFAULTS) so unrelated preferences aren't
    reset, writes the result to disk, and returns the full merged dict.
    """
    current = load_settings()
    current.update(patch)
    _settings_path().write_text(json.dumps(current, indent=2))
    return current
