"""Regression tests for PHASE 1 hardening.

- P2P TLS opt-in (stdlib ssl): socketpair handshake + end-to-end HTTPS server,
  cert-required guard, and plaintext default unchanged.
- tools/shell.py allowlist mode: blocked/permitted paths, config-driven,
  default (blocklist-only) behavior unchanged.
- Bare excepts in core/handler.py and core/nave_loop.py: tightened to
  `except Exception` with ErrorLogger logging, control flow unchanged.
- CLI smoke tests: --help, menu (/menu path), and non-interactive startup.

Run with: .audit-venv/bin/python -m pytest tests/ -q
"""
import ast
import inspect
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("JADE_SKIP_STARTUP", "1")

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VENV_PY = os.path.join(REPO_ROOT, ".audit-venv", "bin", "python")

NEEDS_OPENSSL = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl CLI required for test certs"
)


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _brain_stubbed(monkeypatch):
    """core.handler / core.nave_loop import core.brain (litellm) at module
    level; the audit venv does not install litellm, so stub it."""
    stub = types.ModuleType("core.brain")
    stub.get_provider = lambda: None
    stub.think = lambda *a, **k: {"ok": False}
    stub.think_structured = lambda *a, **k: {"ok": False}
    monkeypatch.setitem(sys.modules, "core.brain", stub)
    for mod in ("core.handler", "core.nave_loop"):
        monkeypatch.delitem(sys.modules, mod, raising=False)


# ---------------------------------------------------------------------------
# P2P TLS opt-in
# ---------------------------------------------------------------------------

@NEEDS_OPENSSL
def test_p2p_tls_socketpair_handshake(tmp_path):
    """TLS handshake over a local socket pair using a self-signed cert."""
    from core import p2p

    cert = str(tmp_path / "p2p.crt")
    key = str(tmp_path / "p2p.key")
    p2p.generate_self_signed_cert(cert, key, hostname="jade-test")
    assert os.path.exists(cert) and os.path.exists(key)

    server_ctx = p2p._build_ssl_context(cert, key)
    a, b = socket.socketpair()
    received = {}

    def serve():
        tls = server_ctx.wrap_socket(a, server_side=True)
        try:
            received["data"] = tls.recv(4)
            tls.sendall(b"pong")
        finally:
            tls.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    client_ctx = ssl.create_default_context()
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE
    tls_b = client_ctx.wrap_socket(b, server_hostname="jade-test")
    try:
        tls_b.sendall(b"ping")
        assert tls_b.recv(4) == b"pong"
    finally:
        tls_b.close()
    t.join(timeout=10)
    assert received.get("data") == b"ping"


@NEEDS_OPENSSL
def test_p2p_tls_server_end_to_end(tmp_path, monkeypatch):
    """Real HTTPS round-trip against run_p2p_server with use_tls=True."""
    from core import p2p
    from core import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: {})
    cert = str(tmp_path / "p2p.crt")
    key = str(tmp_path / "p2p.key")
    p2p.generate_self_signed_cert(cert, key)

    port = _free_port()
    t = threading.Thread(
        target=p2p.run_p2p_server,
        kwargs={"port": port, "use_tls": True, "certfile": cert, "keyfile": key},
        daemon=True,
    )
    t.start()

    res = {"ok": False, "error": "never attempted"}
    deadline = time.time() + 15
    while time.time() < deadline:
        res = p2p.send_remote_command(
            "127.0.0.1", "status", {}, port=port, use_tls=True, verify_tls=False
        )
        if res.get("ok"):
            break
        time.sleep(0.2)
    assert res.get("ok"), f"TLS status request failed: {res}"
    assert json.loads(res["data"])["status"] == "online"


