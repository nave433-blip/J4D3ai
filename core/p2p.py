import http.server
import socketserver
import json
import os
import base64
import hashlib
import hmac
import ipaddress
import shutil
import ssl
import subprocess
import threading
import time
import socket
import uuid
import requests
from concurrent.futures import ThreadPoolExecutor
from rich.console import Console
from rich.prompt import Confirm, Prompt
from core.approvals import confirm
from pathlib import Path
from core.update import CURRENT_VERSION
from core.config import CONFIG_DIR

console = Console()

# SECURITY NOTICE: the P2P protocol is plaintext HTTP with a shared token.
# API keys requested via "get_token" travel unencrypted. Only enable P2P on
# networks you trust, and use a strong p2p_token.
#
# TLS OPT-IN: to encrypt the P2P transport, set "p2p_use_tls": true in
# ~/.cortana/config.json and point "p2p_tls_certfile"/"p2p_tls_keyfile" at PEM
# files. A self-signed certificate is fine on a trusted LAN; generate one with
#   python3 -c "from core.p2p import generate_self_signed_cert; \
#       generate_self_signed_cert('/path/to/p2p.crt', '/path/to/p2p.key')"
# (equivalently: openssl req -x509 -newkey rsa:2048 -keyout p2p.key -out p2p.crt
#  -days 825 -nodes -subj "/CN=cortana-p2p").
# TLS clients skip certificate verification by default (verify_tls=False) so
# self-signed certs work; pass verify_tls=True when you have a CA-signed cert
# you want validated. Default behavior (plaintext HTTP) is unchanged.

# Remote file operations are confined to the CORTANA workspace root.
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

def _is_path_confined(raw_path: str) -> "Path | None":
    """Resolve a requested path and return it only if it stays inside the workspace."""
    try:
        full = Path(os.path.expanduser(raw_path)).resolve()
    except Exception:
        return None
    try:
        full.relative_to(WORKSPACE_ROOT)
    except ValueError:
        return None
    return full

def _is_private_peer(peer_ip: str) -> bool:
    """Proper private-network classification (the old startswith check missed
    most of 172.16/12 and all IPv6 private ranges)."""
    try:
        ip = ipaddress.ip_address(peer_ip)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False

# P2P Protocol versioning and feature compatibility map
P2P_VERSION = CURRENT_VERSION
P2P_FEATURES = {
    "0.1.7": ["status", "edit_file", "read_file"],
    "0.2.6": ["status", "edit_file", "read_file", "list_tokens", "get_token", "think", "handoff", "execute_chunk",
              "hive_cache_get", "hive_cache_put", "capabilities",
              "skill_offer", "session_handoff",
              "file_send", "file_fetch", "message", "p2p_sync_memory"]
}

def is_p2p_compatible(remote_version: str, action: str = "status") -> bool:
    """Check if the remote CORTANA version supports the requested action."""
    supported_features = P2P_FEATURES.get(remote_version, P2P_FEATURES.get("0.1.7"))
    if action == "status": return True
    return action in supported_features

PERMISSION_FILE = CONFIG_DIR / "permissions.json"

def load_permissions():
    if not PERMISSION_FILE.exists():
        return {}
    try:
        with open(PERMISSION_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_permissions(permissions):
    PERMISSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PERMISSION_FILE, "w") as f:
        json.dump(permissions, f, indent=4)

def check_permission(peer_ip, action):
    permissions = load_permissions()
    peer_perms = permissions.get(peer_ip, {})
    choice = peer_perms.get(action)

    if choice == "always":
        return True
    if choice == "never":
        return False

    console.print(f"\n[bold yellow]⚠️ REMOTE REQUEST:[/bold yellow] CORTANA instance at [cyan]{peer_ip}[/cyan] wants to [bold magenta]{action}[/bold magenta].")
    options = {"1": "always", "2": "this once", "3": "never", "4": "not right now"}
    console.print("[1] Always | [2] This once | [3] Never | [4] Not right now")
    ans = Prompt.ask("Choice", choices=["1", "2", "3", "4"], default="4")
    
    final_choice = options[ans]
    if final_choice == "always":
        peer_perms[action] = "always"
        permissions[peer_ip] = peer_perms
        save_permissions(permissions)
        return True
    if final_choice == "this once":
        return True
    if final_choice == "never":
        peer_perms[action] = "never"
        permissions[peer_ip] = peer_perms
        save_permissions(permissions)
        return False
    return False

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# Binary file transfer state.
#
# file_send uploads arrive as numbered chunks; the server holds them in
# memory keyed by transfer_id until the last chunk arrives, then verifies
# the sha256 of the reassembled bytes and atomically renames a temp file
# into the confined destination. Abandoned transfers are pruned.
# ---------------------------------------------------------------------------
P2P_MAX_CHUNK = 1_000_000      # 1 MB per chunk
P2P_MAX_FILE = 64_000_000      # 64 MB per transfer
P2P_TRANSFER_TTL = 600         # drop incomplete transfers after 10 minutes

_file_transfers = {}  # transfer_id -> {"path", "total_chunks", "chunks", "sha256", "started", "peer"}
_file_transfer_lock = threading.Lock()


def _prune_transfers():
    now = time.time()
    with _file_transfer_lock:
        stale = [tid for tid, t in _file_transfers.items()
                 if now - t["started"] > P2P_TRANSFER_TTL]
        for tid in stale:
            del _file_transfers[tid]

