"""
history.py

Stores finalized translation results locally, one JSON-lines file per
day, so past conversations stay on the user's own machine — never sent
to the license server or anywhere else. Pruned according to whichever
retention preference is set on the Settings page (see app_settings.py).

Usage:
    from history import append_entry, prune, history_dir
    append_entry("Thank you.", "ممنون", "English", "Persian (Farsi)")
    prune("30_days")           # call periodically / on launch
    history_dir()              # -> Path, for the "Open History Folder" button
"""

import json
import platform
from datetime import datetime, timedelta
from pathlib import Path

RETENTION_DAYS = {"30_days": 30, "7_days": 7}


def history_dir() -> Path:
    """
    history_dir()
    Usage: returns the folder history files live in, creating it if
    needed. Same per-user app-data location as licensing.py's cached
    token and app_settings.py's preferences file, just a subfolder —
    this is also what the Settings page's "Open History Folder" button
    opens directly in the OS file browser.
    """
    if platform.system() == "Windows":
        base = Path.home() / "AppData" / "Roaming" / "MithraCorp" / "History"
    else:
        base = Path.home() / ".config" / "mithracorp" / "history"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _today_file() -> Path:
    """
    _today_file()
    Usage: internal — one file per calendar day (YYYY-MM-DD.jsonl)
    keeps individual files small and makes date-based retention pruning
    a simple filename comparison rather than parsing file contents.
    """
    return history_dir() / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def append_entry(original: str, translated: str, from_lang: str, to_lang: str) -> None:
    """
    append_entry(original, translated, from_lang, to_lang)
    Usage: call once per finalized (not interim) translation result.
    Appends a single JSON line to today's history file. Safe to call
    even if history_retention is "none" — callers should check that
    preference themselves before calling, this function doesn't know
    about settings.
    """
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "original": original,
        "translated": translated,
        "from_lang": from_lang,
        "to_lang": to_lang,
    }
    with open(_today_file(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def prune(retention: str) -> None:
    """
    prune(retention)
    Usage: call on app launch (and whenever the retention preference
    changes) with the current history_retention value from
    app_settings.py. Deletes whole daily files older than the retention
    window; "forever" deletes nothing, "none" deletes everything (since
    Don't Save History means exactly that — nothing should accumulate).
    """
    if retention == "forever":
        return

    for f in history_dir().glob("*.jsonl"):
        if retention == "none":
            f.unlink(missing_ok=True)
            continue
        try:
            file_date = datetime.strptime(f.stem, "%Y-%m-%d")
        except ValueError:
            continue  # not a date-named file we recognize; leave it alone
        days = RETENTION_DAYS.get(retention, 30)
        if file_date < datetime.now() - timedelta(days=days):
            f.unlink(missing_ok=True)
