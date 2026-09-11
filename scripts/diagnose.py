"""
scripts/diagnose.py

Answers one question: why isn't the app translating?

The three symptoms — no captions, the badge stuck on "offline", and a
blank online-hours pill — all have the same shape and different causes,
which is what makes them hard to tell apart from inside the UI. They are
all downstream of an engine failing to START:

  * Azure can't start (no credentials / no network / bad region)
      -> pipeline.py falls back to the offline engine, so the badge
         reads "offline"
      -> the metered online engine never runs, so there is no usage to
         report and the remaining-time pill stays empty
  * the offline engine then can't start either (no Whisper model, no
    Argos package for the pair)
      -> nothing transcribes, so no captions

So a blank pill plus an "offline" badge is not two bugs, it's one:
Azure didn't start. This script reports on each link in that chain
separately so you can see which one is actually broken, instead of
inferring it from the UI.

Read-only. Changes nothing, needs no license. The only network call it
makes is one Azure token request to check credentials — see
check_online_credentials() for why a per-pair Azure call isn't done.

Usage:
    python scripts/diagnose.py
    python scripts/diagnose.py --skip-online   (no network at all)
"""

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OK = "  [ ok ]"
BAD = "  [FAIL]"
WARN = "  [warn]"


def section(title: str) -> None:
    """
    section(title)
    Usage: internal — prints a heading so the report reads as distinct
    checks rather than one wall of text.
    """
    print(f"\n{title}\n" + "-" * len(title))


def check_imports() -> dict:
    """
    check_imports()
    Usage: `check_imports()` -> {"azure": bool, "whisper": bool, "argos": bool}.
    Reports which engine dependencies are actually installed. A missing
    package here explains an engine failing to start before any config
    or model file is even looked at, so it runs first.
    """
    results = {}
    for label, module in (
        ("azure", "azure.cognitiveservices.speech"),
        ("whisper", "faster_whisper"),
        ("argos", "argostranslate.translate"),
    ):
        try:
            __import__(module)
            results[label] = True
            print(f"{OK} {module}")
        except Exception as exc:  # noqa: BLE001 - reporting tool; any import problem is a finding
            results[label] = False
            print(f"{BAD} {module} — {exc}")
    if not all(results.values()):
        print("       -> pip install -r requirements.txt")
    return results


def check_azure() -> bool:
    """
    check_azure()
    Usage: `check_azure()` -> True when a key and region are both set.
    Does NOT contact Azure — it only reports whether config.py found
    credentials, since an empty AZURE_SPEECH_KEY is by far the most
    common reason the online engine never starts. The key is masked;
    only its length is shown, which is enough to spot an empty or
    truncated value without printing a secret to a terminal.
    """
    from config import settings

    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        print(f"{OK} .env found at {env_file}")
    else:
        print(f"{BAD} no .env file at {env_file}")
        print(f"       -> copy .env.example to .env and fill in AZURE_SPEECH_KEY / AZURE_SPEECH_REGION")

    key, region = settings.azure_speech_key, settings.azure_speech_region
    print(f"       AZURE_SPEECH_KEY:    {'set (' + str(len(key)) + ' chars)' if key else 'EMPTY'}")
    print(f"       AZURE_SPEECH_REGION: {region or 'EMPTY'}")

    if settings.azure_configured:
        print(f"{OK} online engine is configured")
        return True
    print(f"{BAD} online engine CANNOT start — the badge will read 'offline' and")
    print(f"       the remaining-time pill will stay empty, because no metered")
    print(f"       online session ever runs to produce usage to report.")
    return False


def check_whisper_model() -> bool:
    """
    check_whisper_model()
    Usage: `check_whisper_model()` -> True when the offline engine has a
    speech model available. A bundled directory is preferred; without
    one, faster_whisper downloads by size on first use, which works
    during development but needs network and is not what ships.
    """
    from config import settings

    path = Path(settings.whisper_model_path)
    if path.exists():
        print(f"{OK} bundled Whisper model at {path}")
        return True
    print(f"{WARN} no bundled Whisper model at {path}")
    print(f"       faster_whisper will try to DOWNLOAD '{settings.whisper_model_size}' on first use.")
    print(f"       That needs network. With no network the offline engine cannot start.")
    print(f"       -> python scripts/prepare_offline_assets.py")
    return False


