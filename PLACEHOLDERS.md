# PLACEHOLDERS — things only Evan can do

This project never stalls waiting on Evan. Every item below is something
that needs his hands, credentials, money, or decision. Until he acts, the
code takes the documented fallback path and everything keeps working —
tests stay green.

## How placeholders behave

- Anything needing a credential or signup returns a clean "not configured"
  setup error (never a traceback, never a fake success).
- Anything buildable without him is built; anything needing him is stubbed
  with a working local stand-in where one exists.

## The list

### 1. GitHub repo rename — DONE 2026-10-12
- **What:** ~~rename `nave433-blip/cortana` to the JADE name~~ — done:
  the repo is now **`nave433-blip/J4D3ai`**.
- `install.sh` clone/curl URLs, `core/update.py` release-check URL, and
  `core/repair.py` issue-reporting URL all point at `nave433-blip/J4D3ai`.
- Remaining: the local directory is still `~/workspace/jarvis-dev`
  (see item 2); any other stale `cortana` strings in docs are cosmetic.

### 2. Local directory rename — NEEDS EVAN (structural)
- **What:** `~/workspace/jarvis-dev` is the old structural path.
- **Until then:** everything works from the current path. Docs that print
  the path use the current one.

### 3. Azure app registration — NEEDS EVAN
- **What:** register an app in Azure AD (client ID + tenant) for the
  Microsoft 365 connectors (Graph, OneDrive, Outlook, Teams, OneNote).
- **Until then:** connectors detect the missing registration and return a
  setup error pointing at `docs/CONNECTORS_MICROSOFT_GOOGLE.md`. All
  connector tests run against mocks.

### 4. Google `google_client.json` — NEEDS EVAN
- **What:** create an OAuth client in Google Cloud Console and save it as
  `~/.jade/google_client.json` (see docs).
- **Until then:** Google connectors (Tasks, Contacts, Docs/Sheets/Slides,
  YouTube read-only) return a setup error. Tests use mocks.

### 5. Expo login — NEEDS EVAN
- **What:** `eas login` on his machine to build the mobile app.
- **Until then:** `mobile/` is source-only; TypeScript compiles and lints
  clean, but no APK/IPA is produced here.

### 6. Apple Developer membership (paid) — NEEDS EVAN
- **What:** paid membership for iOS distribution.
- **Until then:** iOS runs via Expo Go / dev builds; Android builds via
  EAS don't need it.

### 7. Installs on his machines — NEEDS EVAN
- **What:** running `install.sh` on his Windows gaming rig / Pop!_OS /
  MacBook, where only he has access.
- **Until then:** Linux install path is tested here; other platforms follow
  the same script.

### 8. License calls — NEEDS EVAN (only if they arise)
- **What:** if the best open-source replacement for a proprietary
  dependency is GPL/LGPL, it is NOT vendored silently — the choice is
  listed here for his call.
- **Until then:** the current dependency stays, marked in the audit below.

## Dependency audit (proprietary → open)

| Dependency | License | Status |
|---|---|---|
| Microsoft Graph / Outlook / OneDrive / Teams / OneNote / Azure connectors | clean-room (stdlib `urllib` + own OAuth device-code flow) | ✅ open, no vendor SDK |
| Google Tasks / Contacts / Docs / Sheets / Slides / YouTube connectors | clean-room (`core/connectors/oauth.py` PKCE + direct REST) | ✅ open, no vendor SDK |
| `core/google_auth.py` (legacy) | clean-room: stdlib PKCE + direct REST (`core/connectors/oauth.py`) | ✅ migrated 2026-10-09 — vendor SDK dropped from deps; old keyring tokens auto-migrate on read |
| `tools/cloud.py` Google Drive | clean-room: direct Drive v3 REST (`api_get`) | ✅ migrated 2026-10-09 — `googleapiclient` gone |
| `tools/cloud.py` Dropbox | `dropbox` SDK (MIT) | ✅ open license (MIT); kept, documented |
| Everything else in `pyproject.toml` | MIT / Apache-2.0 / BSD (litellm, rich, typer, etc.) | ✅ |

No GPL/LGPL in the tree. No binary blobs. No closed SDKs.
