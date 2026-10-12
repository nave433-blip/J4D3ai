# Changelog — JADE AI (J4D3)

All notable changes, newest first. `JARVIS.md` (the user's own instructions
file) is never modified by any of this work.

## [JADE] — 2026-10-12 (unreleased — parent cuts the GitHub release)

**The JADE AI release.** Cortana is retired as a product name; the app is now
**JADE AI** (stylized **J4D3**), an independent consumer AI app. GitHub repo
renamed `nave433-blip/cortana` → **`nave433-blip/J4D3ai`**. Config migrates
forward automatically (`~/.jarvis` → `~/.cortana` → `~/.jade`); the CLI entry
point is now `jade`.

- **Clean-room Google auth**: dropped `google-auth-oauthlib` and
  `google-api-python-client` — OAuth2 PKCE and token refresh are now direct
  REST in `core/google_auth.py` / `core/connectors/oauth.py`. No proprietary
  SDK blobs under the Google integrations.
- **Voice stack** (`core/voice/`): plugin architecture (wake-word → capture →
  VAD → STT → TTS) with honest unavailable-errors; OS-native TTS default
  (`say`/`espeak-ng`/SAPI); barge-in with echo grace; PowerShell quoting
  hardened on Windows.
- **OpenAI-compatible API server** (`jade api`): `POST /v1/chat/completions`
  (now with **SSE streaming** via `stream: true`), `GET /v1/models`,
  `POST /v1/audio/speech`, `POST /v1/audio/transcriptions`,
  `GET /v1/audio/voices`. Bearer-token auth, loopback default, clear 400/502
  errors, 1 MiB body cap.
- **MCP client** (`core/mcp_client.py`): stdio servers with Content-Length /
  NDJSON framing; chunked reads (was byte-at-a-time) and a 16 MiB buffer
  guard.
- **Connectors**: Google connectors (Gmail, Calendar, Contacts, Docs, Drive,
  Tasks, YouTube) now **auto-refresh expired access tokens** instead of dying
  an hour after connect. Microsoft Graph/OneDrive/Outlook/Teams/OneNote
  connectors with honest connected/disconnected status.
- **Startup**: `jade --help` imports in ~0.45s (was ~1.55s) — `prompt_toolkit`
  and `requests` load lazily only on the paths that need them.
- **Installer** (`install.sh`): one-line macOS/Linux install (venv + `jade`
  launcher + smoke test); repo URL updated to `nave433-blip/J4D3ai`.
