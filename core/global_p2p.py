import requests
import json
import threading
import time
from core.config import load_config
from core.update import CURRENT_VERSION
from tools.hardware import get_node_id
from rich.console import Console

console = Console()

# Discovery server URL. There is no hosted JADE discovery service;
# set "discovery_server_url" in the config to point at your own
# (see core/p2p_discovery_server.py, or `jade p2p-discovery`).
DISCOVERY_SERVER_URL = "https://jade-discovery-server.example.com"

# Re-register at half the server TTL so a missed heartbeat never drops us.
HEARTBEAT_INTERVAL = 45


def _server_url(cfg) -> str | None:
    url = cfg.get("discovery_server_url") or DISCOVERY_SERVER_URL
    if "example.com" in url:
        return None  # placeholder was never replaced with a real server
    if url.startswith("http://"):
        # node_id/endpoint registrations would travel in plaintext
        console.print("[yellow]Global P2P: discovery server URL uses plaintext "
                      "HTTP — node registrations are unencrypted. Prefer https.[/yellow]")
    return url


def _secret_headers(cfg) -> dict:
    """Forward the optional discovery shared secret (constant-time compared
    server-side). The secret itself is never logged or advertised."""
    secret = cfg.get("discovery_secret")
    return {"X-Discovery-Secret": str(secret)} if secret else {}


def _local_capabilities():
    try:
        from core import hive as hive_mod
        return hive_mod.local_capabilities()
    except Exception:
        return {"providers": [], "models": [], "features": []}


def register_node():
    cfg = load_config()
    if not cfg.get("global_p2p_enabled"):
        return None

    url = _server_url(cfg)
    if url is None:
        console.print("[yellow]Global P2P: no discovery server configured "
                      "(set 'discovery_server_url' in config). Skipping registration.[/yellow]")
        return False

    node_id = get_node_id()
    # Capability registry: provider/model NAMES only.
    # API keys and tokens are NEVER advertised or transmitted.
    payload = {
        "node_id": node_id,
        "endpoint": cfg.get("p2p_public_endpoint"),
        "name": cfg.get("jade_name", "Jade"),
        "capabilities": _local_capabilities(),
        "version": CURRENT_VERSION,
    }

    try:
        response = requests.post(f"{url}/register", json=payload,
                                 headers=_secret_headers(cfg), timeout=5)
        return response.status_code == 200
    except Exception as e:
        console.print(f"[red]Global P2P registration failed: {e}[/red]")
        return False


def get_global_peers():
    cfg = load_config()
    url = _server_url(cfg)
    if url is None:
        return []
    try:
        response = requests.get(f"{url}/peers", headers=_secret_headers(cfg),
                                timeout=5)
        if response.status_code == 200:
            data = response.json()
            # Server returns {"peers": [...]}; tolerate a bare list too.
            if isinstance(data, dict):
                return data.get("peers", [])
            return data if isinstance(data, list) else []
        return []
    except Exception:
        return []


_heartbeat_thread = None
_heartbeat_stop = threading.Event()


def start_heartbeat(interval=HEARTBEAT_INTERVAL):
    """Keep this node's discovery registration alive in the background.

    Re-registers every `interval` seconds (default 45s, i.e. half the
    server's 90s TTL). Idempotent — calling it twice does not start a second
    thread. Returns the thread, or None if global P2P is not configured.
    """
    global _heartbeat_thread
    cfg = load_config()
    if not cfg.get("global_p2p_enabled") or _server_url(cfg) is None:
        return None
    if _heartbeat_thread and _heartbeat_thread.is_alive():
        return _heartbeat_thread
    _heartbeat_stop.clear()

    def _loop():
        while not _heartbeat_stop.wait(interval):
            try:
                register_node()
            except Exception:
                pass

    _heartbeat_thread = threading.Thread(target=_loop, daemon=True,
                                         name="p2p-discovery-heartbeat")
    _heartbeat_thread.start()
    return _heartbeat_thread


def stop_heartbeat():
    _heartbeat_stop.set()
