"""
scripts/prepare_offline_assets.py

Run this ONCE, on a machine with internet access, before building the
Windows installer. It downloads:
  - the faster-whisper model (ctranslate2 format) into models/whisper-small/
  - Argos Translate language packages for every language in languages.py,
    paired with English in both directions, into models/argos/

Both get picked up automatically by config.py (whisper_model_path,
argos_packages_dir) and bundled into the .exe by build.spec, so the
installed app never needs to contact Hugging Face or the Argos package
index at runtime — see engines/offline_engine.py's bundled-first logic.

Usage:
    python scripts/prepare_offline_assets.py
    python scripts/prepare_offline_assets.py --model medium
    python scripts/prepare_offline_assets.py --include-direct
    python scripts/prepare_offline_assets.py --pairs en-fa fa-en
"""

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))  # import languages.py from the project root, not the scripts dir

from languages import PIVOT_ISO, all_iso_codes  # noqa: E402 - needs the sys.path line above
WHISPER_DIR = PROJECT_ROOT / "models" / "whisper-small"
ARGOS_DIR = PROJECT_ROOT / "models" / "argos"

def hub_pairs() -> list:
    """
    hub_pairs()
    Usage: `hub_pairs()` -> ["en-fa", "fa-en", "en-es", ...]. Every
    non-English language in languages.py paired with English in BOTH
    directions. Derived from LANGUAGES rather than hand-listed, because
    the previous hand-maintained list silently drifted: Mandarin and
    Turkish were added to the UI dropdown and never added here, so both
    had zero offline coverage in any direction.

    These 2N packages are all the offline engine strictly needs. Argos
    publishes a hub-and-spoke set around English, and
    engines/offline_engine.py pivots X -> en -> Y for any pair with no
    direct package, so this list alone covers all N*(N-1) combinations.
    """
    others = [code for code in all_iso_codes() if code != PIVOT_ISO]
    pairs = []
    for code in others:
        pairs.append(f"{PIVOT_ISO}-{code}")
        pairs.append(f"{code}-{PIVOT_ISO}")
    return pairs


def direct_pairs() -> list:
    """
    direct_pairs()
    Usage: `direct_pairs()` -> every non-English ordered combination,
    e.g. "fr-es", "tr-zh". Only downloaded with --include-direct.
    Optional: these pairs already work by pivoting through English, but a
    direct package is one translation hop instead of two, so it's both
    faster and better quality where Argos happens to publish one. Most
    do not exist and are skipped with a printed note.
    """
    others = [code for code in all_iso_codes() if code != PIVOT_ISO]
    return [f"{a}-{b}" for a in others for b in others if a != b]


def download_whisper_model(model_size: str, target_dir: Path) -> None:
    """
    download_whisper_model(model_size, target_dir)
    Usage: internal — uses faster-whisper's own downloader (backed by
    huggingface_hub) to fetch the ctranslate2-format model files once,
    then copies them into target_dir so config.py's whisper_model_path
    can load them with local_files_only-style behavior (passing a real
    directory path to WhisperModel(...) never hits the network).
    """
    from faster_whisper.utils import download_model

    print(f"Downloading whisper '{model_size}' model...")
    cached_dir = download_model(model_size)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(cached_dir, target_dir)
    print(f"  -> {target_dir}")


def download_argos_pairs(pairs: list, target_dir: Path) -> None:
    """
    download_argos_pairs(pairs, target_dir)
    Usage: internal — for each "from-to" string in pairs (e.g. "en-fa"),
    downloads the matching .argosmodel package file from the Argos
    package index and saves it into target_dir WITHOUT installing it
    into the local Argos environment — offline_engine.py installs from
    these bundled files at runtime on the end user's machine instead.
    """
    import argostranslate.package

    target_dir.mkdir(parents=True, exist_ok=True)
    print("Updating Argos Translate package index...")
    argostranslate.package.update_package_index()
    available = argostranslate.package.get_available_packages()

    for pair in pairs:
        from_code, to_code = pair.split("-")
        match = next((p for p in available if p.from_code == from_code and p.to_code == to_code), None)
        if match is None:
            print(f"  ! no package found for {pair}, skipping")
            continue
        downloaded_path = Path(match.download())
        dest = target_dir / f"{from_code}_{to_code}.argosmodel"
        shutil.copy(downloaded_path, dest)
        print(f"  -> {dest}")


def main() -> None:
    """
    main()
    Usage: run directly, see module docstring. After this completes,
    models/whisper-small/ and models/argos/*.argosmodel exist locally
    and build.spec's `datas` entries will bundle them into the .exe.
    """
    parser = argparse.ArgumentParser(description="Pre-download offline assets for a fully offline-capable build")
    parser.add_argument("--model", default="small", help="faster-whisper model size (tiny/base/small/medium/large-v3)")
    parser.add_argument(
        "--pairs",
        nargs="*",
        default=None,
        help="explicit language pairs as from-to, e.g. en-fa. Defaults to every language in languages.py paired with English both ways.",
    )
    parser.add_argument(
        "--include-direct",
        action="store_true",
        help="also try to download direct non-English pairs (fr-es, tr-zh, ...). Optional — these already work via the English pivot.",
    )
    args = parser.parse_args()

    pairs = args.pairs if args.pairs is not None else hub_pairs()
    if args.include_direct:
        pairs = list(dict.fromkeys(pairs + direct_pairs()))

    whisper_target = PROJECT_ROOT / "models" / f"whisper-{args.model}"
    download_whisper_model(args.model, whisper_target)
    download_argos_pairs(pairs, ARGOS_DIR)

    print("\nDone. Bundled assets:")
    print(f"  {whisper_target}")
    print(f"  {ARGOS_DIR} ({len(list(ARGOS_DIR.glob('*.argosmodel')))} language pairs)")
    print("\nIf you used a model size other than 'small', update WHISPER_MODEL_PATH")
    print("in config.py (or the .env) and build.spec's datas entry to match.")


if __name__ == "__main__":
    main()
