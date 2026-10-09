# JADE AI (J4D3) — your local AI app (v0.3.0)

**JADE AI** (stylized **J4D3**) is an independent, consumer-first AI
assistant that runs locally on macOS, Linux, and Windows. Chat-first,
local-first, customizable — an app like the big assistants, but yours:
your models, your memory, your machines, your rules. No cloud account
required, no pitch to anyone: this project belongs to its user.

> **Name history:** this project was previously known as Jarvis, then
> Cortana ("Cortana" is also a Microsoft trademark — one more reason for
> the rename). Config, keys, and data migrate automatically: `~/.jarvis`
> and `~/.cortana` are copied to `~/.jade` on first run; old key names and
> the `cortana` / `jarvis` commands keep working as deprecated shims.
> The old Microsoft pitch is archived in `docs/attic/` — the Microsoft 365
> and Google *integrations* stay as user features.

## 🚀 What's New in v0.3.0

- **Rebrand to JADE AI (J4D3)** — new name, new binary (`jade`), new
  mobile bundle id (`com.nave433.jade`); everything migrates forward.
- **P2P expansion merged** — self-hostable discovery, global P2P client,
  SHA-256 file transfers, messaging inbox, presence, pairing codes/QR,
  guarded mDNS/UPnP, double opt-in memory sync (`docs/P2P.md`).
- **Microsoft 365 + Google connectors merged** — Graph, OneDrive, Outlook,
  Teams, OneNote, read-only Azure; Tasks, Contacts, Docs/Sheets/Slides,
  read-only YouTube — all on clean-room REST/OAuth, no vendor SDKs
  (`docs/CONNECTORS_MICROSOFT_GOOGLE.md`).
- **Mobile thin client merged** — Expo React Native (SDK 57) Android/iOS
  app: chat UI, auth, voice input + read-aloud (`mobile/`).
- **Robustness round** — 27 new stress tests (adversarial router inputs,
  concurrency, oversized payloads, corrupt config, retry/backoff); five
  real bugs fixed.

## 🚀 What's New in v0.2.6

The audit-to-release arc — everything below was re-verified with
`compileall` clean and the full test suite green:

- **🖥️ Desktop app, overlay & tray** (`jade gui` / `jade overlay` /
  `jade tray`) — native window (optional `pywebview`, browser fallback),
  floating glassmorphism overlay with global hotkey, and a Windows tray /
  macOS menu-bar / Linux panel icon with quick toggles. First real Windows
  support story.
- **🎙️ Voice plugin architecture** (`core/voice/`) — swappable slots for
  wake-word → capture → VAD → STT → TTS, OS-native TTS default, "Hey
  Jade" opt-in wake word, continuous conversation with barge-in, and a
  git-ignored `proprietary/` drop-in dir for licensed models.
- **🎭 Personalities & voice picker** — `jade` (default), `witty`,
  `clippy` 📎, `professional`; `/personality <name>`, `/voice <profile>`,
  dashboard/GUI pickers.

- **⏰ In-Jade task scheduler** (`/schedule`) — persistent one-shot,
  interval, and cron jobs with whitelisted actions, atomic job storage
  (`0600`), and an append-only run log. No system cron needed:
  `/schedule add --name "nightly pull" --action "/ollama auto-pull" --cron "0 4 * * *"`.
- **🖥️ Local web dashboard** (`/dashboard` or `jade dashboard`) —
  token-authenticated (per-instance token, `0600`, constant-time compare),
  stdlib-only, loopback by default (`--lan` binds LAN with a warning):
  live provider/P2P/Ollama/scheduler panels, safe chat, and a read-only
  command whitelist.
- **🐝 Hive mind, 🐜 agent swarm, 🔌 MCP, 🔎 deep research** — new
  subsystems with honest "unavailable" errors instead of fake stubs
  (`/hive`, `/swarm`, `/mcp`, `/research`).
- **🦙 Ollama fleet management** — `/ollama ps`, `prune`, `bench`,
  `stats`, multi-host pools, auto-pull with `/schedule` integration.
