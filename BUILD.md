# Building MithraVoice_version1.0.0_installer.exe

**This must be run on a Windows machine** (or a Windows CI runner, e.g.
GitHub Actions `windows-latest`). PyInstaller only builds for the OS
it's running on, and Inno Setup's compiler is Windows-only — there's no
way to cross-build a Windows `.exe` from Linux/macOS here.

## 1. Pre-download offline assets (once, needs internet)

```
python scripts\prepare_offline_assets.py
```

Creates `models\whisper-small\` and `models\argos\*.argosmodel`. Do this
on any machine with internet — the output gets bundled into the
installer, so the *end user's* machine needs no internet for the
offline engine to work.

## 2. Get the license public key in place

Copy `keys\mithravoice_public_key.pem` from your license server (see
`server/DEPLOY.md` step 4 — it's generated there, never on a client
machine). The demo key already in this repo works for testing but must
be replaced with the real server-generated one before shipping.

## 3. Install build dependencies

```
pip install -r requirements.txt
pip install pyinstaller
```

## 4. Build the .exe with PyInstaller

```
pyinstaller build.spec
```

Output: `dist\MithraVoice\MithraVoice.exe` — that whole `dist\MithraVoice`
folder is the app; it needs nothing else installed on the target
machine (Python, the whisper model, the Argos packages, and the Azure
SDK's native DLL are all inside it).

## 5. Install Inno Setup (one-time, on the build machine)

Download and install from https://jrsoftware.org/isinfo.php (free).

## 6. Compile the installer

From the project root, either:

```
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

or open `installer.iss` in the Inno Setup Compiler GUI and click
**Compile**.

Output:

```
installer_output\MithraVoice_version1.0.0_installer.exe
```

That's the single file you hand to customers — a double-click
installer with a Start Menu shortcut, desktop icon option, and
uninstaller, containing everything (app, offline models, license
public key) needed to run with zero additional downloads.

## 7. Test it exactly as a user would

Copy `MithraVoice_version1.0.0_installer.exe` to a clean Windows VM
with **no internet** and **no Python installed**, run it, and confirm:
- the installer completes and creates the Start Menu/desktop shortcuts
- launching the app shows the license gate
- activating a real key (needs internet for this one step) succeeds
- after activating once, disconnect the network entirely and relaunch —
  the app should open straight into Live Translation using the offline
  engine
- offline translation actually produces text (proves the bundled
  whisper model + Argos packages loaded correctly, not just that the
  exe launched)
- uninstalling via Control Panel removes the app and the cached license
  token cleanly

## One-command version (for repeat builds)

Once steps 1–2 and 5 are done once, subsequent builds are just:

```
pyinstaller build.spec && "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

## Bumping the version

Edit the `VERSION` file (one line, e.g. `2.0.1`) — that's the only place
you need to change it. Both `build.spec` and `installer.iss` read from
this same file, so:

- `pyinstaller build.spec` outputs to `dist\MithraVoice2.0.1\` (old
  version folders like `dist\MithraVoice1.0.0\` are left untouched —
  you'll only be asked to overwrite if you rebuild the exact same
  version twice)
- `installer.iss` automatically picks up that same folder as its
  source and names its output `MithraVoice_2.0.1.exe`

Nothing else needs editing for a version bump.

## Troubleshooting

- **"Module not found" only in the built .exe, not `python main.py`**
  Almost always a PyInstaller detection gap. `build.spec` already runs
  `collect_all("azure.cognitiveservices.speech")` for the Speech SDK's
  native DLL; if a different package hits this, add it the same way.
- **Offline engine errors on a clean machine but worked on your dev box**
  Check `models\whisper-small\` and `models\argos\` actually exist
  *before* running `pyinstaller build.spec` — step 1 must run first,
  and its output must be present at build time, not just at dev time.
- **App can't reach the license server**
  Confirm `config.py`'s `license_server_url` default
  (`https://key.mithravoice.mithracorp.com`) matches your actual
  server, and that its TLS cert is valid — `requests` refuses a
  self-signed cert by default.
- **ISCC.exe not found**
  Default install path is `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`;
  add it to your PATH, or use the full path shown above.
