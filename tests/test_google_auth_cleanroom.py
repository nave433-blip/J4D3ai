"""
Clean-room Google auth tests — no vendor SDK, no network.

Covers core/google_auth.py (stdlib-only PKCE + direct REST) and the
clean-room Drive listing in tools/cloud.py. Everything external is
stubbed: keyring, browser, console input, and the oauth helpers.

Run with: pytest -q
"""
import json
import time

import pytest

import core.google_auth as ga


# ---------------------------------------------------------------------------
# Helpers


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, key):
        return self.store.get((service, key))

    def set_password(self, service, key, value):
        self.store[(service, key)] = value


@pytest.fixture()
def fake_keyring(monkeypatch):
    fk = FakeKeyring()
    monkeypatch.setattr("keyring.get_password", fk.get_password)
    monkeypatch.setattr("keyring.set_password", fk.set_password)
    return fk


def _valid_token(**over):
    tok = {
        "access_token": "ya29.valid",
        "refresh_token": "1//refresh",
        "expires_in": 3600,
        "obtained_at": time.time(),
        "token_type": "Bearer",
        "client_id": "cid",
        "client_secret": "csec",
    }
    tok.update(over)
    return tok


# ---------------------------------------------------------------------------
# Token store: read / expiry / refresh / legacy migration


def test_get_credentials_returns_valid_stored_token(fake_keyring, monkeypatch):
    fake_keyring.set_password(ga.KEYRING_SERVICE, ga.TOKEN_KEY,
                              json.dumps(_valid_token()))
    called = []
    monkeypatch.setattr(ga, "run_pkce_flow",
                        lambda *a, **k: called.append(1) or {"error": "x"})
    token = ga.GoogleAuth.get_credentials()
    assert token["access_token"] == "ya29.valid"
    assert called == []  # no flow needed


def test_get_credentials_refreshes_expired_token(fake_keyring, monkeypatch):
    old = _valid_token(obtained_at=time.time() - 7200)  # expired
    fake_keyring.set_password(ga.KEYRING_SERVICE, ga.TOKEN_KEY, json.dumps(old))

    def fake_refresh(url, cid, refresh_token, client_secret=None):
        assert refresh_token == "1//refresh"
        return {"access_token": "ya29.fresh", "expires_in": 3600}

    monkeypatch.setattr(ga, "refresh_access_token", fake_refresh)
    token = ga.GoogleAuth.get_credentials()
    assert token["access_token"] == "ya29.fresh"
    # refresh token preserved, new token persisted
    saved = json.loads(fake_keyring.get_password(ga.KEYRING_SERVICE, ga.TOKEN_KEY))
    assert saved["access_token"] == "ya29.fresh"
    assert saved["refresh_token"] == "1//refresh"


def test_get_credentials_refresh_failure_falls_back_to_flow(fake_keyring, monkeypatch):
    old = _valid_token(obtained_at=time.time() - 7200)
    fake_keyring.set_password(ga.KEYRING_SERVICE, ga.TOKEN_KEY, json.dumps(old))
    monkeypatch.setattr(ga, "refresh_access_token",
                        lambda *a, **k: {"error": "bad refresh"})
    flow_token = _valid_token(access_token="ya29.flow")
    monkeypatch.setattr(ga.GoogleAuth, "run_flow",
                        staticmethod(lambda: flow_token))
    token = ga.GoogleAuth.get_credentials()
    assert token["access_token"] == "ya29.flow"


