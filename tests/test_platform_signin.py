"""Round C: sign-in + Jade Account — honesty, keyring storage, local record."""
import json

import pytest

import core.config as config_mod


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    store = {}

    def _get(svc, user):
        return store.get((svc, user))

    def _set(svc, user, val):
        store[(svc, user)] = val

    def _del(svc, user):
        store.pop((svc, user), None)

    monkeypatch.setattr("keyring.get_password", _get)
    monkeypatch.setattr("keyring.set_password", _set)
    monkeypatch.setattr("keyring.delete_password", _del)
    import core.jade_account as ca_mod
    monkeypatch.setattr(ca_mod, "ACCOUNT_FILE", tmp_path / "account.json")
    return tmp_path


def test_providers_known(isolated):
    from core.signin import PROVIDERS
    assert set(PROVIDERS) == {"microsoft", "apple", "google", "github"}


def test_status_honest_without_client_ids(isolated):
    from core.signin import signin_status
    for s in signin_status():
        assert s["signed_in"] is False
        # Google's hint differs (uses google_client.json) but must still guide.
        assert s["needs"] or s["provider"] == "google"


def test_signin_unknown_provider(isolated):
    from core.signin import signin, signout
    assert not signin("yahoo")["ok"]
    assert not signout("yahoo")["ok"]


def test_signout_when_not_signed_in(isolated):
    from core.signin import signout
    assert signout("github")["ok"]


def test_signin_stores_token_and_links_account(isolated, monkeypatch):
    import core.signin as si_mod
    from core.signin import signin, is_signed_in, get_signin
    from core.jade_account import linked_providers

    fake_tok = {"access_token": "tok123", "account": "dev"}

    def fake_device_flow(client_id, scopes):
        assert client_id == "cid123"
        return dict(fake_tok)

    monkeypatch.setattr(si_mod, "_github_device_flow", fake_device_flow)
    monkeypatch.setattr(si_mod, "api_get", lambda url, token: {"login": "dev"})
    # Pretend the client id is configured.
    cfg = {"github_client_id": "cid123"}
    (isolated / "config.json").write_text(json.dumps(cfg))

    res = signin("github")
    assert res["ok"] and res["account"] == "dev"
    assert is_signed_in("github")
    assert get_signin("github")["access_token"] == "tok123"
    assert "github" in linked_providers()


def test_signout_clears_token_and_unlinks(isolated, monkeypatch):
    import core.signin as si_mod
    from core.signin import signin, signout, is_signed_in
    from core.jade_account import linked_providers

    monkeypatch.setattr(si_mod, "_github_device_flow",
                        lambda cid, scopes: {"access_token": "t", "account": "dev"})
    monkeypatch.setattr(si_mod, "api_get", lambda url, token: {"login": "dev"})
    (isolated / "config.json").write_text(json.dumps({"github_client_id": "cid"}))
    assert signin("github")["ok"]
    monkeypatch.setattr("core.approvals.confirm", lambda *a, **k: True)
    assert signout("github")["ok"]
    assert not is_signed_in("github")
    assert "github" not in linked_providers()


def test_account_record(isolated):
    from core.jade_account import (get_account, set_display_name,
                                      link_signin, unlink_signin)
    doc = get_account()
    assert doc["linked"] == {}
    set_display_name("Ada")
    link_signin("google", "ada@example.com")
    doc = get_account()
    assert doc["display_name"] == "Ada"
    assert doc["email"] == "ada@example.com"
    assert doc["linked"]["google"]["account"] == "ada@example.com"
    unlink_signin("google")
    assert get_account()["linked"] == {}


def test_account_doc_exists_and_is_honest():
    import pathlib
    doc = pathlib.Path(__file__).parent.parent / "docs" / "JADE_ACCOUNT.md"
    assert doc.exists()
    text = doc.read_text()
    assert "not a shipped service" in text
