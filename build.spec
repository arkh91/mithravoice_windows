# build.spec
#
# PyInstaller spec for MithraVoice (by MithraCorp).
#
# Usage:
#   1. Run scripts/prepare_offline_assets.py first (needs internet once)
#      so models/whisper-small/ and models/argos/*.argosmodel exist.
#   2. Bump the version by editing the VERSION file (one line, e.g. "2.0.1").
#   3. pyinstaller build.spec
#   4. Output: dist/MithraVoice<version>/MithraVoice_<version>.exe — e.g.
#      dist/MithraVoice2.0.1/MithraVoice_2.0.1.exe. The version-named
#      folder means old builds are never silently overwritten by a new
#      version; you'll only get the overwrite prompt again if you
#      rebuild the SAME version twice.
#   5. Run installer.iss through Inno Setup to get
#      MithraVoice_Setup_<version>.exe — it reads the same VERSION
#      file, so there's nothing to keep in sync by hand. THIS is the
#      real installer to hand to users, not the .exe from step 3/4
#      above (that's just the app itself, no installer wizard). See
#      BUILD.md.
#
# Why --collect-all for azure.cognitiveservices.speech: the Speech SDK
# ships a native DLL (Microsoft.CognitiveServices.Speech.core.dll) that
# PyInstaller's default import analysis doesn't detect, since it's
# loaded via ctypes at runtime rather than a normal Python import. Using
# collect_all() here pulls in every data file, binary, and hidden import
# the package declares, which is the fix for the classic "works with
# `python main.py` but the .exe silently fails to recognize speech"
# symptom.

from PyInstaller.utils.hooks import collect_all

block_cipher = None

with open("VERSION") as f:
    APP_VERSION = f.read().strip()

# The dist FOLDER is named MithraVoice<version> (no underscore, matches
# the folder-per-version scheme), but the .exe FILE inside it is named
# MithraVoice_<version>.exe (with underscore) — e.g.
# dist/MithraVoice1.0.0/MithraVoice_1.0.0.exe. PyInstaller appends .exe
# automatically on Windows, so EXE_NAME below has no extension.
DIST_FOLDER_NAME = f"MithraVoice{APP_VERSION}"
EXE_NAME = f"MithraVoice_{APP_VERSION}"

azure_datas, azure_binaries, azure_hiddenimports = collect_all("azure.cognitiveservices.speech")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=azure_binaries,
    datas=[
        ("index.html", "."),
        ("translation.html", "."),  # full-screen captions window — see api.py's open_translation_window
        ("assets", "assets"),  # logo and other static images referenced by index.html
        ("VERSION", "."),  # so the running app can read its own version (see updater.py) — build-time-only otherwise
        ("models/whisper-small", "models/whisper-small"),
        ("models/argos", "models/argos"),
        ("keys/mithravoice_public_key.pem", "keys"),
        ("assets/mithracorp_logo.ico", "assets"),  # window/taskbar icon, set at runtime via webview.start(icon=...) in main.py
    ] + azure_datas,
    hiddenimports=[
        "engines.azure_engine",
        "engines.offline_engine",
        # pywin32's modules are C extensions PyInstaller's static
        # analysis often misses (same class of issue as the Azure SDK
        # above) — win32timezone in particular is a common silent
        # failure point for pywin32 builds if omitted.
        "win32gui",
        "win32ui",
        "win32con",
        "win32timezone",
    ] + azure_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # --windowed: no console window behind the app
    icon="assets/mithracorp_logo.ico",  # .exe file icon (Explorer, taskbar pin, Alt-Tab) — separate from the runtime window icon set in main.py's webview.start(icon=...)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=DIST_FOLDER_NAME,
)


