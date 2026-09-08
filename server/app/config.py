"""
app/config.py

Server-side configuration, loaded from environment variables (see
.env.example). Keep this separate from the client's config.py — they
run on different machines and must never share the private key.

Usage:
    from app.config import settings
    engine = create_engine(settings.database_url)
"""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """
    _load_dotenv(path)
    Usage: called once at import time to load KEY=VALUE pairs from a
    local .env file without overriding real environment variables that
    are already set (systemd EnvironmentFile= takes precedence in prod).
    """
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


@dataclass
class ServerSettings:
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL", "mysql+pymysql://mithravoice:mithravoice@localhost:3306/mithravoice"
        )
    )
    private_key_path: str = field(default_factory=lambda: os.environ.get("PRIVATE_KEY_PATH", "keys/mithravoice_private_key.pem"))

    # How long an issued license token stays valid before the client must
    # reactivate online. This is the app's *offline grace period* — a
    # device can run fully offline for this long after last contacting
    # the server, even with no internet at all.
    token_validity_days: int = field(default_factory=lambda: int(os.environ.get("TOKEN_VALIDITY_DAYS", "30")))


settings = ServerSettings()
