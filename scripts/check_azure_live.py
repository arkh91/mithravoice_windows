"""
scripts/check_azure_live.py

diagnose.py answers "are the credentials present?". This answers the
question that actually matters: "does Azure accept them?"

Nothing in diagnose.py touches the network. AZURE_SPEECH_KEY can be
expired, revoked, from the wrong region, or from a resource that
refuses region-based auth entirely, and diagnose.py will still print
"online engine is configured" — because it only checked that the two
strings are non-empty. That is why the app can pass diagnostics and
still drop to the offline engine the moment a session starts.

This script opens a real Speech Translation session against a real
push stream and prints whatever Azure says back, including the
cancellation reason and error details that pipeline.py currently only
prints to a console nobody is watching.

Read-only. Sends about three seconds of generated audio, costs a
fraction of a second of quota, changes nothing.

Usage:
    python scripts/check_azure_live.py
    python scripts/check_azure_live.py --from English --to "Persian (Farsi)"
    python scripts/check_azure_live.py --log azure_sdk.log
"""

import argparse
import os
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OK = "  [ ok ]"
BAD = "  [FAIL]"
WARN = "  [warn]"

# How long to wait for Azure to either start a session or cancel one.
# A healthy connection reaches session_started well inside two seconds;
# ten leaves room for a slow link without making a dead endpoint feel
# like a hang.
CONNECT_TIMEOUT_SECONDS = 10.0


def section(title: str) -> None:
    """
    section(title)
    Usage: internal — prints a heading so each check reads as its own
    result rather than one wall of text.
    """
    print(f"\n{title}\n" + "-" * len(title))


def check_env_discovery() -> None:
    """
    check_env_discovery()
    Usage: `check_env_discovery()` — reports which .env file config.py
    will ACTUALLY load, which is not necessarily the one diagnose.py
    reports finding.

    diagnose.py looks for PROJECT_ROOT/".env" (an absolute path).
    config.py's _load_dotenv() looks for Path(".env") — relative to the
    current working directory. Those agree only when the app is
    launched from the project root. Start it from a desktop shortcut, a
    Start-menu entry, or the installed .exe, and config.py silently
    finds no .env at all, azure_configured comes back False, and the
    pipeline falls back to offline before it ever reaches the network.
    Same machine, same files, different launch directory.
    """
    section("0. Where .env is being read from")

    root_env = PROJECT_ROOT / ".env"
    cwd_env = Path.cwd() / ".env"

    print(f"       project root : {PROJECT_ROOT}")
    print(f"       working dir  : {Path.cwd()}")

    if root_env.exists():
        print(f"{OK} .env exists at project root")
    else:
        print(f"{BAD} no .env at project root ({root_env})")

    if cwd_env.exists():
        print(f"{OK} config.py will load .env from the working directory")
    else:
        print(f"{BAD} config.py will NOT find a .env — it looks for a CWD-relative path")
        print("       -> this alone forces the app offline, with no error in the UI")
        print("       -> launching from anywhere but the project root reproduces it")

    if root_env.exists() and not cwd_env.exists():
        print(f"{WARN} diagnose.py reports the root .env and passes; the app reads neither")
        print("       -> apply the config.py fix (anchor _load_dotenv to the project root)")


