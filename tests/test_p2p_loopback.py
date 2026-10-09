"""Track A tests: P2P loopback integration — two localhost peers.

Plaintext and TLS round trips, UDP discovery with advertised ports, and
error/disconnect paths. Uses ephemeral ports and isolated config; no
broadcast needed (discovery is exercised on 127.0.0.1).
"""
import json
import socket
import threading
import time

import pytest
import requests

import core.config as config_mod
import core.p2p as p2p
from core.p2p import (
    _make_p2p_server,
    discover_peer_endpoints,
    generate_self_signed_cert,
    run_udp_discovery_listener,
    send_remote_command,
)


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    fake_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", fake_file)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def peer_factory():
    """Start P2P servers on 127.0.0.1/ephemeral ports; shut them all down."""
    servers = []

    def make(use_tls=False, certfile=None, keyfile=None):
        httpd = _make_p2p_server(0, use_tls=use_tls, certfile=certfile,
                                 keyfile=keyfile, bind="127.0.0.1")
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        servers.append(httpd)
        # serve_forever in a thread: wait until the socket accepts.
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


def _status(peer_ip, port, **kw):
    res = send_remote_command(peer_ip, "status", {}, port=port, **kw)
    assert res["ok"], f"status failed: {res}"
    return json.loads(res["data"])


# ---------------------------------------------------------------- round trips

def test_two_plaintext_peers_roundtrip(isolated_config, peer_factory):
    _, port_a = peer_factory()
    _, port_b = peer_factory()
    assert port_a != port_b

    a = _status("127.0.0.1", port_a)
    b = _status("127.0.0.1", port_b)
    assert a["status"] == "online"
    assert b["status"] == "online"
    assert "version" in a and "name" in b


def test_tls_roundtrip_with_self_signed_cert(isolated_config, peer_factory, tmp_path):
    crt = str(tmp_path / "p2p.crt")
    key = str(tmp_path / "p2p.key")
    generate_self_signed_cert(crt, key)  # openssl CLI; raises if missing

    _, port = peer_factory(use_tls=True, certfile=crt, keyfile=key)
    data = _status("127.0.0.1", port, use_tls=True, verify_tls=False)
    assert data["status"] == "online"


def test_tls_without_cert_raises_helpful_error(isolated_config):
    # Isolated config has no p2p_tls_certfile -> clear, actionable error.
    with pytest.raises(ValueError, match="no certificate is configured"):
        _make_p2p_server(0, use_tls=True, bind="127.0.0.1")


# ---------------------------------------------------------------- discovery

def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ScriptedSocket:
    """Fake UDP socket: replays scripted recvfrom() payloads, records sendto()."""

    def __init__(self, script):
        self._script = list(script)
        self.sent = []

    def setsockopt(self, *a, **k):
        pass

    def settimeout(self, *a, **k):
        pass

    def bind(self, *a, **k):
        pass

    def close(self):
        pass

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        return len(data)

    def recvfrom(self, n):
        if not self._script:
            raise socket.timeout("drained")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_discovery_request_response_protocol(isolated_config, monkeypatch):
    """Listener answers JARVIS_DISCOVERY_REQUEST with name + HTTP port."""
    from core.config import load_config
    scripted = _ScriptedSocket([
        (b"JARVIS_DISCOVERY_REQUEST", ("127.0.0.1", 55555)),
        RuntimeError("stop the loop"),
    ])
    monkeypatch.setattr(p2p.socket, "socket", lambda *a, **k: scripted)
    run_udp_discovery_listener(http_port=12135, discovery_port=19999)
    assert len(scripted.sent) == 1
    payload, addr = scripted.sent[0]
    assert addr == ("127.0.0.1", 55555)
    parts = payload.decode().split("|")
    assert parts[0] == "JARVIS_DISCOVERY_RESPONSE"
    assert parts[1] == load_config().get("jade_name", "Jade")
    assert parts[2] == "12135"  # advertised HTTP port, not discarded


