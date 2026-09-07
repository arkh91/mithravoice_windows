"""
updater.py

Checks whether a newer version of the app has been published, by
comparing the version bundled into this build (see get_current_version)
against what the license server reports as latest (GET /v1/latest-version
— see server/app/main.py and the app_versions table in schema.sql).

This is a best-effort, non-blocking check: no internet or a server
hiccup just means no update notice appears, not an error the user sees.

Usage:
    from updater import check_for_update
    result = check_for_update()
    if result.update_available:
        ...  # show a banner pointing at result.download_url
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from config import settings


@dataclass
class UpdateCheckResult:
    update_available: bool
    current_version: str
    latest_version: Optional[str] = None
    download_url: Optional[str] = None
    notes: Optional[str] = None


def get_current_version() -> str:
    """
    get_current_version()
    Usage: reads the VERSION file bundled into this build (see
    build.spec's datas entry) to find out what version is actually
    running — as opposed to the VERSION file in a source checkout,
    which is just the build-time source of truth for the NEXT build.
    Returns "0.0.0" if somehow missing, so a comparison never crashes,
    it just always looks outdated (safe default — prompts an update
    rather than silently assuming current).
    """
    try:
        from config import _resource_path  # same bundled-vs-source path resolution config.py uses

        path = Path(_resource_path("VERSION"))
        if path.exists():
            return path.read_text().strip()
    except Exception:
        pass
    return "0.0.0"


def _parse_version(v: str):
    """
    _parse_version(v)
    Usage: internal — turns "2.0.1" into (2, 0, 1) for a proper
    numeric comparison rather than a string comparison (which would
    wrongly say "2.10.0" < "2.9.0"). Non-numeric parts fall back to 0.
    """
    parts = []
    for part in v.strip().split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_for_update() -> UpdateCheckResult:
    """
    check_for_update()
    Usage: call once after the license gate passes (see api.py's
    check_for_update). Requires internet; on any failure returns
    update_available=False rather than raising, since a failed update
    check should never block using the app.
    """
    current = get_current_version()
    try:
        resp = requests.get(f"{settings.license_server_url}/v1/latest-version", timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return UpdateCheckResult(update_available=False, current_version=current)
    except ValueError:  # bad JSON
        return UpdateCheckResult(update_available=False, current_version=current)

    latest = data.get("version")
    if not latest:
        return UpdateCheckResult(update_available=False, current_version=current)

    is_newer = _parse_version(latest) > _parse_version(current)
    return UpdateCheckResult(
        update_available=is_newer,
        current_version=current,
        latest_version=latest,
        download_url=data.get("download_url"),
        notes=data.get("notes"),
    )