- **🧊 Thin client** — `/thin` for low-resource nodes against a fat peer.
- **⏳ Conversation time-travel** — `/rewind`, `/branch`, `/branches`, `/diff`.
- **📦 Verified skill sharing** — `/skill` packs are manifest-verified.
- **📋 Proactive briefs & 🤝 session handoff** — `/brief`, `/handoff`.
- **Hardening** — P2P TLS option, capability-flag service advertising,
  startup ≈12× faster (lazy LiteLLM/Paramiko imports with regression
  tests), actionable one-line errors instead of tracebacks, and a
  realistic filesystem MCP fixture for tests.

## 💎 Core Features
- **Multi-Model Aggregator:** Switch between **Ollama**, **OpenAI**, **Anthropic**, **Gemini**, **Mistral**, **DeepSeek**, **Groq**, and more.
- **Gemini-Style Interface:** Transparent reasoning blocks and Warp-inspired command input.
- **AI Tool Hub:** Native launchers for **Claude Code**, **Copilot CLI**, and **Hermes**.
- **Vector Memory:** Semantic context storage using FAISS and local embeddings.

## 📦 Installation

Pick one:

**1. One-line installer (Linux & macOS)** — sets up an isolated venv in `~/.jade-app` and links the `jade` command:
```bash
curl -fsSL https://raw.githubusercontent.com/nave433-blip/jarvis-dev/main/install.sh | bash
```
What it does: installs system audio deps via your package manager (apt/dnf/pacman/zypper, or Homebrew on macOS — uses sudo), fetches the sources, creates a Python 3.12+ venv, installs `jade` to `/usr/local/bin`, and smoke-tests it. Re-running updates safely.
Options: `JADE_SKIP_SYSTEM_DEPS=1` (skip the sudo step), `JADE_DIR=…` (custom location), `JADE_REF=…` (install a branch/tag).

**2. Homebrew (macOS & Linux)** — installs into a Homebrew-managed virtualenv (Python 3.12, portaudio, Ollama):
```bash
brew install --formula https://raw.githubusercontent.com/nave433-blip/jarvis-dev/main/jade.rb
```

**3. pip / pipx** (any platform with Python 3.12+):
```bash
pip install git+https://github.com/nave433-blip/jarvis-dev.git
# or fully isolated:
pipx install git+https://github.com/nave433-blip/jarvis-dev.git
```

After connecting, run `jade` and then `/connect` inside it to set up your AI providers.

> Testing an unmerged branch? Replace `main` with the branch name in the URLs above, and add `JADE_REF=<branch>` to the bash one-liner. See `PACKAGING.md` for the release runbook.

## 🔌 Connecting your AI providers

Run the interactive setup wizard any time:

- `/connect` (interactive chat) or `connect-provider <name> [--host URL] [--key KEY]` (CLI)
- `/connections` (interactive chat) or `connections [--test]` (CLI) — status table for all 18 providers

The wizard lists every supported provider with a status dot (● configured / ○ not),
auto-detects a running **Ollama** (`GET http://localhost:11434/api/tags`, one-confirm setup,
opt-in auto-repair), and accepts a custom host for the other local providers
(vLLM, SGLang, GPT4All, llama.cpp, NVIDIA NeMo, Local server). For API-key providers
it shows the provider's key page URL and reads the key with hidden input.

Key storage: keys are validated **before** they are saved and live in the OS
keyring (service `jade-dev`, account `jade-<provider>`). If no keyring backend
is available, JADE falls back to `~/.jade/keys.json` with `0600` permissions and
prints a clear warning that this is less secure. Keys are never printed, logged, or
echoed — the status table only shows set/not-set. Keys left in the old config-file
store are migrated into the keyring on first successful connect and removed from
the config file.

Validation is real: every provider is checked against a documented live endpoint
(never a "key present" fake), and a failed `/connections --test` tells you exactly
what to do next — e.g. a rejected key points at that provider's key page. The
wizard also labels legitimate free options it verified (Gemini, Groq, Mistral,
Cohere, Together, GitHub, and self-hosted local providers); paid-only providers
carry no free-tier claim rather than a guessed one.

