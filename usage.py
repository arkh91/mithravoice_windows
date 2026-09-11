"""
usage.py

Tracks how much of the online engine's monthly hours quota this
license key has used (see mithracorp.com/mithravoice.html#pricing —
Bronze/Silver/Gold online plans each include a fixed hours/mo
allowance; offline plans and pay-as-you-go are unaffected).

IMPORTANT: the authoritative counter lives on the license server, not
here — a purely local counter could be reset just by deleting a file,
which would defeat a paid quota entirely. This module only reports
elapsed seconds to the server and caches its last answer for smooth,
offline-resilient display; it never decides on its own that a period
has reset or that a quota is exceeded. See server_schema_online_usage.sql
for the proposed server-side table and endpoint contract this talks
to — that server code isn't part of this repo (same as the rest of
licensing.py's calls to settings.license_server_url).

Expected /v1/usage/report contract (proposed):
    POST /v1/usage/report
    body: {"token": "<cached JWT>", "seconds_delta": 42}
    200 response: {
        "seconds_used": 1234,
        "seconds_included": 36000,   # null/omitted => no monthly cap (e.g. pay-as-you-go)
        "seconds_remaining": 34766,  # null/omitted when seconds_included is
        "period_end": "2026-10-01T00:00:00Z",
        "exceeded": false
    }

Usage:
    from usage import report_usage, get_cached_usage_snapshot

    snapshot = report_usage(token, seconds_delta=20)   # call periodically while the online engine is active
    cached = get_cached_usage_snapshot()               # for an instant, no-network first paint
"""

import json
import platform
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import requests

import connectivity
from config import settings

# Mirrors mithracorp.com/mithravoice.html#pricing's online plans.
# Used ONLY as a local fallback (see _local_fallback_snapshot below)
# for when the real server can't be reached at all — e.g. because
# /v1/usage/report doesn't exist there yet. Once that endpoint is
# live, its response is always authoritative and this table is never
# consulted for any key/plan that got a real answer at least once.
PLAN_INCLUDED_HOURS = {
    "online_bronze_monthly": 10,
    "online_silver_monthly": 25,
    "online_gold_monthly": 40,
}

# Back-off schedule for a license server that isn't answering, in
# seconds since the last failed attempt. Consecutive failures walk down
# this list and stay at the last entry.
#
# Without a back-off, report_usage() retried at full rate against a
# host that could not even be resolved. Each attempt cost a DNS
# timeout, and each printed a ~400-character traceback-laden line — so
# an offline session produced a steady scroll of identical failures
# that buried the [pipeline]/[azure] messages actually worth reading,
# for no benefit whatsoever: nothing about the outcome changes between
# one failed DNS lookup and the next one a second later. Usage is never
# lost by waiting, since the delta accumulates in pending_seconds and
# is reconciled on the next successful report.
_RETRY_BACKOFF_SECONDS = (0.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0)

# Consecutive failed reports, and the monotonic() of the most recent
# attempt. Reset to zero the moment any report succeeds.
_consecutive_failures = 0
_last_attempt_at = 0.0


def _should_attempt_server() -> bool:
    """
    _should_attempt_server()
    Usage: internal — True when enough time has passed since the last
    failed report to be worth trying the license server again. Always
    True while things are healthy, so a working setup pays nothing for
    this.
    """
    if _consecutive_failures == 0:
        return True
    index = min(_consecutive_failures, len(_RETRY_BACKOFF_SECONDS) - 1)
    return (time.monotonic() - _last_attempt_at) >= _RETRY_BACKOFF_SECONDS[index]


def _note_attempt(succeeded: bool) -> None:
    """
    _note_attempt(succeeded)
    Usage: internal — records the outcome of one server attempt so
    _should_attempt_server() can pace the next one. A single success
    clears the back-off entirely, so recovery from a network blip is
    immediate rather than having to work back up the schedule.
    """
    global _consecutive_failures, _last_attempt_at
    _last_attempt_at = time.monotonic()
    _consecutive_failures = 0 if succeeded else _consecutive_failures + 1


@dataclass
class UsageSnapshot:
    seconds_used: int
    seconds_included: Optional[int]  # None => no monthly cap (pay-as-you-go / uncapped plan)
    seconds_remaining: Optional[int]  # None when seconds_included is None
    period_end: Optional[str]  # ISO 8601 string from the server, display-only
    exceeded: bool
    fetched_at: float  # time.time() this snapshot was last confirmed by the server
    pending_seconds: float = 0.0  # reported locally but not yet confirmed by the server (network was down)


