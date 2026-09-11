"""
connectivity.py

Answers "can this machine reach Azure right now?" — which is a
different question from "is there a network adapter with an IP", and
the difference is why the app kept insisting it was connected while
the online engine refused to run.

The UI used to answer it with navigator.onLine. That flag reports
whether the OS thinks a network interface is up. It is true on a
café Wi-Fi that hasn't been paid for, on a VPN that's connected but
routing nothing, behind a proxy that blocks WebSockets, and on a
machine whose DNS has quietly stopped resolving. In every one of those
cases the globe went green and Azure was unreachable.

So this probes the host that actually matters — the speech translation
endpoint the online engine opens a WebSocket to — with a TCP+TLS
connect. No credentials, no quota, no request body: just "does the
handshake complete". That is the smallest check that can't be fooled by
a link-up-but-going-nowhere network.

Usage:
    import connectivity
    if connectivity.is_online():
        ...
    monitor = connectivity.ConnectivityMonitor(on_change=lambda online: ...)
    monitor.start()
    monitor.stop()
"""

import socket
import ssl
import threading
import time
from typing import Callable, Optional

from config import settings

# Port 443 for both candidates below; the point of the probe is that a
# TLS handshake to the real service host completes, not merely that
# something answers on some port.
_HTTPS_PORT = 443

# Checked in order, first success wins. The speech host is the one that
# actually matters, so it goes first — a network that reaches the
# fallback but not this one is, for our purposes, offline. The fallback
# exists only so the probe still means something when no region is
# configured yet (first run, before .env is filled in).
_FALLBACK_HOST = "www.microsoft.com"

# A probe costs a TLS handshake, so results are cached briefly. Long
# enough that the watchdog, the UI poll, and a session start happening
# within the same moment share one answer; short enough that pulling
# the Wi-Fi is noticed on the next watchdog tick rather than half a
# minute later.
CACHE_TTL_SECONDS = 3.0

# Deliberately short. This runs on the path to starting a session, and
# a user who has just clicked Resume should not wait on a dead network
# — if Azure can't be reached in this long, going straight to the
# offline engine is both the right answer and the faster one.
PROBE_TIMEOUT_SECONDS = 3.0

# How often ConnectivityMonitor re-probes while idle. Only used for the
# idle globe, so responsiveness matters more than precision; a live
# session gets its connectivity signal from the engine itself.
MONITOR_INTERVAL_SECONDS = 5.0

_cache_lock = threading.Lock()
_cached_result: Optional[bool] = None
_cached_at: float = 0.0


def speech_host() -> str:
    """
    speech_host()
    Usage: `connectivity.speech_host()` -> "eastus.s2s.speech.microsoft.com".
    The host the online engine's WebSocket actually targets, derived
    from the configured region. Returns the generic fallback when no
    region is set, so a probe on a half-configured install still
    reports something meaningful instead of raising.
    """
    region = (settings.azure_speech_region or "").strip()
    if not region:
        return _FALLBACK_HOST
    return f"{region}.s2s.speech.microsoft.com"


def _tls_reachable(host: str, timeout: float) -> bool:
    """
    _tls_reachable(host, timeout)
    Usage: internal — True when a full TLS handshake to host:443
    completes. A plain TCP connect isn't enough: captive portals and
    some corporate middleboxes accept the TCP connection and then serve
    their own thing, which a socket-level check reads as success. The
    handshake is what distinguishes "reached Microsoft" from "reached
    something".
    """
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, _HTTPS_PORT), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host):
                return True
    except Exception:  # noqa: BLE001 - every failure mode here means the same thing: not reachable
        return False


def is_online(force: bool = False, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """
    is_online(force=False, timeout=PROBE_TIMEOUT_SECONDS)
    Usage: `if connectivity.is_online(): ...` — True when Azure's speech
    endpoint is actually reachable. Pass force=True to bypass the
    short-lived cache, which is what you want immediately after a
    suspected network change rather than on a routine poll.
    """
    global _cached_result, _cached_at

    if not force:
        with _cache_lock:
            if _cached_result is not None and (time.monotonic() - _cached_at) < CACHE_TTL_SECONDS:
                return _cached_result

    host = speech_host()
    result = _tls_reachable(host, timeout)
    if not result and host != _FALLBACK_HOST:
        # Distinguishes "the whole network is down" from "this one host
        # is blocked". Both mean the online engine can't run, so the
        # return value is the same either way — but the log line is the
        # difference between the user checking their Wi-Fi and checking
        # their firewall, and that's worth one extra handshake on the
        # failure path only.
        if _tls_reachable(_FALLBACK_HOST, timeout):
            print(f"[connectivity] network is up but {host} is unreachable — likely a firewall or proxy", flush=True)
        else:
            print("[connectivity] no network connectivity", flush=True)

    with _cache_lock:
        _cached_result, _cached_at = result, time.monotonic()
    return result


def invalidate() -> None:
    """
    invalidate()
    Usage: `connectivity.invalidate()` — drops the cached answer so the
    next is_online() re-probes. Call after anything that plausibly
    changed the network (an engine failing on a connection error, the
    user reporting a problem) rather than waiting out the TTL.
    """
    global _cached_result
    with _cache_lock:
        _cached_result = None


class ConnectivityMonitor:
    """
    ConnectivityMonitor
    Usage: drives the idle-state connectivity indicator, where no engine
    is running to report a real answer.

        monitor = ConnectivityMonitor(on_change=lambda online: push_to_ui(online))
        monitor.start()
        ...
        monitor.stop()

    on_change fires only on TRANSITIONS, not every poll, so a UI hook
    doesn't need to debounce. It fires once on the first probe so the
    initial state is reported rather than assumed.
    """

    def __init__(self, on_change: Callable[[bool], None], interval: float = MONITOR_INTERVAL_SECONDS):
        self._on_change = on_change
        self._interval = interval
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._last: Optional[bool] = None

    def _loop(self) -> None:
        """
        _loop()
        Usage: internal — the monitor thread body. Wrapped so a failing
        on_change handler can't kill the thread and leave the indicator
        frozen on whatever it happened to show last.
        """
        while self._running.is_set():
            online = is_online()
            if online != self._last:
                self._last = online
                try:
                    self._on_change(online)
                except Exception as exc:  # noqa: BLE001 - a UI update must not stop monitoring
                    print(f"[connectivity] on_change handler raised: {exc!r}", flush=True)
            # Wait on the event rather than sleeping, so stop() takes
            # effect immediately instead of after a full interval.
            self._running.wait(self._interval)

    def start(self) -> None:
        """
        start()
        Usage: begins polling on a daemon thread. Safe to call twice;
        the second call is a no-op rather than a second thread.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="connectivity-monitor")
        self._thread.start()

    def stop(self) -> None:
        """
        stop()
        Usage: ends polling. Safe to call even if start() never ran.
        """
        self._running.clear()

    @property
    def last_known(self) -> Optional[bool]:
        """
        last_known
        Usage: `monitor.last_known` — the most recent probe result
        without triggering a new one, or None before the first probe.
        """
        return self._last
