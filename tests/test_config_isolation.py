"""Regression tests: load_config() must never leak the live DEFAULT_CONFIG.

Covers the bug where mutating a loaded config polluted process-global
defaults (including nested lists via the shallow {**DEFAULT_CONFIG, ...}
merge), so later load_config() calls returned corrupted values.
"""
import copy
import json

import pytest

import core.config as config_mod
from core.config import DEFAULT_CONFIG, load_config


@pytest.fixture()
def isolated_config_file(tmp_path, monkeypatch):
    """Point CONFIG_FILE at a tmp dir with no config.json present."""
    fake_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", fake_file)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    return fake_file


def _defaults_snapshot():
    return copy.deepcopy(DEFAULT_CONFIG)


def test_mutation_does_not_pollute_defaults_no_file(isolated_config_file):
    before = _defaults_snapshot()
    cfg = load_config()
    cfg["provider"] = "definitely-not-a-provider"
    cfg["ollama_hosts"].append("http://evil.example:11434")
    cfg["shell_allowlist"].append("rm -rf /")

    assert DEFAULT_CONFIG == before, "DEFAULT_CONFIG was mutated!"
    cfg2 = load_config()
    assert cfg2["provider"] == before["provider"]
    assert cfg2["ollama_hosts"] == before["ollama_hosts"]
    assert cfg2["shell_allowlist"] == before["shell_allowlist"]


def test_mutation_does_not_pollute_defaults_with_file(isolated_config_file):
    # Nested list from the FILE must not alias DEFAULT_CONFIG's list either.
    isolated_config_file.write_text(json.dumps({"provider": "openai"}))
    before = _defaults_snapshot()
    cfg = load_config()
    assert cfg["provider"] == "openai"
    cfg["ollama_hosts"].append("http://evil.example:11434")

    assert DEFAULT_CONFIG == before, "DEFAULT_CONFIG was mutated via file-backed config!"
    assert load_config()["ollama_hosts"] == before["ollama_hosts"]


def test_corrupt_file_falls_back_to_clean_defaults(isolated_config_file):
    isolated_config_file.write_text("{not valid json")
    before = _defaults_snapshot()
    cfg = load_config()
    cfg["provider"] = "mutated"
    assert DEFAULT_CONFIG == before
    assert load_config()["provider"] == before["provider"]


def test_returned_configs_are_independent(isolated_config_file):
    a = load_config()
    b = load_config()
    a["ollama_hosts"].append("http://one.example")
    assert b["ollama_hosts"] == DEFAULT_CONFIG["ollama_hosts"]


def test_get_env_with_config_legacy_fallback(monkeypatch, isolated_config_file):
    """Pre-rename CORTANA_MODEL / JARVIS_MODEL env and cortana_model /
    jarvis_model keys feed jade_model lookups (newest rename wins)."""
    from core.config import get_env_with_config
    monkeypatch.delenv("JADE_MODEL", raising=False)
    monkeypatch.delenv("CORTANA_MODEL", raising=False)
    monkeypatch.setenv("JARVIS_MODEL", "env-ancient")
    assert get_env_with_config("jade_model") == "env-ancient"
    monkeypatch.setenv("CORTANA_MODEL", "env-legacy")
    assert get_env_with_config("jade_model") == "env-legacy"
    monkeypatch.delenv("JARVIS_MODEL")
    monkeypatch.delenv("CORTANA_MODEL")
    isolated_config_file.write_text(json.dumps({"cortana_model": "cfg-legacy"}))
    assert get_env_with_config("jade_model") == "cfg-legacy"
    isolated_config_file.write_text(json.dumps({"jarvis_model": "cfg-ancient"}))
    assert get_env_with_config("jade_model") == "cfg-ancient"


def test_get_env_with_config_precedence(monkeypatch, isolated_config_file):
    """New env > legacy env (newest first) > config file."""
    from core.config import get_env_with_config
    isolated_config_file.write_text(json.dumps({"jade_model": "cfg-model"}))
    monkeypatch.delenv("JADE_MODEL", raising=False)
    monkeypatch.delenv("CORTANA_MODEL", raising=False)
    monkeypatch.delenv("JARVIS_MODEL", raising=False)
    assert get_env_with_config("jade_model") == "cfg-model"
    monkeypatch.setenv("JARVIS_MODEL", "ancient-env")
    assert get_env_with_config("jade_model") == "ancient-env"
    monkeypatch.setenv("CORTANA_MODEL", "legacy-env")
    assert get_env_with_config("jade_model") == "legacy-env"
    monkeypatch.setenv("JADE_MODEL", "new-env")
    assert get_env_with_config("jade_model") == "new-env"
    monkeypatch.setenv("CORTANA_MODEL", "new-env")
    assert get_env_with_config("cortana_model") == "new-env"