## 📡 P2P swarm (opt-in)

JADE instances can link over the LAN: `p2p-server` starts your node, `p2p-scan`
finds peers, `p2p-status` shows the swarm, and (with your explicit per-action
approval) peers can exchange files, tokens, and think-tasks.
Plaintext HTTP is the default; set `"p2p_use_tls": true` plus
`"p2p_tls_certfile"`/`"p2p_tls_keyfile"` in `~/.jade/config.json` for TLS
(a self-signed cert is fine on a trusted LAN — generate one with
`generate_self_signed_cert()` from `core.p2p`). Only enable P2P on networks you
trust; API keys requested via `get_token` travel over the P2P transport.

## 📦 Linux code sandbox (opt-in)

The agent's SHELL tool runs commands directly by default. For contained execution
on Linux, set `"code_sandbox": true` in `~/.jade/config.json`:

- fresh temporary working directory per run, wall-clock timeout, CPU/memory/file-size/process limits, scrubbed environment;
- with **bubblewrap** installed: read-only system mounts and optional network cut-off (`--unshare-net`);
- without it: a restricted-subprocess fallback (limits + temp dir only — honestly **not** a security boundary);
- on macOS/Windows the sandbox reports itself unavailable instead of pretending.

## 🌐 Browser capability (optional, no new dependencies)

`tools/browser.py` fetches pages with the best backend available: Playwright if
installed → system Chromium headless → plain-HTTP text fallback. `fetch_text()`
always works; `screenshot()` and scripted `browse()` need a rendering backend and
say exactly how to install one when missing. The text fallback never claims
JavaScript rendering.

## 🐝 Hive mind, agent swarm, MCP, deep research

- **`/hive <question>`** — asks every connected AI provider the same question
  in parallel and synthesizes one consensus answer with per-model attributions.
  Failed providers are skipped, never faked. Opt-in P2P shared result cache
  (`"hive_cache_sharing": true` in `~/.jade/config.json`, default off) lets
  peers share cached answers keyed by prompt hash so the swarm doesn't pay
  twice; entries carry model + timestamp and expire via `"hive_cache_ttl"`.
  Peers advertise provider/model *names* only — API keys never go over the wire.
- **`/swarm <task>`** — planner breaks the task down, parallel workers execute
  (bounded, per-worker timeouts), reviewer synthesizes. Code-running workers use
  the Linux sandbox; nothing runs unsandboxed by accident.
- **`/mcp`** — minimal MCP client (stdio, stdlib only): `/mcp servers`,
  `/mcp tools [server]`, `/mcp call <server> <tool> '{"args":…}' [--yes]`.
  Configure servers under `"mcp_servers"` in `~/.jade/config.json`; tool calls
  from untrusted servers require confirmation unless `--yes` or allow-listed.
- **`/research <topic>`** — multi-query web research: plans sub-queries,
  fetches pages via the browser module, and compiles a Markdown report where
  every `[n]` citation is validated against a fetched source — out-of-range
  citations are stripped, never fabricated. Bounded (4 queries / 8 pages).

## ⏰ Task scheduler

`/schedule` runs persistent jobs inside the Jade process — no system cron,
no background daemon:

```text
/schedule add --name "nightly pull" --action "/ollama auto-pull" --cron "0 4 * * *"
/schedule add --name "hourly brief" --action "/brief now" --every 3600
/schedule add --name "one report" --action "/research local AI" --at "2026-09-26T09:00:00"
/schedule list        /schedule log
/schedule remove <id>  /schedule run <id>
/schedule pause <id>   /schedule resume <id>
```

Jobs persist in `~/.jade/scheduler_jobs.json` (atomic writes, mode `0600`)
with an append-only run history in `scheduler_runs.jsonl`. Actions are
whitelisted (`/brief`, `/ollama auto-pull`, `/research`, `/health`, and
`shell: …` executed **only** through the sandbox) — anything else is
rejected. Missed runs are logged honestly; missed one-shot jobs are disabled
rather than silently discarded. The scheduler starts when Jade enters its
normal interactive mode.

## 🖥️ Web dashboard

