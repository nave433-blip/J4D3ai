"""Rename-migration tests: ~/.cortana -> ~/.jade (and legacy ~/.jarvis),
legacy config keys, legacy keyring service/account fallback, and the
deprecated `jarvis` / `cortana` CLI shims.

NOTE: the legacy names below are deliberate — this file simulates users
arriving from the old product names. Do not bulk-rename them.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

import core.config as config_mod
from core.config import _resolve_config_dir, load_config


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    return home


def _point_config_at(monkeypatch, path):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", path / "config.json")


# ------------------------------------------------------------ dir migration

def test_migration_copies_not_moves(fake_home, monkeypatch, capsys):
    old = fake_home / ".cortana"
    old.mkdir()
    (old / "config.json").write_text(json.dumps({"cortana_model": "llama3"}))
    (old / "notes.txt").write_text("keep me")

    new = _resolve_config_dir()
    assert new == fake_home / ".jade"
    # Copy, not move: old dir fully intact.
    assert (old / "config.json").exists()
    assert (old / "notes.txt").read_text() == "keep me"
    # New dir has the copied content.
    assert (new / "config.json").exists()
    assert (new / "notes.txt").read_text() == "keep me"
    err = capsys.readouterr().err
    assert "Migrated" in err and "~/.cortana" in err and "~/.jade" in err


def test_migration_prefers_cortana_over_jarvis(fake_home, monkeypatch, capsys):
    """When both legacy dirs exist, the newer one (~/.cortana) wins."""
    cortana = fake_home / ".cortana"
    cortana.mkdir()
    (cortana / "config.json").write_text(json.dumps({"cortana_model": "llama3"}))
    jarvis = fake_home / ".jarvis"
    jarvis.mkdir()
    (jarvis / "config.json").write_text(json.dumps({"jarvis_model": "mistral"}))

    new = _resolve_config_dir()
    assert new == fake_home / ".jade"
    assert json.loads((new / "config.json").read_text())["cortana_model"] == "llama3"


def test_migration_falls_back_to_jarvis(fake_home, monkeypatch, capsys):
    old = fake_home / ".jarvis"
    old.mkdir()
    (old / "config.json").write_text(json.dumps({"jarvis_model": "llama3"}))

    new = _resolve_config_dir()
    assert new == fake_home / ".jade"
    assert (new / "config.json").exists()
    err = capsys.readouterr().err
    assert "Migrated" in err and "~/.jarvis" in err and "~/.jade" in err


def test_new_dir_wins_when_both_exist(fake_home, monkeypatch, capsys):
    old = fake_home / ".cortana"
    old.mkdir()
    (old / "sentinel.txt").write_text("do not copy me")
    new = fake_home / ".jade"
    new.mkdir()
    (new / "config.json").write_text(json.dumps({"jade_model": "llama3"}))

    resolved = _resolve_config_dir()
    assert resolved == new
    assert not (new / "sentinel.txt").exists()
    assert capsys.readouterr().err == ""


def test_migration_merges_into_configless_new_dir(fake_home, monkeypatch, capsys):
    """A ~/.jade with only auto-created cache dirs must not block migration."""
    old = fake_home / ".cortana"
    old.mkdir()
    (old / "config.json").write_text(json.dumps({"cortana_model": "mistral"}))
    new = fake_home / ".jade"
    (new / "cache").mkdir(parents=True)

    resolved = _resolve_config_dir()
    assert resolved == new
    assert (new / "config.json").exists()
    assert (new / "cache").is_dir()
    assert (old / "config.json").exists()  # copy, not move
    assert "Migrated" in capsys.readouterr().err


def test_no_old_dir_no_migration(fake_home, monkeypatch, capsys):
    resolved = _resolve_config_dir()
    assert resolved == fake_home / ".jade"
    assert capsys.readouterr().err == ""


# ------------------------------------------------------------ legacy keys

def test_legacy_config_keys_migrated(fake_home, monkeypatch):
    old = fake_home / ".cortana"
    old.mkdir()
    (old / "config.json").write_text(json.dumps({
        "cortana_model": "mistral", "cortana_name": "OldName",
    }))
    _point_config_at(monkeypatch, _resolve_config_dir())
    cfg = load_config()
    assert cfg["jade_model"] == "mistral"
    assert cfg["jade_name"] == "OldName"


def test_oldest_legacy_keys_still_migrated(fake_home, monkeypatch):
    old = fake_home / ".jarvis"
    old.mkdir()
    (old / "config.json").write_text(json.dumps({
        "jarvis_model": "mistral", "jarvis_name": "Ancient",
    }))
    _point_config_at(monkeypatch, _resolve_config_dir())
    cfg = load_config()
    assert cfg["jade_model"] == "mistral"
    assert cfg["jade_name"] == "Ancient"


def test_new_keys_win_over_legacy(fake_home, monkeypatch):
    old = fake_home / ".cortana"
    old.mkdir()
    (old / "config.json").write_text(json.dumps({
        "jade_model": "llama3", "cortana_model": "mistral", "jarvis_model": "old",
        "jade_name": "New", "cortana_name": "Mid", "jarvis_name": "Old",
    }))
    _point_config_at(monkeypatch, _resolve_config_dir())
    cfg = load_config()
    assert cfg["jade_model"] == "llama3"
    assert cfg["jade_name"] == "New"


def test_cortana_keys_win_over_jarvis_keys(fake_home, monkeypatch):
    old = fake_home / ".cortana"
    old.mkdir()
    (old / "config.json").write_text(json.dumps({
        "cortana_model": "mistral", "jarvis_model": "old",
        "cortana_name": "Mid", "jarvis_name": "Old",
    }))
    _point_config_at(monkeypatch, _resolve_config_dir())
    cfg = load_config()
    assert cfg["jade_model"] == "mistral"
    assert cfg["jade_name"] == "Mid"


# ------------------------------------------------------------ keyring fallback

def test_keyring_legacy_service_fallback(monkeypatch):
    import core.connect as connect

    served = {("cortana-dev", "cortana-openai"): "sk-legacy"}
    monkeypatch.setattr(connect, "_keyring_backend_ok", lambda: True)
    monkeypatch.setattr(
        connect.keyring, "get_password",
        lambda service, account: served.get((service, account)))
    assert connect.get_key_secure("openai") == "sk-legacy"


def test_keyring_oldest_service_fallback(monkeypatch):
    import core.connect as connect

    served = {("jarvis-dev", "jarvis-openai"): "sk-ancient"}
    monkeypatch.setattr(connect, "_keyring_backend_ok", lambda: True)
    monkeypatch.setattr(
        connect.keyring, "get_password",
        lambda service, account: served.get((service, account)))
    assert connect.get_key_secure("openai") == "sk-ancient"


def test_keyring_new_service_preferred(monkeypatch):
    import core.connect as connect

    served = {
        ("jade-dev", "jade-openai"): "sk-new",
        ("cortana-dev", "cortana-openai"): "sk-legacy",
        ("jarvis-dev", "jarvis-openai"): "sk-ancient",
    }
    monkeypatch.setattr(connect, "_keyring_backend_ok", lambda: True)
    monkeypatch.setattr(
        connect.keyring, "get_password",
        lambda service, account: served.get((service, account)))
    assert connect.get_key_secure("openai") == "sk-new"


# ------------------------------------------------------------ CLI shim

def test_jarvis_shim_notice_and_forward(tmp_path):
    venv_py = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..",
        ".audit-venv", "bin", "python"))
    if not os.path.exists(venv_py):
        pytest.skip("project venv not present")
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), JADE_SKIP_STARTUP="1")
    code = (
        "import sys; sys.argv = ['jarvis'];"
        "import cli;"
        "cli.app = lambda: print('APP_CALLED');"
        "cli.jarvis_shim()"
    )
    r = subprocess.run([venv_py, "-c", code],
                       cwd=os.path.dirname(os.path.abspath(__file__)) + "/..",
                       capture_output=True, text=True, env=env, timeout=90)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "renamed to 'jade'" in r.stderr
    assert "APP_CALLED" in r.stdout


def test_cortana_shim_notice_and_forward(tmp_path):
    venv_py = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..",
        ".audit-venv", "bin", "python"))
    if not os.path.exists(venv_py):
        pytest.skip("project venv not present")
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), JADE_SKIP_STARTUP="1")
    code = (
        "import sys; sys.argv = ['cortana'];"
        "import cli;"
        "cli.app = lambda: print('APP_CALLED');"
        "cli.cortana_shim()"
    )
    r = subprocess.run([venv_py, "-c", code],
                       cwd=os.path.dirname(os.path.abspath(__file__)) + "/..",
                       capture_output=True, text=True, env=env, timeout=90)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "renamed to 'jade'" in r.stderr
    assert "APP_CALLED" in r.stdout