def _expected_hop_pairs() -> set:
    """
    _expected_hop_pairs()
    Usage: internal — the set of "from-to" hops the offline engine
    needs, i.e. every non-English language in languages.py paired with
    English both ways. Reads scripts/prepare_offline_assets.py's own
    hub_pairs() when that function exists, but does NOT hard-fail if it
    doesn't: an older prepare_offline_assets.py (pre-3.9.1, before
    hub_pairs() was added) is exactly the kind of mismatch this script
    exists to catch, and a diagnostic tool crashing on outdated *other*
    files defeats the point. Falls back to computing the same set
    directly from languages.py.
    """
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "prep", PROJECT_ROOT / "scripts" / "prepare_offline_assets.py"
        )
        prep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prep)
        return set(prep.hub_pairs())
    except (AttributeError, Exception):  # noqa: BLE001 - reporting tool; any problem here falls back below
        print(f"{WARN} scripts/prepare_offline_assets.py has no hub_pairs() — it looks like an")
        print(f"       older version. Computing the expected pair list from languages.py instead.")
        from languages import PIVOT_ISO, all_iso_codes

        codes = [c for c in all_iso_codes() if c != PIVOT_ISO]
        return {f"{PIVOT_ISO}-{c}" for c in codes} | {f"{c}-{PIVOT_ISO}" for c in codes}


def check_argos_packages() -> set:
    """
    check_argos_packages()
    Usage: `check_argos_packages()` -> the set of "from-to" hops that are
    usable offline, counting both bundled .argosmodel files and packages
    already installed into the local Argos environment. Printed as a
    comparison against what prepare_offline_assets.py is supposed to
    have produced, so a partial or never-run prepare step is obvious.
    """
    from config import settings

    expected = _expected_hop_pairs()

    available = set()

    packages_dir = Path(settings.argos_packages_dir)
    if packages_dir.exists():
        bundled = {f.stem.replace("_", "-") for f in packages_dir.glob("*.argosmodel")}
        available |= bundled
        print(f"{OK} argos package dir: {packages_dir} ({len(bundled)} file(s))")
    else:
        print(f"{BAD} no argos package dir at {packages_dir}")

    try:
        import argostranslate.translate

        installed = argostranslate.translate.get_installed_languages()
        pairs = {
            f"{a.code}-{b.code}"
            for a in installed
            for b in installed
            if a.code != b.code and a.get_translation(b) is not None
        }
        available |= pairs
        if pairs:
            print(f"{OK} {len(pairs)} pair(s) already installed in the Argos environment")
    except Exception as exc:  # noqa: BLE001 - reporting tool
        print(f"{WARN} could not read installed Argos languages — {exc}")

    missing = sorted(expected - available)
    if missing:
        print(f"{BAD} missing {len(missing)}/{len(expected)} required hop(s): {', '.join(missing)}")
        print(f"       -> python scripts/prepare_offline_assets.py")
    else:
        print(f"{OK} all {len(expected)} required hops present")
    return available


def _all_ordered_pairs() -> list:
    """
    _all_ordered_pairs()
    Usage: internal — every (from_display, to_display) combination from
    languages.py, in a fixed order (LANGUAGES' own key order), so the
    online and offline matrices below list pairs identically and can be
    compared line-for-line.
    """
    import itertools

    from languages import LANGUAGES

    return list(itertools.permutations(LANGUAGES.keys(), 2))


def _pair_label(from_display: str, to_display: str) -> str:
    """
    _pair_label(from_display, to_display)
    Usage: internal — "EN -> FA" style label for a matrix row. Uses the
    ISO code rather than the full display name so 72 rows stay scannable
    ("EN -> ZH" not "English -> Mandarin Chinese").
    """
    from languages import to_iso639

    return f"{to_iso639(from_display).upper()} -> {to_iso639(to_display).upper()}"