class CortanaP2PHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        peer_ip = self.client_address[0]
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_response(400); self.end_headers(); return

        post_data = self.rfile.read(content_length)
        try:
            data = json.loads(post_data)
        except json.JSONDecodeError:
            self.send_response(400); self.end_headers(); return

        action = data.get("action")
        sender_version = data.get("version", "0.1.0")
        auth_token = data.get("token")

        # Security: Check for P2P Token if configured (constant-time compare)
        from core.config import load_config
        cfg = load_config()
        required_token = cfg.get("p2p_token")
        if required_token and not hmac.compare_digest(str(auth_token), str(required_token)):
            if action != "status":
                self.send_response(401); self.end_headers()
                self.wfile.write(b"Unauthorized: Invalid P2P Token"); return

        # Basic WAN restriction: only private/loopback peers may use mutating actions
        is_local = _is_private_peer(peer_ip)
        if not is_local and action not in ["think", "status"]:
             self.send_response(403); self.end_headers()
             self.wfile.write(b"Forbidden: WAN access restricted."); return

        if not is_p2p_compatible(sender_version, action):
            self.send_response(426); self.end_headers()
            self.wfile.write(f"Incompatible Version: Peer is running {sender_version}, but {action} requires a newer build.".encode()); return
        
        if action == "status":
            from core.resource_manager import resource_manager
            health = resource_manager.check_hive_health()
            self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
            try:
                from core import hive as hive_mod
                capabilities = hive_mod.local_capabilities()
            except Exception:
                capabilities = {"providers": [], "models": [], "features": []}
            try:
                from tools.hardware import get_node_id
                node_id = get_node_id()
            except Exception:
                node_id = "unknown"
            status_data = {
                "status": "online",
                "node_id": node_id,
                "name": cfg.get("cortana_name", "Cortana"),
                "version": CURRENT_VERSION,
                "model": cfg.get("cortana_model", "unknown"),
                "local_models": cfg.get("detected_local_models", []),
                "p2p_enabled": cfg.get("p2p_enabled", True),
                "hive_load": health,
                "last_seen": time.time(),
                # Capability registry: provider/model NAMES only.
                # API keys and tokens are NEVER advertised or transmitted.
                "capabilities": capabilities,
            }
            self.wfile.write(json.dumps(status_data).encode()); return

        # Hive shared result cache. Participation is opt-in on BOTH sides:
        # this node only serves/stores when "hive_cache_sharing" is true.
        # Entries carry prompts, answers, model names, timestamps — never keys.
        if action == "hive_cache_get":
            from core import hive as hive_mod
            if not hive_mod.sharing_enabled():
                self.send_response(403); self.end_headers()
                self.wfile.write(b"Cache sharing is disabled on this node."); return
            h = str(data.get("prompt_hash", ""))
            entries = hive_mod.cache_lookup_local_hash(h) if h else []
            self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({"entries": entries}).encode()); return

        if action == "hive_cache_put":
            from core import hive as hive_mod
            if not hive_mod.sharing_enabled():
                self.send_response(403); self.end_headers()
                self.wfile.write(b"Cache sharing is disabled on this node."); return
            entry = data.get("entry") or {}
            try:
                ok = bool(entry.get("prompt_hash") and entry.get("answer"))
                if ok:
                    hive_mod.cache_store_hash(
                        entry.get("prompt_hash"), entry.get("model", "peer"),
                        entry.get("answer"), entry.get("timestamp") or time.time(),
                        origin=f"peer:{peer_ip}")
            except Exception:
                ok = False
            self.send_response(200 if ok else 400)
            self.send_header('Content-type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({"stored": ok}).encode()); return

        if action == "list_tokens":
            from core.services import list_available_keys
            self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps(list_available_keys()).encode()); return

        # p2p_sync_memory carries its own permission name ("sync_memory") and
        # its own config opt-in; both are enforced inside its branch below.
        if action != "p2p_sync_memory" and not check_permission(peer_ip, action):
            self.send_response(403); self.end_headers()
            self.wfile.write(b"Permission denied"); return

        if action == "get_token":
            from core.services import get_api_key
            key = get_api_key(data.get("provider"))
            if key:
                self.send_response(200); self.send_header('Content-type', 'text/plain'); self.end_headers()
                self.wfile.write(key.encode())
            else:
                self.send_response(404); self.end_headers(); self.wfile.write(b"Token not found")

        elif action == "read_file":
            file_path = data.get("path")
            full_path = _is_path_confined(file_path) if file_path else None
            if full_path is None:
                self.send_response(403); self.end_headers()
                self.wfile.write(b"Forbidden: path is outside the CORTANA workspace"); return
            try:
                if full_path.is_file():
                    with open(full_path, "r") as f: content = f.read()
                    self.send_response(200); self.send_header('Content-type', 'text/plain'); self.end_headers()
                    self.wfile.write(content.encode())
                else:
                    self.send_response(404); self.end_headers(); self.wfile.write(b"File not found")
            except Exception as e:
                self.send_response(500); self.end_headers(); self.wfile.write(str(e).encode())

        elif action == "edit_file":
            file_path, old_s, new_s = data.get("path"), data.get("old_string"), data.get("new_string")
            if not all([file_path, old_s, new_s]):
                self.send_response(400); self.end_headers(); return
            full_path = _is_path_confined(file_path)
            if full_path is None:
                self.send_response(403); self.end_headers()
                self.wfile.write(b"Forbidden: path is outside the CORTANA workspace"); return
            from tools.editor import replace_in_file
            try:
                res = replace_in_file(str(full_path), old_s, new_s, interactive=False)
                self.send_response(200); self.send_header('Content-type', 'text/plain'); self.end_headers()
                self.wfile.write(res.encode())
            except Exception as e:
                self.send_response(500); self.end_headers(); self.wfile.write(str(e).encode())

        elif action == "file_send":
            # Binary upload, one chunk per request:
            # {transfer_id, path, chunk_index, total_chunks, sha256, data_b64}
            # Permission-gated and LAN-only like edit_file (enforced above).
            _prune_transfers()
            tid = str(data.get("transfer_id") or "")
            file_path = data.get("path")
            try:
                chunk_index = int(data.get("chunk_index", -1))
                total_chunks = int(data.get("total_chunks", 0))
            except (TypeError, ValueError):
                chunk_index, total_chunks = -1, 0
            expect_sha = str(data.get("sha256") or "")
            full_path = _is_path_confined(file_path) if file_path else None
            if not tid or full_path is None:
                self.send_response(403); self.end_headers()
                self.wfile.write(b"Forbidden: bad transfer id or path outside the CORTANA workspace"); return
            if total_chunks < 1 or total_chunks > (P2P_MAX_FILE // 1024 + 1) \
                    or not (0 <= chunk_index < total_chunks):
                self.send_response(400); self.end_headers()
                self.wfile.write(b"Bad chunk_index/total_chunks"); return
            try:
                chunk = base64.b64decode(data.get("data_b64") or b"", validate=True)
            except Exception:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"Invalid base64 chunk"); return
            if len(chunk) > P2P_MAX_CHUNK:
                self.send_response(413); self.end_headers()
                self.wfile.write(b"Chunk too large"); return
            with _file_transfer_lock:
                t = _file_transfers.get(tid)
                if t is None:
                    t = {"path": full_path, "total_chunks": total_chunks,
                         "chunks": {}, "sha256": expect_sha,
                         "started": time.time(), "peer": peer_ip}
                    _file_transfers[tid] = t
                if str(t["path"]) != str(full_path) or t["total_chunks"] != total_chunks:
                    self.send_response(409); self.end_headers()
                    self.wfile.write(b"Transfer id reused with different path/size"); return
                t["chunks"][chunk_index] = chunk
                received = len(t["chunks"])
                complete = received == total_chunks
                if complete:
                    blob = b"".join(t["chunks"][i] for i in range(total_chunks))
                    del _file_transfers[tid]
            if complete:
                if len(blob) > P2P_MAX_FILE:
                    self.send_response(413); self.end_headers()
                    self.wfile.write(b"File too large"); return
                if expect_sha and hashlib.sha256(blob).hexdigest() != expect_sha.lower():
                    self.send_response(422); self.end_headers()
                    self.wfile.write(b"SHA256 mismatch: transfer discarded"); return
                try:
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = full_path.with_name(full_path.name + f".p2p-{tid[:8]}.tmp")
                    with open(tmp, "wb") as f:
                        f.write(blob)
                    os.replace(tmp, full_path)  # atomic rename
                except Exception as e:
                    self.send_response(500); self.end_headers()
                    self.wfile.write(str(e).encode()); return
            self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "received": received,
                                         "total_chunks": total_chunks,
                                         "complete": complete}).encode())

        elif action == "file_fetch":
            # Binary download, range read: {path, offset, length} ->
            # {data_b64, offset, total_size, sha256}. Permission-gated and
            # LAN-only like read_file (enforced above).
            file_path = data.get("path")
            full_path = _is_path_confined(file_path) if file_path else None
            if full_path is None:
                self.send_response(403); self.end_headers()
                self.wfile.write(b"Forbidden: path outside the CORTANA workspace"); return
            try:
                offset = max(0, int(data.get("offset", 0)))
                length = int(data.get("length", P2P_MAX_CHUNK))
                length = max(1, min(length, P2P_MAX_CHUNK))
            except (TypeError, ValueError):
                self.send_response(400); self.end_headers()
                self.wfile.write(b"Bad offset/length"); return
            try:
                if not full_path.is_file():
                    self.send_response(404); self.end_headers()
                    self.wfile.write(b"File not found"); return
                total = full_path.stat().st_size
                if total > P2P_MAX_FILE:
                    self.send_response(413); self.end_headers()
                    self.wfile.write(b"File too large"); return
                with open(full_path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read(length)
                digest = hashlib.sha256(full_path.read_bytes()).hexdigest()
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(str(e).encode()); return
            self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({
                "data_b64": base64.b64encode(chunk).decode(),
                "offset": offset, "length": len(chunk),
                "total_size": total, "sha256": digest}).encode())

        elif action == "message":
            # Direct peer message -> local inbox. Permission-gated (above):
            # a peer needs your approval before it can drop messages here.
            text = str(data.get("text") or "").strip()
            if not text:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"Empty message"); return
            if len(text) > P2P_MESSAGE_MAX:
                self.send_response(413); self.end_headers()
                self.wfile.write(b"Message too long"); return
            entry = {
                "from_node_id": str(data.get("from_node_id") or "unknown"),
                "from_ip": peer_ip,
                "timestamp": time.time(),
                "text": text,
            }
            _inbox_store(entry)
            self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "stored": True}).encode())

        elif action == "p2p_sync_memory":
            # Opt-in memory-core sync between your own devices.
            #
            # TRUST ASSUMPTION (read this before enabling): a peer that
            # passes both gates below can PULL (read) and PUSH (write) your
            # memory cores. Only enable with nodes you fully trust —
            # typically your own machines.
            #
            # Gate 1 (both sides): "p2p_memory_sync": true in config.
            # Gate 2 (this side):  the "sync_memory" permission for the
            #   peer's IP — default deny (the prompt defaults to refusing).
            from core import memory_cores as mc_mod
            if not cfg.get("p2p_memory_sync"):
                self.send_response(403); self.end_headers()
                self.wfile.write(b"Memory sync is not enabled on this node "
                                 b"(set 'p2p_memory_sync': true to opt in)."); return
            if not check_permission(peer_ip, "sync_memory"):
                self.send_response(403); self.end_headers()
                self.wfile.write(b"Permission denied (sync_memory)"); return
            mode = str(data.get("mode") or "")
            core = str(data.get("core") or "")
            if core not in mc_mod.CORES:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"Unknown memory core"); return
            if mode == "push":
                entries = data.get("entries") or []
                if not isinstance(entries, list) or len(entries) > 200:
                    self.send_response(400); self.end_headers()
                    self.wfile.write(b"entries must be a list of at most 200"); return
                stored = 0
                for e in entries:
                    text = (e.get("text") if isinstance(e, dict) else "") or ""
                    if text.strip():
                        r = mc_mod.remember(core, text[:4000],
                                           source=f"p2p:{peer_ip}",
                                           scope="p2p-sync")
                        if r.get("ok"):
                            stored += 1
                self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "stored": stored}).encode())
            elif mode == "pull":
                try:
                    limit = max(1, min(int(data.get("limit", 50)), 200))
                except (TypeError, ValueError):
                    limit = 50
                entries = mc_mod.recall(core, limit=limit)
                self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "core": core,
                                             "entries": entries}).encode())
            else:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"mode must be 'push' or 'pull'"); return

        elif action == "think":
            from core.resource_manager import resource_manager
            if not resource_manager.check_hive_health()["safe"]:
                self.send_response(503); self.end_headers()
                self.wfile.write(b"Hive Mind Busy: Resource cap reached."); return
            
            task, model, stream = data.get("task"), data.get("model"), data.get("stream", False)
            try:
                if stream:
                    from core.brain import think_stream
                    self.send_response(200); self.send_header('Content-type', 'text/event-stream'); self.end_headers()
                    for chunk in think_stream("P2P Remote Task", task, model=model):
                        self.wfile.write(chunk.encode()); self.wfile.flush()
                else:
                    from core.brain import think_structured
                    res = think_structured("P2P Remote Task", task, model=model)
                    self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                    self.wfile.write(json.dumps(res).encode())
            except Exception as e:
                if not stream: self.send_response(500); self.end_headers()
                self.wfile.write(str(e).encode())

        elif action == "handoff":
            payload = data.get("payload", {})
            keys = payload.get("keys", {})
            fs_edits = payload.get("fs_edits", [])
            console.print(f"\n[bold magenta]🚀 INCOMING HANDOFF FROM {peer_ip}[/bold magenta]")
            if confirm("Accept this handoff?"):
                from core.services import set_api_key
                for provider, key in keys.items():
                    if confirm(f"Accept {provider} key?", sensitive=True): set_api_key(provider, key)
                self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
            else:
                self.send_response(403); self.end_headers(); self.wfile.write(b"Rejected")

        elif action == "execute_chunk":
            from core.brain import think_structured
            res = think_structured("P2P Swarm Chunk", data.get("task"))
            self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps(res).encode())

        elif action == "skill_offer":
            # P2P skill sharing: quarantine + verify + explicit local accept.
            # Never auto-installs. Added by the ollama-edge round.
            from core.skillshare import handle_skill_offer
            status, ctype, body = handle_skill_offer(peer_ip, data)
            self.send_response(status); self.send_header('Content-type', ctype); self.end_headers()
            self.wfile.write(body)

        elif action == "session_handoff":
            # Cross-device session handoff: key-free payload, explicit accept.
            # Added by the ollama-edge round.
            from core.handoff import handle_session_handoff
            status, ctype, body = handle_session_handoff(peer_ip, data.get("payload", {}))
            self.send_response(status); self.send_header('Content-type', ctype); self.end_headers()
            self.wfile.write(body)

