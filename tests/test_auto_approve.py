"""Tests for the auto-approve ("allow all") dev toggle.

Auto-approve is an explicit opt-in (config ``"auto_approve": true``,
``JADE_AUTO_APPROVE=1``, or the ``--yes`` CLI flag). It is never on by
default, prints a warning banner at startup, logs every auto-approved action,
and never covers credential/trust prompts (``sensitive=True``).
"""
import json
import os
import stat
import subprocess
import sys

import pytest

import core.config as config_mod
from core import approvals
from core.approvals import confirm, is_auto_approve


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Fake CONFIG_DIR/CONFIG_FILE, clean env, reset session flag."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    for var in ("JADE_AUTO_APPROVE", "JARVIS_AUTO_APPROVE"):
        monkeypatch.delenv(var, raising=False)
    approvals.set_session_yes(False)
    yield tmp_path
    approvals.set_session_yes(False)


def _write_config(path, data):
    (path / "config.json").write_text(json.dumps(data))


# ------------------------------------------------------------------ opt-in

def test_off_by_default(isolated):
    assert is_auto_approve() is False


def test_config_option_enables(isolated):
    _write_config(isolated, {"auto_approve": True})
    assert is_auto_approve() is True


def test_config_option_explicit_false(isolated):
    _write_config(isolated, {"auto_approve": False})
    assert is_auto_approve() is False


def test_env_var_enables(isolated, monkeypatch):
    monkeypatch.setenv("JADE_AUTO_APPROVE", "1")
    assert is_auto_approve() is True


def test_env_var_truthy_spellings(isolated, monkeypatch):
    for value in ("1", "true", "yes", "on", "True", "YES"):
        monkeypatch.setenv("JADE_AUTO_APPROVE", value)
        assert is_auto_approve() is True, value


def test_env_var_falsy_does_not_enable(isolated, monkeypatch):
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("JADE_AUTO_APPROVE", value)
        assert is_auto_approve() is False, value


def test_legacy_env_fallback(isolated, monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_APPROVE", "1")
    assert is_auto_approve() is True


def test_session_yes_flag_enables(isolated):
    approvals.set_session_yes(True)
    try:
        assert is_auto_approve() is True
    finally:
        approvals.set_session_yes(False)


# ------------------------------------------------------------------ confirm()

def test_confirm_asks_when_off(isolated, monkeypatch):
    asked = []
    monkeypatch.setattr(
        approvals.Confirm, "ask",
        lambda *a, **k: asked.append((a, k)) or False)
    assert confirm("Do the thing?") is False
    assert len(asked) == 1
    assert not (isolated / "logs" / "auto_approve.log").exists()


def test_confirm_auto_approves_routine(isolated, monkeypatch, capsys):
    monkeypatch.setenv("JADE_AUTO_APPROVE", "1")

    def _boom(*a, **k):
        raise AssertionError("Confirm.ask must not be called when auto-approving")

    monkeypatch.setattr(approvals.Confirm, "ask", _boom)
    assert confirm("Delete these 3 models?") is True

    log = isolated / "logs" / "auto_approve.log"
    assert log.exists()
    assert "Delete these 3 models?" in log.read_text()
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600
    assert "auto-approve" in capsys.readouterr().out


def test_confirm_sensitive_still_asks(isolated, monkeypatch):
    monkeypatch.setenv("JADE_AUTO_APPROVE", "1")
    asked = []
    monkeypatch.setattr(
        approvals.Confirm, "ask",
        lambda *a, **k: asked.append(True) or False)
    assert confirm("Accept openai key?", sensitive=True) is False
    assert asked == [True]
    # Sensitive prompts are never logged as auto-approved.
    assert not (isolated / "logs" / "auto_approve.log").exists()


def test_confirm_passes_default_through(isolated, monkeypatch):
    seen = {}

    def _fake(prompt, default=False, **k):
        seen["default"] = default
        return default

    monkeypatch.setattr(approvals.Confirm, "ask", _fake)
    assert confirm("Use Ollama?", default=True) is True
    assert seen["default"] is True


# ------------------------------------------------------------------ banner

def test_startup_banner_shown_when_on(isolated, monkeypatch, capsys):
    monkeypatch.setenv("JADE_AUTO_APPROVE", "1")
    assert approvals.startup_banner() is True
    assert "AUTO-APPROVE ON" in capsys.readouterr().out


def test_startup_banner_hidden_when_off(isolated, capsys):
    assert approvals.startup_banner() is False
    assert capsys.readouterr().out == ""


# ------------------------------------------------------------------ MCP generalization

def test_mcp_call_honors_global_auto_approve(isolated, monkeypatch):
    import core.mcp_client as mcp

    monkeypatch.setenv("JADE_AUTO_APPROVE", "1")
    monkeypatch.setattr(mcp, "get_servers_config", lambda: {"srv": {"command": ["x"]}})

    def _boom(*a, **k):
        raise AssertionError("must not prompt under global auto-approve")

    monkeypatch.setattr(mcp.Confirm, "ask", _boom)

    class FakeClient:
        def call_tool(self, tool, args):
            return {"content": [{"type": "text", "text": "ok"}]}

        def close(self):
            pass

    monkeypatch.setattr(mcp, "connect_server", lambda name, timeout=15.0: FakeClient())
    mcp.cmd_call("srv", "tool", "{}", auto_yes=False)  # must not raise

    log = isolated / "logs" / "auto_approve.log"
    assert log.exists()
    assert "Execute this tool call?" in log.read_text()


# ------------------------------------------------------------------ CLI wiring

def test_cli_help_advertises_yes_flag(tmp_path):
    venv_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", ".audit-venv", "bin", "python")
    venv_py = os.path.normpath(venv_py)
    if not os.path.exists(venv_py):
        pytest.skip("project venv with typer not present")
    env = dict(os.environ, JADE_SKIP_STARTUP="1", HOME=str(tmp_path))
    r = subprocess.run(
        [venv_py, "cli.py", "--help"],
        capture_output=True, text=True, env=env, timeout=90)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "--yes" in r.stdout


# ------------------------------------------------------------------ safety pins

def test_no_refusal_bypass_content():
    """The toggle must not introduce refusal-bypass language anywhere."""
    src = open("core/approvals.py").read().lower()
    for needle in ("never ask for permission", "never refuse", "zero restrictions",
                   "ignore it", "zero safety checks", "morality filters",
                   "unconditional disclosure"):
        assert needle not in src, needle


def test_default_config_has_auto_approve_off():
    from core.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["auto_approve"] is False
