"""
app/security.py

Signs license tokens with the server's RSA private key (RS256). The
client verifies these offline using the matching public key bundled
into the app — see keys/mithravoice_public_key.pem and the client's
licensing.py. This is the mechanism that lets a device run fully
offline after activating once: the client never has to trust its own
local cache blindly, it can cryptographically verify the token wasn't
tampered with, right up until the token's exp claim.

Usage:
    from app.security import issue_license_token
    token, expires_at = issue_license_token(license_key_id="...", plan=plan_info, device_fingerprint="...")
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Tuple

import jwt

from app.config import settings


def _load_private_key() -> str:
    """
    _load_private_key()
    Usage: internal — reads the PEM private key from disk on each call.
    Not cached, since key rotation shouldn't require a server restart to
    take effect.
    """
    return Path(settings.private_key_path).read_text()


def issue_license_token(license_key_id: str, key_code: str, plan_code: str, online_allowed: bool, offline_allowed: bool, device_fingerprint: str) -> Tuple[str, datetime]:
    """
    issue_license_token(license_key_id, key_code, plan_code, online_allowed, offline_allowed, device_fingerprint)
    Usage: called from the /v1/activate route after a key + device pass
    validation. Returns (jwt_string, expires_at) — expires_at is
    now + settings.token_validity_days, i.e. the offline grace period.
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=settings.token_validity_days)

    claims = {
        "sub": license_key_id,
        "key_code": key_code,
        "plan": plan_code,
        "online_allowed": online_allowed,
        "offline_allowed": offline_allowed,
        "device_fingerprint": device_fingerprint,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(claims, _load_private_key(), algorithm="RS256")
    return token, expires_at