`jade dashboard` (or `/dashboard` inside chat) starts a local web UI —
stdlib `http.server` only, no dependencies:

- binds **loopback (`127.0.0.1`) by default**; `--lan` binds all interfaces
  with an explicit warning;
- per-instance random token stored at `~/.jade/dashboard_token` (`0600`),
  sent as `?token=` or the `X-Jade-Token` header, compared with
  `hmac.compare_digest`;
- live panels read real state: providers, P2P peers, Ollama hosts/models,
  scheduler jobs + recent runs, brief watcher, and a redacted log tail;
- chat posts to `core.brain.think`; a read-only command whitelist
  (`/health`, `/models`, `/schedule list`, `/schedule log`, `/ollama ps`,
  `/ollama stats`, `/brief status`, `/p2p-status`) — nothing else executes.

Flags: `--port 0` (random free port), `--open`, `--lan`.

## 🖥️ Desktop app, overlay & tray

`jade gui` launches the desktop app — the same local dashboard UI in a
native window, plus:

- **Overlay** (`jade overlay` or `/overlay`): a floating Perplexity-style
  glassmorphism panel — ask, get an answer, dismiss with Esc. Summon it
  globally with Ctrl+Shift+Space (needs the optional `keyboard` package).
- **Tray / menu bar / panel icon** (`jade tray` or `/tray`): Windows
  system tray, macOS menu bar, Linux panel applet with quick toggles for
  listening, voice, overlay, and personality (needs optional `pystray`).
- The native window needs optional `pywebview`; without it Jade opens
  the same UI in your browser and tells you exactly what's missing. The CLI
  never requires any GUI package.

## 🎙️ Voice, wake word & personalities

Voice is a plugin architecture (`core/voice/`) — every stage is a swappable
slot with a working default or an honest "unavailable" message:

- **TTS** defaults to your OS-native speech (`say` on macOS, `espeak-ng` /
  `spd-say` on Linux, SAPI on Windows) — no new packages. `/voice
  <profile>` picks a voice profile; profiles are JSON files under
  `core/voice/profiles/`, so new ones drop in.
- **STT** transcribes voice AND general sound-to-text (`SpeechRecognition`
  or offline `faster-whisper`, both optional).
- **"Hey Jade"** (`jade voice-wake on` or `/voice-wake`): opt-in
  wake-word listening that summons the overlay. All detection is local; a
  privacy notice is acknowledged on first enable. Custom `.onnx` models drop
  in via `wakeword_model_path`.
- **Conversation**: continuous mode with barge-in — interrupt her
  mid-sentence and she yields. Push-to-talk and always-listening modes,
  audio device selection, noise-suppression hooks.
- **`core/voice/proprietary/`** (git-ignored): the drop-in home for licensed
  models — e.g. a future authentic Jade voice for the
  `licensed-jade` profile. One config line activates them; they're never
  transmitted anywhere.
- **Personalities**: `jade` (default — loyal, dry wit), `witty`
  (irreverent humor), `clippy` (full paperclip energy 📎), `professional`
  (straight-laced). `/personality <name>`, `jade personality`, the
  `personality` config key, or the dashboard/GUI picker.

## 🦙 Ollama fleet management

```text
/ollama ps          list hosts and their models
/ollama pull <m>    pull a model on every reachable host
/ollama prune       remove unused models (confirms first)
/ollama bench       quick throughput benchmark per host
/ollama stats       usage telemetry recorded by chat sessions
/ollama auto-pull   pull configured models everywhere (great with /schedule)
/connect ollama --host http://other:11434   add hosts to the pool
```

Down servers and missing models report one actionable line
("Couldn't reach Ollama … start it with `ollama serve`",
"Ollama doesn't have model '…' — pull it with `/ollama pull …`")
instead of a traceback or a silent cloud fallback.

## 🧊 Thin client

`/thin` turns a low-resource node into a thin client: the chat UI runs
locally while heavy work is forwarded to a reachable fat peer. If no peer
is reachable it says so plainly instead of pretending.

## ⏳ Conversation time-travel

