"""
check_fingerprint.py

Standalone diagnostic — NOT part of the app itself. Prints exactly
what get_device_fingerprint() in licensing.py would compute, plus its
two raw ingredients, so you can tell whether it's stable across
separate launches or drifting (which would explain a false "already
activated on another device" error).

Usage:
    python check_fingerprint.py

Run it two or three times in separate terminal invocations (not just
twice in a loop in the same run — that always matches, since nothing
changes mid-process) and compare the "fingerprint" line each time.
- Same every time -> the fingerprint itself is fine; the "already in
  use" message is the server genuinely reporting another activation
  (or the seat limit for this key), not a client-side bug.
- Different each time -> uuid.getnode() is falling back to a random
  MAC on this machine (common on VPNs, some VMs, or adapters Windows
  hides from Python), and that's the real bug: the server sees a new
  "device" every launch and burns through the seat limit accordingly.
"""

import hashlib
import platform
import uuid

mac = uuid.getnode()
hostname = platform.node()
raw = f"{mac}:{hostname}"
fingerprint = hashlib.sha256(raw.encode()).hexdigest()[:32]

print(f"uuid.getnode()   = {mac} (hex: {mac:012x})")
print(f"platform.node()  = {hostname!r}")
print(f"raw string       = {raw!r}")
print(f"fingerprint      = {fingerprint}")

# uuid.getnode() sets bit 0 of the first octet (the "multicast" bit)
# on any address it had to fabricate, since no real MAC has that bit
# set — this is the standard way to tell "real hardware MAC" from
# "randomly generated fallback" apart.
if mac & 0x010000000000:
    print()
    print("^ This looks FABRICATED, not a real hardware MAC (multicast")
    print("  bit is set). uuid.getnode() likely can't see a real network")
    print("  adapter on this system, and may return a DIFFERENT random")
    print("  value on every run — which would explain the false")
    print("  'already used on another device' error.")
else:
    print()
    print("^ This looks like a real hardware MAC address (multicast bit")
    print("  not set), so it should be stable across runs.")