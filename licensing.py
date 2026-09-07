"""
licensing.py

Client-side half of the subscription-key system. Handles:
  - generating a stable per-machine fingerprint
  - calling the license server's /v1/activate the first time a user
    enters a key
  - caching the signed token locally
  - verifying that cached token OFFLINE on every subsequent launch,
    using the bundled public key — no network required until the token
    expires (settings.token_validity_days on the server, default 30 days)

Usage:
    status = get_cached_license_status()
    if status.valid:
        ...  # let the user into the app
    else:
        result = activate("MVCE-7F3A-9K2Q-XPL4")
        if result.ok:
            ...
"""

import hashlib
import json
import platform
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import jwt
import requests

from config import settings


@dataclass
class LicenseStatus:
    valid: bool
    reason: Optional[str] = None  # e.g. "no_license", "expired", "invalid_signature"
    plan_code: Optional[str] = None
    online_allowed: bool = False
    offline_allowed: bool = False
    key_code: Optional[str] = None  # the subscription key this token was issued for — needed for deactivate_this_device()


@dataclass
class ActivationResult:
    ok: bool
    error: Optional[str] = None  # server-provided reason: not_found / revoked / expired / seat_limit_reached / network_error


def _license_cache_path() -> Path:
    """
    _license_cache_path()
    Usage: internal — returns the OS-appropriate location for the cached
    license token (%APPDATA%\\MithraCorp\\license.jwt on Windows,
    ~/.config/mithracorp/license.jwt elsewhere), creating the parent
    directory if needed.
    """
    if platform.system() == "Windows":
        base = Path.home() / "AppData" / "Roaming" / "MithraCorp"
    else:
        base = Path.home() / ".config" / "mithracorp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "license.jwt"


def get_device_fingerprint() -> str:
    """
    get_device_fingerprint()
    Usage: called before every activate() call. Derives a stable hash
    from the machine's MAC address and hostname — stable across app
    reinstalls on the same machine (so reinstalling never silently
    burns a new seat), but distinct per physical/virtual machine.
    """
    raw = f"{uuid.getnode()}:{platform.node()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _load_public_key() -> str:
    """
    _load_public_key()
    Usage: internal — reads the bundled public key used to verify
    license tokens offline. Path comes from config.py so PyInstaller
    builds can point it at the bundled resource location.
    """
    return Path(settings.license_public_key_path).read_text()


def _verify_token(token: str) -> LicenseStatus:
    """
    _verify_token(token)
    Usage: internal — verifies signature + expiry of a cached or
    freshly-issued JWT entirely offline using the bundled public key.
    This is what makes the app usable with zero internet connectivity
    right up until the token's exp claim, without ever trusting the
    local cache file's contents un-checked.
    """
    try:
        claims = jwt.decode(token, _load_public_key(), algorithms=["RS256"])
    except jwt.ExpiredSignatureError:
        return LicenseStatus(valid=False, reason="expired")
    except jwt.InvalidTokenError:
        return LicenseStatus(valid=False, reason="invalid_signature")

    if claims.get("device_fingerprint") != get_device_fingerprint():
        return LicenseStatus(valid=False, reason="wrong_device")

    return LicenseStatus(
        valid=True,
        plan_code=claims.get("plan"),
        online_allowed=bool(claims.get("online_allowed", False)),
        offline_allowed=bool(claims.get("offline_allowed", False)),
        key_code=claims.get("key_code"),
    )


def get_cached_license_status() -> LicenseStatus:
    """
    get_cached_license_status()
    Usage: call on every app launch, before showing Live Translation.
    Returns valid=False with reason="no_license" if the user has never
    activated on this machine; otherwise verifies the cached token
    offline and returns its validity.
    """
    path = _license_cache_path()
    if not path.exists():
        return LicenseStatus(valid=False, reason="no_license")
    return _verify_token(path.read_text().strip())


def get_cached_token() -> Optional[str]:
    """
    get_cached_token()
    Usage: returns the raw cached JWT string, or None if there isn't
    one. Used by api.py's verify_license_online() to send the token to
    the server's /v1/heartbeat endpoint for a live revocation check.
    """
    path = _license_cache_path()
    if not path.exists():
        return None
    return path.read_text().strip()


def clear_cached_token() -> None:
    """
    clear_cached_token()
    Usage: deletes the local cached token without contacting the
    server — for when the server has ALREADY told us the key is no
    longer valid (e.g. via a failed heartbeat) and we just need to
    reflect that locally. Contrast with deactivate_this_device(), which
    also notifies the server (for a user-initiated "sign out").
    """
    path = _license_cache_path()
    if path.exists():
        path.unlink()


def activate(key_code: str) -> ActivationResult:
    """
    activate(key_code)
    Usage: called from Api.activate_license() when the user submits the
    license-gate form. Requires internet for this one call; on success,
    caches the returned token so future launches work via
    get_cached_license_status() without any network access.
    """
    fingerprint = get_device_fingerprint()
    payload = {
        "key_code": key_code.strip(),
        "device_fingerprint": fingerprint,
        "hostname": platform.node(),
        "os": platform.platform(),
    }
    try:
        resp = requests.post(f"{settings.license_server_url}/v1/activate", json=payload, timeout=10)
    except requests.RequestException:
        return ActivationResult(ok=False, error="network_error")

    if resp.status_code != 200:
        try:
            error = resp.json().get("detail", "activation_failed")
        except json.JSONDecodeError:
            error = "activation_failed"
        return ActivationResult(ok=False, error=error)

    data = resp.json()
    token = data["token"]

    # Verify before trusting it — a compromised or misconfigured server
    # response shouldn't silently grant access.
    status = _verify_token(token)
    if not status.valid:
        return ActivationResult(ok=False, error=status.reason or "invalid_token")

    _license_cache_path().write_text(token)
    return ActivationResult(ok=True)


def deactivate_this_device(key_code: str) -> None:
    """
    deactivate_this_device(key_code)
    Usage: optional — call if you add a "sign out of this device" action
    to the UI. Frees the seat on the server and removes the local cache
    so get_cached_license_status() reports no_license afterward. Best
    effort: if there's no network, still clears the local cache.
    """
    fingerprint = get_device_fingerprint()
    try:
        requests.post(
            f"{settings.license_server_url}/v1/deactivate",
            json={"key_code": key_code, "device_fingerprint": fingerprint},
            timeout=10,
        )
    except requests.RequestException:
        pass
    path = _license_cache_path()
    if path.exists():
        path.unlink()
