# MithraVoice for Windows

**Real-time speech translation for Windows, with Azure Speech Translation online and a fully local offline engine.**

MithraVoice captures microphone audio, recognizes speech, translates it in real time, and displays the result as live captions. The application is designed to keep translation available when the Internet is unreliable by supporting both an Azure-based online engine and a local offline engine.

## Features

- Real-time speech recognition and translation
- Windows desktop application built with Python + pywebview
- **Azure Speech Translation** for the online engine
- **Whisper + Argos Translate** for the offline engine
- Automatic online/offline engine switching
- Live connectivity status in the UI
- Microphone/device monitoring
- Caption window / overlay support
- Local settings persistence
- Online usage tracking when the Azure engine is active
- Licensing and plan-based engine access

## Translation Engine Modes

MithraVoice provides three engine preferences in **Settings → Translation Engine**.

### 1. Auto — Online preferred

**Recommended mode.**

Auto prefers Azure Speech Translation whenever the online engine is available.

Behavior:

```text
                 ┌─────────────────────┐
                 │      Auto Mode       │
                 └──────────┬──────────┘
                            │
                    Can Azure be reached?
                       /             \
                     YES              NO
                      │                │
                      ▼                ▼
              Azure Online      Offline Engine
                      │                │
                      └───────┬────────┘
                              │
                       Translation runs
```

When the application is already translating online and the Internet/Azure connection is lost, MithraVoice detects the failure and switches to the offline engine without requiring the user to restart the application.

When connectivity returns, Auto mode can attempt to restore Azure automatically. A session intentionally started in **Offline Only** mode is never moved online automatically.

**Important:** Auto mode requires an active plan that permits the corresponding engine(s). If only one engine is available under the current plan, Auto is constrained to that available engine.

### 2. Online Only — Azure only

This mode uses **Azure Speech Translation exclusively**.

- Requires Internet connectivity.
- Does not fall back to the offline engine.
- If Azure becomes unreachable, translation cannot continue until connectivity is restored.
- The application should notify the user that the Internet/Wi-Fi connection needs to be checked.
- Useful when Azure quality is preferred and offline translation is not desired.

```text
Online Only
     │
     ▼
Azure Speech Translation
     │
     ├── Connected → Translate
     │
     └── Connection lost → Notify user / stop online translation
```

### 3. Offline Only — Local engine

This mode uses the local translation stack and does not attempt to connect to Azure for translation.

The offline engine uses:

- **Whisper / faster-whisper** for local speech recognition
- **Argos Translate** for local machine translation
- Bundled offline models/packages for supported language pairs

Offline Only is useful when:

- There is no Internet connection.
- Privacy or local processing is preferred.
- Azure is not available.
- The user does not want online translation usage.

```text
Offline Only
      │
      ▼
Local Whisper + Argos Translate
      │
      ▼
Translation continues without Internet
```

## Engine Behavior Summary

| Setting | Primary Engine | Internet Required | Automatic Offline Fallback | Automatic Return to Azure |
|---|---|---:|---:|---:|
| **Auto** | Azure | Preferred, not required | Yes | Yes, when the fallback was caused by network loss |
| **Online Only** | Azure | Yes | No | N/A |
| **Offline Only** | Local | No | N/A | No |

## Usage Reporting

MithraVoice usage reporting applies **only to the Azure online engine**.

- **Azure / Online engine:** Usage is reported while Azure Speech Translation is actively being used.
- **Offline engine:** No usage reporting is performed. Offline speech recognition and translation do not send usage information to the online usage-reporting service.
- **Auto mode:** Usage reporting follows the engine that is actually running. While Auto is using Azure, online usage reporting applies. If Auto switches to the offline engine because connectivity is lost, online usage reporting stops with the Azure session.
- **Offline Only:** No usage reporting is performed at any time.

This distinction is important: using the local offline engine does **not** consume or report Azure online usage.

