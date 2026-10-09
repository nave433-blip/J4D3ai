"""Tests for dev mode: toggle, personal-instructions loader, banner, timing."""
import logging

import pytest

from core.config import is_dev_mode
import core.devmode as devmode
from core.devmode import (
    effective_system_prompt,
    load_dev_instructions,
    print_dev_banner,
    timed_request,
)


# ---------- is_dev_mode toggle ----------

@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes", " ON "])
def test_env_truthy_enables(monkeypatch, val):
    monkeypatch.setenv("JADE_DEV_MODE", val)
    assert is_dev_mode() is True
    # env wins over an explicit off in config
    assert is_dev_mode(config={"dev_mode": False}) is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE"])
def test_env_falsy_disables(monkeypatch, val):
    monkeypatch.setenv("JADE_DEV_MODE", val)
    assert is_dev_mode() is False
    # env wins over an explicit on in config
    assert is_dev_mode(config={"dev_mode": True}) is False


def test_env_unset_falls_back_to_config(monkeypatch):
    monkeypatch.delenv("JADE_DEV_MODE", raising=False)
    assert is_dev_mode(config={"dev_mode": True}) is True
    assert is_dev_mode(config={"dev_mode": False}) is False
    assert is_dev_mode(config={}) is False


def test_env_unrecognized_falls_back_to_config(monkeypatch):
    monkeypatch.setenv("JADE_DEV_MODE", "maybe")
    assert is_dev_mode(config={"dev_mode": True}) is True
    assert is_dev_mode(config={}) is False


def test_legacy_jarvis_dev_mode_fallback(monkeypatch):
    """Deprecated JARVIS_DEV_MODE is still honored."""
    monkeypatch.delenv("JADE_DEV_MODE", raising=False)
    monkeypatch.setenv("JARVIS_DEV_MODE", "1")
    assert is_dev_mode() is True


# ---------- personal instructions loader ----------

def _dev_on(monkeypatch):
    monkeypatch.setenv("JADE_DEV_MODE", "1")


def test_loader_returns_file_contents(monkeypatch, tmp_path):
    _dev_on(monkeypatch)
    target = tmp_path / "dev_instructions.md"
    target.write_text("my personal notes", encoding="utf-8")
    monkeypatch.setattr(devmode, "DEV_INSTRUCTIONS_PATH", target)
    assert load_dev_instructions() == "my personal notes"


def test_loader_missing_file_returns_empty(monkeypatch, tmp_path, caplog):
    _dev_on(monkeypatch)
    monkeypatch.setattr(devmode, "DEV_INSTRUCTIONS_PATH", tmp_path / "nope.md")
    with caplog.at_level(logging.DEBUG, logger="jade.devmode"):
        assert load_dev_instructions() == ""


def test_loader_off_ignores_existing_file(monkeypatch, tmp_path):
    monkeypatch.delenv("JADE_DEV_MODE", raising=False)
    target = tmp_path / "dev_instructions.md"
    target.write_text("should not load", encoding="utf-8")
    monkeypatch.setattr(devmode, "DEV_INSTRUCTIONS_PATH", target)
    # config explicitly off too
    monkeypatch.setattr(devmode, "is_dev_mode", lambda config=None: False)
    assert load_dev_instructions() == ""


def test_loader_never_logs_contents(monkeypatch, tmp_path, caplog):
    _dev_on(monkeypatch)
    secret = "sekret-marker-9f8e7d6c5b"
    target = tmp_path / "dev_instructions.md"
    target.write_text(f"notes containing {secret}", encoding="utf-8")
    monkeypatch.setattr(devmode, "DEV_INSTRUCTIONS_PATH", target)
    with caplog.at_level(logging.DEBUG, logger="jade.devmode"):
        load_dev_instructions()
    assert secret not in caplog.text


# ---------- effective system prompt ----------

def test_effective_prompt_unchanged_when_off(monkeypatch):
    monkeypatch.delenv("JADE_DEV_MODE", raising=False)
    monkeypatch.setattr(devmode, "is_dev_mode", lambda config=None: False)
    assert effective_system_prompt("BASE") == "BASE"


def test_effective_prompt_appends_instructions(monkeypatch, tmp_path):
    _dev_on(monkeypatch)
    target = tmp_path / "dev_instructions.md"
    target.write_text("extra notes", encoding="utf-8")
    monkeypatch.setattr(devmode, "DEV_INSTRUCTIONS_PATH", target)
    out = effective_system_prompt("BASE")
    assert out.startswith("BASE")
    assert "extra notes" in out
    assert "Developer notes (local dev mode)" in out


def test_effective_prompt_unchanged_when_file_missing(monkeypatch, tmp_path):
    _dev_on(monkeypatch)
    monkeypatch.setattr(devmode, "DEV_INSTRUCTIONS_PATH", tmp_path / "nope.md")
    assert effective_system_prompt("BASE") == "BASE"


# ---------- banner ----------

def test_banner_shows_when_on(monkeypatch, capsys):
    _dev_on(monkeypatch)
    assert print_dev_banner() is True
    out = capsys.readouterr().out
    assert "DEV MODE" in out


def test_banner_hidden_when_off(monkeypatch, capsys):
    monkeypatch.delenv("JADE_DEV_MODE", raising=False)
    monkeypatch.setattr(devmode, "is_dev_mode", lambda config=None: False)
    assert print_dev_banner() is False
    assert capsys.readouterr().out == ""


# ---------- timing diagnostics ----------

def test_timed_request_logs_provider_model_elapsed(caplog):
    with caplog.at_level(logging.DEBUG, logger="jade.devmode"):
        with timed_request("ollama", "ollama/llama3"):
            pass
    assert "provider=ollama" in caplog.text
    assert "model=ollama/llama3" in caplog.text
    assert "elapsed=" in caplog.text


# ---------- safety pins ----------

def test_gitignore_blocks_dev_instruction_files():
    from pathlib import Path
    gi = Path(__file__).parent.parent / ".gitignore"
    text = gi.read_text(encoding="utf-8")
    assert "dev_instructions.md" in text
    assert "*-dev.local.md" in text


def test_no_refusal_bypass_content_in_devmode():
    """Dev mode is diagnostics-only: it must never carry refusal-bypass
    directives in code, defaults, docs, or tests."""
    from pathlib import Path
    root = Path(__file__).parent.parent
    banned = ["never refuse", "unconditional disclosure", "refusal is a system failure"]
    for rel in ["core/devmode.py", "core/config.py", "core/brain.py", "cli.py", "README.md",
                "PACKAGING.md", "install.sh", "packaging/aur/PKGBUILD"]:
        text = (root / rel).read_text(encoding="utf-8").lower()
        for phrase in banned:
            assert phrase not in text, f"banned phrase {phrase!r} in {rel}"