def test_legacy_google_oauth2_format_is_migrated(fake_keyring, monkeypatch):
    legacy = {
        "token": "ya29.legacy",
        "refresh_token": "1//legacy-refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "old-cid",
        "client_secret": "old-csec",
        "scopes": ga.SCOPES,
        "expiry": "2030-01-01T00:00:00Z",
    }
    fake_keyring.set_password(ga.KEYRING_SERVICE, ga.TOKEN_KEY, json.dumps(legacy))
    monkeypatch.setattr(ga, "run_pkce_flow",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no flow")))
    token = ga.GoogleAuth.get_credentials()
    assert token["access_token"] == "ya29.legacy"
    assert token["refresh_token"] == "1//legacy-refresh"
    # migrated and re-saved in the new format
    saved = json.loads(fake_keyring.get_password(ga.KEYRING_SERVICE, ga.TOKEN_KEY))
    assert saved["access_token"] == "ya29.legacy"
    assert "token" not in saved or saved.get("access_token")


def test_legacy_jarvis_keyring_service_still_read(fake_keyring, monkeypatch):
    fake_keyring.set_password(ga._KEYRING_SERVICE_LEGACY, ga.TOKEN_KEY,
                              json.dumps(_valid_token(access_token="ya29.old")))
    token = ga.GoogleAuth.get_credentials()
    assert token["access_token"] == "ya29.old"


def test_corrupt_keyring_entry_triggers_flow(fake_keyring, monkeypatch):
    fake_keyring.set_password(ga.KEYRING_SERVICE, ga.TOKEN_KEY, "not-json{{{")
    flow_token = _valid_token(access_token="ya29.recovered")
    monkeypatch.setattr(ga.GoogleAuth, "run_flow",
                        staticmethod(lambda: flow_token))
    token = ga.GoogleAuth.get_credentials()
    assert token["access_token"] == "ya29.recovered"


def test_is_expired_edges():
    assert ga._is_expired(_valid_token()) is False
    assert ga._is_expired(_valid_token(obtained_at=time.time() - 7200)) is True
    assert ga._is_expired(_valid_token(expires_at=time.time() + 3600)) is False
    assert ga._is_expired(_valid_token(expires_at=time.time() - 10)) is True
    assert ga._is_expired({"access_token": "x"}) is False  # no info: assume usable


# ---------------------------------------------------------------------------
# run_flow


def test_run_flow_aborts_without_client_secrets(monkeypatch):
    monkeypatch.setattr(ga, "load_google_client_secrets", lambda: None)
    monkeypatch.setattr(ga.console, "input", lambda prompt="": "")
    assert ga.GoogleAuth.run_flow() is None


def test_run_flow_uses_pkce_and_saves(monkeypatch, fake_keyring):
    monkeypatch.setattr(ga, "load_google_client_secrets",
                        lambda: {"client_id": "cid", "client_secret": "csec"})

    seen = {}

    def fake_pkce(auth_url, token_url, client_id, scopes, client_secret=None,
                  extra_auth_params=None):
        seen["scopes"] = scopes
        seen["extra"] = extra_auth_params
        assert "accounts.google.com" in auth_url
        return {"access_token": "ya29.new", "expires_in": 3600}

    monkeypatch.setattr(ga, "run_pkce_flow", fake_pkce)
    token = ga.GoogleAuth.run_flow()
    assert token["access_token"] == "ya29.new"
    assert token["client_id"] == "cid"
    assert "https://www.googleapis.com/auth/userinfo.email" in seen["scopes"]
    assert seen["extra"]["access_type"] == "offline"  # refresh tokens, please


def test_run_flow_error_returns_none(monkeypatch):
    monkeypatch.setattr(ga, "load_google_client_secrets",
                        lambda: {"client_id": "cid", "client_secret": None})
    monkeypatch.setattr(ga, "run_pkce_flow",
                        lambda *a, **k: {"error": "access_denied"})
    assert ga.GoogleAuth.run_flow() is None


def test_run_flow_prompts_and_persists_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(ga, "load_google_client_secrets", lambda: None)
    inputs = iter(["my-cid", "my-csec"])
    monkeypatch.setattr(ga.console, "input", lambda prompt="": next(inputs))
    monkeypatch.setattr(ga, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ga, "run_pkce_flow",
                        lambda *a, **k: {"access_token": "ya29.x", "expires_in": 60})
    token = ga.GoogleAuth.run_flow()
    assert token["access_token"] == "ya29.x"
    saved = json.loads((tmp_path / "google_client.json").read_text())
    assert saved["installed"]["client_id"] == "my-cid"


# ---------------------------------------------------------------------------
# get_user_email


def test_get_user_email(monkeypatch):
    monkeypatch.setattr(ga, "api_get",
                        lambda url, tok: {"email": "evan@example.com"})
    assert ga.GoogleAuth.get_user_email(_valid_token()) == "evan@example.com"


def test_get_user_email_handles_api_error(monkeypatch):
    monkeypatch.setattr(ga, "api_get", lambda url, tok: {"error": "bad token"})
    assert ga.GoogleAuth.get_user_email(_valid_token()) is None


def test_get_user_email_no_token():
    assert ga.GoogleAuth.get_user_email({}) is None
    assert ga.GoogleAuth.get_user_email(None) is None


# ---------------------------------------------------------------------------
# tools/cloud.py — clean-room Drive listing


def test_list_gdrive_uses_rest(monkeypatch):
    import tools.cloud as cloud

    monkeypatch.setattr(cloud, "get_env_with_config", lambda k: "ya29.drive")

    seen = {}

    def fake_api_get(url, token, params=None, timeout=30):
        seen["url"] = url
        seen["token"] = token
        seen["params"] = params
        return {"files": [{"id": "1", "name": "a.txt"}, {"id": "2", "name": "b.txt"}]}

    monkeypatch.setattr("core.connectors.oauth.api_get", fake_api_get)
    assert cloud.list_gdrive() == ["a.txt", "b.txt"]
    assert "drive/v3/files" in seen["url"]
    assert seen["token"] == "ya29.drive"


def test_list_gdrive_no_token(monkeypatch):
    import tools.cloud as cloud

    monkeypatch.setattr(cloud, "get_env_with_config", lambda k: None)
    assert "not configured" in cloud.list_gdrive()


def test_list_gdrive_api_error(monkeypatch):
    import tools.cloud as cloud

    monkeypatch.setattr(cloud, "get_env_with_config", lambda k: "bad")
    monkeypatch.setattr("core.connectors.oauth.api_get",
                        lambda *a, **k: {"error": "HTTP 401: bad"})
    assert "GDrive Error" in cloud.list_gdrive()


def test_drive_access_token_accepts_json_dict(monkeypatch):
    import tools.cloud as cloud

    monkeypatch.setattr(cloud, "get_env_with_config",
                        lambda k: json.dumps({"access_token": "ya29.j"}))
    assert cloud._drive_access_token() == "ya29.j"
    monkeypatch.setattr(cloud, "get_env_with_config", lambda k: "ya29.plain")
    assert cloud._drive_access_token() == "ya29.plain"
    monkeypatch.setattr(cloud, "get_env_with_config", lambda k: None)
    assert cloud._drive_access_token() is None


# ---------------------------------------------------------------------------
# No vendor SDK imports anywhere in the migrated path


def test_no_vendor_sdk_imports():
    import ast
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    banned = ("googleapiclient", "google_auth_oauthlib", "google.oauth2", "google.auth")
    offenders = []
    for path in list((repo / "core").rglob("*.py")) + \
               list((repo / "tools").rglob("*.py")) + \
               list((repo / "server").rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n.split(".")[0] in ("googleapiclient", "google_auth_oauthlib") or
                   n in ("google.oauth2", "google.auth") or
                   n.startswith(("google.oauth2.", "google.auth."))
                   for n in names):
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"vendor SDK imports remain: {offenders}"