| Engine State | Usage Reporting |
|---|---|
| **Azure Online** | Yes |
| **Auto → Azure** | Yes |
| **Auto → Offline** | No |
| **Offline Only** | No |

## Connectivity Detection

MithraVoice does not rely only on the browser's `navigator.onLine` flag to decide whether Azure is usable.

The application actively checks whether the configured Azure Speech endpoint can be reached over TCP/TLS. This helps distinguish between:

- Wi-Fi being connected but having no Internet access
- DNS/routing failures
- Firewalls or proxies blocking Azure
- A genuinely reachable Azure endpoint

The connectivity probe uses a short cache and timeout so that starting a translation session does not hang for a long time when the network is unavailable.

During an active Auto session, the pipeline also performs periodic health checks. A connection failure causes the pipeline to transition through:

```text
Online
  ↓
Connection problem detected
  ↓
Switching engines…
  ↓
Offline
```

The UI exposes this state through the engine/connectivity indicator.

## Online Engine

The online engine is implemented with the Azure Cognitive Services Speech SDK.

Azure is treated as connected only after the Speech SDK confirms the connection. The application does not immediately label a session "online" merely because the SDK accepted the request to start recognition.

This prevents the UI from reporting a false online state for several seconds while Azure is actually unreachable.

### Azure configuration

Create a `.env` file based on `.env.example`:

```env
AZURE_SPEECH_KEY=your_azure_speech_key
AZURE_SPEECH_REGION=your_azure_region
```

Alternatively, the Azure Speech endpoint can be configured when using endpoint-based authentication supported by the application configuration.

## Offline Engine

The offline engine is intended to work without Internet access after its assets have been prepared/bundled.

The project uses:

```text
Microphone
   ↓
Whisper / faster-whisper
   ↓
Recognized speech
   ↓
Argos Translate
   ↓
Translated text
   ↓
MithraVoice captions
```

### Preparing offline assets

Before producing a distributable Windows build, prepare the offline models/packages while Internet access is available:

```powershell
python scripts\prepare_offline_assets.py
```

The resulting model and translation assets are bundled into the Windows application so the end user does not need to download them when switching to Offline Only or when Auto falls back during a network outage.

## Supported Engine Preference Storage

The selected engine preference is stored locally in the user's MithraCorp application settings.

Default:

```json
{
  "engine_mode": "auto"
}
```

Supported values:

```text
auto
online
offline
```

On Windows, application settings are stored under:

```text
%APPDATA%\MithraCorp\app_settings.json
```

## Plan / License Enforcement

Engine selection is enforced by the application API, not only by the UI.

For example:

- If a plan allows **online only**, Auto is effectively constrained to Online.
- If a plan allows **offline only**, Auto is effectively constrained to Offline.
- If a plan allows both, Auto can use Azure and fall back to the local engine.
- A request for an engine not included in the current plan is rejected by the backend/API.

This prevents a UI-only restriction from being bypassed by directly calling the application API.

## Project Structure

```text
.
├── api.py                    # pywebview API bridge and application control
├── app_settings.py           # Persistent user preferences
├── audio.py                  # Microphone/audio capture
├── azure_engine.py           # Azure Speech Translation engine
├── base.py                   # Common speech translator interface
├── config.py                 # Runtime/environment configuration
├── connectivity.py           # Azure reachability checks
├── history.py                # Local translation history
├── index.html                # Main application UI
├── languages.py              # Language mappings
├── licensing.py              # License handling
├── main.py                   # Windows application entry point
├── overlay.html              # Caption overlay window
├── pipeline.py               # Engine selection, watchdog and failover logic
├── requirements.txt          # Python dependencies
├── scripts/
│   └── prepare_offline_assets.py
├── translation.html          # Translation/caption UI
├── updater.py                # Application update support
├── usage.py                  # Online usage tracking
└── window_capture.py         # Windows application/window capture support
```

## Development Setup

### Requirements