# ---------------------------------------------------------------------------
# Optional TLS (stdlib ssl). All helpers are opt-in; defaults stay plaintext.
# ---------------------------------------------------------------------------

def generate_self_signed_cert(certfile, keyfile=None, hostname="cortana-p2p"):
    """Generate a self-signed cert/key pair using the openssl CLI.

    Uses an argv list (no shell=True), so paths cannot inject commands.
    Returns (certfile, keyfile) as strings.
    """
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError(
            "openssl CLI not found; install OpenSSL or supply your own PEM files."
        )
    certfile, keyfile = str(certfile), str(keyfile or certfile)
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048",
         "-keyout", keyfile, "-out", certfile,
         "-days", "825", "-nodes", "-subj", f"/CN={hostname}"],
        check=True, capture_output=True, text=True,
    )
    return certfile, keyfile


def _tls_enabled(use_tls, cfg):
    """Resolve the effective TLS flag: explicit argument wins, else config."""
    if use_tls is None:
        use_tls = bool(cfg.get("p2p_use_tls", False))
    return use_tls


def _p2p_scheme(use_tls, cfg):
    return "https" if _tls_enabled(use_tls, cfg) else "http"


def _build_ssl_context(certfile, keyfile=None):
    """Build a server-side SSLContext from PEM cert/key files."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile or certfile)
    return ctx


def _quiet_insecure_warnings():
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _make_p2p_server(port=0, use_tls=False, certfile=None, keyfile=None, bind="127.0.0.1"):
    """Create (but do not start) a configured P2P HTTP(S) server.

    port=0 lets the OS pick a free port (read it back from
    server.server_address[1]). Used by run_p2p_server() and by tests that
    need deterministic startup/shutdown without the UDP discovery listener.
    """
    from core.config import load_config
    cfg = load_config()
    httpd = ThreadedHTTPServer((bind, port), CortanaP2PHandler)
    if use_tls:
        certfile = certfile or cfg.get("p2p_tls_certfile")
        keyfile = keyfile or cfg.get("p2p_tls_keyfile") or certfile
        if not certfile:
            raise ValueError(
                "P2P TLS is enabled but no certificate is configured. "
                "Set 'p2p_tls_certfile' (and 'p2p_tls_keyfile') in "
                "~/.cortana/config.json or pass certfile=/keyfile= explicitly."
            )
        httpd.socket = _build_ssl_context(certfile, keyfile).wrap_socket(
            httpd.socket, server_side=True
        )
    return httpd


def run_p2p_server(port=11435, use_tls=None, certfile=None, keyfile=None,
                   discovery_port=11436):
    """Start the P2P HTTP(S) server.

    TLS is opt-in: pass use_tls=True, or set "p2p_use_tls": true in the config
    together with "p2p_tls_certfile"/"p2p_tls_keyfile". With no flag and no
    config, the server stays plaintext HTTP exactly as before.

    discovery_port selects the UDP discovery port (default 11436); pass None
    to skip the discovery listener (useful when several peers share a host,
    e.g. in tests).
    """
    from core.config import load_config
    cfg = load_config()
    use_tls = _tls_enabled(use_tls, cfg)
    if discovery_port is not None:
        threading.Thread(target=run_udp_discovery_listener,
                         args=(port,), kwargs={"discovery_port": discovery_port},
                         daemon=True).start()
    scheme = "https" if use_tls else "http"
    with _make_p2p_server(port, use_tls=use_tls, certfile=certfile,
                          keyfile=keyfile, bind="") as httpd:
        console.print(f"[green]🚀 CORTANA P2P Server listening on port {port} ({scheme})...[/green]")
        httpd.serve_forever()

def run_udp_discovery_listener(http_port, discovery_port=11436):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(('', discovery_port))
        while True:
            data, addr = sock.recvfrom(1024)
            if data == b"JARVIS_DISCOVERY_REQUEST":
                from core.config import load_config
                name = load_config().get("cortana_name", "Cortana")
                sock.sendto(f"JARVIS_DISCOVERY_RESPONSE|{name}|{http_port}".encode(), addr)
    except Exception as e: console.print(f"[dim]UDP Discovery Error: {e}[/dim]")
    finally: sock.close()

def discover_peer_endpoints(discovery_port=11436, timeout=1.5, target='<broadcast>'):
    """UDP discovery returning [(ip, http_port, name)].

    Parses the advertised HTTP port from each JARVIS_DISCOVERY_RESPONSE
    (format: b"JARVIS_DISCOVERY_RESPONSE|<name>|<http_port>"). Responses that
    do not parse fall back to the default P2P port 11435. `target` is the
    address the request is sent to (broadcast by default; 127.0.0.1 lets
    tests exercise discovery deterministically on loopback).
    """
    found = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if target == '<broadcast>':
        # Broadcast capability is only needed for actual broadcasts; a
        # unicast probe (e.g. target="127.0.0.1" in tests) must not require
        # it — some sandboxes forbid SO_BROADCAST sockets entirely.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(b"JARVIS_DISCOVERY_REQUEST", (target, discovery_port))
        start = time.time()
        while time.time() - start < timeout:
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                break
            if not data.startswith(b"JARVIS_DISCOVERY_RESPONSE"):
                continue
            parts = data.decode(errors="replace").split("|")
            name = parts[1] if len(parts) > 1 else "unknown"
            try:
                http_port = int(parts[2]) if len(parts) > 2 else 11435
            except ValueError:
                http_port = 11435
            found.append((addr[0], http_port, name))
    except Exception:
        pass
    finally:
        sock.close()
    return found


def scan_for_cortana_peer_endpoints(port=11435, timeout=1.5, use_tls=None,
                                   verify_tls=False, discovery_port=11436):
    """Discover peers as (ip, http_port) pairs.

    UDP-discovered peers are probed at their *advertised* port (previously the
    advertised port was discarded, so peers on non-default ports could never
    be reached through discovery). The subnet fallback probes `port`.
    """
    from core.config import load_config
    cfg = load_config()
    scheme = _p2p_scheme(use_tls, cfg)
    if scheme == "https" and not verify_tls:
        _quiet_insecure_warnings()
    found = set()

    for ip, adv_port, _name in discover_peer_endpoints(discovery_port, timeout):
        try:
            r = requests.post(f"{scheme}://{ip}:{adv_port}",
                              json={"action": "status"},
                              timeout=0.3, verify=verify_tls)
            if r.status_code == 200:
                found.add((ip, adv_port))
        except Exception:
            pass

    # Subnet Fallback
    from tools.network import get_local_ip
    local_ip = get_local_ip()
    prefix = ".".join(local_ip.split(".")[:-1]) + "."
    def check_peer(ip):
        try:
            r = requests.post(f"{scheme}://{ip}:{port}", json={"action": "status"},
                              timeout=0.3, verify=verify_tls)
            if r.status_code == 200: return (ip, port)
        except Exception: pass
        return None
    with ThreadPoolExecutor(max_workers=50) as executor:
        for res in executor.map(check_peer, [prefix + str(i) for i in range(1, 255)]):
            if res: found.add(res)

    # Global Registry Fallback
    try:
        from core.global_p2p import get_global_peers
        for peer in get_global_peers():
            ep = peer.get("endpoint")
            if ep: found.add((ep, port))
    except Exception: pass

    return sorted(found)


def scan_for_cortana_peers(port=11435, timeout=1.5, use_tls=None, verify_tls=False,
                          discovery_port=11436):
    """Backwards-compatible wrapper returning just the peer IPs."""
    return sorted({ip for ip, _port in scan_for_cortana_peer_endpoints(
        port, timeout, use_tls, verify_tls, discovery_port)})


def _parse_endpoint(endpoint, default_port=11435):
    """Split a discovery-server endpoint ('host', 'host:port', 'http://h:p')
    into (host, port). Returns (None, None) when unparseable."""
    if not endpoint:
        return None, None
    ep = str(endpoint).strip()
    if "://" in ep:
        ep = ep.split("://", 1)[1]
    ep = ep.rstrip("/")
    if ":" in ep and not ep.startswith("["):
        host, _, port_s = ep.rpartition(":")
        try:
            return host or None, int(port_s)
        except ValueError:
            return ep, default_port
    return ep or None, default_port


def p2p_peers(port=11435, timeout=1.5, use_tls=None, verify_tls=False,
              discovery_port=11436, target='<broadcast>'):
    """Merged presence list: LAN peers (UDP discovery) + global peers
    (discovery server), deduplicated by node_id.

    Returns a list of dicts, each shaped like::

        {"node_id": ..., "name": ..., "version": ...,
         "sources": ["lan", "global"],          # one or both
         "lan_endpoint": {"ip": ..., "port": ...} | None,
         "global_endpoint": {"host": ..., "port": ...} | None,
         "last_seen": <unix ts> | None}

    ``last_seen`` for LAN peers is the time of the last successful status
    probe; for global peers it is the discovery server's heartbeat stamp.
    Deliberately skips the slow /24 subnet sweep that
    :func:`scan_for_cortana_peer_endpoints` performs — presence is about who
    announced themselves, not a port scan.
    """
    from core.config import load_config
    cfg = load_config()
    scheme = _p2p_scheme(use_tls, cfg)
    if scheme == "https" and not verify_tls:
        _quiet_insecure_warnings()

    merged = {}  # node_id -> entry

    def _entry(node_id):
        return merged.setdefault(node_id, {
            "node_id": node_id, "name": "unknown", "version": "unknown",
            "sources": [], "lan_endpoint": None, "global_endpoint": None,
            "last_seen": None,
        })

    # --- LAN: UDP discovery, then a status probe for identity ---------------
    lan_found = discover_peer_endpoints(discovery_port, timeout, target=target)

    def _probe(item):
        ip, adv_port, adv_name = item
        entry = {"ip": ip, "port": adv_port, "name": adv_name,
                 "node_id": f"lan:{ip}:{adv_port}", "version": "unknown",
                 "last_seen": None}
        try:
            r = requests.post(f"{scheme}://{ip}:{adv_port}",
                              json={"action": "status"}, timeout=1.0,
                              verify=verify_tls)
            if r.status_code == 200:
                data = r.json()
                entry["node_id"] = str(data.get("node_id") or entry["node_id"])
                entry["name"] = data.get("name", adv_name)
                entry["version"] = data.get("version", "unknown")
                entry["last_seen"] = time.time()
        except Exception:
            pass
        return entry

    with ThreadPoolExecutor(max_workers=20) as ex:
        probed = list(ex.map(_probe, lan_found))
    for p in probed:
        e = _entry(p["node_id"])
        e["name"] = p["name"] if e["name"] == "unknown" else e["name"]
        e["version"] = p["version"]
        e["lan_endpoint"] = {"ip": p["ip"], "port": p["port"]}
        if p["last_seen"]:
            e["last_seen"] = p["last_seen"]
        if "lan" not in e["sources"]:
            e["sources"].append("lan")

    # --- mDNS (optional; skipped silently when zeroconf is absent) --------
    try:
        from core.p2p_optional import mdns_discover
        mdns_hits = mdns_discover(timeout=min(timeout, 2.0)) or []
    except Exception:
        mdns_hits = []
    for hit in mdns_hits:
        if not isinstance(hit, dict):
            continue
        node_id = str(hit.get("node_id") or "").strip()
        if not node_id:
            node_id = f"mdns:{hit.get('host')}:{hit.get('port')}"
        e = _entry(node_id)
        if hit.get("name"):
            e["name"] = hit["name"] if e["name"] == "unknown" else e["name"]
        if hit.get("version"):
            e["version"] = hit["version"]
        e["lan_endpoint"] = e["lan_endpoint"] or {"ip": hit.get("host"),
                                                 "port": hit.get("port")}
        if "mdns" not in e["sources"]:
            e["sources"].append("mdns")

    # --- global: discovery server registry ----------------------------------
    try:
        from core.global_p2p import get_global_peers
        global_peers = get_global_peers()
    except Exception:
        global_peers = []
    for gp in global_peers:
        if not isinstance(gp, dict):
            continue
        node_id = str(gp.get("node_id") or "").strip()
        if not node_id:
            continue
        host, gport = _parse_endpoint(gp.get("endpoint"), default_port=port)
        e = _entry(node_id)
        e["name"] = gp.get("name") or e["name"]
        if gp.get("version"):
            e["version"] = gp["version"]
        e["global_endpoint"] = {"host": host, "port": gport}
        if gp.get("last_seen"):
            e["last_seen"] = gp["last_seen"]
        if "global" not in e["sources"]:
            e["sources"].append("global")

    return sorted(merged.values(), key=lambda e: e["node_id"])

def send_remote_command(peer_ip, action, params, port=11435, use_tls=None, verify_tls=False):
    """POST a command to a peer.

    TLS is opt-in (use_tls=True or "p2p_use_tls": true in config); the scheme
    then becomes https. verify_tls=False (default) skips certificate
    verification so self-signed LAN certs work — set it True for CA-signed
    certs you want validated.
    """
    from core.config import load_config
    cfg = load_config()
    scheme = _p2p_scheme(use_tls, cfg)
    if scheme == "https" and not verify_tls:
        _quiet_insecure_warnings()
    url = f"{scheme}://{peer_ip}:{port}"
    data = {"action": action, "version": CURRENT_VERSION, "token": cfg.get("p2p_token"), **params}
    stream = params.get("stream", False)
    try:
        r = requests.post(url, json=data, timeout=30, stream=stream, verify=verify_tls)
        if r.status_code == 200:
            return {"ok": True, "stream": r.iter_content(chunk_size=None)} if stream else {"ok": True, "data": r.text}
        return {"ok": False, "error": f"Error {r.status_code}: {r.text}"}
    except Exception as e: return {"ok": False, "error": str(e)}

def p2p_send_file(peer_ip, local_path, remote_path, port=11435, use_tls=None,
                 verify_tls=False, chunk_size=P2P_MAX_CHUNK):
    """Upload a local file to a peer in numbered chunks.

    Returns {"ok": True, "bytes": N, "sha256": hex} on success. The peer
    verifies the sha256 of the reassembled file before atomically moving it
    into place, so a short or corrupted transfer can never leave a partial
    file at the destination.
    """
    local = Path(os.path.expanduser(local_path))
    if not local.is_file():
        return {"ok": False, "error": f"Local file not found: {local_path}"}
    blob = local.read_bytes()
    if len(blob) > P2P_MAX_FILE:
        return {"ok": False, "error": f"File exceeds {P2P_MAX_FILE} byte P2P limit"}
    digest = hashlib.sha256(blob).hexdigest()
    tid = uuid.uuid4().hex
    chunk_size = max(1024, min(int(chunk_size), P2P_MAX_CHUNK))
    chunks = [blob[i:i + chunk_size] for i in range(0, len(blob), chunk_size)] or [b""]
    for idx, chunk in enumerate(chunks):
        res = send_remote_command(peer_ip, "file_send", {
            "transfer_id": tid, "path": remote_path,
            "chunk_index": idx, "total_chunks": len(chunks),
            "sha256": digest,
            "data_b64": base64.b64encode(chunk).decode(),
        }, port=port, use_tls=use_tls, verify_tls=verify_tls)
        if not res["ok"]:
            return {"ok": False, "error": f"chunk {idx}: {res['error']}"}
        try:
            ack = json.loads(res["data"])
        except Exception:
            return {"ok": False, "error": f"chunk {idx}: bad ack"}
        if not ack.get("ok"):
            return {"ok": False, "error": f"chunk {idx}: rejected by peer"}
    if not ack.get("complete"):
        return {"ok": False, "error": "peer never acknowledged completion"}
    return {"ok": True, "bytes": len(blob), "sha256": digest}


def p2p_fetch_file(peer_ip, remote_path, local_path, port=11435, use_tls=None,
                  verify_tls=False, chunk_size=P2P_MAX_CHUNK):
    """Download a file from a peer with range reads, verifying sha256.

    Writes to a temp file and atomically renames into place, so a failed or
    tampered download never leaves a partial file behind.
    """
    local = Path(os.path.expanduser(local_path))
    chunk_size = max(1024, min(int(chunk_size), P2P_MAX_CHUNK))
    tmp = local.with_name(local.name + ".p2p-dl.tmp")
    offset, total, expect_sha = 0, None, None
    try:
        with open(tmp, "wb") as f:
            while True:
                res = send_remote_command(peer_ip, "file_fetch", {
                    "path": remote_path, "offset": offset, "length": chunk_size,
                }, port=port, use_tls=use_tls, verify_tls=verify_tls)
                if not res["ok"]:
                    raise RuntimeError(res["error"])
                try:
                    msg = json.loads(res["data"])
                except Exception:
                    raise RuntimeError("bad chunk response")
                if total is None:
                    total = int(msg["total_size"])
                    expect_sha = str(msg["sha256"])
                    if total > P2P_MAX_FILE:
                        raise RuntimeError("remote file exceeds P2P size limit")
                f.write(base64.b64decode(msg["data_b64"]))
                offset += int(msg["length"])
                if offset >= total or not msg["length"]:
                    break
        blob = tmp.read_bytes()
        if hashlib.sha256(blob).hexdigest() != (expect_sha or "").lower():
            raise RuntimeError("SHA256 mismatch: download discarded")
        local.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, local)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    return {"ok": True, "bytes": len(blob), "sha256": expect_sha}

# ---------------------------------------------------------------------------
# Peer direct messaging. Inbox lives at ~/.cortana/p2p_inbox.json, capped at
# 500 entries (newest kept). Path is resolved at call time so tests can
# isolate it by monkeypatching core.config.CONFIG_DIR.
# ---------------------------------------------------------------------------
P2P_INBOX_CAP = 500
P2P_MESSAGE_MAX = 10_000  # max characters per message


def _inbox_file():
    from core.config import CONFIG_DIR
    return CONFIG_DIR / "p2p_inbox.json"


def _inbox_read():
    path = _inbox_file()
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _inbox_store(entry):
    path = _inbox_file()
    entries = _inbox_read()
    entries.append(entry)
    entries = entries[-P2P_INBOX_CAP:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=2))
        tmp.replace(path)
    except Exception:
        pass
    return entry


def p2p_inbox(limit=50):
    """Read the local P2P message inbox (newest first)."""
    entries = _inbox_read()
    entries.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
    return entries[:max(1, int(limit))]


def p2p_inbox_clear():
    """Empty the local P2P message inbox. Returns the number cleared."""
    entries = _inbox_read()
    try:
        _inbox_file().write_text("[]")
    except Exception:
        pass
    return len(entries)


# ---------------------------------------------------------------------------
# Opt-in memory sync between your own devices.
#
# TRUST ASSUMPTION: both nodes must set "p2p_memory_sync": true, and the
# receiving node must grant the "sync_memory" permission to the peer's IP.
# A peer with that permission can read AND write your memory cores — only
# ever grant it to machines you fully trust (normally your own).
# ---------------------------------------------------------------------------

def _memory_sync_opted_in():
    from core.config import load_config
    return bool(load_config().get("p2p_memory_sync"))


def p2p_push_memory(peer_ip, core, query="", limit=50, port=11435,
                    use_tls=None, verify_tls=False):
    """Push matching entries from a local memory core to a peer.

    Both sides must have "p2p_memory_sync": true, and the peer must grant
    this IP the "sync_memory" permission.
    """
    if not _memory_sync_opted_in():
        return {"ok": False, "error": "local opt-in missing: set "
                "'p2p_memory_sync': true in ~/.cortana/config.json"}
    from core import memory_cores as mc_mod
    if core not in mc_mod.CORES:
        return {"ok": False, "error": f"unknown memory core '{core}'"}
    entries = mc_mod.recall(core, query=query, limit=max(1, min(limit, 200)))
    texts = [{"text": e.get("text", "")} for e in entries if e.get("text")]
    res = send_remote_command(peer_ip, "p2p_sync_memory",
                             {"mode": "push", "core": core, "entries": texts},
                             port=port, use_tls=use_tls, verify_tls=verify_tls)
    if not res["ok"]:
        return res
    try:
        return {"ok": True, "data": json.loads(res["data"])}
    except Exception:
        return {"ok": True, "data": res["data"]}


def p2p_pull_memory(peer_ip, core, limit=50, port=11435,
                    use_tls=None, verify_tls=False):
    """Pull a peer's memory-core entries into the local core of the same name.

    Pulled entries are stored locally with source "p2p:<peer_ip>". Same
    double opt-in as :func:`p2p_push_memory`.
    """
    if not _memory_sync_opted_in():
        return {"ok": False, "error": "local opt-in missing: set "
                "'p2p_memory_sync': true in ~/.cortana/config.json"}
    from core import memory_cores as mc_mod
    if core not in mc_mod.CORES:
        return {"ok": False, "error": f"unknown memory core '{core}'"}
    res = send_remote_command(peer_ip, "p2p_sync_memory",
                             {"mode": "pull", "core": core, "limit": limit},
                             port=port, use_tls=use_tls, verify_tls=verify_tls)
    if not res["ok"]:
        return res
    try:
        payload = json.loads(res["data"])
    except Exception:
        return {"ok": False, "error": "bad pull response"}
    stored = 0
    for e in payload.get("entries") or []:
        text = (e.get("text") if isinstance(e, dict) else "") or ""
        if text.strip():
            r = mc_mod.remember(core, text[:4000],
                               source=f"p2p:{peer_ip}", scope="p2p-sync")
            if r.get("ok"):
                stored += 1
    return {"ok": True, "pulled": len(payload.get("entries") or []),
            "stored": stored}


def p2p_send_message(peer_ip, text, port=11435, use_tls=None, verify_tls=False):
    """Send a direct text message to a peer's inbox."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    if len(text) > P2P_MESSAGE_MAX:
        return {"ok": False, "error": f"message exceeds {P2P_MESSAGE_MAX} chars"}
    try:
        from tools.hardware import get_node_id
        node_id = get_node_id()
    except Exception:
        node_id = "unknown"
    res = send_remote_command(peer_ip, "message",
                             {"text": text, "from_node_id": node_id},
                             port=port, use_tls=use_tls, verify_tls=verify_tls)
    if not res["ok"]:
        return res
    try:
        return {"ok": True, "data": json.loads(res["data"])}
    except Exception:
        return {"ok": True, "data": res["data"]}


