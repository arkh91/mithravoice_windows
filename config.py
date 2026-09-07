"""
config.py

Central place for all runtime configuration: Azure credentials, offline
model choices, license server URL, and default languages. Values come
from environment variables, optionally loaded from a local .env file
(see .env.example).

Paths to bundled assets (whisper model, Argos packages, the license
public key) are resolved relative to sys._MEIPASS when running as a
frozen PyInstaller exe, and relative to this file when running from
source — see _resource_path().

Usage:
    from config import settings
    if settings.azure_configured:
        ...
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """
    _load_dotenv(path)
    Usage: called once at import time. Reads a simple KEY=VALUE .env file
    (if present) and injects any keys not already set in os.environ, so
    real environment variables always take precedence over the file.
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


def _resource_path(relative: str) -> str:
    """
    _resource_path(relative)
    Usage: pass a path relative to the project root (e.g.
    "models/whisper-small") to get an absolute path that works both
    when running `python main.py` from source AND when running the
    PyInstaller-built .exe, where bundled data files are extracted to
    sys._MEIPASS instead of living next to the script.
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return str(base / relative)


@dataclass
class Settings:
    # --- Azure Speech Translation (online engine) ---
    azure_speech_key: str = field(default_factory=lambda: os.environ.get("AZURE_SPEECH_KEY", ""))
    azure_speech_region: str = field(default_factory=lambda: os.environ.get("AZURE_SPEECH_REGION", ""))

    # --- Offline engine (faster-whisper + Argos Translate) ---
    # If whisper_model_path points at a real bundled ctranslate2 model
    # directory (see scripts/prepare_offline_assets.py), that's loaded
    # directly with no network access. Falls back to whisper_model_size
    # for dev convenience (auto-downloads from Hugging Face on first use).
    whisper_model_path: str = field(default_factory=lambda: os.environ.get("WHISPER_MODEL_PATH", _resource_path("models/whisper-small")))
    whisper_model_size: str = field(default_factory=lambda: os.environ.get("WHISPER_MODEL_SIZE", "small"))
    whisper_device: str = field(default_factory=lambda: os.environ.get("WHISPER_DEVICE", "cpu"))
    whisper_compute_type: str = field(default_factory=lambda: os.environ.get("WHISPER_COMPUTE_TYPE", "int8"))

    # Directory of pre-downloaded .argosmodel package files bundled into
    # the installer (see scripts/prepare_offline_assets.py). Installed
    # from here offline on first use of a given language pair instead of
    # hitting the Argos package index over the network.
    argos_packages_dir: str = field(default_factory=lambda: os.environ.get("ARGOS_PACKAGES_DIR", _resource_path("models/argos")))

    # --- Licensing ---
    license_server_url: str = field(default_factory=lambda: os.environ.get("LICENSE_SERVER_URL", "https://key.mithravoice.mithracorp.com"))
    license_public_key_path: str = field(default_factory=lambda: os.environ.get("LICENSE_PUBLIC_KEY_PATH", _resource_path("keys/mithravoice_public_key.pem")))

    # --- Defaults matching the UI on first launch ---
    default_from_lang: str = "en-US"
    default_to_lang: str = "fa"

    @property
    def azure_configured(self) -> bool:
        """
        azure_configured
        Usage: check `settings.azure_configured` before attempting to use
        the online engine; True only when both the key and region are set.
        """
        return bool(self.azure_speech_key and self.azure_speech_region)


settings = Settings()

