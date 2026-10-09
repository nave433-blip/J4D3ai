# Jade P2P — the complete guide

Jade instances can talk to each other directly: run commands on a peer,
move files, send messages, share memory between your own devices, and find
each other on a LAN or across the internet. This document covers all of it,
including what is **not** possible — stated plainly.

## Contents

1. [Quick start (LAN)](#1-quick-start-lan)
2. [Pairing](#2-pairing)
3. [Global mode (across the internet)](#3-global-mode-across-the-internet)
4. [The discovery server](#4-the-discovery-server)
5. [Security model](#5-security-model)
6. [Ports](#6-ports)
7. [What is NOT possible](#7-what-is-not-possible)
8. [Memory sync](#8-memory-sync)
9. [Action / permission reference](#9-action--permission-reference)
10. [CLI reference](#10-cli-reference)
11. [Troubleshooting](#11-troubleshooting)

## 1. Quick start (LAN)

On machine A (the one you want to reach):

```
jade p2p-server
```

On machine B:

```
jade p2p-peers        # who is around (LAN + global, deduped)
jade p2p-status       # live table of peers
jade p2p-read 192.168.1.20 notes/todo.txt
```

The first time machine B asks machine A for anything beyond `status`,
machine A prints a permission prompt (`Always / This once / Never /
Not right now`). Nothing mutating happens without that approval.

Set a shared token on both machines so strangers on the LAN cannot call
your node (constant-time compared; wrong token → HTTP 401):

```
jade p2p-set-token "a-long-random-secret"
```

## 2. Pairing

Pairing exchanges the shared token so two of your machines trust each
other. It is **LAN-first**: the code carries the token, but it cannot
cross NAT by itself.

On the machine you want to connect **to**:

```
jade p2p-pair
```

This prints a short code (token fingerprint + LAN endpoint). If the
`qrcode` package is installed you also get a scannable QR; otherwise the
plain string. If no P2P token is set yet, pairing generates a strong one
and saves it.

On the machine connecting **from**:

```
jade p2p-join <code>
```

The joiner finds the peer with UDP broadcast discovery, matches it by
node id, stores the shared token (asking before replacing an existing
one), and verifies with an authenticated call.

If the peer is **not** on the LAN, `p2p-join` does not pretend — it
prints exactly what is needed:

1. On the peer's network, port-forward **TCP 11435** to the peer machine.
2. On the peer, set `"p2p_public_endpoint": "<peer-host>:11435"` in
   `~/.jade/config.json` (or use a discovery server, below).
3. Re-run `jade p2p-join <code>`.

## 3. Global mode (across the internet)

Global P2P means peers find each other through a **discovery server**
you run yourself. There is no hosted Jade discovery service.

1. Run the server somewhere reachable (a VPS, home server, Raspberry Pi):

   ```
   jade p2p-discovery --port 11437 --secret "another-long-secret" --persist ~/.jade/discovery-peers.json
   ```

2. On each node, in `~/.jade/config.json`:

   ```json
   {
     "global_p2p_enabled": true,
     "discovery_server_url": "http://your-server:11437",
     "discovery_secret": "another-long-secret",
     "p2p_public_endpoint": "your-public-host:11435"
   }
   ```

3. Each node registers (heartbeats) every 45 seconds; the server drops
   nodes not heard from within the TTL (default 90s).

4. `jade p2p-peers` now lists global peers with source `global`;
   `p2p-status` includes them too.

Direct connections still go peer-to-peer — the discovery server only
introduces nodes, it never relays traffic. Both sides still need TCP
11435 reachable (port-forward or public IP), and the shared `p2p_token`
still authenticates every call.

## 4. The discovery server

`core/p2p_discovery_server.py`, also via `jade p2p-discovery`.

| Endpoint | Method | Body / params | Auth |
|---|---|---|---|
| `/register` | POST | `{"node_id","endpoint","name","capabilities","version"}` | secret if configured |
| `/peers` | GET | — | secret if configured |
| `/health` | GET | — | none |

- **Heartbeat TTL**: default 90s (`--ttl`). A background sweeper prunes
  expired registrations; re-register at ≤ TTL/2 to stay listed
  (`core.global_p2p.start_heartbeat()` does 45s).
- **Shared secret** (`--secret`): required as the `X-Discovery-Secret`
  header or `?secret=` query parameter on `/register` and `/peers`,
  compared in constant time. Without it, registration is open — do not
  expose an unprotected server to the internet.
- **Persistence** (`--persist path.json`): the registry is written
  atomically on every change and reloaded on startup (already-expired
  entries are dropped on load).
- The server stores node ids, endpoints, names, and capability *names*
  only — never API keys.

## 5. Security model

- **Shared token** (`p2p_token`): every action except `status` requires
  it when set; compared with `hmac.compare_digest`. Wrong token → 401.
- **TLS opt-in**: set `"p2p_use_tls": true` plus
  `"p2p_tls_certfile"`/`"p2p_tls_keyfile"` in config. Self-signed is
  fine on a trusted LAN (`core.p2p.generate_self_signed_cert`).
  Default is plaintext HTTP — only run P2P on networks you trust.
- **WAN restrictions**: peers outside private/loopback ranges may only
  call `status` and `think`. Everything else (`read_file`, `edit_file`,
  `file_send`, `file_fetch`, `message`, `get_token`, `p2p_sync_memory`,
  …) is LAN-only (HTTP 403 from a WAN address).
- **Per-peer permissions** (`~/.jade/permissions.json`): the first
  mutating request from an IP prompts `Always / This once / Never /
  Not right now`. Stored decisions are honored silently afterwards.
- **Workspace confinement**: `read_file`, `edit_file`, `file_send`,
  `file_fetch` resolve the requested path and refuse anything outside
  the Jade workspace root (HTTP 403).
- **Capability names only**: `status` advertises provider/model *names*.
  API keys are never advertised or transmitted by the protocol itself
  (`get_token`/`handoff` move keys only with explicit user approval,
  over whatever transport you configured — plaintext by default).
- **Inbox**: capped at 500 entries; messages capped at 10,000 chars.
- **File transfer**: 1MB chunks, 64MB per file; sha256-verified before
  the destination file is replaced atomically. A failed or tampered
  transfer can never leave a partial file behind.

## 6. Ports

| Port | Protocol | Purpose |
|---|---|---|
| 11435 | TCP | P2P HTTP(S) API — the port peers connect to |
| 11436 | UDP | LAN discovery broadcasts (link-local only) |
| 11437 | TCP | Discovery server (only on the machine that runs it) |

Forward **11435/TCP** to reach a node from the internet. 11436/UDP is
for LAN discovery broadcasts and must not be forwarded.

Optional helpers (only if you install the packages — never required):

- `core.p2p_optional.p2p_upnp_map()` — tries a UPnP port mapping via
  `miniupnpc`; returns an honest `{"ok","external_ip","error"}` dict.
- mDNS advertisement/discovery via `zeroconf`
  (`core.p2p_optional.mdns_advertise()` / `mdns_discover()`), picked up
  by `p2p-peers` as an extra `mdns` source when available.

## 7. What is NOT possible

Said plainly, so nobody builds on a false assumption:

- **No TURN relay.** If two nodes cannot open a direct TCP connection
  (both behind NAT with no port-forward and no UPnP), they cannot talk.
  The discovery server introduces peers; it does not relay traffic.
- **No automatic NAT traversal.** Without UPnP succeeding or a manual
  port-forward, inbound internet connections to a node are impossible.
  This is a property of NAT, not a missing feature.
- **No cloud pairing.** A pairing code is a token in a trench coat — it
  does not magically reach a machine across the internet.
- **No end-to-end encryption by default.** The base protocol is HTTP.
  Use the TLS opt-in (or a VPN / SSH tunnel / Tailscale-style overlay)
  if the network is hostile.
- **No remote shell.** There is deliberately no "run arbitrary command"
  action. The closest primitives are `think` (ask the peer's model) and
  `execute_chunk` (a model subtask) — both permission-gated and
  WAN-restricted.

## 8. Memory sync

Sync memory cores (`facts`, `preferences`, `projects`, `episodic`)
between your own devices:

```
# on BOTH machines, in ~/.jade/config.json:
{ "p2p_memory_sync": true }

jade p2p-sync-memory 192.168.1.20 facts push   # send my facts to the peer
jade p2p-sync-memory 192.168.1.20 facts pull   # fetch the peer's facts into mine
```

**Trust assumption — read before enabling:** a peer that passes both
gates (config opt-in on both sides + the `sync_memory` permission,
default deny) can **read and write your memory cores**. Only enable
this between machines you fully trust — normally your own.

## 9. Action / permission reference

Wire actions on the P2P HTTP API (`POST /` with
`{"action","version","token", ...}`). `P2P_FEATURES["0.2.6"]` is the
compatibility list both sides check.

| Action | Params | Permission gate | WAN allowed | Notes |
|---|---|---|---|---|
| `status` | — | none | ✅ | now includes `node_id`, `last_seen` |
| `think` | `task, model, stream` | `think` | ✅ | resource-capped |
| `list_tokens` | — | none | ❌ | capability **names** only |
| `get_token` | `provider` | `get_token` | ❌ | plaintext unless TLS |
| `read_file` | `path` | `read_file` | ❌ | text, workspace-confined |
| `edit_file` | `path, old_string, new_string` | `edit_file` | ❌ | workspace-confined |
| `file_send` | `transfer_id, path, chunk_index, total_chunks, sha256, data_b64` | `file_send` | ❌ | binary, sha256-verified, atomic |
| `file_fetch` | `path, offset, length` | `file_fetch` | ❌ | binary range read |
| `message` | `text, from_node_id` | `message` | ❌ | → `~/.jade/p2p_inbox.json` |
| `p2p_sync_memory` | `mode, core, entries?, limit?` | **`sync_memory`** (new, default deny) | ❌ | + `p2p_memory_sync` opt-in both sides |
| `hive_cache_get/put` | `prompt_hash / entry` | none (own opt-in flag) | ❌ | `hive_cache_sharing` both sides |
| `handoff` | `payload` | `handoff` | ❌ | explicit accept prompt |
| `execute_chunk` | `task` | `execute_chunk` | ❌ | model subtask |
| `skill_offer` | … | `skill_offer` | ❌ | quarantined, never auto-installs |
| `session_handoff` | `payload` | `session_handoff` | ❌ | key-free, explicit accept |

## 10. CLI reference

| Command | What it does |
|---|---|
| `jade p2p-server` | Start the P2P server (this machine accepts requests) |
| `jade p2p-scan` | List peer IPs on the LAN |
| `jade p2p-status` | Live table of peers (LAN + global) |
| `jade p2p-peers` | Merged presence: `lan` / `global` (`mdns`) sources, deduped by node id |
| `jade p2p-pair` | Print a pairing code (+ QR if `qrcode` installed) |
| `jade p2p-join <code>` | Join via pairing code (LAN; honest off-LAN instructions) |
| `jade p2p-set-token <t>` | Set the shared P2P token |
| `jade p2p-tokens` | Browse/request provider tokens from peers |
| `jade p2p-read <ip> <path>` | Read a text file from a peer |
| `jade p2p-edit <ip> <path> <old> <new>` | Edit a text file on a peer |
| `jade p2p-message <ip> <text> [--port]` | Send a message to a peer's inbox |
| `jade p2p-inbox [--limit N] [--clear]` | List / clear this node's inbox |
| `jade p2p-sync-memory <ip> <core> <push\|pull>` | Sync a memory core with your own device |
| `jade p2p-discovery [--port] [--ttl] [--secret] [--persist]` | Run the discovery server |

Python API (same names in `core.p2p`): `send_remote_command`,
`p2p_send_file(peer_ip, local_path, remote_path)`,
`p2p_fetch_file(peer_ip, remote_path, local_path)`,
`p2p_send_message`, `p2p_inbox`, `p2p_inbox_clear`, `p2p_peers`,
`p2p_push_memory`, `p2p_pull_memory`, `discover_peer_endpoints`.

## 11. Troubleshooting

- **Peer not found on LAN**: same subnet? UDP broadcasts do not cross
  routers/VLANs. Some guest Wi-Fi networks block client-to-client
  traffic entirely — nothing Jade-side can fix that.
- **401 Unauthorized**: `p2p_token` mismatch. Re-pair or re-run
  `p2p-set-token` with the same value on both nodes.
- **403 from WAN**: by design — mutating actions are LAN-only.
- **Discovery server shows no peers**: nodes must re-register within
  the TTL (heartbeat is 45s vs 90s default); check `discovery_secret`
  matches on both ends; `/health` shows the live count.
- **Pairing code rejected**: codes are single-peer (bound to a node
  id). Generate a fresh one with `p2p-pair` on the target machine.
- **TLS errors**: clients skip verification by default for self-signed
  LAN certs; pass `verify_tls=True` only with a CA-signed cert.
