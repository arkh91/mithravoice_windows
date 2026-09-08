"""
publish_version.py

Admin CLI for announcing a new app release. Marks the given version as
"latest" (and unmarks whichever was latest before), so the desktop
app's next launch-time check (updater.py) picks it up and prompts users
to update.

Run this AFTER you've actually built and uploaded the installer to
https://mithracorp.com/mithravoice/versions.html — this just tells
existing installs a new one exists, it doesn't build or host anything.

Usage:
    python publish_version.py --version 2.0.2 \
        --url https://mithracorp.com/mithravoice/versions.html \
        --notes "Fixed translation latency, added Settings page"
"""

import argparse

from app.database import SessionLocal
from app.models import AppVersion


def main() -> None:
    """
    main()
    Usage: run directly — see module docstring for the CLI example.
    """
    parser = argparse.ArgumentParser(description="Publish a new MithraVoice version as latest")
    parser.add_argument("--version", required=True, help="e.g. 2.0.2 — must match the VERSION file used to build it")
    parser.add_argument("--url", default="https://mithracorp.com/mithravoice/versions.html", help="where users download it")
    parser.add_argument("--notes", default="", help="short release notes shown in the update prompt")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        # Unmark whatever was previously latest — exactly one row should
        # ever have is_latest=1.
        db.query(AppVersion).filter(AppVersion.is_latest.is_(True)).update({"is_latest": False})

        existing = db.query(AppVersion).filter(AppVersion.version == args.version).first()
        if existing is not None:
            existing.is_latest = True
            existing.download_url = args.url
            existing.release_notes = args.notes
        else:
            db.add(AppVersion(version=args.version, is_latest=True, download_url=args.url, release_notes=args.notes))

        db.commit()
        print(f"Published {args.version} as latest. Existing installs will see the update prompt on next launch.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