- Windows 10/11 recommended
- Python 3.11+ / the version supported by the current dependency lock/build environment
- Microphone/audio input device
- Azure Speech resource for online translation
- Internet access when downloading dependencies and preparing offline assets

### Install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy the environment template:

```powershell
Copy-Item .env.example .env
```

Fill in the Azure credentials when online translation is required.

### Run from source

```powershell
python main.py
```

## Building the Windows Installer

The project includes PyInstaller and Inno Setup build support.

Prepare offline assets first:

```powershell
python scripts\prepare_offline_assets.py
```

Install build tools:

```powershell
pip install -r requirements.txt
pip install pyinstaller
```

Build the application:

```powershell
pyinstaller build.spec
```

Then compile the installer with Inno Setup:

```powershell
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

See [BUILD.md](BUILD.md) for the complete build and verification process.

## Testing the Three Engine Modes

A useful test matrix is:

### Auto

1. Start with working Internet/Azure access.
2. Start translation.
3. Confirm the UI reports **Online**.
4. Disable Wi-Fi/Ethernet or otherwise remove Azure reachability.
5. Confirm the application changes to **Switching engines…** and then **Offline**.
6. Continue speaking and confirm local translation continues.
7. Restore Internet access.
8. Confirm Auto can restore Azure when the connection is healthy again.

### Online Only

1. Select **Online Only**.
2. Start translation with Internet available.
3. Confirm Azure is active.
4. Disable Internet access.
5. Confirm the application does **not** silently switch to Offline.
6. Confirm the user is informed that Internet/Wi-Fi connectivity is required.

### Offline Only

1. Select **Offline Only**.
2. Disable Internet access.
3. Start translation.
4. Confirm the offline engine starts successfully.
5. Confirm translation continues without any Azure connection attempt.

## Troubleshooting

### Auto immediately uses Offline

Possible causes:

- Azure credentials are missing or invalid.
- The configured Azure region/endpoint cannot be reached.
- A firewall/proxy is blocking Azure.
- Offline-only plan restrictions are forcing local translation.
- The bundled offline assets are available but the application correctly determined Azure was unavailable.

Check the application logs for `[connectivity]`, `[azure]`, `[pipeline]`, and `[api]` messages.

### Online Only does not start

Check:

- Internet/Wi-Fi connection.
- Azure Speech key.
- Azure Speech region/endpoint.
- Azure resource status/quota.
- Firewall/proxy rules.

Online Only intentionally does not use the offline engine as a fallback.

### Offline Only fails to start

Check that the Whisper model and Argos translation packages were prepared before the build:

```powershell
python scripts\prepare_offline_assets.py
```

The application will report an offline-engine startup error when required local assets are missing or unavailable for the selected language pair.

### Azure appears connected and then immediately goes offline

The current Azure engine explicitly waits for Azure to confirm the connection before reporting the online state. This is designed to avoid the older behavior where the UI could briefly claim "online" before the Speech SDK reported a connection failure.

## Architecture Overview

```text
                         MithraVoice
                              │
                 ┌────────────┴────────────┐
                 │                         │
             Audio Input               Settings
                 │                         │
                 └────────────┬────────────┘
                              │
                       TranslationPipeline
                              │
               ┌──────────────┼──────────────┐
               │              │              │
             Auto          Online          Offline
               │              │              │
          Connectivity      Azure        Local engine
             probe           SDK       Whisper + Argos
               │              │              │
               └──────────────┼──────────────┘
                              │
                           Results
                              │
                    Live captions / overlay
```

The pipeline is responsible for engine lifecycle management, watchdog checks, and failover. The UI receives engine-state notifications such as `online`, `offline`, `connecting`, and `failed` so that the displayed state reflects what is actually running.

## Version

Current project version: **3.9.6**

The version is stored in the `VERSION` file.

## License / Service

MithraVoice is a MithraCorp project. Online translation availability, licensing, usage limits, and supported engine access may depend on the user's active service plan.
