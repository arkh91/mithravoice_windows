"""
create_license_key.py

Admin CLI for issuing a new subscription key. There's no billing
integration in this project yet, so this is the tool you run by hand on
the server (or wire up to a Stripe webhook later) after a sale goes
through.

Usage:
    python create_license_key.py --email jane@example.com --plan solo_monthly --months 1
    python create_license_key.py --email jane@example.com --plan team_monthly --months 12
"""

import argparse
import secrets
import string
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import LicenseKey, Plan, Subscription, User


def generate_key_code() -> str:
    """
    generate_key_code()
    Usage: produces a human-typeable key like 'MVCE-7F3A-9K2Q-XPL4' —
    grouped in 4s so it's easy to read back over the phone/email, using
    an unambiguous alphabet (no 0/O or 1/I confusion).
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "MVCE-" + "-".join(groups)


def main() -> None:
    """
    main()
    Usage: run directly — see module docstring for CLI examples. Creates
    the user if they don't already exist, opens a new subscription for
    the given plan and duration, and prints the generated key code to
    send to the customer.
    """
    parser = argparse.ArgumentParser(description="Issue a new MithraVoice license key")
    parser.add_argument("--email", required=True)
    parser.add_argument("--full-name", default=None)
    parser.add_argument("--plan", required=True, help="plan code, e.g. solo_monthly")
    parser.add_argument("--months", type=int, default=1, help="subscription length in months")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        plan = db.query(Plan).filter(Plan.code == args.plan).first()
        if plan is None:
            raise SystemExit(f"No such plan: {args.plan} (check the plans table)")

        user = db.query(User).filter(User.email == args.email).first()
        if user is None:
            user = User(email=args.email, full_name=args.full_name)
            db.add(user)
            db.flush()

        now = datetime.now(timezone.utc)
        period_end = now + timedelta(days=30 * args.months)

        subscription = Subscription(
            user_id=user.id, plan_id=plan.id, status="active",
            current_period_start=now, current_period_end=period_end,
        )
        db.add(subscription)
        db.flush()

        key_code = generate_key_code()
        license_key = LicenseKey(
            subscription_id=subscription.id, key_code=key_code,
            status="active", expires_at=period_end,
        )
        db.add(license_key)
        db.commit()

        print(f"Issued key for {args.email} ({plan.name}, {args.months} month(s)):")
        print(f"  {key_code}")
        print(f"  expires {period_end.date().isoformat()}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
