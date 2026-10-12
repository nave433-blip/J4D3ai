"""Round: Google connector token auto-refresh.

No network. The refresh HTTP call and client secrets are stubbed; the
expiry math and save/refresh wiring are exercised for real.
"""
import time

import pytest

from core.connectors import _google as gmod


def _tok(**over):
    base = {"access_token": "old-access", "refresh_token": "old-refresh",
            "expires_in": 3600, "account": "user@example.com"}
    base.update(over)
    return base


def test_stamp_token_records_expiry():
    before = time.time()
    stamped = gmod._stamp_token(_tok())
    assert stamped["obtained_at"] >= before
    assert stamped["expires_at"] == pytest.approx(
        stamped["obtained_at"] + 3600, abs=2)


def test_is_expired_logic():
    assert gmod._is_expired(gmod._stamp_token(_tok())) is False
    assert gmod._is_expired(_tok(expires_at=time.time() - 10)) is True
    # 60s skew: a token dying in 30s already counts as expired.
    assert gmod._is_expired(_tok(expires_at=time.time() + 30)) is True
    assert gmod._is_expired(_tok(expires_at=time.time() + 300)) is False
    # No expiry info at all: can't judge, assume usable.
    assert gmod._is_expired({"access_token": "x"}) is False
    # Legacy shape: obtained_at + expires_in.
    legacy = _tok(obtained_at=time.time() - 4000, expires_in=3600)
    legacy.pop("expires_at", None)
    assert gmod._is_expired(legacy) is True


class _Conn(gmod.GoogleConnector):
    id = "test-google"
    display = "Test Google"

    def __init__(self, token):
        self._token = token
        self.saved = []

    def get_token(self):
        return self._token

    def save_token(self, token):
        self.saved.append(token)
        self._token = token


def test_fresh_token_returned_when_valid():
    conn = _Conn(gmod._stamp_token(_tok()))
    assert conn.fresh_access_token() == "old-access"
    assert conn.saved == [], "no refresh or save should happen"


def test_fresh_token_refreshes_when_expired(monkeypatch):
    conn = _Conn(_tok(expires_at=time.time() - 10))
    monkeypatch.setattr(gmod, "load_google_client_secrets",
                        lambda: {"client_id": "cid", "client_secret": "csec"})
    seen = {}

    def _fake_refresh(url, cid, rtok, client_secret=None):
        seen.update(url=url, cid=cid, rtok=rtok, csec=client_secret)
        return {"access_token": "new-access", "expires_in": 3600}

    monkeypatch.setattr(gmod, "refresh_access_token", _fake_refresh)
    assert conn.fresh_access_token() == "new-access"
    assert seen["rtok"] == "old-refresh"
    assert seen["cid"] == "cid"
    saved = conn.saved[0]
    assert saved["access_token"] == "new-access"
    # Google omits refresh_token on refresh — the old one must be kept.
    assert saved["refresh_token"] == "old-refresh"
    assert saved["account"] == "user@example.com"
    assert "expires_at" in saved


def test_fresh_token_empty_when_refresh_fails(monkeypatch):
    conn = _Conn(_tok(expires_at=time.time() - 10))
    monkeypatch.setattr(gmod, "load_google_client_secrets",
                        lambda: {"client_id": "cid"})
    monkeypatch.setattr(gmod, "refresh_access_token",
                        lambda *a, **k: {"error": "bad refresh"})
    assert conn.fresh_access_token() == ""


def test_fresh_token_empty_without_refresh_token():
    conn = _Conn({"access_token": "x", "expires_at": time.time() - 10})
    assert conn.fresh_access_token() == ""


def test_fresh_token_empty_when_disconnected():
    assert _Conn(None).fresh_access_token() == ""
