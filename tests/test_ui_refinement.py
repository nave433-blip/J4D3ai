"""UI refinement round: suggestions, shared UI helpers, connect status logic.

Unit tests (fast) + piped-REPL tests (slower, same harness as
test_repl_robustness.py).
"""
import json
import os
import subprocess

import pytest
from rich.console import Console

from core.handler import CommandHandler
from core.ui import (
    build_provider_status_table,
    get_menu_grid,
    next_steps_panel,
    ui_error,
    unknown_command_panel,
)
from core.connect import is_configured, render_next_steps

REPO_ROOT = "/home/hatch/workspace/jarvis-dev"
VENV_PY = os.path.join(REPO_ROOT, ".audit-venv", "bin", "python")


def _render(renderable) -> str:
    console = Console(width=100)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# --------------------------------------------------------------------------
# Command suggestions
# --------------------------------------------------------------------------

@pytest.fixture()
def handler():
    """CommandHandler with the same slash-command surface cli.py registers."""
    h = CommandHandler()
    for cmd in ["/chat", "/fix", "/forge", "/connect", "/connections",
                "/models", "/model", "/menu", "/help", "/exit", "/config",
                "/doctor", "/memory", "/dashboard", "/analyze"]:
        h.register(cmd, lambda x: None, help="System command")
    return h


def test_suggest_close_match(handler):
    assert "/connect" in handler.suggest("/conenct")
    assert "/help" in handler.suggest("/hepl")


def test_suggest_gibberish_returns_empty(handler):
    assert handler.suggest("/xyzzy") == []


def test_resolve_registered_exact(handler):
    matched, args = handler.resolve("/connect")
    assert matched == "/connect"
    assert args == []


def test_resolve_unknown_returns_none(handler):
    assert handler.resolve("/xyzzy") == (None, None)


def test_resolve_preserves_fuzzy_autocorrect(handler):
    matched, _ = handler.resolve("/conenct")
    assert matched == "/connect"


# --------------------------------------------------------------------------
# Shared UI helpers
# --------------------------------------------------------------------------

def test_ui_error_panel_contents():
    out = _render(ui_error("No answer", "Could not answer.", why="boom", next_steps="Run /connect"))
    assert "Could not answer." in out
    assert "Run /connect" in out


def test_ui_error_panel_without_optional_parts():
    out = _render(ui_error("T", "Something broke."))
    assert "Something broke." in out


def test_unknown_command_panel_with_suggestions():
    out = _render(unknown_command_panel("/conenct", ["/connect", "/connections"]))
    assert "Unknown command" in out
    assert "/connect" in out
    assert "Did you mean" in out


def test_unknown_command_panel_without_suggestions():
    out = _render(unknown_command_panel("/xyzzy", []))
    assert "Unknown command" in out
    assert "/help" in out


def test_menu_grid_has_groups():
    out = _render(get_menu_grid())
    assert "Core AI Agents" in out
    assert "DevOps" in out
    assert "Accounts & Setup" in out
    assert "/connect" in out


def test_next_steps_panel_numbers_steps():
    out = _render(next_steps_panel(["do a", "do b"], title="Go"))
    assert "do a" in out and "do b" in out
    assert "Go" in out


def test_provider_status_table_labels():
    table = build_provider_status_table([("OLLAMA", True), ("OPENAI", False)])
    out = _render(table)
    assert "OLLAMA" in out and "OPENAI" in out
    assert "connected" in out and "not linked" in out


# --------------------------------------------------------------------------
# Connect status logic
# --------------------------------------------------------------------------

def test_is_configured_unknown_provider_is_false():
    assert is_configured("definitely-not-a-real-provider") is False


def test_is_configured_uses_keyring_key(monkeypatch):
    from core import connect
    monkeypatch.setattr(connect, "get_key_secure", lambda p: "k" if p == "openai" else None)
    assert is_configured("openai") is True
    assert is_configured("anthropic") is False


def test_render_next_steps_none_when_all_good():
    rows = [{"provider": "openai", "display": "OpenAI", "configured": True,
             "reachable": True, "needs_attention": False, "reason": ""}]
    assert render_next_steps(rows) is None


def test_render_next_steps_actionable():
    rows = [{"provider": "openai", "display": "OpenAI", "configured": False,
             "reachable": None, "needs_attention": True, "reason": "not configured"}]
    panel = render_next_steps(rows)
    assert panel is not None
    out = _render(panel)
    assert "/connect" in out
    assert "OpenAI" in out


# --------------------------------------------------------------------------
# Piped-REPL behavior
# --------------------------------------------------------------------------

def _repl_env(tmp_path):
    home = tmp_path / "home"
    (home / ".jarvis").mkdir(parents=True)
    (home / ".jarvis" / "config.json").write_text(json.dumps(
        {"provider": "ollama", "jarvis_model": "llama3"}))
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["JADE_SKIP_STARTUP"] = "1"
    return env


def _run_repl(tmp_path, script, timeout=90):
    env = _repl_env(tmp_path)
    return subprocess.run(
        [VENV_PY, "cli.py"], cwd=REPO_ROOT, env=env,
        input=script, capture_output=True, text=True, timeout=timeout,
    )


def _clean(result):
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert "Traceback (most recent call last)" not in out, out[-3000:]
    return out


def test_repl_unknown_command_suggests_not_runs(tmp_path):
    """/xyzzy must show the unknown-command panel, not burn an LLM call."""
    out = _clean(_run_repl(tmp_path, "/xyzzy\n/exit\n"))
    assert "Unknown command" in out
    assert "Goodbye" in out


def test_repl_menu_uses_grouped_grid(tmp_path):
    out = _clean(_run_repl(tmp_path, "/menu\n/exit\n"))
    assert "Core AI Agents" in out
    assert "Accounts & Setup" in out


def test_repl_connections_shows_next_steps(tmp_path):
    out = _clean(_run_repl(tmp_path, "/connections\n/exit\n"))
    assert "AI Provider Connections" in out
    # Nothing is configured in the sandbox HOME: advice must name /connect.
    assert "/connect" in out
