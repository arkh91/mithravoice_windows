; installer.iss
;
; Inno Setup script for MithraVoice. Reads the version from the VERSION
; file (same one build.spec reads) so there's a single source of truth —
; bump VERSION once, and both the dist\ folder name and this installer's
; output filename update automatically. Produces:
;   MithraVoice_Setup_<version>.exe   e.g. MithraVoice_Setup_2.0.2.exe
;
; IMPORTANT — don't confuse this with the app's own .exe:
;   dist\MithraVoice2.0.2\MithraVoice_2.0.2.exe   <- the app itself,
;     produced by PyInstaller. Running this directly launches the app
;     in place; it does NOT install anything or show a setup wizard.
;   installer_output\MithraVoice_Setup_2.0.2.exe  <- THIS is the real
;     installer, produced by compiling this .iss file. This is the one
;     to hand to users — it shows the setup wizard and registers the
;     app in Windows' Programs and Features.
;
; Prerequisites (run first, in order):
;   1. python scripts\prepare_offline_assets.py
;   2. Edit VERSION if you're bumping the version (one line, e.g. "2.0.1")
;   3. pyinstaller build.spec         -> dist\MithraVoice<version>\MithraVoice_<version>.exe
;   4. Install Inno Setup (https://jrsoftware.org/isinfo.php)
;   5. Compile this file:
;        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;      or open it in the Inno Setup Compiler GUI and click Compile.
;
; Output lands in .\installer_output\MithraVoice_Setup_<version>.exe

#define MyAppName "MithraVoice"
#define MyAppPublisher "MithraCorp"
#define MyAppURL "https://mithracorp.com"

; Read VERSION into MyAppVersion. Single-line form on purpose — the
; #sub/#expr pattern for this is a common ISPP idiom but has scoping
; quirks that can silently fail to set the define globally with no
; preprocessing error shown, which is exactly what happened here. This
; simpler form has no such ambiguity: FileOpen/FileRead/Trim all
; evaluate inline in one #define, so there's no separate scope for the
; result to get lost in.
#define MyAppVersion Trim(FileRead(FileOpen("VERSION")))

; Matches build.spec's EXE_NAME = f"MithraVoice_{APP_VERSION}"
#define MyAppExeName "MithraVoice_" + MyAppVersion + ".exe"

; Matches build.spec's DIST_FOLDER_NAME = f"MithraVoice{APP_VERSION}"
#define DistFolderName MyAppName + MyAppVersion

[Setup]
AppId={{B7B6C6A1-4F1F-4E4C-9D2E-8C6C8E1E7A11}}
AppName={#MyAppName}
; Without this, Inno's default Programs-and-Features "Name" column
; falls back to "{#MyAppName} {#MyAppVersion}" (e.g. "MithraVoice
; 2.0.3") — pinning AppVerName to just the app name keeps that list
; showing plain "MithraVoice" regardless of what VERSION currently is.
AppVerName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=MithraVoice_Setup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Icon for the installer wizard itself and the Setup_*.exe file.
SetupIconFile=assets\mithracorp_logo.ico
; Icon shown in Programs and Features next to "MithraVoice" — without
; this, Windows falls back to a generic uninstaller icon instead of
; the app's own. Points at the already-installed app .exe (which
; carries the same icon via build.spec's icon= setting), so there's
; nothing extra to keep in sync here.
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Pulls in everything PyInstaller produced for THIS version: MithraVoice.exe,
; its bundled DLLs, index.html, models\, and keys\ — recursesubdirs means
; the offline whisper/Argos assets and the license public key all ship
; inside the installer, so the installed app needs no internet. Source
; path automatically matches whatever version VERSION currently holds.
Source: "dist\{#DistFolderName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Removes the cached license token on uninstall so a clean reinstall
; doesn't silently reuse a stale/expired token. Comment this out if you
; want uninstall to preserve the activation (e.g. for quick reinstalls).
Type: filesandordirs; Name: "{userappdata}\MithraCorp"

