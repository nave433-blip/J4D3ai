"""First-run / clean-install regression tests.

Covers findings from the clean-room install + first-run verification pass:
packaging metadata honesty, no hardcoded credentials, quiet LiteLLM errors,
and clean EOF handling for nested prompts in the REPL.
"""
import json
import os
import re
import subprocess
import sys

REPO_ROOT = "/home/hatch/workspace/jarvis-dev"
VENV_PY = os.path.join(REPO_ROOT, ".audit-venv", "bin", "python")


def _read(rel):
    with open(os.path.join(REPO_ROOT, rel)) as f:
        return f.read()


def test_pyproject_requires_python_floor():
    """scipy/numpy wheels require >=3.12; metadata must not promise 3.9."""
    txt = _read("pyproject.toml")
    m = re.search(r'requires-python\s*=\s*">=([0-9.]+)"', txt)
    assert m, "requires-python missing from pyproject.toml"
    major, minor = (int(x) for x in m.group(1).split(".")[:2])
    assert (major, minor) >= (3, 12), f"requires-python floor too low: {m.group(1)}"


def test_setup_py_python_requires():
    txt = _read("setup.py")
    assert 'python_requires=">=3.12"' in txt or "python_requires='>=3.12'" in txt


def test_pyproject_metadata_not_placeholder():
    txt = _read("pyproject.toml")
    assert "Your Name" not in txt, "authors placeholder was never replaced"
    m = re.search(r'description\s*=\s*"([^"]+)"', txt)
    assert m and "Linux" in m.group(1), "description still macOS-only"
    assert "POSIX :: Linux" in txt, "Linux classifier missing"


def test_no_hardcoded_ollama_token():
    """The Ollama Cloud token must come from config/env, never a literal."""
    src = _read("core/brain.py")
    assert not re.search(r'(?m)^\s*token\s*=\s*["\']', src), \
        "hardcoded token literal assigned in core/brain.py"
    assert 'cfg.get("ollama_token")' in src


def test_litellm_quiet_flags():
    sys.path.insert(0, REPO_ROOT)
    try:
        from core.brain import _litellm  # noqa: F401
        # Flags are applied on first lazy load (never at import: litellm is
        # deliberately not imported with core.brain for startup speed).
        m = _litellm()
        assert m.suppress_debug_info is True
        assert m.set_verbose is False
    finally:
        sys.path.remove(REPO_ROOT)


def test_short_err_single_line():
    sys.path.insert(0, REPO_ROOT)
    try:
        from core.brain import _short_err
        assert _short_err(ValueError("boom\nTraceback (most recent call last):\n  ...")) == "boom"
        assert _short_err(ValueError("   ")) != ""
    finally:
        sys.path.remove(REPO_ROOT)


def _repl_env(tmp_path):
    home = tmp_path / "home"
    (home / ".jarvis").mkdir(parents=True)
    (home / ".jarvis" / "config.json").write_text(json.dumps(
        {"provider": "ollama", "jarvis_model": "llama3"}))
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["JADE_SKIP_STARTUP"] = "1"
    return env


def test_repl_eof_in_nested_prompt_clean(tmp_path):
    """Piped stdin exhausted inside /connect's Prompt.ask: exit 0, no 'System Error',
    no debug-analysis prompt (regression: EOFError was swallowed by the generic
    handler and offered 'autonomous debug analysis')."""
    env = _repl_env(tmp_path)
    result = subprocess.run(
        [VENV_PY, "cli.py", "interactive"], cwd=REPO_ROOT, env=env,
        input="/connect\n", capture_output=True, text=True, timeout=90,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-2000:]
    assert "System Error" not in out, out[-2000:]
    assert "autonomous debug analysis" not in out, out[-2000:]
    assert "Traceback (most recent call last)" not in out, out[-2000:]
