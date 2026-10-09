"""REPL robustness: drive the real interactive CLI via piped stdin.

No pexpect: stdin is a pipe, so prompt_toolkit raises EOFError when the
script is exhausted and the loop must break cleanly (exit 0, no traceback).
JADE_SKIP_STARTUP=1 keeps startup prompts from eating the piped script;
HOME is sandboxed so no real user config is touched.
"""
import json
import os
import subprocess
import sys

REPO_ROOT = "/home/hatch/workspace/jarvis-dev"
VENV_PY = os.path.join(REPO_ROOT, ".audit-venv", "bin", "python")

STUB_BINARIES = ("hermes", "openclaw", "opencode", "codex", "gh", "droid",
                 "pi", "pool")


def _repl_env(tmp_path):
    home = tmp_path / "home"
    (home / ".jarvis").mkdir(parents=True)
    (home / ".jarvis" / "config.json").write_text(json.dumps(
        {"provider": "ollama", "jarvis_model": "llama3"}))
    bindir = tmp_path / "repl-bin"
    bindir.mkdir()
    for name in STUB_BINARIES:
        stub = bindir / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
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


def test_repl_help_then_exit(tmp_path):
    """/help renders the grouped command reference (not the agent loop)."""
    out = _clean(_run_repl(tmp_path, "/help\n/exit\n"))
    assert "JADE Command Reference" in out
    assert "Brain Loop Interrupted" not in out
    assert "Goodbye" in out


def test_repl_connections_then_exit(tmp_path):
    out = _clean(_run_repl(tmp_path, "/connections\n/exit\n"))
    assert "AI Provider Connections" in out


def test_repl_menu_then_exit(tmp_path):
    _clean(_run_repl(tmp_path, "/menu\n/exit\n"))


def test_repl_unknown_command_degrades_gracefully(tmp_path):
    """Unknown /cmd hits the LLM intent parser; failure must not crash.

    Note: the rendered litellm error panel contains the word 'Traceback' as
    part of the exception-chain text, so we only assert a clean exit here.
    """
    r = _run_repl(tmp_path, "/blarg-nope-cmd\n/exit\n")
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    assert "Goodbye" in r.stdout + r.stderr


def test_repl_immediate_eof_exits_cleanly(tmp_path):
    """Ctrl-D on the first prompt: clean exit, no traceback."""
    env = _repl_env(tmp_path)
    r = subprocess.run(
        [VENV_PY, "cli.py"], cwd=REPO_ROOT, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-3000:]
    assert "Traceback (most recent call last)" not in out


def test_repl_blank_lines_then_exit(tmp_path):
    _clean(_run_repl(tmp_path, "\n\n\n/exit\n"))


def test_repl_t_alias_routes(tmp_path):
    """/t is the /troubleshoot alias; it must not fall into chat."""
    out = _clean(_run_repl(tmp_path, "/t\n/exit\n", timeout=120))
    # /t with no args asks for the command; EOF aborts the prompt cleanly.
    # Either way it must not be treated as chat/debug_loop input.
    assert "ISOLATING TARGET: /t" not in out
