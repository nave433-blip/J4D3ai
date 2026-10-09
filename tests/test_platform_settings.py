"""Round C: typed settings schema — validation, coercion, persistence, sync."""
import json

import pytest

import core.config as config_mod
from core.config import DEFAULT_CONFIG, load_config


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    import core.settings as settings_mod
    monkeypatch.setattr(settings_mod, "console", __import__("rich.console", fromlist=["Console"]).Console(file=open("/dev/null", "w")))
    return tmp_path


def test_schema_defaults_match_config_defaults(isolated):
    """Every schema key must exist in DEFAULT_CONFIG with the same default."""
    from core.settings import get_schema
    for entry in get_schema():
        key = entry["key"]
        assert key in DEFAULT_CONFIG, f"schema key '{key}' missing from DEFAULT_CONFIG"
        assert DEFAULT_CONFIG[key] == entry["default"], f"default mismatch for '{key}'"


def test_config_keys_all_in_schema_or_legacy(isolated):
    """Every DEFAULT_CONFIG key is either in the schema or a legacy/provider key."""
    from core.settings import get_schema
    schema_keys = {e["key"] for e in get_schema()}
    legacy_ok = {"provider", "ollama_host", "ollama_hosts", "ollama_cloud_host",
                 "ollama_token", "lm_studio_host", "llama_cpp_host", "gpt4all_host",
                 "jade_model", "jade_name", "auto_approve", "gemini_api_key",
                 "anthropic_api_key", "xai_api_key", "openai_api_key", "mistral_api_key",
                 "nvidia_api_key", "deepseek_api_key", "moonshot_api_key",
                 "dropbox_token", "gdrive_token", "github_token", "personality",
                 "active_prompt", "model_mode", "self_repair", "p2p_use_tls",
                 "p2p_tls_certfile", "p2p_tls_keyfile", "shell_allowlist",
                 "dev_mode", "api_keys", "active_profile", "active_project"}
    for key in DEFAULT_CONFIG:
        assert key in schema_keys or key in legacy_ok, f"DEFAULT_CONFIG key '{key}' not covered"


def test_bool_coercion(isolated):
    from core.settings import set_setting, get_setting
    assert set_setting("compact_mode", "yes", _confirm_sensitive=False)["ok"]
    assert get_setting("compact_mode") is True
    assert set_setting("compact_mode", "0", _confirm_sensitive=False)["ok"]
    assert get_setting("compact_mode") is False
    res = set_setting("compact_mode", "maybe", _confirm_sensitive=False)
    assert not res["ok"] and "boolean" in res["error"]


def test_int_validation(isolated):
    from core.settings import set_setting
    assert set_setting("chat_history_limit", "50", _confirm_sensitive=False)["ok"]
    res = set_setting("chat_history_limit", "-5", _confirm_sensitive=False)
    assert not res["ok"]
    res = set_setting("chat_history_limit", "abc", _confirm_sensitive=False)
    assert not res["ok"]


def test_choice_validation(isolated):
    from core.settings import set_setting, get_setting
    assert set_setting("theme", "light", _confirm_sensitive=False)["ok"]
    assert get_setting("theme") == "light"
    res = set_setting("theme", "neon", _confirm_sensitive=False)
    assert not res["ok"] and "one of" in res["error"]


def test_unknown_key_rejected(isolated):
    from core.settings import set_setting, get_setting
    assert not set_setting("nope_not_real", "1", _confirm_sensitive=False)["ok"]
    with pytest.raises(KeyError):
        get_setting("nope_not_real")


def test_reset_and_export(isolated):
    from core.settings import set_setting, reset_setting, get_setting, export_settings
    set_setting("theme", "light", _confirm_sensitive=False)
    reset_setting("theme")
    assert get_setting("theme") == "dark"
    exported = export_settings()
    assert exported["theme"] == "dark"
    assert "voice_enabled" in exported


def test_old_unknown_keys_preserved(isolated, tmp_path):
    """Migration safety: unknown old keys survive a settings write."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"some_future_key": "keepme"}))
    from core.settings import set_setting, load_config as _lc
    set_setting("theme", "light", _confirm_sensitive=False)
    assert _lc()["some_future_key"] == "keepme"


def test_sensitive_key_requires_confirmation(isolated, monkeypatch):
    from core.settings import set_setting, get_setting
    monkeypatch.setattr("core.settings.confirm", lambda *a, **k: False)
    res = set_setting("confirm_destructive", False)
    assert not res["ok"] and res["error"] == "cancelled"
    assert get_setting("confirm_destructive") is True
    monkeypatch.setattr("core.settings.confirm", lambda *a, **k: True)
    res = set_setting("confirm_destructive", False)
    assert res["ok"]