def p2p_status_report(port=11435, use_tls=None, verify_tls=False):
    from rich.table import Table
    from rich.live import Live
    from core.config import load_config
    cfg = load_config()
    scheme = _p2p_scheme(use_tls, cfg)
    if scheme == "https" and not verify_tls:
        _quiet_insecure_warnings()
    table = Table(title="CORTANA P2P Swarm Status", border_style="cyan")
    table.add_column("Peer IP"); table.add_column("Name"); table.add_column("Version"); table.add_column("Compatibility"); table.add_column("Model"); table.add_column("Latency")
    console.print("[bold cyan]📡 Discovering CORTANA instances...[/bold cyan]")
    with Live(table, refresh_per_second=4):
        peers = scan_for_cortana_peer_endpoints(port=port, use_tls=use_tls,
                                               verify_tls=verify_tls)
        for ip, peer_port in peers:
            start = time.time()
            try:
                r = requests.post(f"{scheme}://{ip}:{peer_port}", json={"action": "status"}, timeout=0.8, verify=verify_tls)
                if r.status_code == 200:
                    data = r.json()
                    lat = f"{(time.time() - start)*1000:.1f}ms"
                    sync = "[green]✓ MATCH[/green]" if data.get("version") == CURRENT_VERSION else "[yellow]⚠ LEGACY[/yellow]"
                    table.add_row(ip, data.get("name"), data.get("version"), sync, data.get("model"), lat)
            except Exception: pass
    if peers:
        console.print("[dim]Chief, we're connected to the battlenet.[/dim]")

