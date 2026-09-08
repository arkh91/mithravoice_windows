"""
app/models.py

SQLAlchemy ORM models mirroring server/schema.sql exactly — see that
file for the authoritative table/column documentation. Kept in sync by
hand since this project doesn't (yet) use Alembic migrations; if you add
a column here, add it to schema.sql too.

IDs are CHAR(36) UUID strings rather than a Postgres-only UUID type, to
match the MySQL schema. Python generates the UUID (uuid.uuid4()) on
insert; the database's own DEFAULT (UUID()) in schema.sql is just a
safety net for rows inserted outside the app.

online_usage_periods (OnlineUsagePeriod below) is the one exception:
its id is a plain auto-increment BIGINT, not a UUID, matching
server_schema_online_usage.sql exactly (it was designed and reviewed
separately from the rest of this schema).

Usage:
    from app.models import LicenseKey
    key = db.query(LicenseKey).filter_by(key_code=code).first()
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _new_uuid() -> str:
    """
    _new_uuid()
    Usage: default= callable for every UUID primary key column below.
    Kept as a named function (rather than a lambda) so it shows up
    cleanly in SQLAlchemy's repr/debug output. Defined before any class
    that references it, since `default=_new_uuid` is evaluated as soon
    as that line in the class body runs.
    """
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user")


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    max_devices: Mapped[int] = mapped_column(Integer, default=1)
    online_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    offline_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    # NULL => no monthly online-hours cap (pay-as-you-go, or an offline
    # plan where this column is simply unused). Set in hours*3600 when
    # a period is first created (see crud.get_or_create_usage_period).
    online_hours_included: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    billing_interval: Mapped[str] = mapped_column(String(20), default="monthly")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(String(20), default="active")
    current_period_start: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    current_period_end: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="subscriptions")
    plan: Mapped["Plan"] = relationship()
    license_keys: Mapped[list["LicenseKey"]] = relationship(back_populates="subscription")


class LicenseKey(Base):
    __tablename__ = "license_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id", ondelete="CASCADE"))
    key_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active")
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    subscription: Mapped["Subscription"] = relationship(back_populates="license_keys")
    devices: Mapped[list["Device"]] = relationship(back_populates="license_key")


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("license_key_id", "fingerprint"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    license_key_id: Mapped[str] = mapped_column(String(36), ForeignKey("license_keys.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=True)
    os: Mapped[str] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    license_key: Mapped["LicenseKey"] = relationship(back_populates="devices")


class ActivationEvent(Base):
    __tablename__ = "activation_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    license_key_id: Mapped[str] = mapped_column(String(36), ForeignKey("license_keys.id", ondelete="SET NULL"), nullable=True)
    device_id: Mapped[str] = mapped_column(String(36), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OnlineUsagePeriod(Base):
    """
    OnlineUsagePeriod
    Usage: from app.models import OnlineUsagePeriod
    One row per (key_code, calendar month) — see
    server_schema_online_usage.sql for the column rationale. A missing
    row for the current period IS the reset (seconds_used starts at 0
    implicitly); no separate reset job is needed.
    """
    __tablename__ = "online_usage_periods"
    __table_args__ = (UniqueConstraint("key_code", "period_start"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key_code: Mapped[str] = mapped_column(String(32), ForeignKey("license_keys.key_code"), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    seconds_used: Mapped[int] = mapped_column(Integer, default=0)
    seconds_included: Mapped[int] = mapped_column(Integer, nullable=False)
    exceeded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppVersion(Base):
    __tablename__ = "app_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    version: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=False)
    download_url: Mapped[str] = mapped_column(String(500), nullable=False)
    release_notes: Mapped[str] = mapped_column(Text, nullable=True)
    released_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)