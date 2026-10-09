"""P2P expansion tests: discovery server, binary transfer, messaging,
presence merge, pairing, memory sync, guarded optionals.

All network tests run on loopback with ephemeral ports and isolated
config; nothing touches the real ~/.jade or the real network.
"""
import base64
import hashlib
import importlib.util
import json
import os
import socket
import threading
import time

import pytest
import requests

import core.config as config_mod
import core.p2p as p2p
from core.p2p import _make_p2p_server, run_udp_discovery_listener


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    fake_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", fake_file)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    # p2p.py binds PERMISSION_FILE/WORKSPACE_ROOT at import time.
    monkeypatch.setattr(p2p, "PERMISSION_FILE", tmp_path / "permissions.json")
    monkeypatch.setattr(p2p, "WORKSPACE_ROOT", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    return tmp_path


@pytest.fixture()
def peer_factory():
    servers = []

    def make():
        httpd = _make_p2p_server(0, bind="127.0.0.1")
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        servers.append(httpd)
        port = httpd.server_address[1]
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                s.close()
                break
            except OSError:
                time.sleep(0.05)
        return httpd, port

    yield make
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _grant(tmp_path, *actions):
    perms = {"127.0.0.1": {a: "always" for a in actions}}
    (tmp_path / "permissions.json").write_text(json.dumps(perms))


def _discovery_server(ttl=90, secret=None):
    from core.p2p_discovery_server import start_discovery_server_background
    srv = start_discovery_server_background(ttl=ttl, secret=secret)
    return srv, srv.server_address[1]


def _stop_discovery(srv):
    srv.registry.stop_sweeper()
    srv.shutdown()
    srv.server_close()


def _udp_loopback_works():
    """True when this environment permits UDP loopback traffic.

    The sandbox blocks UDP sendto entirely (the pre-existing
    test_udp_discovery_live_loopback skips for the same reason).
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.sendto(b"ping", ("127.0.0.1", port))
        s.settimeout(1.0)
        s.recvfrom(64)
        s.close()
        return True
    except Exception:
        return False


class _LanDiscovery:
    """Context-ish helper: real UDP discovery listener when the environment
    allows it, otherwise a stubbed discover_peer_endpoints returning the
    live HTTP peer. Either way p2p_peers()/join_pairing() exercise the real
    status-probe + merge + join logic over loopback TCP."""

    def __init__(self, monkeypatch, http_port):
        self.monkeypatch = monkeypatch
        self.http_port = http_port
        self.udp_port = None
        self.live = _udp_loopback_works()

    def __enter__(self):
        if self.live:
            self.udp_port = _free_udp_port()
            t = threading.Thread(
                target=run_udp_discovery_listener,
                kwargs={"http_port": self.http_port,
                        "discovery_port": self.udp_port},
                daemon=True)
            t.start()
            time.sleep(0.3)
            return {"discovery_port": self.udp_port, "target": "127.0.0.1"}
        # Stub only the UDP broadcast; the HTTP status probe stays real.
        port = self.http_port
        self.monkeypatch.setattr(
            p2p, "discover_peer_endpoints",
            lambda *a, **k: [("127.0.0.1", port, "LANPeer")])
        return {}

    def __exit__(self, *exc):
        return False


def _use_discovery(tmp_path, port, secret=None):
    cfg = {"discovery_server_url": f"http://127.0.0.1:{port}"}
    if secret:
        cfg["discovery_secret"] = secret
    (tmp_path / "config.json").write_text(json.dumps(cfg))


# ------------------------------------------------- discovery server tests

def test_discovery_register_and_peers_protocol_match(isolated_config, tmp_path):
    """global_p2p.register_node()/get_global_peers() work against the real
    server: the protocol mismatch (missing capabilities/version) is fixed."""
    srv, port = _discovery_server()
    try:
        _use_discovery(tmp_path, port)
        cfg = json.loads((tmp_path / "config.json").read_text())
        cfg["global_p2p_enabled"] = True
        cfg["jade_name"] = "ProtoNode"
        (tmp_path / "config.json").write_text(json.dumps(cfg))

        from core.global_p2p import register_node, get_global_peers
        assert register_node() is True
        peers = get_global_peers()
        assert len(peers) == 1
        rec = peers[0]
        assert rec["name"] == "ProtoNode"
        assert rec["version"] == p2p.CURRENT_VERSION  # version now sent
        assert isinstance(rec["capabilities"], dict)  # capabilities now sent
        assert rec["last_seen"] > 0
    finally:
        _stop_discovery(srv)


def test_discovery_ttl_expiry(tmp_path):
    srv, port = _discovery_server(ttl=2)
    try:
        requests.post(f"http://127.0.0.1:{port}/register",
                      json={"node_id": "short-lived", "endpoint": "x", "name": "n"},
                      timeout=5)
        assert len(requests.get(f"http://127.0.0.1:{port}/peers",
                                timeout=5).json()["peers"]) == 1
        time.sleep(3.5)
        assert requests.get(f"http://127.0.0.1:{port}/peers",
                            timeout=5).json()["peers"] == []
    finally:
        _stop_discovery(srv)


def test_discovery_secret_enforced(tmp_path):
    srv, port = _discovery_server(secret="topsecret")
    try:
        base = f"http://127.0.0.1:{port}"
        assert requests.post(base + "/register",
                             json={"node_id": "a"}, timeout=5).status_code == 401
        assert requests.get(base + "/peers", timeout=5).status_code == 401
        r = requests.post(base + "/register",
                          json={"node_id": "a", "endpoint": "e", "name": "n"},
                          headers={"X-Discovery-Secret": "topsecret"}, timeout=5)
        assert r.status_code == 200
        r = requests.get(base + "/peers?secret=topsecret", timeout=5)
        assert r.status_code == 200 and len(r.json()["peers"]) == 1
    finally:
        _stop_discovery(srv)


def test_discovery_persistence(tmp_path):
    pp = tmp_path / "peers.json"
    srv, port = _discovery_server()
    srv.registry.persist_path = pp
    try:
        requests.post(f"http://127.0.0.1:{port}/register",
                      json={"node_id": "keepme", "endpoint": "e", "name": "n"},
                      timeout=5)
        assert pp.exists()
    finally:
        _stop_discovery(srv)
    srv2, port2 = _discovery_server()
    srv2.registry.persist_path = pp
    srv2.registry._load()
    try:
        peers = requests.get(f"http://127.0.0.1:{port2}/peers",
                             timeout=5).json()["peers"]
        assert [p["node_id"] for p in peers] == ["keepme"]
    finally:
        _stop_discovery(srv2)


def test_discovery_heartbeat_keeps_node_listed(isolated_config, tmp_path):
    from core import global_p2p
    srv, port = _discovery_server(ttl=3)
    try:
        _use_discovery(tmp_path, port)
        cfg = json.loads((tmp_path / "config.json").read_text())
        cfg["global_p2p_enabled"] = True
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        global_p2p.start_heartbeat(interval=1)
        time.sleep(5)  # several heartbeats inside a 3s TTL
        assert len(global_p2p.get_global_peers()) == 1
    finally:
        global_p2p.stop_heartbeat()
        _stop_discovery(srv)


# ------------------------------------------------------- file transfer

def test_file_send_fetch_binary_roundtrip(isolated_config, peer_factory, tmp_path):
    _grant(tmp_path, "file_send", "file_fetch")
    _, port = peer_factory()
    blob = os.urandom(2_500_000)  # binary, forces ~40 chunks at 64k
    src = tmp_path / "src.bin"
    src.write_bytes(blob)
    remote = str(tmp_path / "ws" / "uploads" / "blob.bin")

    res = p2p.p2p_send_file("127.0.0.1", str(src), remote, port=port,
                            chunk_size=65536)
    assert res["ok"], res
    assert res["sha256"] == hashlib.sha256(blob).hexdigest()
    assert (tmp_path / "ws" / "uploads" / "blob.bin").read_bytes() == blob

    dst = tmp_path / "dst.bin"
    res = p2p.p2p_fetch_file("127.0.0.1", remote, str(dst), port=port,
                             chunk_size=65536)
    assert res["ok"], res
    assert dst.read_bytes() == blob
    assert res["sha256"] == hashlib.sha256(blob).hexdigest()


def test_file_send_rejects_sha256_mismatch(isolated_config, peer_factory, tmp_path):
    _grant(tmp_path, "file_send")
    _, port = peer_factory()
    remote = str(tmp_path / "ws" / "evil.bin")
    blob = b"tampered-content"
    tid = "mismatch-test-tid"
    r = requests.post(f"http://127.0.0.1:{port}", json={
        "action": "file_send", "version": p2p.CURRENT_VERSION,
        "transfer_id": tid, "path": remote,
        "chunk_index": 0, "total_chunks": 1,
        "sha256": "0" * 64,  # wrong on purpose
        "data_b64": base64.b64encode(blob).decode(),
    }, timeout=10)
    assert r.status_code == 422
    assert not (tmp_path / "ws" / "evil.bin").exists()


def test_file_transfer_rejects_path_outside_workspace(isolated_config, peer_factory, tmp_path):
    _grant(tmp_path, "file_send", "file_fetch")
    _, port = peer_factory()
    res = p2p.p2p_send_file("127.0.0.1", str(tmp_path / "config.json"),
                            "/etc/jade-evil.bin", port=port)
    assert not res["ok"]
    res = p2p.p2p_fetch_file("127.0.0.1", "/etc/hostname",
                             str(tmp_path / "o.bin"), port=port)
    assert not res["ok"]


def test_file_transfer_needs_permission(isolated_config, peer_factory, tmp_path):
    # permissions.json seeded with "never": no interactive prompt, 403.
    (tmp_path / "permissions.json").write_text(
        json.dumps({"127.0.0.1": {"file_send": "never"}}))
    _, port = peer_factory()
    src = tmp_path / "a.bin"
    src.write_bytes(b"data")
    res = p2p.p2p_send_file("127.0.0.1", str(src),
                            str(tmp_path / "ws" / "a.bin"), port=port)
    assert not res["ok"] and "403" in res["error"]


# ------------------------------------------------------------- messaging

def test_message_inbox_roundtrip(isolated_config, peer_factory, tmp_path):
    _grant(tmp_path, "message")
    _, port = peer_factory()
    res = p2p.p2p_send_message("127.0.0.1", "hello from peer", port=port)
    assert res["ok"], res
    msgs = p2p.p2p_inbox()
    assert len(msgs) == 1
    assert msgs[0]["text"] == "hello from peer"
    assert msgs[0]["from_ip"] == "127.0.0.1"
    assert msgs[0]["timestamp"] > 0
    assert p2p.p2p_inbox_clear() == 1
    assert p2p.p2p_inbox() == []


def test_inbox_cap_and_empty_message(isolated_config, peer_factory, tmp_path):
    _grant(tmp_path, "message")
    _, port = peer_factory()
    res = p2p.p2p_send_message("127.0.0.1", "   ", port=port)
    assert not res["ok"]
    for i in range(p2p.P2P_INBOX_CAP + 10):
        p2p._inbox_store({"from_node_id": "x", "from_ip": "y",
                          "timestamp": float(i), "text": f"m{i}"})
    stored = p2p._inbox_read()
    assert len(stored) == p2p.P2P_INBOX_CAP
    assert stored[0]["text"] == "m10"  # oldest evicted, newest kept


# -------------------------------------------------------------- presence

def test_status_includes_node_id_and_last_seen(isolated_config, peer_factory):
    _, port = peer_factory()
    res = p2p.send_remote_command("127.0.0.1", "status", {}, port=port)
    assert res["ok"]
    data = json.loads(res["data"])
    assert data["node_id"] and data["node_id"] != "unknown"
    assert data["last_seen"] > 0


def test_p2p_peers_merges_lan_and_global_dedup(isolated_config, peer_factory,
                                               tmp_path, monkeypatch):
    _, http_port = peer_factory()

    # Same node_id registered globally -> one merged entry, both sources.
    from tools.hardware import get_node_id
    srv, dport = _discovery_server()
    try:
        _use_discovery(tmp_path, dport)
        srv.registry.register(get_node_id(), "10.9.9.9:11435",
                              "GlobalAlias", {}, p2p.CURRENT_VERSION)
        with _LanDiscovery(monkeypatch, http_port) as lan_kw:
            peers = p2p.p2p_peers(timeout=1.5, **lan_kw)
        match = [e for e in peers if e["node_id"] == get_node_id()]
        assert len(match) == 1, peers
        entry = match[0]
        assert set(entry["sources"]) == {"lan", "global"}
        assert entry["lan_endpoint"] == {"ip": "127.0.0.1", "port": http_port}
        assert entry["global_endpoint"]["host"] == "10.9.9.9"
        assert entry["last_seen"] and entry["last_seen"] > 0
    finally:
        _stop_discovery(srv)


def test_p2p_peers_empty_without_peers(isolated_config, tmp_path):
    peers = p2p.p2p_peers(discovery_port=_free_udp_port(),
                          target="127.0.0.1", timeout=0.5)
    assert peers == []


# --------------------------------------------------------------- pairing

def test_pairing_code_roundtrip(isolated_config, tmp_path):
    from core import p2p_pairing as pairing
    code, info = pairing.make_pairing_code()
    assert code.startswith(pairing.PAIRING_PREFIX)
    assert len(info["token_fingerprint"]) == 12
    payload = pairing.parse_pairing_code(code)
    assert payload["token"] == json.loads(
        (tmp_path / "config.json").read_text())["p2p_token"]
    assert payload["node_id"] == info["node_id"]
    with pytest.raises(ValueError):
        pairing.parse_pairing_code("not-a-code")
    with pytest.raises(ValueError):
        pairing.parse_pairing_code(code[:-4] + "!!!!")


def test_pairing_join_on_lan(isolated_config, peer_factory, tmp_path, monkeypatch):
    from core import p2p_pairing as pairing
    _, http_port = peer_factory()

    (tmp_path / "config.json").write_text(json.dumps({"jade_name": "PairTarget"}))
    code, info = pairing.make_pairing_code(port=http_port)
    assert info["token_generated"]  # fresh isolated config had no token

    with _LanDiscovery(monkeypatch, http_port) as lan_kw:
        res = pairing.join_pairing(code, port=http_port, timeout=1.5,
                                   assume_yes=True, **lan_kw)
    assert res["ok"], res
    assert res["peer"] == "PairTarget"
    assert res["ip"] == "127.0.0.1" and res["port"] == http_port
    # token persisted so future calls authenticate
    assert json.loads((tmp_path / "config.json").read_text())["p2p_token"] == \
        pairing.parse_pairing_code(code)["token"]


def test_pairing_join_off_lan_is_honest(isolated_config, tmp_path):
    from core import p2p_pairing as pairing
    code, _ = pairing.make_pairing_code()
    res = pairing.join_pairing(code, discovery_port=_free_udp_port(),
                               target="127.0.0.1", timeout=0.5,
                               assume_yes=True)
    assert not res["ok"]
    assert "port-forward" in res["hint"]
    assert "11435" in res["hint"]
    assert "p2p_public_endpoint" in res["hint"]


def test_pairing_qr_graceful_without_lib():
    from core import p2p_pairing as pairing
    if importlib.util.find_spec("qrcode") is None:
        assert pairing.render_pairing_qr("CP1-abc") is None
    else:
        qr = pairing.render_pairing_qr("CP1-abc")
        assert qr and isinstance(qr, str)


# ------------------------------------------------------------ memory sync

@pytest.fixture()
def memory_isolated(isolated_config, tmp_path, monkeypatch):
    import core.memory_cores as mc_mod
    monkeypatch.setattr(mc_mod, "CORES_DIR", tmp_path / "memory" / "cores")
    return tmp_path


def _opt_in_memory(tmp_path):
    cfg = json.loads((tmp_path / "config.json").read_text()) \
        if (tmp_path / "config.json").exists() else {}
    cfg["p2p_memory_sync"] = True
    (tmp_path / "config.json").write_text(json.dumps(cfg))


def test_memory_sync_client_refuses_without_local_opt_in(memory_isolated, peer_factory, tmp_path):
    _, port = peer_factory()
    res = p2p.p2p_push_memory("127.0.0.1", "facts", port=port)
    assert not res["ok"] and "opt-in" in res["error"]
    res = p2p.p2p_pull_memory("127.0.0.1", "facts", port=port)
    assert not res["ok"] and "opt-in" in res["error"]


def test_memory_sync_server_refuses_without_opt_in(memory_isolated, peer_factory, tmp_path):
    _grant(tmp_path, "sync_memory")
    _, port = peer_factory()
    r = requests.post(f"http://127.0.0.1:{port}", json={
        "action": "p2p_sync_memory", "version": p2p.CURRENT_VERSION,
        "mode": "pull", "core": "facts", "limit": 5}, timeout=10)
    assert r.status_code == 403  # server-side opt-in missing


def test_memory_sync_server_refuses_without_permission(memory_isolated, peer_factory, tmp_path):
    _opt_in_memory(tmp_path)
    (tmp_path / "permissions.json").write_text(
        json.dumps({"127.0.0.1": {"sync_memory": "never"}}))  # default deny
    _, port = peer_factory()
    r = requests.post(f"http://127.0.0.1:{port}", json={
        "action": "p2p_sync_memory", "version": p2p.CURRENT_VERSION,
        "mode": "pull", "core": "facts", "limit": 5}, timeout=10)
    assert r.status_code == 403


def test_memory_sync_push_pull_roundtrip(memory_isolated, peer_factory, tmp_path):
    import core.memory_cores as mc_mod
    _opt_in_memory(tmp_path)
    _grant(tmp_path, "sync_memory")
    _, port = peer_factory()

    mc_mod.remember("facts", "my dog is named Biscuit")
    res = p2p.p2p_push_memory("127.0.0.1", "facts", port=port)
    assert res["ok"], res
    assert res["data"]["stored"] >= 1
    synced = [e for e in mc_mod.recall("facts", limit=50)
              if e.get("scope") == "p2p-sync"]
    assert any("Biscuit" in e["text"] and e["source"].startswith("p2p:")
               for e in synced)

    mc_mod.remember("preferences", "peer likes dark mode", source="user")
    res = p2p.p2p_pull_memory("127.0.0.1", "preferences", port=port)
    assert res["ok"], res
    assert res["pulled"] >= 1 and res["stored"] >= 1


def test_memory_sync_rejects_unknown_core(memory_isolated, peer_factory, tmp_path):
    _opt_in_memory(tmp_path)
    _grant(tmp_path, "sync_memory")
    _, port = peer_factory()
    assert not p2p.p2p_push_memory("127.0.0.1", "nope", port=port)["ok"]


# --------------------------------------------------- guarded optionals

def test_upnp_absent_is_honest():
    from core import p2p_optional as opt
    if importlib.util.find_spec("miniupnpc") is None:
        r = opt.p2p_upnp_map()
        assert r["ok"] is False and "miniupnpc" in r["error"]
        r = opt.p2p_upnp_unmap()
        assert r["ok"] is False
    else:
        miniupnpc = pytest.importorskip("miniupnpc")
        r = opt.p2p_upnp_map(port=19999)  # no gateway expected in CI
        assert set(r) == {"ok", "external_ip", "error"}


def test_mdns_absent_is_honest():
    from core import p2p_optional as opt
    if importlib.util.find_spec("zeroconf") is None:
        assert opt.mdns_advertise() is None
        assert opt.mdns_discover(timeout=0.1) is None
    else:
        pytest.importorskip("zeroconf")
        handle = opt.mdns_advertise(port=19998, name="TestMDNS")
        try:
            assert handle is not None
            hits = opt.mdns_discover(timeout=2.0)
            assert any(h.get("port") == 19998 for h in hits)
        finally:
            handle.close()


# ------------------------------------------------------------ CLI surface

def test_cli_registers_new_p2p_commands():
    import cli as cli_mod
    names = set()
    for cmd in cli_mod.app.registered_commands:
        names.add(cmd.name or "")
        # typer CommandInfo may nest; also check the callback name
        cb = getattr(cmd, "callback", None)
        if cb is not None:
            names.add(cb.__name__.replace("_", "-"))
    for expected in ("p2p-discovery", "p2p-pair", "p2p-join", "p2p-message",
                     "p2p-inbox", "p2p-peers", "p2p-sync-memory"):
        assert expected in names, f"missing CLI command: {expected} ({sorted(names)})"


# ------------------------------------------------------------ version self-compatibility

def test_current_version_negotiates_full_feature_set():
    """P2P_VERSION must exist in P2P_FEATURES: two peers on the current
    version must agree on the full action set (no silent fallback to the
    0.1.7 minimal set after a version bump)."""
    from core.p2p import P2P_VERSION, P2P_FEATURES, is_p2p_compatible
    assert P2P_VERSION in P2P_FEATURES, f"{P2P_VERSION} missing from P2P_FEATURES"
    for action in ("file_send", "file_fetch", "message", "p2p_sync_memory",
                   "think", "execute_chunk", "skill_offer"):
        assert is_p2p_compatible(P2P_VERSION, action), action
