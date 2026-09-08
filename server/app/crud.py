"""
app/crud.py

Database operations backing the license API: looking up a key, checking
it's still valid, enforcing the plan's device-seat limit, tracking
monthly online-hours usage, and recording every activation attempt for
audit/support purposes.

Note on datetimes: MySQL's DATETIME/TIMESTAMP columns are stored
timezone-naive here (no TIMESTAMP WITH TIME ZONE in MySQL), so every
comparison below uses naive datetime.utcnow() rather than
datetime.now(timezone.utc) — mixing the two raises TypeError. The
server's own clock is assumed to be UTC or NTP-synced; see DEPLOY.md.

Usage:
    from app import crud
    key = crud.get_active_license_key(db, "MVCE-7F3A-9K2Q-XPL4")
    device, reason = crud.register_device(db, key, fingerprint, hostname, os_name)
"""

from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.models import ActivationEvent, Device, LicenseKey, OnlineUsagePeriod, Plan, Subscription


def get_license_key(db: Session, key_code: str) -> Optional[LicenseKey]:
    """
    get_license_key(db, key_code)
    Usage: raw lookup by the string the user typed in, no validity
    checks — callers that need "is this usable right now" should use
    get_active_license_key instead.
    """
    return db.query(LicenseKey).filter(LicenseKey.key_code == key_code).first()


def get_active_license_key(db: Session, key_code: str) -> Tuple[Optional[LicenseKey], Optional[str]]:
    """
    get_active_license_key(db, key_code)
    Usage: the check the /v1/activate route runs before doing anything
    else. Returns (license_key, None) if the key is usable, or
    (None, reason) — reason is a short machine-readable string suitable
    for logging and for the UI's error message ("not_found",
    "revoked", "expired", "subscription_inactive").
    """
    key = get_license_key(db, key_code)
    if key is None:
        return None, "not_found"
    if key.status == "revoked":
        return None, "revoked"
    if key.expires_at < datetime.utcnow():
        return None, "expired"
    if key.subscription.status not in ("active",):
        return None, "subscription_inactive"
    return key, None


def register_device(db: Session, key: LicenseKey, fingerprint: str, hostname: Optional[str], os_name: Optional[str]) -> Tuple[Optional[Device], Optional[str]]:
    """
    register_device(db, key, fingerprint, hostname, os_name)
    Usage: called after get_active_license_key succeeds. If this device
    fingerprint has already activated this key, just refreshes
    last_seen_at (so reinstalling on the same machine never costs a
    seat). Otherwise checks the plan's max_devices before adding a new
    row. Returns (device, None) on success or (None, "seat_limit_reached").
    """
    existing = db.query(Device).filter(Device.license_key_id == key.id, Device.fingerprint == fingerprint).first()
    if existing is not None:
        existing.last_seen_at = datetime.utcnow()
        existing.hostname = hostname or existing.hostname
        existing.os = os_name or existing.os
        db.commit()
        return existing, None

    plan: Plan = key.subscription.plan
    active_device_count = (
        db.query(Device).filter(Device.license_key_id == key.id, Device.is_active.is_(True)).count()
    )
    if active_device_count >= plan.max_devices:
        return None, "seat_limit_reached"

    device = Device(license_key_id=key.id, fingerprint=fingerprint, hostname=hostname, os=os_name)
    db.add(device)
    db.commit()
    db.refresh(device)
    return device, None


def log_event(db: Session, event_type: str, license_key_id: Optional[str] = None, device_id: Optional[str] = None, detail: Optional[str] = None, ip_address: Optional[str] = None) -> None:
    """
    log_event(db, event_type, license_key_id, device_id, detail, ip_address)
    Usage: call after every activate/heartbeat/deactivate/denied outcome
    so support can look up "what happened to this key" without digging
    through server logs. event_type is one of the activation_events
    CHECK constraint values.
    """
    db.add(
        ActivationEvent(
            event_type=event_type,
            license_key_id=license_key_id,
            device_id=device_id,
            detail=detail,
            ip_address=ip_address,
        )
    )
    db.commit()


def deactivate_device(db: Session, key: LicenseKey, fingerprint: str) -> bool:
    """
    deactivate_device(db, key, fingerprint)
    Usage: frees a seat when a user explicitly signs out of a device
    (e.g. before reinstalling Windows). Returns True if a matching
    device was found and deactivated, False otherwise.
    """
    device = db.query(Device).filter(Device.license_key_id == key.id, Device.fingerprint == fingerprint).first()
    if device is None:
        return False
    device.is_active = False
    db.commit()
    return True


def get_or_create_usage_period(db: Session, key_code: str, seconds_included: int) -> OnlineUsagePeriod:
    """
    get_or_create_usage_period(db, key_code, seconds_included)
    Usage: called from /v1/usage/report on every report. Looks up (or
    creates) the row for this key's current calendar month. A missing
    row for the current period IS the reset — seconds_used starts at 0.
    seconds_included is only used when a period must be created (a plan
    change mid-month never rewrites an in-progress period's cap).
    """
    today = datetime.utcnow().date()
    period_start = today.replace(day=1)
    next_month = period_start.month % 12 + 1
    next_year = period_start.year + (1 if period_start.month == 12 else 0)
    period_end = period_start.replace(year=next_year, month=next_month)

    period = (
        db.query(OnlineUsagePeriod)
        .filter(OnlineUsagePeriod.key_code == key_code, OnlineUsagePeriod.period_start == period_start)
        .first()
    )
    if period is None:
        period = OnlineUsagePeriod(
            key_code=key_code,
            period_start=period_start,
            period_end=period_end,
            seconds_used=0,
            seconds_included=seconds_included,
        )
        db.add(period)
        db.commit()
        db.refresh(period)
    return period


def add_usage_seconds(db: Session, period: OnlineUsagePeriod, seconds_delta: float) -> OnlineUsagePeriod:
    """
    add_usage_seconds(db, period, seconds_delta)
    Usage: called right after get_or_create_usage_period() with however
    many online-engine seconds just elapsed. Sets exceeded_at the first
    time seconds_used crosses seconds_included this period, and leaves
    it set even if seconds_used is later adjusted.
    """
    period.seconds_used += int(seconds_delta)
    if period.seconds_used >= period.seconds_included and period.exceeded_at is None:
        period.exceeded_at = datetime.utcnow()
    db.commit()
    db.refresh(period)
    return period