def check_credentials() -> tuple:
    """
    check_credentials()
    Usage: `key, region, endpoint = check_credentials()` — reports the
    resolved credentials and, importantly, the SHAPE of the key.

    Key length is a real signal. A classic Speech resource issues a
    32-character hex key. An 84-character non-hex key comes from an
    Azure AI Services / AI Foundry multi-service resource, which is
    created WITH a custom subdomain — and a custom-domain resource
    rejects region-based auth with 401 no matter how correct the key
    is. That combination is invisible to a check that only measures
    whether the string is empty.
    """
    section("1. Credentials as the app resolves them")

    from config import settings

    key = settings.azure_speech_key
    region = settings.azure_speech_region
    endpoint = os.environ.get("AZURE_SPEECH_ENDPOINT", "")

    if not key:
        print(f"{BAD} AZURE_SPEECH_KEY is empty")
        return "", "", ""
    if not region and not endpoint:
        print(f"{BAD} AZURE_SPEECH_REGION is empty and no AZURE_SPEECH_ENDPOINT is set")
        return key, "", ""

    is_hex = all(c in "0123456789abcdefABCDEF" for c in key)
    print(f"{OK} key    : {len(key)} chars, {'hex' if is_hex else 'non-hex'}, ends {key[-4:]!r}")
    print(f"{OK} region : {region!r}")
    print(f"       endpoint : {endpoint!r}" if endpoint else "       endpoint : (not set — using region-based auth)")

    if len(key) > 40 and not is_hex and not endpoint:
        print(f"{WARN} this looks like an Azure AI Services / AI Foundry key, not a classic Speech key")
        print("       Those resources have a custom subdomain, and a custom-domain")
        print("       resource returns 401 for region-based auth. If step 2 fails,")
        print("       this is why — set AZURE_SPEECH_ENDPOINT and use the patched")
        print("       azure_engine.py, or create a plain Speech resource instead.")

    return key, region, endpoint


def check_token(key: str, region: str) -> bool:
    """
    check_token(key, region)
    Usage: `check_token(key, "eastus")` -> True when Azure issues a
    token. Separates the two failures that look identical from inside
    the app: credentials Azure rejects (401/403, a fast clean answer)
    versus a network that never reaches Azure at all (timeout, DNS
    failure, TLS interception). The Speech SDK reports both as a
    cancellation with much less detail.
    """
    section("2. Does Azure accept the key? (REST token request)")

    try:
        import requests
    except ImportError:
        print(f"{WARN} requests not installed — skipping (pip install requests)")
        return True

    url = f"https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
    print(f"       POST {url}")
    try:
        response = requests.post(url, headers={"Ocp-Apim-Subscription-Key": key}, timeout=15)
    except Exception as exc:  # noqa: BLE001 - reporting tool; every failure mode is a finding
        print(f"{BAD} could not reach Azure at all — {exc!r}")
        print("       -> firewall, proxy, or DNS. Not a credentials problem.")
        return False

    if response.status_code == 200:
        print(f"{OK} 200 — key and region are valid and accepted")
        return True

    print(f"{BAD} HTTP {response.status_code}")
    if response.status_code in (401, 403):
        print("       -> the key is wrong, revoked, or belongs to a different region,")
        print("          OR the resource requires its custom endpoint instead of a region.")
        print("          Check: portal -> your resource -> Keys and Endpoint. The region")
        print("          shown there must match AZURE_SPEECH_REGION exactly.")
    elif response.status_code == 429:
        print("       -> rate limited / quota exhausted on this resource.")
    return False


def check_reachability(region: str) -> bool:
    """
    check_reachability(region)
    Usage: `check_reachability("eastus")` -> True when a TLS connection
    to the speech translation host succeeds.

    Worth its own step because the token request above uses ordinary
    HTTPS, while translation runs over a WebSocket to a DIFFERENT host.
    Corporate proxies, some VPNs, and a few consumer security suites
    allow the first and silently block the second — which presents
    exactly as "credentials fine, app still offline".
    """
    section("3. Is the translation WebSocket host reachable?")

    # Both hosts, because which one the SDK uses depends on its
    # version: current builds route translation through the universal
    # v2 endpoint on {region}.stt.speech.microsoft.com, older ones used
    # the dedicated speech-to-speech host. Reporting a failure after
    # probing only one would be wrong half the time.
    reachable = False
    for host in (f"{region}.stt.speech.microsoft.com", f"{region}.s2s.speech.microsoft.com"):
        print(f"       TLS connect to {host}:443")
        try:
            context = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=10) as sock:
                with context.wrap_socket(sock, server_hostname=host) as tls:
                    issuer = dict(x[0] for x in tls.getpeercert().get("issuer", [])).get("organizationName", "?")
            print(f"{OK} connected — certificate issued by {issuer}")
            reachable = True
            if "Microsoft" not in issuer and "DigiCert" not in issuer:
                print(f"{WARN} that issuer is not Microsoft's — something is intercepting TLS")
                print("       -> a proxy or antivirus doing HTTPS inspection breaks the Speech SDK")
        except Exception as exc:  # noqa: BLE001 - reporting tool
            print(f"{BAD} could not open a TLS connection — {exc!r}")

    if not reachable:
        print("       -> the WebSocket the online engine needs is blocked")
    return reachable