def check_online_credentials(timeout: float = 8.0) -> bool:
    """
    check_online_credentials(timeout=8.0)
    Usage: `check_online_credentials()` -> True when Azure accepts the
    key/region. Makes exactly ONE network call — to the STS token
    endpoint, which every Azure Speech key can reach regardless of which
    language it's used for — rather than one call per language pair.

    A real per-pair check would mean opening 72 live speech-translation
    sessions, each needing several seconds of synthesized audio to
    produce a result, each burning quota. That's a load test, not a
    diagnostic, and it would make this script slow, costly, and the
    thing you'd hesitate to re-run. Since Azure's translation quality
    and target-locale support don't vary by which key is calling — only
    the key's validity and region do — one credential check covers
    all 72 pairs at once. What actually differs pair-to-pair is only the
    locale/target CODE MAPPING in languages.py, which the matrix below
    verifies for every pair with no network needed.
    """
    from config import settings

    if not settings.azure_configured:
        print(f"{WARN} skipping — no Azure credentials configured (see section 2)")
        return False

    url = f"https://{settings.azure_speech_region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
    req = urllib.request.Request(
        url, method="POST", headers={"Ocp-Apim-Subscription-Key": settings.azure_speech_key}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                print(f"{OK} Azure accepted the key for region '{settings.azure_speech_region}'")
                return True
            print(f"{BAD} Azure token endpoint returned HTTP {resp.status}")
            return False
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print(f"{BAD} Azure rejected the key (401) — AZURE_SPEECH_KEY is wrong or disabled")
        elif exc.code == 403:
            print(f"{BAD} Azure rejected the key (403) — check the key is for region '{settings.azure_speech_region}'")
        else:
            print(f"{BAD} Azure token endpoint returned HTTP {exc.code}")
        return False
    except urllib.error.URLError as exc:
        print(f"{BAD} could not reach Azure ({exc.reason}) — check network / region name")
        return False


def check_online_matrix(credentials_ok: bool) -> None:
    """
    check_online_matrix(credentials_ok)
    Usage: prints every EN<->X and X<->Y pair the online engine would be
    asked to handle. Each row validates that languages.py has a working
    Azure speech-locale + target-code mapping for that pair — the thing
    that's actually per-pair; see check_online_credentials() for why the
    live network call is done once, not 72 times.
    """
    from languages import to_azure_speech_locale, to_azure_target

    if not credentials_ok:
        print(f"{WARN} credential check above did not pass — pairs below show code-mapping only,")
        print(f"       not proof Azure will accept them.\n")

    pairs = _all_ordered_pairs()
    failures = 0
    for from_display, to_display in pairs:
        try:
            to_azure_speech_locale(from_display)
            to_azure_target(to_display)
        except Exception as exc:  # noqa: BLE001 - reporting tool
            failures += 1
            print(f"{BAD} {_pair_label(from_display, to_display)}  {exc}")
            continue
        if credentials_ok:
            print(f"{OK} {_pair_label(from_display, to_display)}")
    if failures == 0 and credentials_ok:
        print(f"\n       all {len(pairs)} pair(s) have valid Azure locale/target codes")
    elif failures:
        print(f"\n       {len(pairs) - failures}/{len(pairs)} pair(s) have valid Azure codes")


def check_offline_matrix(available: set) -> bool:
    """
    check_offline_matrix(available)
    Usage: resolves the offline translation route for EVERY pair in
    languages.py using ONLY the packages already on disk, with the
    network download path disabled. That's the honest test: it tells you
    whether the shipped app would work on a machine with no internet,
    rather than quietly succeeding because this machine happens to be
    online. Returns True iff every pair resolved.
    """
    from engines.offline_engine import OfflineEngine
    from languages import to_iso639

    engine = OfflineEngine()
    if not hasattr(engine, "_resolve_route"):
        # Version mismatch, not a missing package: see the long comment
        # this replaced for the full story. Section 4 above can report
        # every package present and this will still fail, because the
        # code that would USE those packages for a pivot route doesn't
        # exist yet on disk.
        print(f"{BAD} engines/offline_engine.py is an OLDER version — it has no _resolve_route().")
        print(f"       Every other check can pass while this one fails; it means the packages")
        print(f"       are all there but the code to route through them for non-English pairs")
        print(f"       isn't. Replace engines/offline_engine.py with the current version.")
        return False
    engine._installed_pair_exists = lambda a, b: f"{a}-{b}" in available
    engine._install_bundled_hop = lambda a, b: f"{a}-{b}" in available
    engine._install_downloaded_hop = lambda a, b: False  # simulate a machine with no network

    pairs = _all_ordered_pairs()
    failures = 0
    for from_display, to_display in pairs:
        a, b = to_iso639(from_display), to_iso639(to_display)
        try:
            route = engine._resolve_route(a, b)
            label = "direct" if len(route) == 2 else "via " + route[1]
            print(f"{OK} {_pair_label(from_display, to_display)}  ({label})")
        except Exception as exc:  # noqa: BLE001 - reporting tool
            failures += 1
            print(f"{BAD} {_pair_label(from_display, to_display)}  {exc}")
    print(f"\n       {len(pairs) - failures}/{len(pairs)} pair(s) translatable with no network")
    return failures == 0


def main() -> None:
    """
    main()
    Usage: `python scripts/diagnose.py`. Runs every check in the order
    the app itself would hit them and prints a verdict naming the single
    thing to fix first, because fixing the first broken link usually
    clears the other symptoms with it.
    """
    parser = argparse.ArgumentParser(description="Diagnose why MithraVoice isn't translating.")
    parser.add_argument(
        "--skip-online",
        action="store_true",
        help="skip the Azure credential check (no network call at all, e.g. for CI or an offline machine)",
    )
    args = parser.parse_args()

    version = (PROJECT_ROOT / "VERSION").read_text().strip() if (PROJECT_ROOT / "VERSION").exists() else "?"
    print(f"MithraVoice {version} — diagnostics")
    print(f"Python {sys.version.split()[0]}  |  project root {PROJECT_ROOT}")

    section("1. Engine dependencies")
    imports = check_imports()

    section("2. Online engine (Azure)")
    azure_ok = check_azure()

    section("3. Offline engine — speech model")
    whisper_ok = check_whisper_model()

    section("4. Offline engine — translation packages")
    available = check_argos_packages()

    credentials_ok = False
    if azure_ok and not args.skip_online:
        section("5. Online translation matrix (Azure)")
        credentials_ok = check_online_credentials()
        print()
        check_online_matrix(credentials_ok)
    elif args.skip_online:
        section("5. Online translation matrix (Azure)")
        print(f"{WARN} skipped (--skip-online)")

    section("6. Offline translation matrix (Argos, no network)")
    try:
        routes_ok = check_offline_matrix(available)
    except Exception as exc:  # noqa: BLE001 - reporting tool
        print(f"{BAD} could not resolve routes — {exc}")
        routes_ok = False

    section("Verdict")
    if not all(imports.values()):
        print("Dependencies are missing. Fix that first:")
        print("    pip install -r requirements.txt")
    elif not azure_ok and not (whisper_ok and routes_ok):
        print("NEITHER engine can start — this is the no-captions case.")
        print("Azure has no credentials, and the offline engine has no usable")
        print("model/packages, so there is nothing left to fall back to.")
        print("    1) copy .env.example to .env and fill in your Azure key + region")
        print("    2) python scripts/prepare_offline_assets.py")
    elif not azure_ok:
        print("Only the OFFLINE engine can start.")
        print("That alone explains the 'offline' badge and the empty remaining-time")
        print("pill — both are correct behaviour when no online session ever runs.")
        print("    -> copy .env.example to .env and fill in AZURE_SPEECH_KEY / AZURE_SPEECH_REGION")
    elif not (whisper_ok and routes_ok):
        print("Online works, but there is no offline fallback.")
        print("Translation will stop rather than degrade if the network drops.")
        print("    -> python scripts/prepare_offline_assets.py")
    else:
        print("Both engines look startable. If captions still don't appear, run")
        print("`python main.py` from a terminal and watch for [pipeline] lines —")
        print("they name the engine that failed and why.")


if __name__ == "__main__":
    main()