@NEEDS_OPENSSL
def test_p2p_plaintext_default_still_works(tmp_path, monkeypatch):
    """Default (no TLS flag/config) stays plaintext HTTP and working."""
    from core import p2p
    from core import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: {})
    port = _free_port()
    t = threading.Thread(
        target=p2p.run_p2p_server, kwargs={"port": port}, daemon=True
    )
    t.start()

    res = {"ok": False, "error": "never attempted"}
    deadline = time.time() + 15
    while time.time() < deadline:
        res = p2p.send_remote_command("127.0.0.1", "status", {}, port=port)
        if res.get("ok"):
            break
        time.sleep(0.2)
    assert res.get("ok"), f"plaintext status request failed: {res}"
    assert json.loads(res["data"])["status"] == "online"


def test_p2p_tls_without_cert_raises(monkeypatch):
    """Enabling TLS with no certificate is a loud error, not a silent fallback."""
    from core import p2p
    from core import config as config_mod

    monkeypatch.setattr(
        config_mod, "load_config", lambda: {"p2p_use_tls": True}
    )
    with pytest.raises(ValueError):
        p2p.run_p2p_server(port=_free_port(), use_tls=True)


def test_p2p_scheme_resolution():
    from core.p2p import _p2p_scheme, _tls_enabled

    assert _tls_enabled(None, {}) is False
    assert _tls_enabled(None, {"p2p_use_tls": True}) is True
    assert _tls_enabled(False, {"p2p_use_tls": True}) is False  # explicit wins
    assert _p2p_scheme(None, {}) == "http"
    assert _p2p_scheme(None, {"p2p_use_tls": True}) == "https"


# ---------------------------------------------------------------------------
# Shell allowlist mode
# ---------------------------------------------------------------------------

def test_shell_allowlist_blocks_unlisted():
    from tools import shell

    res = shell.run("rm -rf /tmp/definitely-not-here", allowlist=["echo ", "ls "])
    assert res["status"] == "blocked"
    assert "allowlist" in res["error"]


def test_shell_allowlist_permits_listed_prefix():
    from tools import shell

    res = shell.run("echo hello", allowlist=["echo "])
    assert res["return_code"] == 0
    assert "hello" in res["stdout"]


def test_shell_allowlist_still_applies_blocklist():
    """Allowlist is an extra restriction; unsafe patterns still need confirm."""
    from tools import shell

    res = shell.run("echo hi; rm -rf /", allowlist=["echo "])
    assert res["status"] == "needs_confirmation"


def test_shell_allowlist_off_by_default(monkeypatch):
    """No config key -> legacy blocklist-only behavior, unchanged."""
    from core import config as config_mod
    from tools import shell

    monkeypatch.setattr(config_mod, "load_config", lambda: {})
    res = shell.run("echo hi")
    assert res["return_code"] == 0
    res = shell.run("rm -rf /tmp/x")
    assert res["status"] == "needs_confirmation"


def test_shell_allowlist_from_config(monkeypatch):
    from core import config as config_mod
    from tools import shell

    monkeypatch.setattr(
        config_mod, "load_config", lambda: {"shell_allowlist": ["echo "]}
    )
    assert shell.run("echo hi")["return_code"] == 0
    assert shell.run("id")["status"] == "blocked"
    assert shell.run_simple("id").startswith("Blocked:")


# ---------------------------------------------------------------------------
# Bare-except tightening (handler.py, nave_loop.py)
# ---------------------------------------------------------------------------

def test_no_bare_except_in_handler_and_nave_loop(monkeypatch):
    _brain_stubbed(monkeypatch)
    import core.handler as handler_mod
    import core.nave_loop as nave_loop_mod

    for mod in (handler_mod, nave_loop_mod):
        tree = ast.parse(inspect.getsource(mod))
        bare = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.ExceptHandler) and n.type is None
        ]
        assert not bare, f"bare except: remaining in {mod.__name__}"