```text
/rewind [n]        step back n turns (default 1)
/branch <name>     fork the conversation here
/branches          list branches
/diff [a] [b]      show what changed between turns/branches
```

## 📦 Skill sharing

`/skill` installs skill packs shared by other Jade nodes. Every pack is
manifest-verified before anything runs — unverified content is refused,
never executed.

## 📋 Proactive briefs & 🤝 session handoff

```text
/brief now         "while you were away" brief on demand
/brief status      watcher state
/handoff <peer>    hand this live session to another node
```

## ⌨️ Command reference

| Command | What it does |
|---|---|
| `/chat <text>` | Talk to the active provider |
| `/connect [provider]` | Link AI providers (API keys, Ollama, …) |
| `/connections [--test]` | Provider status table, optionally live-tested |
| `/models` | Switch provider / model |
| `/hive <question>` | Ask all connected AIs, get one consensus answer |
| `/swarm <task>` | Parallel planner → workers → reviewer |
| `/mcp` | External tool servers: `servers`, `tools`, `call` |
| `/research <topic>` | Cited multi-query web research report |
| `/schedule` | `add/list/remove/run/pause/resume/log` persistent jobs |
| `/dashboard` | Start the local web dashboard |
| `/gui` | Launch the desktop app (native window) |
| `/overlay` | Floating glassmorphism overlay (global hotkey) |
| `/tray` | System tray / menu bar / panel icon |
| `/voice` | Voice capture, or `/voice <profile>` to pick a TTS voice |
| `/voice-profile [name]` | Pick a TTS voice profile |
| `/voice-wake [on|off]` | "Hey Jade" wake-word listening |
| `/personality [name]` | Pick a personality (jade, witty, clippy, professional) |
| `/ollama` | `ps/pull/prune/bench/stats/auto-pull` fleet management |
| `/thin` | Thin client against a fat peer |
| `/rewind [n]`, `/branch`, `/branches`, `/diff` | Conversation time-travel |
| `/skill` | Install verified skill packs |
| `/brief` | Proactive briefs: `now`, `status` |
| `/handoff <peer>` | Hand the session to another node |
| `/doctor` | System health check & self-repair |
| `/network` | Local network discovery & port scanning |
| `/server` | Monitor ports, processes, services |
| `/hardware` | USB & physical port probing |
| `/ssh <host> <cmd>` | Run commands on remote servers |
| `/memory <query>` | Search persistent knowledge base |
| `/fix`, `/forge`, `/plan`, `/troubleshoot`, `/analyze`, `/nave` | AI engineering agents |
| `/config`, `/personality`, `/prompts`, `/help`, `/menu`, `/exit` | Settings & UI |

## 🛠 Dev mode

For the developer's own machine only — diagnostics and visibility, no behavior changes:

```bash
export JADE_DEV_MODE=1   # or set "dev_mode": true in ~/.jade/config.json
```

When enabled: DEBUG logging, per-request timing with the serving provider/model in the logs, and a `🛠 DEV MODE` banner at REPL startup so it's visually obvious this isn't the public build.

Optional personal instructions: create `~/.jade/dev_instructions.md` with your own notes — they're appended to the system prompt in dev mode. The file lives **outside the repo** (in your home directory) and is never committed; dev mode works fine without it.

### ⚠️ Auto-approve (development toggle)

For the developer's own machine only — answers routine confirmation prompts
`yes` automatically so stacked confirmations don't interrupt a dev session.
**Never enabled by default.** Opt in any of three ways:

```bash
jade --yes ...              # or -y, for one CLI session
export JADE_AUTO_APPROVE=1  # for the shell session
# or set "auto_approve": true in ~/.jade/config.json
```

Startup prints a clear warning: **AUTO-APPROVE ON — all routine prompts will be
answered yes**. Every auto-approved action is logged to
`~/.jade/logs/auto_approve.log` (mode `0600`). Credential and trust
prompts (provider keys, P2P key/filesystem sharing, unverified hosts) always
ask, and the sandbox is never weakened by this toggle.

## 👨‍💻 Created By
**Nave433 (Evan Shipley)**
