"""Self-hostable rendezvous (discovery) server for Jade global P2P.

This is the server that :mod:`core.global_p2p` talks to. There is no hosted
Jade discovery service — run this yourself on any machine reachable by
your nodes (a VPS, a home server, a Raspberry Pi) and point each node's
``discovery_server_url`` config at it::

    jade p2p-discovery --port 11437
    # on each node, in ~/.jade/config.json:
    #   "global_p2p_enabled": true,
    #   "discovery_server_url": "http://your-server:11437"

Protocol
--------
* ``POST /register`` — JSON body ``{"node_id", "endpoint", "name",
  "capabilities", "version"}``. Acts as a heartbeat: re-register at least
  once per TTL to stay listed. Returns ``{"ok": true}``.
* ``GET /peers`` — returns ``{"peers": [<registrations>] }``; expired
  entries are pruned first. Each entry carries ``node_id, endpoint, name,
  capabilities, version, last_seen`` (unix timestamp).
* ``GET /health`` — ``{"ok": true, "peers": N}``.

Security
--------
* Optional shared secret: when the server is started with a secret,
  ``/register`` and ``/peers`` require it, supplied either as the
  ``X-Discovery-Secret`` header or the ``?secret=`` query parameter, and
  compared with :func:`hmac.compare_digest` (constant time).
* The server stores no API keys — only node ids, endpoints, names, and
  capability *names* (exactly what :mod:`core.global_p2p` sends).

Stdlib only. No authentication beyond the optional shared secret, so do not
expose an unprotected discovery server to the open internet.
"""
import hmac
import http.server
import json
import socketserver
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DISCOVERY_DEFAULT_PORT = 11437
PEER_TTL_SECONDS = 90