def _silent_pcm(seconds: float = 3.0, sample_rate: int = 16000) -> bytes:
    """
    _silent_pcm(seconds, sample_rate)
    Usage: internal — returns near-silent 16 kHz mono 16-bit PCM to push
    at the recognizer. The CONTENT is irrelevant: nothing here is
    checking transcription quality, only whether Azure opens a session
    when audio starts flowing. Not literally zeroed, because a stream
    of pure zeros is a case some audio paths special-case; a tiny
    dither is more representative of a real microphone in a quiet room.
    """
    import numpy as np

    samples = np.random.default_rng(0).integers(-8, 9, size=int(seconds * sample_rate), dtype="int16")
    return samples.tobytes()


def check_live_session(key: str, region: str, endpoint: str, from_lang: str, to_lang: str, log_file: str) -> bool:
    """
    check_live_session(key, region, endpoint, from_lang, to_lang, log_file)
    Usage: the real test — builds the same SpeechTranslationConfig
    azure_engine.py builds, pushes generated audio through the same
    kind of PushAudioInputStream, and waits for Azure to either start
    the session or cancel it.

    Waits on the Connection object's `connected` event, NOT on
    `session_started`. That distinction is the whole point: the SDK
    fires session_started locally as soon as a recognition session
    begins, before any traffic reaches Azure, so waiting on it reports
    success in about 0.00 seconds against a completely dead endpoint.
    An earlier version of this script did exactly that and passed while
    the app was still failing. Connection.open() plus the `connected`
    event is a transport-level fact.
    """
    section("4. Live session against Azure")

    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError as exc:
        print(f"{BAD} Speech SDK not importable — {exc}")
        return False

    from languages import to_azure_speech_locale, to_azure_target

    if log_file:
        os.environ["SPEECHSDK_LOG_FILENAME"] = str(Path(log_file).resolve())
        print(f"       SDK trace log -> {Path(log_file).resolve()}")

    if endpoint:
        print(f"       auth mode: endpoint ({endpoint})")
        config = speechsdk.translation.SpeechTranslationConfig(subscription=key, endpoint=endpoint)
    else:
        print(f"       auth mode: region ({region})")
        config = speechsdk.translation.SpeechTranslationConfig(subscription=key, region=region)

    config.speech_recognition_language = to_azure_speech_locale(from_lang)
    target_code = to_azure_target(to_lang)
    config.add_target_language(target_code)
    print(f"       {from_lang} ({config.speech_recognition_language}) -> {to_lang} ({target_code})")

    stream_format = speechsdk.audio.AudioStreamFormat(samples_per_second=16000, bits_per_sample=16, channels=1)
    push_stream = speechsdk.audio.PushAudioInputStream(stream_format=stream_format)
    recognizer = speechsdk.translation.TranslationRecognizer(
        translation_config=config,
        audio_config=speechsdk.audio.AudioConfig(stream=push_stream),
    )

    settled = threading.Event()
    outcome = {"connected": False, "reason": None, "details": None, "code": None}

    def _on_connected(evt) -> None:
        """The WebSocket to Azure is genuinely open — real proof, unlike session_started."""
        outcome["connected"] = True
        settled.set()

    def _on_canceled(evt) -> None:
        """Azure refused or dropped the connection — the detail we're here for."""
        if evt.reason == speechsdk.CancellationReason.EndOfStream:
            return
        outcome["reason"] = str(evt.reason)
        outcome["details"] = evt.error_details
        outcome["code"] = str(getattr(evt, "error_code", ""))
        settled.set()

    recognizer.canceled.connect(_on_canceled)
    connection = speechsdk.Connection.from_recognizer(recognizer)
    connection.connected.connect(_on_connected)

    started_at = time.monotonic()
    connection.open(True)
    recognizer.start_continuous_recognition()
    push_stream.write(_silent_pcm())
    settled.wait(CONNECT_TIMEOUT_SECONDS)
    elapsed = time.monotonic() - started_at

    for teardown in (recognizer.stop_continuous_recognition, connection.close, push_stream.close):
        try:
            teardown()
        except Exception:  # noqa: BLE001 - teardown of an already-dead session
            pass

    if outcome["connected"]:
        print(f"{OK} connection confirmed by Azure in {elapsed:.2f}s — the online engine works")
        print("       -> if the app still drops to offline, the cause is downstream:")
        print("          plan entitlement in start_session(), or the .env path in step 0")
        return True

    if outcome["reason"] is None:
        print(f"{BAD} no response in {CONNECT_TIMEOUT_SECONDS:.0f}s — no session, no cancellation")
        print("       -> the connection is being swallowed, not refused. Proxy or VPN.")
        return False

    print(f"{BAD} Azure canceled the session after {elapsed:.2f}s")
    print(f"       reason  : {outcome['reason']}")
    print(f"       code    : {outcome['code']}")
    print(f"       details : {outcome['details']}")
    lowered = (outcome["details"] or "").lower()
    if "401" in lowered or "authentication" in lowered or "forbidden" in lowered:
        print("       -> credentials rejected. If the key is definitely current, the")
        print("          resource wants endpoint-based auth: set AZURE_SPEECH_ENDPOINT")
        print("          to the endpoint on its Keys and Endpoint page and re-run.")
    elif "1006" in lowered or "connection" in lowered or "timeout" in lowered:
        print("       -> network path to Azure is blocked or unstable, not a key problem.")
    return False