def p2p_token_menu():
    console.print("[yellow]⚠️ Tokens are transferred over plaintext HTTP. "
                  "Only request tokens from peers on networks you trust.[/yellow]")
    peers = scan_for_cortana_peers()
    if not peers: console.print("[yellow]No peers found.[/yellow]"); return
    table = Table(title="Peer Tokens")
    table.add_column("ID"); table.add_column("IP"); table.add_column("Tokens")
    peer_map = {}
    for i, ip in enumerate(peers):
        res = send_remote_command(ip, "list_tokens", {})
        if res["ok"]:
            tokens = json.loads(res["data"])
            table.add_row(str(i+1), ip, ", ".join(tokens))
            peer_map[str(i+1)] = {"ip": ip, "tokens": tokens}
    console.print(table)
    choice = Prompt.ask("Select ID to request token", default="b")
    if choice in peer_map:
        p = peer_map[choice]
        t_choice = Prompt.ask("Select token", choices=p["tokens"])
        res = send_remote_command(p["ip"], "get_token", {"provider": t_choice})
        if res["ok"]:
            from core.services import set_api_key
            set_api_key(t_choice, res["data"])
            console.print("[green]✅ Token received![/green]")

def start_server_background(port=11435, use_tls=None, certfile=None, keyfile=None,
                            discovery_port=11436):
    t = threading.Thread(target=run_p2p_server,
                         kwargs={"port": port, "use_tls": use_tls,
                                 "certfile": certfile, "keyfile": keyfile,
                                 "discovery_port": discovery_port},
                         daemon=True)
    t.start()
    return t


# Deprecated pre-rename aliases (the wire protocol is unchanged, so these
# only matter for Python callers importing the old names).
JarvisP2PHandler = CortanaP2PHandler


def scan_for_jarvis_peers(*args, **kwargs):
    """Deprecated alias of :func:`scan_for_cortana_peers`."""
    return scan_for_cortana_peers(*args, **kwargs)


def scan_for_jarvis_peer_endpoints(*args, **kwargs):
    """Deprecated alias of :func:`scan_for_cortana_peer_endpoints`."""
    return scan_for_cortana_peer_endpoints(*args, **kwargs)
