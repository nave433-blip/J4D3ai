"""Round C: connector framework — registry, honesty, token storage, actions."""
import json

import pytest

import core.config as config_mod


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    # Dict-backed fake keyring.
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
    return store


def test_registry_lists_all_connectors(isolated):
    from core import connectors
    ids = sorted(c.id for c in connectors.list_connectors())
    assert ids == ["azure", "gcal", "gcontacts", "gdocs", "gdrive", "gmail",
                   "gtasks", "onedrive", "onenote", "outlook", "teams",
                   "youtube"]
    with pytest.raises(KeyError):
        connectors.get_connector("nope")


def test_status_honest_when_disconnected(isolated):
    from core import connectors
    for c in connectors.list_connectors():
        st = c.status()
        assert st["connected"] is False
        assert st["needs"], f"{c.id} must explain what's needed"


def test_actions_refuse_without_connection(isolated):
    from core import connectors
    drive = connectors.get_connector("gdrive")
    res = drive.run_action("list_files")
    assert not res["ok"] and "not connected" in res["error"]
    res = drive.run_action("bogus_action")
    assert not res["ok"]


def test_token_roundtrip_and_disconnect(isolated, monkeypatch):
    from core import connectors
    monkeypatch.setattr("core.approvals.confirm", lambda *a, **k: True)
    gmail = connectors.get_connector("gmail")
    gmail.save_token({"access_token": "x", "account": "a@b.c"})
    assert gmail.is_connected()
    assert gmail.status()["account"] == "a@b.c"
    res = gmail.disconnect()
    assert res["ok"] and not gmail.is_connected()


def test_outlook_needs_client_id(isolated):
    from core import connectors
    outlook = connectors.get_connector("outlook")
    st = outlook.status()
    assert not st["connected"]
    assert "ms_client_id" in st["needs"]


def test_connectors_expose_documented_actions(isolated):
    from core import connectors
    drive = connectors.get_connector("gdrive")
    assert set(drive.actions()) == {"list_files", "search_files", "read_file"}
    gmail = connectors.get_connector("gmail")
    assert set(gmail.actions()) == {"list_messages", "search", "read_message"}
    gcal = connectors.get_connector("gcal")
    assert set(gcal.actions()) == {"upcoming"}
    outlook = connectors.get_connector("outlook")
    assert {"list_messages", "search_messages", "read_message", "upcoming_events"} <= set(outlook.actions())
    for c in connectors.list_connectors():
        for name, info in c.actions().items():
            assert info.get("description"), f"{c.id}.{name} needs a description"


def test_pkce_pair_shape():
    from core.connectors.oauth import pkce_pair
    verifier, challenge = pkce_pair()
    assert len(verifier) >= 43 and len(challenge) >= 43
    assert verifier != challenge
