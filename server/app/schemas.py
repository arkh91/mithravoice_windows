"""
app/schemas.py

Pydantic models for the license API's request/response bodies. Kept
separate from app/models.py (the DB layer) so the wire format can evolve
independently of the schema.

Usage:
    @app.post("/v1/activate", response_model=ActivateResponse)
    def activate(body: ActivateRequest): ...
"""

from typing import Optional

from pydantic import BaseModel


class ActivateRequest(BaseModel):
    key_code: str
    device_fingerprint: str
    hostname: Optional[str] = None
    os: Optional[str] = None


class PlanInfo(BaseModel):
    code: str
    name: str
    online_allowed: bool
    offline_allowed: bool
    max_devices: int


class ActivateResponse(BaseModel):
    token: str  # signed JWT the client caches and verifies offline
    plan: PlanInfo
    expires_at: str  # ISO timestamp — informational; the real expiry is inside the JWT


class HeartbeatRequest(BaseModel):
    token: str


class HeartbeatResponse(BaseModel):
    valid: bool
    reason: Optional[str] = None


class DeactivateRequest(BaseModel):
    key_code: str
    device_fingerprint: str


class LatestVersionResponse(BaseModel):
    version: str
    download_url: str
    notes: Optional[str] = None


class UsageReportRequest(BaseModel):
    token: str
    seconds_delta: float


class UsageReportResponse(BaseModel):
    seconds_used: int
    seconds_included: Optional[int]  # None => no monthly cap (pay-as-you-go / uncapped plan)
    seconds_remaining: Optional[int]  # None when seconds_included is None
    period_end: str
    exceeded: bool