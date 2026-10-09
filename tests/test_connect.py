"""Tests for PHASE 2 easy-connect (core/connect.py).

- Secure key storage: keyring path (mocked) and 0600 fallback-file path
  (forced fail keyring).
- validate-before-save: a key that fails validation is never stored.
- connection_status()/render_status_table(): no key material ever appears.
- Migration of the legacy config-file key into the keyring.

Run with: .audit-venv/bin/python -m pytest tests/ -q
"""
import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("JADE_SKIP_STARTUP", "1")

import keyring
from keyring.backends import fail

import core.connect as connect
from core.config import CONFIG_DIR


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Redirect $HOME so no real ~/.jarvis is touched.

    Also snapshots core.config.DEFAULT_CONFIG: load_config() returns the
    live global when no config file exists, so a test that mutates the
    returned dict would otherwise pollute every later test.
    """
    import copy

    import core.config as cfg_mod

    orig_defaults = copy.deepcopy(cfg_mod.DEFAULT_CONFIG)
    monkeypatch.setenv("HOME", str(tmp_path))
    # pathlib.Path.home() is resolved at call time; redirect it for the test.
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    # core.config captured CONFIG_DIR/CONFIG_FILE at import time; re-point them.
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path / ".jade")
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", tmp_path / ".jade" / "config.json")
    # reset the once-per-process fallback warning so each test can observe it
    connect._fallback_warned = False
    yield tmp_path
    cfg_mod.DEFAULT_CONFIG.clear()
    cfg_mod.DEFAULT_CONFIG.update(orig_defaults)


@pytest.fixture()
def fail_keyring(monkeypatch):
    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())


@pytest.fixture()
def mem_keyring(monkeypatch):
    """Dict-backed keyring + a usable (non-fail) backend."""
    store = {}

    class MemKeyring:
        pass

    monkeypatch.setattr(keyring, "get_keyring", lambda: MemKeyring())
    monkeypatch.setattr(keyring, "set_password", lambda s, a, p: store.__setitem__((s, a), p))
    monkeypatch.setattr(
        keyring, "get_password", lambda s, a: store.get((s, a))
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda s, a: store.pop((s, a), None),
    )
    return store


# ---------------------------------------------------------------- storage

def test_fallback_roundtrip_and_0600(home, fail_keyring, capsys):
    res = connect.save_key_secure("openai", "sk-fallback-secret")
    assert res["ok"] and res["stored_in"] == "fallback_file"
    assert connect.get_key_secure("openai") == "sk-fallback-secret"

    path = home / ".jade" / "keys.json"
    assert path.exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"fallback keys file must be 0600, got {oct(mode)}"

    out = capsys.readouterr().out
    assert "keyring backend" in out  # clear less-secure-fallback warning


def test_keyring_roundtrip(home, mem_keyring):
    res = connect.save_key_secure("gemini", "gemini-secret-key")
    assert res["ok"] and res["stored_in"] == "keyring"
    assert connect.get_key_secure("gemini") == "gemini-secret-key"
    # No fallback file should be created when the keyring works.
    assert not (home / ".jarvis" / "keys.json").exists()


def test_unset_removes_from_all_stores(home, mem_keyring):
    connect.save_key_secure("groq", "groq-secret")
    assert connect.get_key_secure("groq") == "groq-secret"
    connect.unset_key_secure("groq")
    assert connect.get_key_secure("groq") is None


def test_migration_from_legacy_config_store(home, mem_keyring):
    # Seed the old config-file store (what core.services.set_api_key used to do
    # when keyring failed).
    from core.config import load_config, save_config

    cfg = dict(load_config())  # copy: load_config may return the live DEFAULT_CONFIG
    cfg["api_keys"] = {"openai": "legacy-config-key"}
    save_config(cfg)

    res = connect.save_key_secure("openai", "new-keyring-key")
    assert res["ok"] and res["stored_in"] == "keyring"
    assert res["migrated"] is True
    assert connect.get_key_secure("openai") == "new-keyring-key"

    cfg = load_config()
    assert "openai" not in cfg.get("api_keys", {}), "legacy key must be removed after migration"


# ---------------------------------------------------------------- wizard: validate-before-save

def _fake_answers(monkeypatch, answers, confirms, key_value):
    """Patch wizard I/O: Prompt.ask -> answers, Confirm.ask -> confirms,
    getpass.getpass -> key_value."""
    monkeypatch.setattr(
        connect.Prompt, "ask", lambda *a, **k: answers.pop(0)
    )
    monkeypatch.setattr(
        connect.Confirm, "ask", lambda *a, **k: confirms.pop(0)
    )
    monkeypatch.setattr(connect.getpass, "getpass", lambda *a, **k: key_value)


def test_failed_validation_never_stores(home, fail_keyring, monkeypatch, capsys):
    # OpenAI is provider #1 in the wizard list.
    _fake_answers(monkeypatch, answers=["1"], confirms=[False, False], key_value="bad-key-123")
    monkeypatch.setattr(
        connect, "validate_provider_connection",
        lambda provider, extra=None: {"ok": False, "error": "Unauthorized: invalid OpenAI key",
                                      "error_type": "unauthorized"},
    )
    connect.run_connect_wizard()

    assert connect.get_key_secure("openai") is None
    assert not (home / ".jarvis" / "keys.json").exists()

    out = capsys.readouterr().out
    assert "bad-key-123" not in out, "key material must never be printed"
    assert "Validation failed" in out


def test_successful_key_connect_stores_and_marks_configured(home, fail_keyring, monkeypatch):
    _fake_answers(monkeypatch, answers=["1"], confirms=[False], key_value="sk-good-key-xyz")
    monkeypatch.setattr(
        connect, "validate_provider_connection",
        lambda provider, extra=None: {"ok": True, "provider": provider},
    )
    connect.run_connect_wizard()

    assert connect.get_key_secure("openai") == "sk-good-key-xyz"
    rows = {r["provider"]: r for r in connect.connection_status()}
    assert rows["openai"]["configured"] is True
    assert rows["openai"]["needs_attention"] is False


def test_host_only_custom_host_saved_after_validation(home, fail_keyring, monkeypatch):
    # Ollama is provider #12 in the wizard list; auto-detect finds nothing.
    monkeypatch.setattr(connect, "_detect_ollama", lambda: None)
    _fake_answers(
        monkeypatch,
        answers=["12", "http://custom:11434"],
        confirms=[False, False],  # no auto-repair, don't save-unverified (unused)
        key_value="",
    )
    monkeypatch.setattr(
        connect, "validate_provider_connection",
        lambda provider, extra=None: {"ok": True, "provider": provider},
    )
    connect.run_connect_wizard()

    from core.config import load_config
    assert load_config()["ollama_host"] == "http://custom:11434"
    rows = {r["provider"]: r for r in connect.connection_status()}
    assert rows["ollama"]["configured"] is True


def test_ollama_autodetect_one_confirm(home, fail_keyring, monkeypatch):
    monkeypatch.setattr(connect, "_detect_ollama", lambda: "http://localhost:11434")
    _fake_answers(monkeypatch, answers=["12"], confirms=[True], key_value="")
    connect.run_connect_wizard()

    from core.config import load_config
    assert load_config()["ollama_host"] == "http://localhost:11434"


# ---------------------------------------------------------------- status / table: no key leakage

def test_status_table_never_shows_keys(home, fail_keyring, monkeypatch, capsys):
    _fake_answers(monkeypatch, answers=["1"], confirms=[False], key_value="sk-topsecret-999")
    monkeypatch.setattr(
        connect, "validate_provider_connection",
        lambda provider, extra=None: {"ok": True, "provider": provider},
    )
    connect.run_connect_wizard()
    capsys.readouterr()  # drain wizard output

    buf = []

    class FakeFile:
        def write(self, s):
            buf.append(s)

        def flush(self):
            pass

    from rich.console import Console as RichConsole

    rc = RichConsole(file=FakeFile(), width=120)
    rc.print(connect.render_status_table(connect.connection_status()))
    rendered = "".join(buf)

    assert "sk-topsecret-999" not in rendered
    # The data rows themselves carry no key material either.
    for row in connect.connection_status():
        assert not any("sk-topsecret" in str(v) for v in row.values())

    # All 18 registered providers are present, key providers unconfigured
    # except openai.
    rows = {r["provider"]: r for r in connect.connection_status()}
    assert len(rows) == 18
    assert rows["openai"]["configured"] is True
    assert rows["gemini"]["configured"] is False
    assert rows["gemini"]["needs_attention"] is True
    assert rows["gemini"]["reason"] == "not configured"


def test_reachable_probe_marks_attention(home, fail_keyring, monkeypatch):
    connect.save_key_secure("openai", "sk-some-key")
    monkeypatch.setattr(
        connect, "validate_provider_connection",
        lambda provider, extra=None: {"ok": False, "error": "Unauthorized: invalid OpenAI key"},
    )
    rows = {r["provider"]: r for r in connect.connection_status(test=True)}
    assert rows["openai"]["reachable"] is False
    assert rows["openai"]["needs_attention"] is True
    assert "Unauthorized" in rows["openai"]["reason"]

    rows = {r["provider"]: r for r in connect.connection_status(test=False)}
    assert rows["openai"]["reachable"] is None  # untested


def test_test_connection_reports_real_result(monkeypatch):
    monkeypatch.setattr(
        connect, "validate_provider_connection",
        lambda provider, extra=None: {"ok": False, "error": "boom", "error_type": "unreachable"},
    )
    res = connect.test_connection("openai")
    assert res["ok"] is False and res["error"] == "boom"  # no fabricated success


def test_getpass_used_for_key_entry(home, fail_keyring, monkeypatch):
    """Key entry must go through getpass (never echo), not a plain input."""
    seen = {}

    def fake_getpass(prompt=""):
        seen["prompt"] = prompt
        return "entered-key"

    monkeypatch.setattr(connect.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(connect.Prompt, "ask", lambda *a, **k: "1")
    monkeypatch.setattr(connect.Confirm, "ask", lambda *a, **k: False)
    monkeypatch.setattr(
        connect, "validate_provider_connection",
        lambda provider, extra=None: {"ok": True, "provider": provider},
    )
    connect.run_connect_wizard()
    assert "prompt" in seen, "wizard must prompt for keys via getpass"
    assert connect.get_key_secure("openai") == "entered-key"