class ThreadedDiscoveryServer(socketserver.ThreadingMixIn,
                              http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DiscoveryRegistry:
    """In-memory peer registry with TTL expiry and optional JSON persistence."""

    def __init__(self, ttl=PEER_TTL_SECONDS, persist_path=None, secret=None):
        self.ttl = max(1, int(ttl))
        self.persist_path = Path(persist_path) if persist_path else None
        self.secret = secret
        self._peers = {}  # node_id -> record dict
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sweeper = None
        if self.persist_path:
            self._load()

    # -- persistence ----------------------------------------------------
    def _load(self):
        try:
            raw = json.loads(self.persist_path.read_text())
        except Exception:
            return
        now = time.time()
        with self._lock:
            for rec in raw if isinstance(raw, list) else []:
                if not isinstance(rec, dict) or not rec.get("node_id"):
                    continue
                if now - float(rec.get("last_seen", 0)) > self.ttl:
                    continue  # already expired while we were down
                self._peers[str(rec["node_id"])] = rec

    def _save_locked(self):
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self._peers.values()), indent=2))
            tmp.replace(self.persist_path)  # atomic rename
        except Exception:
            pass  # persistence is best-effort; the registry keeps working

    # -- registry ops ---------------------------------------------------
    def register(self, node_id, endpoint, name, capabilities, version):
        node_id = str(node_id or "").strip()
        if not node_id:
            raise ValueError("node_id is required")
        record = {
            "node_id": node_id,
            "endpoint": endpoint,
            "name": name or "Jade",
            "capabilities": capabilities or {},
            "version": version or "unknown",
            "last_seen": time.time(),
        }
        with self._lock:
            self._peers[node_id] = record
            self._save_locked()
        return record

    def peers(self):
        """Return live peers, pruning expired entries first."""
        now = time.time()
        with self._lock:
            expired = [nid for nid, rec in self._peers.items()
                       if now - float(rec.get("last_seen", 0)) > self.ttl]
            for nid in expired:
                del self._peers[nid]
            if expired:
                self._save_locked()
            return sorted(self._peers.values(),
                          key=lambda r: r.get("last_seen", 0), reverse=True)

    def check_secret(self, presented):
        """Constant-time shared-secret check. Open server when no secret set."""
        if not self.secret:
            return True
        if not presented:
            return False
        return hmac.compare_digest(str(presented), str(self.secret))

    # -- background expiry sweeper --------------------------------------
    def start_sweeper(self, interval=None):
        if self._sweeper and self._sweeper.is_alive():
            return
        interval = interval or max(10, self.ttl // 3)
        self._stop.clear()

        def _sweep():
            while not self._stop.wait(interval):
                self.peers()  # pruning happens inside

        self._sweeper = threading.Thread(target=_sweep, daemon=True,
                                         name="discovery-sweeper")
        self._sweeper.start()

    def stop_sweeper(self):
        self._stop.set()


class DiscoveryHandler(http.server.BaseHTTPRequestHandler):
    server_version = "JadeDiscovery/1.0"

    @property
    def _registry(self) -> DiscoveryRegistry:
        return self.server.registry  # set by make_discovery_server()

    def _presented_secret(self):
        header = self.headers.get("X-Discovery-Secret")
        if header:
            return header
        qs = parse_qs(urlparse(self.path).query)
        vals = qs.get("secret")
        return vals[0] if vals else None

    def _require_secret(self):
        if self._registry.check_secret(self._presented_secret()):
            return True
        self.send_response(401)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error": "invalid or missing discovery secret"}')
        return False

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter than BaseHTTPRequestHandler
        pass

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"ok": True,
                                  "peers": len(self._registry.peers())})
            return
        if path == "/peers":
            if not self._require_secret():
                return
            self._send_json(200, {"peers": self._registry.peers()})
            return
        self._send_json(404, {"error": "unknown path"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/register":
            self._send_json(404, {"error": "unknown path"})
            return
        if not self._require_secret():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            self._send_json(400, {"error": "missing or oversized body"})
            return
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(400, {"error": "invalid JSON"})
            return
        try:
            record = self._registry.register(
                data.get("node_id"), data.get("endpoint"),
                data.get("name"), data.get("capabilities"),
                data.get("version"))
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        self._send_json(200, {"ok": True, "node_id": record["node_id"]})


def make_discovery_server(port=0, ttl=PEER_TTL_SECONDS, secret=None,
                          persist_path=None, bind=""):
    """Create (but do not start) a discovery server. port=0 picks a free port."""
    registry = DiscoveryRegistry(ttl=ttl, persist_path=persist_path,
                                 secret=secret)
    httpd = ThreadedDiscoveryServer((bind, port), DiscoveryHandler)
    httpd.registry = registry
    return httpd


def run_discovery_server(port=DISCOVERY_DEFAULT_PORT, ttl=PEER_TTL_SECONDS,
                         secret=None, persist_path=None):
    """Blocking: serve the discovery registry forever (with expiry sweeper)."""
    httpd = make_discovery_server(port, ttl=ttl, secret=secret,
                                  persist_path=persist_path)
    httpd.registry.start_sweeper()
    bound = httpd.server_address[1]
    print(f"Jade discovery server on port {bound} "
          f"(TTL {httpd.registry.ttl}s"
          f"{', secret protected' if secret else ', open registration'}"
          f"{', persisting to ' + str(persist_path) if persist_path else ''})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.registry.stop_sweeper()
        httpd.server_close()


def start_discovery_server_background(port=0, ttl=PEER_TTL_SECONDS,
                                      secret=None, persist_path=None):
    """Start a discovery server on a daemon thread; returns the server object.

    Used by tests and by embedders. Callers should call
    ``server.registry.stop_sweeper(); server.shutdown(); server.server_close()``
    when done.
    """
    httpd = make_discovery_server(port, ttl=ttl, secret=secret,
                                  persist_path=persist_path, bind="127.0.0.1")
    httpd.registry.start_sweeper(interval=1)
    t = threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="discovery-server")
    t.start()
    return httpd