- **Tests**: 782 passed, 1 skipped. (12 failures are pre-existing environment
  issues — REPL tests needing a TTY and the litellm loader test where litellm
  isn't installed — verified identical on unmodified code.)

## [CORTANA — archived below]
## [desktop-voice] — 2026-09-25 (branch `feature/desktop-voice`, not yet merged)

**Round B: desktop app + voice, everything plus the kitchen sink.**

- **Desktop GUI** (`cortana gui`): chat-first window built on web tech over
  the existing local dashboard HTTP core. Native window via optional
  `pywebview`; falls back to the browser with an honest install hint. CLI
  works with zero GUI dependencies.
- **Perplexity-style overlay** (`cortana overlay`): floating glassmorphism
  overlay (frosted blur via `backdrop-filter`, graceful fallback where the
  OS can't blur), ask → answer → dismiss (Esc). Global hotkey
  (default Ctrl+Shift+Space) via optional `keyboard` package.
- **OS-native presence** (`cortana tray`): Windows system tray, macOS menu
  bar, Linux panel applet via optional `pystray`, with quick toggles
  (listening, voice, overlay, personality picker). Honest "not supported
  here" when the backend is missing — first real Windows support story.
- **"Hey Cortana" wake word** (`cortana voice-wake on`): opt-in
  always-listening hotword that summons the overlay. Pluggable engine
  (`openwakeword` default when installed); custom `.onnx` model drops in via
  `wakeword_model_path`. Privacy notice acknowledged on first enable; mic
  audio never leaves the machine for detection.
- **Voice plugin architecture** (`core/voice/`): independent drop-in slots —
  wake-word → audio capture → VAD → STT → TTS — each with a documented
  interface, a working default (or honest unavailable error), and an engine
  registry for proprietary drop-ins. Features: continuous conversation mode,
  barge-in (interrupt her mid-sentence, she yields), push-to-talk AND
  always-listening, audio device selection, noise-suppression hooks.
  STT covers voice AND sound-to-text. TTS defaults to the OS-native
  synthesizer (`say`/`espeak-ng`/`spd-say`/SAPI) — no new packages needed.
- **`core/voice/proprietary/`** (git-ignored): drop-in directory with README
  templates — licensed voice models and custom wake-word models go here,
  one config line activates them. Never transmitted anywhere.
- **Personalities**: `cortana` (new default — loyal, dry wit),
  `witty` (irreverent humor, original writing), `clippy` (full paperclip
  energy, just for fun), `professional` (straight-laced). `/personality
  <name>`, `cortana personality`, config `personality`, dashboard/GUI
  picker. Legacy keys (`sarcastic`, `concise`, `mentor`, `nave_ai`) still
  resolve. Fixed a real bug: the personality prompt was computed but never
  passed to the model. No refusal-bypass content (pinned by tests).
- **Voice picker**: `/voice <profile>`, `cortana voice-profile`, GUI
  picker. Profiles are JSON data files (`core/voice/profiles/`) — new ones
  drop in. Includes a `licensed-cortana` placeholder slot for a future
  licensed voice model.
- **Logo**: new Cortana mark (`assets/cortana-mark.png`, +64/256px sizes)
  wired as app icon, tray/menu-bar icon, and overlay mark.
- Dashboard: new Personality & Voice picker panel; `/overlay` page served
  with the same token auth.

Tests: +35 new (`tests/test_desktop_voice.py`), full suite 474 passed,
1 skipped (pre-existing UDP env skip), `compileall` clean.

## [platform] — 2026-09-25 (branch `feature/platform`, not yet merged)

**Max customization round (competitor-grade): settings, profiles, projects,
Sims, prompts, memory cores, connectors, sign-in, and a Cortana Account slot.**

- **Typed settings schema** (`core/settings.py`): ~45 settings across 12
  categories with validation, coercion, live-vs-restart metadata,
  sensitive-key confirmations, profile override hook, and an interactive menu.
  Old/unknown config keys are preserved on save; legacy keys keep loading.
- **Profiles** (`core/profiles.py`): named settings bundles under
  `~/.cortana/profiles/` with quick-switch API, per-profile memory scope,
  and GUI-ready `list_profiles`/`get_active_profile`/`switch_profile`.
- **Projects** (`core/projects.py`): named workspaces under
  `~/.cortana/projects/<slug>/` with custom instructions, file/folder
  attachments, active-project context injected into prompts (Round B/C safe),
  per-project JSONL chat logs, and safe bounded attachment reading.
- **Cortana Sims** (`core/sims.py`): portable JSON bots (name, avatar,
  personality, system prompt, model, tool allowlist) under `~/.cortana/sims/`
  with create/list/chat/import/export/delete and a hard tool-allowlist choke
  point that strips sensitive tools from imported Sims.
- **Prompt library** (`core/prompts.py`): save/list/apply/delete prompt
  templates, per-personality overrides, and `get_prompt_for_personality`
  hook consumed by Round B's personality system.
- **Memory cores** (`core/memory_cores.py`): inspectable JSONL cores (facts,
  preferences, projects, episodic) under `~/.cortana/memory/cores/` with
  add/recall/forget/export/clear/stats, optional profile/project scopes, and
  a consent-gated auto-capture helper that is off by default. Does not touch
  P2P; never transmits tokens or private memories.
- **Connector framework** (`core/connectors/`): OAuth2/PKCE helpers (stdlib
  HTTP), keyring-backed tokens, per-connector scopes, confirmation before
  interactive actions, and honest "not connected" statuses. Connectors:
  Google Drive (read-only), Gmail (read-only), Google Calendar (upcoming,
  read-only), Outlook/Microsoft 365 Graph (mail + calendar, read-only).
  Requires real user OAuth client registrations; no live OAuth verified.
- **Sign-in** (`core/signin.py`): client-side Microsoft (OAuth2/PKCE), Apple
  (OAuth2), Google (OAuth2/PKCE via `google_client.json`), GitHub (device
  flow) with keyring-backed tokens. **Apple sign-in currently incomplete:
  callback only handles GET, not the form_post response Apple sends.**
- **Cortana Account** (`core/cortana_account.py`): local account record at
  `~/.cortana/account.json` — display name, linked providers, data controls
  (`export_local_data` bundles config, profiles, Sims, prompts, memory
  cores). `docs/CORTANA_ACCOUNT.md` states clearly the hosted backend does
  not exist yet; API keys, OAuth tokens, and private vector memories are
  never synced.
- **CLI**: `settings`, `profile`, `project`, `sim`, `prompt`, `memories`,
  `connector`, `signin`, `account` command groups; matching REPL slash
  commands with menus.
- **Dashboard**: editable settings page + authenticated API (`GET/POST
  /api/settings`) driven by the same schema; secret-like keys are masked and
  cannot be changed from the dashboard.

## [round-a] — 2026-09-25 (branch `feature/council-coding`, not yet merged)

The "power + adoption" round: four tracks, all local, nothing published.

### New: Council mode (`/council`, experimental, opt-in, off by default)

Multiple connected providers propose answers to a question, critique one
another's proposals, and converge over bounded rounds (default 3, `--rounds N`).
Member providers/models come from the `council_members` config key or default to
the first configured providers. Per-round proposals show with attribution,
critiques render dimmed, and the converged final answer gets its own panel.
Bounded cost: the estimated call count (`M*R + M*(R-1) + 1`) is shown and
confirmed before starting (skipped only under auto-approve). Reuses the
`/hive` provider-fanout machinery.

### New: coding-first CLI (`/code`, `/suggest`, `/explain`)

Project-aware agentic coding loop, Linux-first (macOS still works):
`/code <task> [in <dir>]` reads repo context (respects `.gitignore` via
`git ls-files`, with an honest fallback walker), proposes a unified diff,
**always confirms before applying** (auto-approve aware), snapshots affected
files to `.cortana/checkpoints/`, and prints rewind instructions.
`/suggest <task>` proposes a shell command (never auto-executes);
`/explain <command>` explains one in plain language. Project prefs live in
`.cortana/config.json` (model, sandbox policy, extra ignores). Generated code
only ever executes through the existing `tools/sandbox.py`.

### New: Microsoft adoption kit

- **OpenAI-compatible API server** (`core/apiserver.py`, `/api` command):
  `POST /v1/chat/completions` and `GET /v1/models`, Bearer-token auth,
  loopback-only by default, stdlib only. Existing Copilot-style clients can
  point at Cortana with just a URL change. `stream:true` gets an honest 400.
- **`cortana_core` importable library**: curated lazy re-exports of the stable
  engine API (`think`, `think_structured`, `hive_ask`, config, sandbox,
  `confirm`) decoupled from the CLI; added to the packaging includes.
- **VS Code extension scaffold** (`extensions/vscode/`): reference TypeScript
  extension (`cortana.ask`, `cortana.completeWithContext`) talking to the local
  API server — documented scaffold, not built or published.
- **`docs/ADOPTION.md`**: architecture overview, Copilot→Cortana feature map
  (gaps marked honestly), integration points, a `proprietary/` drop-in contract
  for licensed components (voice/wake-word slots), and security notes. No
  proprietary code was reverse-engineered; only public interfaces are used.

### New: GitHub connector (`/github`, `tools/github.py`)

Repos, issues, PRs, Actions status, code search. Prefers the `gh` CLI when
authenticated, falls back to stdlib REST with a keyring-stored token
(`CORTANA_GITHUB_TOKEN` env fallback). Read-only by default; writes
(create/comment) require confirmation. `core/repair.py`'s crash-report flow
keeps its single consent prompt (no double-ask).

## [server] — 2026-09-25 (branch `feature/server`, not yet merged)

**Reference cloud server** (`server/`): the honest minimal backend a
local-first assistant needs, so Microsoft (or anyone) can deploy it with
near-zero heavy lifting. Pure stdlib + `cryptography` (server-only;
`setup.py` now excludes `server/` from the CLI package).

- **Accounts**: email+password signup/login (PBKDF2-SHA256, 600k iters,
  per-user salt; argon2 upgrade path documented), session tokens (only
  SHA-256 stored), change-password, logout.
- **Social sign-in**: `POST /v1/auth/oidc/{google,apple,microsoft}` with
  real server-side ID-token validation (RS256 signature via JWKS,
  iss/aud/exp checks — unverifiable tokens are rejected, never trusted);
  GitHub via server-side code exchange + `/user` lookup (documented as
  non-OIDC). Exact client contract in `server/README.md` for the
  client-side auth round.
- **OAuth brokerage**: the server holds one OAuth app per provider
  (Google Drive, Gmail, Google Calendar, Outlook, GitHub); PKCE authorize
  flow, per-user tokens encrypted at rest (Fernet), refresh handled
  server-side — clients never see refresh tokens. Refuses to store tokens
  if the encryption key/package is missing instead of storing insecurely.
- **Device pairing**: short `CORT-XXXXXX` codes with explicit owner
  accept/reject; server is signaling-only and never sees conversation
  content (actual handoff stays device-to-device via `core/handoff.py`).
- **Hosted chat**: OpenAI-compatible `POST /v1/chat/completions`
  (incl. SSE streaming) relaying to a configured provider — the hosted
  counterpart to the on-device API; honest 501 when unconfigured.
- **One-command deploy**: `Dockerfile`, `docker-compose.yml` (app +
  postgres + redis provisioned for the production path), `.env.example`,
  Azure Container Apps bicep template, `server/README.md` 5-minute guide.
- **Honesty docs**: `server/PRODUCTION.md` — the exact hardening checklist
  (Postgres swap, Key Vault, Argon2, TLS, Redis rate limiting, audit
  logging, backups) with "deliberately out of scope" section.
- 27 new tests in `tests/test_server.py` (in-process server, no live
  network: stubbed JWKS/token/GitHub/chat upstreams). Rate limiting,
  security headers, secret-scrubbing log filter included.

## [rename] — 2026-09-25 (branch `rename/cortana`, not yet merged)

**Project rename: JARVIS → CORTANA.** Everything the user touches now says
Cortana: CLI (`cortana`, with a deprecated `jarvis` shim that prints a rename
notice and forwards), config dir `~/.cortana` (old `~/.jarvis` is copied, not
moved), config keys (`cortana_model`, `cortana_name`), keyring services
(`cortana-dev`, `cortana_cli`, `cortana_google_auth`), dashboard token header
`X-Cortana-Token`, installer (`~/.cortana-app`, `/usr/local/bin/cortana`),
Homebrew formula (`cortana.rb`), AUR package (`cortana`), PyPI distribution
(`cortana`). P2P wire actions and UDP discovery bytes are unchanged so older
nodes stay interoperable. A few tasteful easter eggs: a `/clippy` command, a
Halo-flavored boot line, and a classic-Windows sign-off on `/exit`.

New dev toggle: **auto-approve**. `cortana --yes` / `-y`,
`CORTANA_AUTO_APPROVE=1`, or `"auto_approve": true` in config answers routine
confirmation prompts yes automatically (never on by default). Startup warns
clearly, every auto-approved action is logged to
`~/.cortana/logs/auto_approve.log` (0600), credential/trust prompts still ask,
and the sandbox is unaffected. Migration notes: `~/.jarvis` is copied to
`~/.cortana` (old dir left untouched, also when only cache dirs exist);
`get_env_with_config("cortana_*")` honors legacy `JARVIS_*` env spellings.


All notable changes, newest first. Dates are when the work landed on the
`audit/fix` branch. `JARVIS.md` (the user's own instructions file) is never
modified by any of this work.

## [0.2.6] — 2026-09-25 (release candidate, not yet tagged)

The scheduler/dashboard release — the end of the audit arc. Every item below
was re-verified with `compileall` clean and the full test suite green.

### New: task scheduler (`/schedule`)

- Persistent in-process scheduler: one-shot (`--at`), interval (`--every`),
  and cron jobs (`--cron "0 4 * * *"`).
- Jobs stored in `~/.cortana/scheduler_jobs.json` (atomic writes, mode
  `0600`); append-only run history in `~/.cortana/scheduler_runs.jsonl`.
- Whitelisted actions only: `/brief`, `/ollama auto-pull`, `/research`,
  `/health`, and `shell: …` executed **exclusively** through the sandbox —
  anything else is rejected.
- Missed runs are logged honestly; missed one-shot jobs are disabled rather
  than silently discarded. Starts with the normal interactive session.
- `/ollama auto-pull` help now points at `/schedule` instead of system cron.

### New: local web dashboard (`/dashboard`, `cortana dashboard`)

- Stdlib-only `http.server` UI replacing the old Rich terminal dashboard.
- Binds loopback (`127.0.0.1`) by default; `--lan` binds all interfaces with
  an explicit warning.
- Per-instance random token at `~/.cortana/dashboard_token` (`0600`), sent as
  `?token=` or `X-Cortana-Token`, compared with `hmac.compare_digest`.
- Live panels read real state: providers, P2P peers, Ollama hosts/models,
  scheduler jobs + recent runs, brief watcher, redacted log tail.
- Chat calls `core.brain.think`; a read-only command whitelist
  (`/health`, `/models`, `/schedule list`, `/schedule log`, `/ollama ps`,
  `/ollama stats`, `/brief status`, `/p2p-status`) — nothing else executes.
- Log rendering redacts credential/token-shaped material before display.

### Hardening

- **Startup ≈12× faster** (`import cli`: ~5.0s → ~0.43s): LiteLLM and Paramiko
  now import lazily on first real use, with regression tests that forbid
  preloading.
- **Actionable errors**: Ollama unreachable → "start it with `ollama serve`";
  model not pulled → "`/ollama pull <model>`"; missing provider key →
  "run `/connect`"; dashboard port in use → "try `--port 0`". One line each,
  no tracebacks in normal mode.
- **P2P capability flags**: peers advertise `local_services.scheduler` and
  `local_services.dashboard` separately from P2P action compatibility, so
  no peer falsely claims scheduler/dashboard as callable P2P actions.
- Realistic filesystem MCP fixture (`tests/fixtures/mcp_fileserver.py`)
  speaking real MCP-style JSON-RPC over stdio (tools/list, tools/call,
  root-confined file tools).
- `/menu` updated for all new commands.

### Docs

- README: new "What's New in v0.2.6", feature sections for every new
  subsystem, and a full command reference table.
- This changelog.

## 0.1.7 → 0.2.6 audit arc (2026-09-25, branch `audit/fix`)

Everything between the last packaged release and 0.2.6, in landing order:

1. **Easy-connect**: provider linking wizard, secure key storage (keyring →
   config fallback), `is_configured()` single definition, `/connections`
   status tables, verified per-provider key-page URLs.
2. **Capability round**: deterministic P2P loopback tests (plaintext + TLS),
   real per-provider validation endpoints, opt-in Linux code sandbox
   (`tools/sandbox.py`: resource limits + bubblewrap isolation), honest
   browser backend probing (`tools/browser.py` never claims JS rendering).
3. **Packaging**: idempotent one-line `install.sh` (Linux/macOS), Homebrew
   formula (v0.1.7 tarball, verified sha256), PyPI wheel/sdist, `PACKAGING.md`
   runbook, AUR skeleton. Nothing published yet.
4. **Hive mind / swarm / MCP / research**: `/hive` consensus across
   providers (failures skipped, never faked), `/swarm` planner→workers→
   reviewer (sandboxed code workers), stdlib-only MCP client, cited
   multi-query `/research` with citation validation.
5. **Ollama fleet / edge round**: `/ollama ps/pull/prune/bench/stats`,
   multi-host pools, `/thin` thin client, `/rewind`/`/branch`/`/branches`/
   `/diff` time-travel, verified `/skill` sharing, `/brief` watcher,
   `/handoff` session transfer.

## [0.1.7] — last packaged release

The final state before the audit: multi-LLM support, Gemini-style slash
commands, personality profiles, memory management, installer script, and the
Homebrew formula + AUR skeleton now kept at this version until 0.2.6 is
tagged.
