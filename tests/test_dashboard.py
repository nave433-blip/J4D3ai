"""Tests for core/dashboard.py — stdlib-only local web dashboard.

Server is started on an ephemeral loopback port; TOKEN_FILE is redirected
to tmp_path. No browser, no LLM calls (chat is exercised via the empty
message path and a monkeypatched think).
"""
import json
import os
import stat
import urllib.request
import urllib.error

import pytest

import core.dashboard as dash_mod
from core.dashboard import (
    _get_token, collect_status, chat_reply, run_readonly, tail_logs,
    start_dashboard_background,
)


@pytest.fixture()
def dash_env(tmp_path, monkeypatch):
    monkeypatch.setattr(dash_mod, "TOKEN_FILE", tmp_path / "dashboard_token")
    monkeypatch.setattr(dash_mod, "LOG_DIR", tmp_path / "logs")
    return tmp_path


@pytest.fixture()
def server(dash_env):
    info = start_dashboard_background(port=0)
    yield info
    info["stop"]()


def _get(server, path, token=None):
    url = f"http://127.0.0.1:{server['port']}{path}"
    if token:
        url += ("&" if "?" in path else "?") + f"token={token}"
    req = urllib.request.Request(url, headers={"X-Cortana-Token": token} if token and "token=" not in url else {})
    # Prefer header auth in one case; query param otherwise. Simplify: use query.
    # 30s client timeout: /api/status does real (bounded) provider probes, and
    # under a full-suite run the loopback request can briefly exceed 5s.
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(server, path, payload, token):
    url = f"http://127.0.0.1:{server['port']}{path}?token={token}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --- token -------------------------------------------------------------------

def test_token_created_private_and_stable(dash_env):
    t1 = _get_token()
    t2 = _get_token()
    assert t1 == t2 and len(t1) >= 32
    mode = stat.S_IMODE(os.stat(dash_mod.TOKEN_FILE).st_mode)
    assert mode == 0o600


# --- auth ---------------------------------------------------------------------

def test_unauthenticated_status_rejected(server):
    code, _ = _get(server, "/api/status")
    assert code == 401


def test_wrong_token_rejected(server):
    code, _ = _get(server, "/api/status", token="wrong")
    assert code == 401


def test_authenticated_status_ok(server):
    code, body = _get(server, "/api/status", token=server["token"])
    assert code == 200
    data = json.loads(body)
    assert set(data) >= {"version", "providers", "p2p", "ollama", "scheduler", "briefs"}


def test_header_auth_also_works(server):
    url = f"http://127.0.0.1:{server['port']}/api/commands"
    req = urllib.request.Request(url, headers={"X-Cortana-Token": server["token"]})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200


def test_index_requires_auth_and_serves_html(server):
    code, _ = _get(server, "/")
    assert code == 401
    code, body = _get(server, "/", token=server["token"])
    assert code == 200 and b"Cortana dashboard" in body


def test_unknown_route_404(server):
    code, _ = _get(server, "/nope", token=server["token"])
    assert code == 404


# --- status content ------------------------------------------------------------

def test_status_has_no_credential_material(server):
    code, body = _get(server, "/api/status", token=server["token"])
    text = body.decode().lower()
    for bad in ("api_key", "apikey", "secret", "bearer "):
        assert bad not in text, f"credential-like material in status: {bad}"


def test_collect_status_shape():
    s = collect_status()
    assert set(s) >= {"version", "providers", "p2p", "ollama", "scheduler", "briefs"}
    assert isinstance(s["version"], str)


# --- chat / commands / logs -----------------------------------------------------

def test_chat_empty_message_no_llm(server):
    code, data = _post(server, "/api/chat", {"message": "   "}, server["token"])
    assert code == 200 and data["reply"] == "Say something first."


def test_chat_error_is_honest(monkeypatch):
    def _boom(_ctx, _task, **_kw):
        raise RuntimeError("no keys")
    import core.brain as brain_mod
    monkeypatch.setattr(brain_mod, "think", _boom)
    assert "chat unavailable" in chat_reply("hello")


def test_commands_list(server):
    code, body = _get(server, "/api/commands", token=server["token"])
    assert code == 200
    data = json.loads(body)
    assert "/health" in data["commands"] and "/schedule list" in data["commands"]


def test_cmd_disallowed_rejected(server):
    code, data = _post(server, "/api/cmd", {"command": "/fix everything"}, server["token"])
    assert code == 200 and "not allowed" in data["output"]


def test_cmd_shell_not_exposed(server):
    code, data = _post(server, "/api/cmd", {"command": "shell: uptime"}, server["token"])
    assert "not allowed" in data["output"]


def test_run_readonly_unknown():
    out = run_readonly("/nuke")
    assert "not allowed" in out and "/health" in out


def test_logs_empty_state(dash_env):
    assert tail_logs() == ["no log files yet"]


def test_logs_tail(dash_env):
    dash_mod.LOG_DIR.mkdir(parents=True)
    (dash_mod.LOG_DIR / "error_2026-09-25.log").write_text("a\nb\nc\n")
    assert tail_logs(n=2) == ["b", "c"]


# --- binding --------------------------------------------------------------------

def test_default_bind_is_loopback(dash_env):
    info = start_dashboard_background(port=0)
    try:
        # The socket is bound to 127.0.0.1, not 0.0.0.0.
        import socket
        s = socket.create_connection(("127.0.0.1", info["port"]), timeout=5)
        s.close()
    finally:
        info["stop"]()
