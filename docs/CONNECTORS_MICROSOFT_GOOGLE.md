# Cortana Connectors: Microsoft & Google

Cortana links your real accounts — no fake connections, ever. A connector
reports `connected: False` with exact setup steps until a real token
exists. **No credentials are ever shipped with Cortana**; you register
your own OAuth apps and Cortana stores only *your* tokens.

## Microsoft (Microsoft Graph)

### One-time setup

1. Go to https://portal.azure.com → **App registrations** → **New registration**.
   - Name: `Cortana` (anything works).
   - Supported account types: *Accounts in any organizational directory and
     personal Microsoft accounts* (so personal Outlook/OneDrive works).
   - Redirect URI: leave blank — Cortana uses the **device-code flow**,
     which needs no redirect.
2. Note the **Application (client) ID**.
3. API permissions → Add → Microsoft Graph → **Delegated permissions**,
   add the scopes from the table below. (Some Teams scopes need admin
   consent on work/school tenants — see below.)
4. In Cortana: `cortana settings set ms_client_id <application-id>`
5. Link: `cortana connector connect outlook` (or `onedrive`, `teams`,
   `onenote`). Cortana prints a `microsoft.com/devicelogin` code; approve
   it in your browser.

All Microsoft connectors share one token file,
`~/.cortana/ms_graph_token.json` (0600). Disconnecting any Microsoft
connector clears the shared session for all of them.

### Scope table

| Connector | ID | Delegated scopes | Notes |
|---|---|---|---|
| Outlook / Microsoft 365 | `outlook` | `Mail.Read` `Mail.Send` `Calendars.ReadWrite` `Contacts.Read` `Tasks.ReadWrite` | Mail send + calendar/contacts/To-Do CRUD included |
| OneDrive | `onedrive` | `Files.ReadWrite` | Uploads limited to 4 MiB (Graph simple-upload limit; resumable sessions not implemented) |
| Teams | `teams` | `Team.ReadBasic.All` `Channel.ReadBasic.All` `ChannelMessage.Read.All` `ChannelMessage.Send` | `ChannelMessage.Read.All` needs **admin consent** on organizational tenants; personal accounts can't read channel messages at all via delegated permissions |
| OneNote | `onenote` | `Notes.ReadWrite` | Page creation takes simple HTML |
| Azure (read-only) | `azure` | n/a (service principal) | See below |

### Azure (read-only) setup

1. App registrations → New registration (single tenant is fine).
2. Certificates & secrets → New client secret — copy it once.
3. Subscriptions → your subscription → Access control (IAM) → Add role
   assignment → **Reader** → select your app.
4. `cortana settings set azure_tenant_id <tenant>` and
   `cortana settings set azure_client_id <app-id>`, then
   `cortana connector connect azure` (secret is prompted for and goes to
   the OS keyring, never into chat or config files).

Deliberately **read-only**: list subscriptions, resource groups, VM power
states. No start/stop/delete — a chat assistant shouldn't be one
misclick from deallocating your VMs.

### Token storage & revocation

- Microsoft user tokens: `~/.cortana/ms_graph_token.json` (0600 file).
  Revoke at https://account.microsoft.com → Privacy → Apps and services.
- Azure client secret: OS keyring (`cortana_connector_azure`).
  Revoke by deleting the secret in the Azure portal.
- Google tokens: OS keyring (`cortana_connector_<id>`).
  Revoke at https://myaccount.google.com/permissions.

---

## Google

### One-time setup

1. https://console.cloud.google.com → create a project → **APIs & Services**
   → **Library**: enable *Gmail API*, *Google Drive API*, *Google Calendar
   API*, *Google Tasks API*, *People API*, *Google Docs API*,
   *Google Sheets API*, *Google Slides API*, *YouTube Data API v3*
   (enable only what you'll use).
2. **OAuth consent screen** → External → add yourself as a test user
   (while the app is in Testing mode, only test users can consent).
3. **Credentials** → Create Credentials → OAuth client ID → **Desktop app**
   → download the JSON → save as `~/.cortana/google_client.json`.
4. `cortana connector connect gmail` (or `gdrive`, `gcal`, `gtasks`,
   `gcontacts`, `gdocs`, `youtube`). A browser window opens for consent.

### Scope table

| Connector | ID | Scopes | Notes |
|---|---|---|---|
| Gmail | `gmail` | `gmail.readonly` | Read-only |
| Google Drive | `gdrive` | `drive.readonly` | Read-only; file listing/search/read |
| Google Calendar | `gcal` | `calendar.readonly` | Read-only; upcoming events |
| Google Tasks | `gtasks` | `tasks` | Full CRUD on lists and tasks |
| Google Contacts | `gcontacts` | `contacts` | List/search/create your own contacts |
| Google Docs/Sheets/Slides | `gdocs` | `documents` `spreadsheets` `presentations` | Doc read/create/append, sheet read/append, slide text listing |
| YouTube | `youtube` | `youtube.readonly` | Read-only; see quota note |

### YouTube quota reality

Every Cloud project gets **10,000 quota units/day** by default.
`search.list` costs **100 units** — about 100 searches/day on the free
quota. Channel/video reads cost 1–3 units. If you exceed it, the API
returns `quotaExceeded` and the connector reports it honestly. Request a
quota increase in the Cloud Console if you need more.

---

## Windows integration (`core/windows_integration.py`)

Windows-only helpers; on other platforms every function returns a clean
"Windows only" error instead of faking it.

- **Toast notifications**: uses `winrt` if installed, otherwise falls
  back to PowerShell driving the built-in `Windows.UI.Notifications`
  API — no extra packages required.
- **Run at login**: toggles one value,
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Cortana` — the
  *only* registry write this module performs.
- **System summary**: OS build/display version from the registry plus
  `core/hardware_check.py`'s psutil data.
- **Protocol handlers**: read-only check of which app owns `http:`,
  `mailto:`, etc.

---

## NOT possible / NOT built (honest list)

- **Google Keep**: there is no official Keep API. Any "connector" would
  be screen-scraping with your password — we don't ship that.
- **Google Photos**: the Library API's useful scopes are restricted and
  Google rejects verification for general-purpose projects, so a Photos
  connector would fail for normal users. Not shipped rather than broken.
- **Kernel drivers**: Cortana is a user-space Python app. Installing
  Windows drivers needs EV-signed `.sys`/`.inf` packages via
  `pnputil`/Device Manager with admin rights. `driver_info()` says this
  plainly instead of pretending.
- **Teams channel-message reading on work tenants** without admin
  consent (`ChannelMessage.Read.All`), and **personal Microsoft accounts**
  can't grant it at all — Graph returns 403 and the connector tells you
  why.
- **OneDrive uploads over 4 MiB**: Graph's simple upload caps at 4 MiB;
  resumable upload sessions are not implemented yet (the connector says
  so instead of silently failing).
- **Sending mail as someone else / app-only mail**: delegated
  permissions only — Cortana sends as *you*.
