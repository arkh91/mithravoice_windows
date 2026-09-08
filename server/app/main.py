"""
app/main.py

The license server's HTTP API. Run behind nginx + TLS on the Ubuntu box
(see server/DEPLOY.md) — this file itself just defines the routes.

Endpoints:
    POST /v1/activate      — validate a key + register this device, issue a signed token
    POST /v1/heartbeat     — optional periodic re-check while online (catches revocations early)
    POST /v1/deactivate    — free up a device seat
    POST /v1/usage/report  — report elapsed online-engine seconds, get back hours remaining
    GET  /v1/latest-version — desktop app update check
    GET  /healthz          — for the systemd/nginx health check

Usage:
    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.models import AppVersion
from app.schemas import (
    ActivateRequest,
    ActivateResponse,
    DeactivateRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    LatestVersionResponse,
    PlanInfo,
    UsageReportRequest,
    UsageReportResponse,
)
from app.security import issue_license_token

app = FastAPI(title="MithraVoice License Server")


@app.get("/healthz")
def healthz():
    """
    healthz()
    Usage (HTTP): GET /healthz — used by nginx/uptime monitors. No DB
    access, so it stays fast and doesn't count against connection pool
    limits during an outage.
    """
    return {"ok": True}


@app.post("/v1/activate", response_model=ActivateResponse)
def activate(body: ActivateRequest, request: Request, db: Session = Depends(get_db)):
    """
    activate(body, request, db)
    Usage (HTTP): POST /v1/activate {key_code, device_fingerprint, hostname, os}
    Called by the desktop app the first time a user enters their
    subscription key, and again whenever the cached token has expired.
    Validates the key, enforces the plan's seat limit, records the
    device, and returns a signed JWT the client can verify offline for
    settings.token_validity_days.
    """
    ip = request.client.host if request.client else None

    key, reason = crud.get_active_license_key(db, body.key_code)
    if key is None:
        crud.log_event(db, "denied", detail=f"activate:{reason}:{body.key_code}", ip_address=ip)
        raise HTTPException(status_code=403, detail=reason)

    device, reason = crud.register_device(db, key, body.device_fingerprint, body.hostname, body.os)
    if device is None:
        crud.log_event(db, "denied", license_key_id=str(key.id), detail=f"activate:{reason}", ip_address=ip)
        raise HTTPException(status_code=403, detail=reason)

    plan = key.subscription.plan
    token, expires_at = issue_license_token(
        license_key_id=str(key.id),
        key_code=key.key_code,
        plan_code=plan.code,
        online_allowed=plan.online_allowed,
        offline_allowed=plan.offline_allowed,
        device_fingerprint=body.device_fingerprint,
    )
    crud.log_event(db, "activate", license_key_id=str(key.id), device_id=str(device.id), ip_address=ip)

    return ActivateResponse(
        token=token,
        plan=PlanInfo(
            code=plan.code,
            name=plan.name,
            online_allowed=plan.online_allowed,
            offline_allowed=plan.offline_allowed,
            max_devices=plan.max_devices,
        ),
        expires_at=expires_at.isoformat(),
    )


@app.post("/v1/heartbeat", response_model=HeartbeatResponse)
def heartbeat(body: HeartbeatRequest, db: Session = Depends(get_db)):
    """
    heartbeat(body, db)
    Usage (HTTP): POST /v1/heartbeat {token}
    Optional call the client can make whenever it has internet, to catch
    a revoked key before the cached token's natural expiry. Decodes the
    token WITHOUT verifying signature/exp here (that already happened
    client-side); this endpoint's only job is checking the underlying
    key hasn't been revoked or expired server-side since the token was
    issued.
    """
    import jwt as pyjwt

    try:
        claims = pyjwt.decode(body.token, options={"verify_signature": False})
    except Exception:
        return HeartbeatResponse(valid=False, reason="malformed_token")

    key, reason = crud.get_active_license_key(db, claims.get("key_code", ""))
    if key is None:
        return HeartbeatResponse(valid=False, reason=reason)
    return HeartbeatResponse(valid=True)


@app.post("/v1/deactivate")
def deactivate(body: DeactivateRequest, db: Session = Depends(get_db)):
    """
    deactivate(body, db)
    Usage (HTTP): POST /v1/deactivate {key_code, device_fingerprint}
    Frees a device seat, e.g. before the user reinstalls their OS or
    moves to a new machine and wants their old activation released.
    """
    key = crud.get_license_key(db, body.key_code)
    if key is None:
        raise HTTPException(status_code=404, detail="not_found")
    freed = crud.deactivate_device(db, key, body.device_fingerprint)
    crud.log_event(db, "deactivate", license_key_id=str(key.id), detail=f"freed={freed}")
    return {"ok": True, "freed": freed}


@app.post("/v1/usage/report", response_model=UsageReportResponse)
def report_usage(body: UsageReportRequest, db: Session = Depends(get_db)):
    """
    report_usage(body, db)
    Usage (HTTP): POST /v1/usage/report {token, seconds_delta}
    Called periodically by the client (see usage.py's report_usage())
    while the online engine is active. Decodes the key_code out of the
    token the same way /v1/heartbeat does (signature already verified
    client-side), adds seconds_delta to this month's usage period for
    that key, and returns the running total so the client can render an
    accurate countdown / stop the session if exceeded. For plans with
    no online_hours_included cap (pay-as-you-go, or an offline-only
    plan), returns seconds_included=None so the client shows "no cap"
    instead of tracking a period at all.
    """
    import jwt as pyjwt

    try:
        claims = pyjwt.decode(body.token, options={"verify_signature": False})
    except Exception:
        raise HTTPException(status_code=401, detail="malformed_token")

    key, reason = crud.get_active_license_key(db, claims.get("key_code", ""))
    if key is None:
        raise HTTPException(status_code=403, detail=reason)

    plan = key.subscription.plan
    if plan.online_hours_included is None:
        return UsageReportResponse(
            seconds_used=0,
            seconds_included=None,
            seconds_remaining=None,
            period_end=key.subscription.current_period_end.isoformat(),
            exceeded=False,
        )

    period = crud.get_or_create_usage_period(db, key.key_code, plan.online_hours_included * 3600)
    period = crud.add_usage_seconds(db, period, body.seconds_delta)

    return UsageReportResponse(
        seconds_used=period.seconds_used,
        seconds_included=period.seconds_included,
        seconds_remaining=max(0, period.seconds_included - period.seconds_used),
        period_end=period.period_end.isoformat(),
        exceeded=period.exceeded_at is not None,
    )


@app.get("/v1/latest-version", response_model=LatestVersionResponse)
def latest_version(db: Session = Depends(get_db)):
    """
    latest_version(db)
    Usage (HTTP): GET /v1/latest-version
    Called by the desktop app's updater.py on launch to check whether a
    newer version is available. Reads whichever row in app_versions has
    is_latest=1 — that flag is managed by publish_version.py, never set
    by hand. Returns 404 if no version has ever been published yet.
    """
    version = db.query(AppVersion).filter(AppVersion.is_latest.is_(True)).first()
    if version is None:
        raise HTTPException(status_code=404, detail="no_version_published")
    return LatestVersionResponse(
        version=version.version,
        download_url=version.download_url,
        notes=version.release_notes,
    )