def main() -> None:
    """
    main()
    Usage: `python scripts/check_azure_live.py` — runs every check in
    order and prints a verdict. Each step is independent, so a failure
    early on still lets the later ones report.
    """
    parser = argparse.ArgumentParser(description="Test whether the online engine can actually reach Azure.")
    parser.add_argument("--from", dest="from_lang", default="English", help="source language display name")
    parser.add_argument("--to", dest="to_lang", default="Persian (Farsi)", help="target language display name")
    parser.add_argument("--log", dest="log_file", default="", help="write an SDK trace log to this file")
    args = parser.parse_args()

    print("MithraVoice — live Azure check")
    print(f"Python {sys.version.split()[0]}  |  project root {PROJECT_ROOT}")

    check_env_discovery()
    key, region, endpoint = check_credentials()
    if not key:
        print("\nVerdict\n-------")
        print("No key resolved, so nothing else can be tested. Fix step 0 first.")
        return

    token_ok = check_token(key, region) if region else False
    reach_ok = check_reachability(region) if region else False
    live_ok = check_live_session(key, region, endpoint, args.from_lang, args.to_lang, args.log_file)

    section("Verdict")
    if live_ok:
        print("Azure is reachable and accepting this key.")
        print("The online engine should run. If the badge still goes grey on Resume,")
        print("run `python main.py` from a terminal and read the [pipeline] line —")
        print("it names which check failed inside the app.")
    elif not reach_ok:
        print("The network is the problem, not the credentials.")
        print("The translation WebSocket host is unreachable from this machine.")
        print("Check VPN, corporate proxy, and any antivirus doing HTTPS inspection.")
    elif not token_ok:
        print("Azure is reachable but rejecting this key/region pair.")
        print("Confirm both on the resource's Keys and Endpoint page in the portal.")
        print("If the key is 84 characters, the resource likely needs endpoint auth.")
    else:
        print("The key authenticates over REST but the translation session still fails.")
        print("Re-run with --log azure_sdk.log and read the tail of that file.")


if __name__ == "__main__":
    main()