def test_tokenize_fallback_still_works_and_logs(monkeypatch):
    _brain_stubbed(monkeypatch)
    from core.handler import CommandHandler
    from core import logger as logger_mod

    logged = []
    monkeypatch.setattr(
        logger_mod.ErrorLogger, "log_error",
        staticmethod(lambda e, context="": logged.append((context, str(e)))),
    )
    h = CommandHandler()
    assert h._tokenize("a b c") == ["a", "b", "c"]  # happy path: no logging
    assert logged == []
    # shlex fails on unbalanced quote -> fallback preserved, error logged
    assert h._tokenize('unbalanced "quote') == ["unbalanced", '"quote']
    assert len(logged) == 1
    assert logged[0][0] == "command_handler._tokenize"


def test_call_llm_parse_failure_logs_and_falls_back_to_noop(monkeypatch):
    _brain_stubbed(monkeypatch)
    import core.handler as handler_mod
    from core.handler import CommandHandler
    from core import logger as logger_mod

    class BadProvider:
        def ask(self, *a, **k):
            return "this is not json at all"

    # handler did `from core.brain import get_provider`: patch its own namespace
    monkeypatch.setattr(handler_mod, "get_provider", lambda: BadProvider())
    logged = []
    monkeypatch.setattr(
        logger_mod.ErrorLogger, "log_error",
        staticmethod(lambda e, context="": logged.append((context, str(e)))),
    )
    h = CommandHandler()
    res = h._call_llm_parse("do something")
    # Control flow unchanged: unparsable LLM output -> noop fallback
    assert res == {"type": "noop", "target": "", "args": [], "confirm": False, "ui": None}
    assert len(logged) == 1
    assert logged[0][0] == "command_handler._call_llm_parse"


def test_nave_loop_integrator_parse_failure_is_logged(monkeypatch):
    _brain_stubbed(monkeypatch)
    import core.nave_loop as nave_loop_mod

    src = inspect.getsource(nave_loop_mod.run_nave_loop)
    assert "except Exception as e:" in src
    assert 'ErrorLogger.log_error(e, context="nave_loop.integrator_json_parse")' in src
    # Fallback assignment preserved (control flow unchanged)
    assert 'integrator_json = {"final_answer": final_raw}' in src


# ---------------------------------------------------------------------------
# CLI smoke tests (subprocess, sandboxed HOME, non-interactive)
# ---------------------------------------------------------------------------

def _smoke_env(tmp_path):
    home = tmp_path / "home"
    (home / ".jarvis").mkdir(parents=True)
    (home / ".jarvis" / "config.json").write_text(json.dumps({
        "provider": "ollama",
        "ollama_token": "smoke-test-token",
        "ollama_cloud_host": "https://ollama.com/api",
        "p2p_enabled": True,
    }))
    # Stub the agent-CLI check binaries so startup's agent detection
    # (core.agent_manager.check_agents) sees them as present and never
    # prompts for `npm install -g` / `curl | bash` installs in the sandbox.
    bindir = tmp_path / "smoke-bin"
    bindir.mkdir()
    for name in ("hermes", "openclaw", "opencode", "codex", "gh", "droid",
                 "pi", "pool"):
        stub = bindir / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["JADE_SKIP_STARTUP"] = "1"
    return env


def test_cli_help_smoke(tmp_path):
    env = _smoke_env(tmp_path)
    r = subprocess.run(
        [VENV_PY, "cli.py", "--help"], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert "JADE" in r.stdout


def test_cli_menu_smoke(tmp_path):
    """The /menu command path, exercised non-interactively via typer."""
    env = _smoke_env(tmp_path)
    r = subprocess.run(
        [VENV_PY, "cli.py", "menu"], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert "JADE SYSTEM INTERFACE" in r.stdout


def test_cli_startup_eof_exits_cleanly(tmp_path):
    """No-args startup with closed stdin must not traceback."""
    env = _smoke_env(tmp_path)
    r = subprocess.run(
        [VENV_PY, "cli.py"], cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    assert "Traceback" not in r.stdout + r.stderr