def test_discover_peer_endpoints_parses_advertised_port(monkeypatch):
    """Client side: parses (ip, http_port, name) from discovery responses."""
    scripted = _ScriptedSocket([
        (b"JARVIS_DISCOVERY_RESPONSE|PEER-A|12135", ("127.0.0.1", 40001)),
        (b"JARVIS_DISCOVERY_RESPONSE|PEER-B|12136", ("127.0.0.1", 40002)),
    ])
    monkeypatch.setattr(p2p.socket, "socket", lambda *a, **k: scripted)
    found = discover_peer_endpoints(discovery_port=19999, timeout=2.0,
                                    target="127.0.0.1")
    assert ("127.0.0.1", 12135, "PEER-A") in found
    assert ("127.0.0.1", 12136, "PEER-B") in found
    # The request itself must be the discovery probe.
    assert scripted.sent[0][0] == b"JARVIS_DISCOVERY_REQUEST"


def test_discover_peer_endpoints_tolerates_malformed_response(monkeypatch):
    scripted = _ScriptedSocket([
        (b"GARBAGE", ("127.0.0.1", 40001)),  # ignored: wrong prefix
        (b"JARVIS_DISCOVERY_RESPONSE|ONLY-NAME", ("127.0.0.1", 40002)),
    ])
    monkeypatch.setattr(p2p.socket, "socket", lambda *a, **k: scripted)
    found = discover_peer_endpoints(discovery_port=19999, timeout=2.0,
                                    target="127.0.0.1")
    assert found == [("127.0.0.1", 11435, "ONLY-NAME")]  # default port fallback


def test_udp_discovery_live_loopback(isolated_config):
    """End-to-end UDP discovery on loopback (skipped where UDP is blocked)."""
    discovery_port = _free_udp_port()
    http_port = 12137
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.sendto(b"ping", ("127.0.0.1", discovery_port))
        probe.close()
    except PermissionError:
        pytest.skip("UDP loopback blocked in this environment")
    t = threading.Thread(target=run_udp_discovery_listener,
                         kwargs={"http_port": http_port,
                                 "discovery_port": discovery_port},
                         daemon=True)
    t.start()
    time.sleep(0.3)  # let the listener bind
    found = discover_peer_endpoints(discovery_port=discovery_port,
                                    timeout=2.0, target="127.0.0.1")
    assert ("127.0.0.1", http_port) in [(ip, port) for ip, port, _ in found]


def test_discovery_response_format():
    # Contract both sides rely on.
    resp = b"JARVIS_DISCOVERY_RESPONSE|MY-PEER|12135"
    parts = resp.decode().split("|")
    assert parts[0] == "JARVIS_DISCOVERY_RESPONSE"
    assert parts[1] == "MY-PEER"
    assert int(parts[2]) == 12135


# ---------------------------------------------------------------- error paths

def test_malformed_json_returns_400(isolated_config, peer_factory):
    _, port = peer_factory()
    r = requests.post(f"http://127.0.0.1:{port}", data=b"not-json",
                      headers={"Content-Type": "application/json"}, timeout=5)
    assert r.status_code == 400


def test_empty_body_returns_400(isolated_config, peer_factory):
    _, port = peer_factory()
    r = requests.post(f"http://127.0.0.1:{port}", data=b"", timeout=5)
    assert r.status_code == 400


def test_wrong_p2p_token_returns_401(isolated_config, peer_factory):
    config_mod.CONFIG_FILE.write_text(json.dumps({"p2p_token": "correct-token"}))
    _, port = peer_factory()
    r = requests.post(f"http://127.0.0.1:{port}",
                      json={"action": "list_tokens", "version": "0.1.7",
                            "token": "wrong-token"},
                      timeout=5)
    assert r.status_code == 401


def test_correct_p2p_token_allows_action(isolated_config, peer_factory):
    config_mod.CONFIG_FILE.write_text(json.dumps({"p2p_token": "correct-token"}))
    _, port = peer_factory()
    # status is exempt from the token check; list_tokens is not.
    r = requests.post(f"http://127.0.0.1:{port}",
                      json={"action": "list_tokens", "version": p2p.CURRENT_VERSION,
                            "token": "correct-token"},
                      timeout=5)
    assert r.status_code == 200


def test_disconnect_reports_error_not_hang(isolated_config, peer_factory):
    httpd, port = peer_factory()
    httpd.shutdown()
    httpd.server_close()
    time.sleep(0.2)
    res = send_remote_command("127.0.0.1", "status", {}, port=port)
    assert res["ok"] is False
    assert res["error"]


def test_unreachable_peer_reports_error(isolated_config):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    res = send_remote_command("127.0.0.1", "status", {}, port=free)
    assert res["ok"] is False