def _usage_cache_path() -> Path:
    """
    _usage_cache_path()
    Usage: internal — same OS-appropriate per-user app-data location
    licensing.py and app_settings.py already use, just a different
    filename.
    """
    if platform.system() == "Windows":
        base = Path.home() / "AppData" / "Roaming" / "MithraCorp"
    else:
        base = Path.home() / ".config" / "mithracorp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "online_usage.json"


def get_cached_usage_snapshot() -> Optional[UsageSnapshot]:
    """
    get_cached_usage_snapshot()
    Usage: called on launch (and by api.py's get_usage_status()) to
    show the countdown immediately without waiting on a network round
    trip. Returns None if there's no cached snapshot yet (first launch,
    or a plan with no online access at all).
    """
    path = _usage_cache_path()
    if not path.exists():
        return None
    try:
        return UsageSnapshot(**json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def _save_snapshot(snapshot: UsageSnapshot) -> None:
    """
    _save_snapshot(snapshot)
    Usage: internal — persists after every report_usage() call
    (success or failure) so the cache always reflects the best
    information currently available, including any not-yet-confirmed
    pending_seconds.
    """
    _usage_cache_path().write_text(json.dumps(asdict(snapshot)))


def clear_cached_usage() -> None:
    """
    clear_cached_usage()
    Usage: call from api.py whenever the active license key changes —
    a fresh activate_license() (new key) or deactivate_device() (this
    device no longer holds any key). _usage_cache_path() is keyed
    per-DEVICE, not per-key, since nothing about this module's file
    name or contents ever recorded which key a cached snapshot
    belonged to. Left uncleared, activating a brand-new key would
    silently inherit whatever seconds_used/seconds_remaining the
    PREVIOUS key last cached here, showing the new key's countdown as
    however much time the old key happened to have left rather than
    the new key's own fresh allowance — that mismatch is exactly what
    this function exists to prevent. Safe to call even if there's no
    cache file yet (e.g. first-ever activation on this device).
    """
    path = _usage_cache_path()
    if path.exists():
        path.unlink()


def _local_fallback_snapshot(plan_code: Optional[str], seconds_delta: float) -> Optional[UsageSnapshot]:
    """
    _local_fallback_snapshot(plan_code, seconds_delta)
    Usage: internal — builds a fresh, LOCALLY-COMPUTED snapshot when
    there's no cache yet AND the server call failed (most likely
    because /v1/usage/report doesn't exist there yet — see this
    module's docstring). This is a stand-in, not authoritative: it
    starts a plan's known included hours (see PLAN_INCLUDED_HOURS)
    at 0 used and counts up locally from here, with no knowledge of
    usage from a previous install or another device. It gets replaced
    entirely the moment a real server response ever comes back
    successfully. Returns None for plan codes with no fixed monthly
    cap (pay-as-you-go, or an unrecognized/missing plan code) — the UI
    treats that the same as an offline/unmetered engine (see
    index.html's renderUsagePill(), which shows "∞" for both).
    """
    if not plan_code:
        return None
    included_hours = PLAN_INCLUDED_HOURS.get(plan_code.strip().lower())
    if included_hours is None:
        return None
    seconds_included = included_hours * 3600
    seconds_used = int(seconds_delta)
    return UsageSnapshot(
        seconds_used=seconds_used,
        seconds_included=seconds_included,
        seconds_remaining=max(0, seconds_included - seconds_used),
        period_end=None,
        exceeded=seconds_used >= seconds_included,
        fetched_at=time.time(),
        pending_seconds=seconds_delta,  # never actually confirmed by a server
    )


def _bank_locally(seconds_delta: float, plan_code: Optional[str]) -> Optional[UsageSnapshot]:
    """
    _bank_locally(seconds_delta, plan_code)
    Usage: internal — the "couldn't reach the server this time" path,
    shared by all three reasons that happens (no network, backing off,
    or the request itself failed). Adds seconds_delta to the cached
    snapshot's pending_seconds and returns an OPTIMISTIC snapshot —
    last known remaining minus everything pending, clamped at 0 — so
    the on-screen countdown keeps ticking smoothly instead of freezing
    or jumping. The real total is reconciled by the next successful
    report; nothing is ever lost by not calling the server right now.

    Returns None only when there is no cache AND plan_code doesn't map
    to a known capped plan, i.e. there is genuinely nothing to show.
    """
    cached = get_cached_usage_snapshot()
    if cached is None:
        fallback = _local_fallback_snapshot(plan_code, seconds_delta)
        if fallback is not None:
            _save_snapshot(fallback)
        return fallback

    cached.pending_seconds += seconds_delta
    if cached.seconds_remaining is not None:
        cached.seconds_remaining = max(0, cached.seconds_remaining - int(seconds_delta))
        cached.seconds_used += int(seconds_delta)
    _save_snapshot(cached)
    return cached


def report_usage(token: Optional[str], seconds_delta: float, plan_code: Optional[str] = None) -> Optional[UsageSnapshot]:
    """
    report_usage(token, seconds_delta, plan_code=None)
    Usage: call periodically (see api.py's _flush_online_usage()) with
    however many seconds of ONLINE engine time have elapsed since the
    last call — never for offline engine time, which isn't metered.
    seconds_delta of 0 is a valid no-op way to just refresh the cached
    snapshot from the server without adding usage. plan_code (e.g.
    "bronze") is only used for _local_fallback_snapshot() above, on
    the very first call when there's no cache yet AND the server is
    unreachable.

    On success: the server's answer is authoritative and fully
    replaces the cache, with pending_seconds reset to 0.

    On failure (offline, server unreachable, or /v1/usage/report not
    implemented yet): the call is banked locally instead of being lost
    — see _bank_locally() — and an optimistic snapshot is returned so
    the on-screen countdown keeps ticking smoothly through a network
    blip rather than freezing or jumping.

    Skips the server entirely, taking that same banking path, when
    connectivity.is_online() says there is nothing to reach, or when a
    recent failure means we are still backing off (see
    _RETRY_BACKOFF_SECONDS). Neither skip loses usage; both exist
    because a doomed request costs a DNS timeout and a log line and
    buys nothing.

    Returns None only when there's no cache, the server call failed,
    AND plan_code doesn't map to a known capped plan — i.e. genuinely
    nothing to show (offline-only/pay-as-you-go plans, or no plan
    known at all).
    """
    if not token:
        print("[usage] report_usage called with no cached license token — skipping the server and returning whatever's cached, if anything", flush=True)
        return get_cached_usage_snapshot()

    # Two cheap reasons not to call the server at all. Both take the
    # same path as a failed call — the delta is banked in
    # pending_seconds and reconciled later — so skipping costs nothing
    # but a DNS timeout and a log line.
    #
    # The connectivity check matters most during exactly the situation
    # these logs were captured in: a session that fell back to offline
    # because the network went away. Usage isn't even accruing then
    # (offline time is not metered — see the README's Usage Reporting
    # table), so hammering an unresolvable hostname is pure noise.
    if not _should_attempt_server():
        return _bank_locally(seconds_delta, plan_code)
    if not connectivity.is_online():
        _note_attempt(succeeded=False)
        if _consecutive_failures == 1:
            print("[usage] no network; banking usage locally until the license server is reachable again", flush=True)
        return _bank_locally(seconds_delta, plan_code)

    try:
        resp = requests.post(
            f"{settings.license_server_url}/v1/usage/report",
            json={"token": token, "seconds_delta": seconds_delta},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        # Previously silent — this is the ONLY thing standing between
        # "usage isn't reaching the DB" and knowing why: a genuinely
        # unreachable server (DNS/connection/timeout) looks identical
        # from the caller's side to a reachable server that rejected
        # the request (401/403 bad token, 500 server bug), but they
        # need very different fixes. exc.response is only set for the
        # latter case (an HTTPError from raise_for_status()) — print
        # its body too, since that's usually the actual detail message
        # (e.g. {"detail": "revoked"} or a stack trace) FastAPI sent
        # back, not just the generic status code.
        detail = ""
        if getattr(exc, "response", None) is not None:
            detail = f" — server responded {exc.response.status_code}: {exc.response.text[:300]!r}"
        _note_attempt(succeeded=False)
        # Logged on the first failure of a run only. Repeating an
        # identical several-hundred-character line every few seconds
        # for the whole duration of an outage doesn't add information,
        # it just hides the [pipeline] and [azure] lines that do. The
        # back-off (see _RETRY_BACKOFF_SECONDS) means later failures
        # are also far rarer.
        if _consecutive_failures == 1:
            print(f"[usage] report_usage to {settings.license_server_url}/v1/usage/report failed, banking usage "
                  f"locally and backing off: {exc!r}{detail}", flush=True)
        return _bank_locally(seconds_delta, plan_code)

    _note_attempt(succeeded=True)
    snapshot = UsageSnapshot(
        seconds_used=data.get("seconds_used", 0),
        seconds_included=data.get("seconds_included"),
        seconds_remaining=data.get("seconds_remaining"),
        period_end=data.get("period_end"),
        exceeded=bool(data.get("exceeded", False)),
        fetched_at=time.time(),
        pending_seconds=0.0,
    )
    _save_snapshot(snapshot)
    return snapshot
