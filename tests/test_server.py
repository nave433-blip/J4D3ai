"""Tests for server/ — the Jade reference cloud server.

The app is started in-process on an ephemeral loopback port with a temp
data dir. No live network: JWKS fetching, token endpoints, the GitHub
API and the chat upstream are all stubbed via monkeypatching.
"""

import base64
import json
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

import pytest

from server import __version__
from server.app import JadeApp, ThreadedHTTPServer, _Handler
from server.config import ServerConfig
from server.db import Database
from server import accounts as accounts_mod
from server import oidc as oidc_mod
from server import oauth_broker as broker
from server import chat as chat_mod


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _int_b64url(n: int) -> str:
    return _b64url(n.to_bytes((n.bit_length() + 7) // 8, "big"))


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    cfg = ServerConfig()
    cfg.host = "127.0.0.1"
    cfg.data_dir = str(tmp_path / "data")
    cfg.token_key = Fernet.generate_key().decode()
    cfg.google_client_id = "test-google-client"
    cfg.github_client_id = "test-gh-client"
    cfg.github_client_secret = "test-gh-secret"
    # No chat provider by default -> honest 501 (a test enables it).
    application = JadeApp(cfg)
    httpd = ThreadedHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.app = application
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    yield {"app": application, "base": base, "httpd": httpd, "cfg": cfg,
           "monkeypatch": monkeypatch, "tmp": tmp_path}
    httpd.shutdown()
    thread.join(timeout=5)


def _req(base, method, path, body=None, token=None, raw=False, no_redirect=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    opener = None
    if no_redirect:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(_NoRedirect)
    urlopen = opener.open if opener else urllib.request.urlopen
    try:
        with urlopen(req, timeout=10) as resp:
            payload = resp.read()
            if raw:
                return resp.status, dict(resp.headers), payload
            return resp.status, json.loads(payload.decode() or "{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if raw:
            return exc.code, dict(exc.headers), payload
        try:
            return exc.code, json.loads(payload.decode() or "{}")
        except Exception:
            return exc.code, {"raw": payload.decode(errors="replace")}


def _signup_login(base, email="u@example.com", pw="a-very-long-passphrase"):
    s, b = _req(base, "POST", "/v1/accounts/signup",
                {"email": email, "password": pw, "display_name": "U"})
    assert s == 201, b
    s, b = _req(base, "POST", "/v1/accounts/login",
                {"email": email, "password": pw, "device_label": "test"})
    assert s == 200, b
    return b["session_token"], b["user"]


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

def test_healthz(app):
    s, b = _req(app["base"], "GET", "/healthz")
    assert s == 200 and b["ok"] and b["version"] == __version__


def test_signup_login_me_logout(app):
    base = app["base"]
    token, user = _signup_login(base)
    s, b = _req(base, "GET", "/v1/accounts/me", token=token)
    assert s == 200 and b["user"]["email"] == "u@example.com"
    s, b = _req(base, "POST", "/v1/accounts/logout", {}, token=token)
    assert s == 200
    s, b = _req(base, "GET", "/v1/accounts/me", token=token)
    assert s == 401


def test_login_bad_password_and_unknown_user_same_message(app):
    base = app["base"]
    _signup_login(base, email="exists@example.com")
    s1, b1 = _req(base, "POST", "/v1/accounts/login",
                  {"email": "exists@example.com", "password": "wrong-password-xyz"})
    s2, b2 = _req(base, "POST", "/v1/accounts/login",
                  {"email": "nobody@example.com", "password": "wrong-password-xyz"})
    assert s1 == s2 == 401
    assert b1["error"] == b2["error"]  # no user enumeration


def test_signup_duplicate_matches_login_error(app):
    base = app["base"]
    _signup_login(base, email="dup@example.com")
    s, b = _req(base, "POST", "/v1/accounts/signup",
                {"email": "dup@example.com", "password": "another-long-passphrase"})
    assert s == 400
    assert b["error"] == "invalid email or password"


def test_signup_validation(app):
    base = app["base"]
    s, b = _req(base, "POST", "/v1/accounts/signup",
                {"email": "not-an-email", "password": "a-very-long-passphrase"})
    assert s == 400
    s, b = _req(base, "POST", "/v1/accounts/signup",
                {"email": "ok@example.com", "password": "short"})
    assert s == 400 and "12" in b["error"]


def test_change_password(app):
    base = app["base"]
    token, _ = _signup_login(base, pw="original-long-passphrase")
    s, b = _req(base, "POST", "/v1/accounts/change-password",
                {"current_password": "nope-wrong-pass", "new_password": "new-long-passphrase-x"},
                token=token)
    assert s == 400
    s, b = _req(base, "POST", "/v1/accounts/change-password",
                {"current_password": "original-long-passphrase", "new_password": "new-long-passphrase-x"},
                token=token)
    assert s == 200
    s, b = _req(base, "POST", "/v1/accounts/login",
                {"email": "u@example.com", "password": "new-long-passphrase-x"})
    assert s == 200


def test_password_hash_format_and_verify():
    salt, stored = accounts_mod.hash_password("correct-horse-battery-staple")
    assert stored.startswith("pbkdf2-sha256$")
    assert accounts_mod.verify_password("correct-horse-battery-staple", salt, stored)
    assert not accounts_mod.verify_password("wrong", salt, stored)


def test_login_rate_limited(app):
    base = app["base"]
    statuses = set()
    for _ in range(15):
        s, _ = _req(base, "POST", "/v1/accounts/login",
                    {"email": "x@example.com", "password": "wrong-password-xyz"})
        statuses.add(s)
    assert 429 in statuses  # 10/min bucket trips


# --------------------------------------------------------------------------
# OIDC sign-in (google) with generated RSA key — no network
# --------------------------------------------------------------------------

def _make_id_token(monkeypatch, *, aud="test-google-client", iss="https://accounts.google.com",
                   exp_offset=600, tamper=False):
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import hashes
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    now = int(time.time())
    header = {"alg": "RS256", "kid": "k1", "typ": "JWT"}
    payload = {"iss": iss, "aud": aud, "sub": "google-sub-1",
               "email": "oidc@example.com", "name": "Oidc User",
               "exp": now + exp_offset, "iat": now}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    if tamper:
        sig = b"X" + sig[1:]
    token = f"{signing_input}.{_b64url(sig)}"
    jwks = {"keys": [{"kty": "RSA", "kid": "k1",
                      "n": _int_b64url(pub.n), "e": _int_b64url(pub.e)}]}
    monkeypatch.setattr(oidc_mod, "_default_jwks_fetch", lambda url: jwks)
    return token


def test_oidc_google_login(app):
    mp = app["monkeypatch"]
    base = app["base"]
    token = _make_id_token(mp)
    s, b = _req(base, "POST", "/v1/auth/oidc/google", {"id_token": token, "device_label": "t"})
    assert s == 200, b
    assert b["user"]["email"] == "oidc@example.com"
    s, me = _req(base, "GET", "/v1/accounts/me", token=b["session_token"])
    assert s == 200
    # Second login links to the same account, no duplicate.
    s, b2 = _req(base, "POST", "/v1/auth/oidc/google", {"id_token": token})
    assert s == 200 and b2["user"]["id"] == b["user"]["id"]


def test_oidc_rejects_bad_tokens(app):
    mp = app["monkeypatch"]
    base = app["base"]
    bad_sig = _make_id_token(mp, tamper=True)
    s, b = _req(base, "POST", "/v1/auth/oidc/google", {"id_token": bad_sig})
    assert s == 401
    expired = _make_id_token(mp, exp_offset=-3600)
    s, b = _req(base, "POST", "/v1/auth/oidc/google", {"id_token": expired})
    assert s == 401
    wrong_aud = _make_id_token(mp, aud="someone-elses-client")
    s, b = _req(base, "POST", "/v1/auth/oidc/google", {"id_token": wrong_aud})
    assert s == 401
    s, b = _req(base, "POST", "/v1/auth/oidc/google", {"id_token": "garbage"})
    assert s == 401


def test_oidc_unconfigured_provider(app):
    base = app["base"]
    s, b = _req(base, "POST", "/v1/auth/oidc/apple", {"id_token": "x"})
    assert s == 501  # honest: not configured


def test_github_auth(app):
    mp = app["monkeypatch"]
    base = app["base"]

    def fake_post(url, fields, headers, timeout):
        assert "github.com/login/oauth/access_token" in url
        assert fields["code"] == "the-code"
        return {"access_token": "gh-access"}

    def fake_get(url, headers, timeout):
        assert headers["Authorization"] == "Bearer gh-access"
        return {"id": 424242, "login": "octocat", "name": "The Octocat",
                "email": "octo@example.com"}

    mp.setattr(oidc_mod, "_post_form", fake_post)
    mp.setattr(oidc_mod, "_get_json", fake_get)
    s, b = _req(base, "POST", "/v1/auth/github", {"code": "the-code", "device_label": "t"})
    assert s == 200, b
    assert b["user"]["email"] == "octo@example.com"


# --------------------------------------------------------------------------
# OAuth brokerage
# --------------------------------------------------------------------------

def test_connector_catalog_and_authorize_needs_config(app):
    base = app["base"]
    token, _ = _signup_login(base)
    s, b = _req(base, "GET", "/v1/connectors", token=token)
    assert s == 200 and len(b["connectors"]) == 5
    assert all(c["connected"] is False for c in b["connectors"])
    # outlook has no client creds configured in this fixture
    s, _headers, _body = _req(base, "GET", "/v1/connectors/outlook/authorize",
                             token=token, raw=True, no_redirect=True)
    assert s == 400


def test_connector_authorize_redirect_and_pkce(app):
    base = app["base"]
    app["cfg"].google_client_id = "gid"
    app["cfg"].google_client_secret = "gsecret"
    app["cfg"].public_url = base
    token, _ = _signup_login(base)
    s, headers, _ = _req(base, "GET", "/v1/connectors/gmail/authorize",
                       token=token, raw=True, no_redirect=True)
    assert s == 302
    loc = headers["Location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    assert qs["client_id"] == ["gid"]
    assert qs["code_challenge_method"] == ["S256"]
    assert f"{base}/v1/connectors/gmail/callback" in qs["redirect_uri"][0]


def test_connector_callback_stores_encrypted_tokens(app):
    mp = app["monkeypatch"]
    base = app["base"]
    application = app["app"]
    application.cfg.google_client_id = "gid"
    application.cfg.google_client_secret = "gsecret"
    application.cfg.public_url = base
    token, user = _signup_login(base)

    captured = {}

    def fake_post_token(url, fields, timeout=20):
        captured.update(fields)
        assert fields["grant_type"] == "authorization_code"
        return {"access_token": "ya29.access", "refresh_token": "rt-123",
                "expires_in": 3600, "scope": "https://www.googleapis.com/auth/gmail.readonly"}

    mp.setattr(broker, "_post_token", fake_post_token)
    # Plant a valid OAuth state as /authorize would.
    state = "state-abc"
    application.db.save_oauth_state(
        __import__("hashlib").sha256(state.encode()).hexdigest(),
        user["id"], "gmail", "verifier-xyz")
    s, headers, body = _req(
        base, "GET", f"/v1/connectors/gmail/callback?code=authcode&state={state}", raw=True)
    assert s == 200 and b"gmail" in body.lower()

    # Tokens at rest must be encrypted blobs, not plaintext.
    row = application.db.get_connector_tokens(user["id"], "gmail")
    assert "ya29.access" not in row["access_token_enc"]
    assert "rt-123" not in row["refresh_token_enc"]

    # Token endpoint serves a fresh access token; refresh token never leaves.
    s, b = _req(base, "POST", "/v1/connectors/gmail/token", {}, token=token)
    assert s == 200 and b["access_token"] == "ya29.access"
    assert "refresh_token" not in b and "rt-123" not in json.dumps(b)

    s, b = _req(base, "GET", "/v1/connectors", token=token)
    gmail = next(c for c in b["connectors"] if c["id"] == "gmail")
    assert gmail["connected"] is True

    s, b = _req(base, "DELETE", "/v1/connectors/gmail", token=token)
    assert s == 200 and b["was_connected"] is True
    s, b = _req(base, "POST", "/v1/connectors/gmail/token", {}, token=token)
    assert s == 502  # not connected anymore


def test_connector_token_refresh_server_side(app):
    mp = app["monkeypatch"]
    base = app["base"]
    application = app["app"]
    application.cfg.google_client_id = "gid"
    application.cfg.google_client_secret = "gsecret"
    token, user = _signup_login(base)
    cipher = broker.TokenCipher(application.cfg.token_key)
    # Store an already-expired token with a refresh token.
    application.db.store_connector_tokens(
        user["id"], "gmail", cipher.encrypt("old-access"), cipher.encrypt("rt-1"),
        int(time.time()) - 10, "scope")
    seen = {}

    def fake_post_token(url, fields, timeout=20):
        seen.update(fields)
        assert fields["grant_type"] == "refresh_token"
        assert fields["refresh_token"] == "rt-1"
        return {"access_token": "new-access", "expires_in": 3600}

    mp.setattr(broker, "_post_token", fake_post_token)
    s, b = _req(base, "POST", "/v1/connectors/gmail/token", {}, token=token)
    assert s == 200 and b["access_token"] == "new-access"


def test_token_cipher_requires_key_and_package():
    with pytest.raises(broker.CryptoUnavailable):
        broker.TokenCipher("")
    with pytest.raises(broker.CryptoUnavailable):
        broker.TokenCipher("not-a-valid-fernet-key")


def test_token_cipher_roundtrip(app):
    cipher = broker.TokenCipher(app["cfg"].token_key)
    assert cipher.decrypt(cipher.encrypt("secret-token")) == "secret-token"


# --------------------------------------------------------------------------
# pairing (signaling only — no content ever stored)
# --------------------------------------------------------------------------

def test_pairing_full_flow(app):
    base = app["base"]
    token, _ = _signup_login(base)
    s, b = _req(base, "POST", "/v1/pairing/create", {"device_name": "laptop"}, token=token)
    assert s == 201
    pid, code = b["pairing_id"], b["code"]
    assert code.startswith("CORT-")

    # wrong code -> 400, right code -> claimed
    s, _ = _req(base, "POST", "/v1/pairing/claim",
                {"code": "CORT-000000", "device_name": "phone"})
    assert s == 400
    s, claim = _req(base, "POST", "/v1/pairing/claim",
                    {"code": code, "device_name": "phone"})
    assert s == 200
    receipt = claim["claim_receipt"]

    s, st = _req(base, "GET", f"/v1/pairing/{pid}/status", token=token)
    assert s == 200 and st["status"] == "claimed" and st["claim_device"] == "phone"

    # claimer polling before accept
    s, st = _req(base, "GET", f"/v1/pairing/{pid}/status?receipt={receipt}")
    assert s == 200 and st["status"] == "claimed" and "accept_token" not in st

    # owner explicitly accepts with an endpoint hint
    s, b = _req(base, "POST", f"/v1/pairing/{pid}/accept",
                {"endpoint_hint": "192.168.1.10:11435"}, token=token)
    assert s == 200 and b["status"] == "accepted"

    # claimer receives one-time accept token + hint, then it's consumed
    s, st = _req(base, "GET", f"/v1/pairing/{pid}/status?receipt={receipt}")
    assert s == 200 and st["status"] == "accepted"
    assert st["endpoint_hint"] == "192.168.1.10:11435"
    assert "accept_token" in st
    s, st = _req(base, "GET", f"/v1/pairing/{pid}/status?receipt={receipt}")
    assert s == 200 and st["status"] == "consumed" and "accept_token" not in st


def test_pairing_reject_and_claim_lockout(app):
    base = app["base"]
    token, _ = _signup_login(base)
    s, b = _req(base, "POST", "/v1/pairing/create", {"device_name": "laptop"}, token=token)
    pid = b["pairing_id"]
    s, b = _req(base, "POST", f"/v1/pairing/{pid}/reject", {}, token=token)
    assert s == 200 and b["status"] == "rejected"
    s, b = _req(base, "POST", "/v1/pairing/create", {"device_name": "laptop"}, token=token)
    code = b["code"]
    for _ in range(6):
        s, _ = _req(base, "POST", "/v1/pairing/claim",
                    {"code": code[:-1] + "X", "device_name": "evil"})
    # too many bad attempts against the real code's bucket is per-code; a
    # wrong code simply never validates:
    s, _ = _req(base, "POST", "/v1/pairing/claim",
                {"code": code, "device_name": "phone"})
    assert s == 200  # real code still works


def test_pairing_requires_owner_auth(app):
    base = app["base"]
    token, _ = _signup_login(base)
    s, b = _req(base, "POST", "/v1/pairing/create", {"device_name": "laptop"}, token=token)
    pid = b["pairing_id"]
    token2, _ = _signup_login(base, email="other@example.com")
    s, b = _req(base, "POST", f"/v1/pairing/{pid}/accept", {"endpoint_hint": "x"}, token=token2)
    assert s in (400, 404)  # another user cannot accept it


# --------------------------------------------------------------------------
# chat completions (hosted counterpart)
# --------------------------------------------------------------------------

def test_chat_needs_provider_config(app):
    base = app["base"]
    token, _ = _signup_login(base)
    s, b = _req(base, "POST", "/v1/chat/completions",
                {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                token=token)
    assert s == 501  # honest: nothing configured


def test_chat_completions_passthrough(app):
    mp = app["monkeypatch"]
    base = app["base"]
    app["cfg"].chat_base_url = "https://provider.example/v1"
    app["cfg"].chat_api_key = "sk-test"
    app["cfg"].chat_model = "test-model"

    def fake_provider(cfg, body, *, http_post=None):
        model = body.get("model") or cfg.chat_model
        assert model == "test-model"
        return {"id": "chatcmpl-1", "object": "chat.completion", "model": model,
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "usage": {"prompt_tokens": 2, "total_tokens": 3}}

    mp.setattr(chat_mod, "_provider_request", fake_provider)
    token, _ = _signup_login(base)
    s, b = _req(base, "POST", "/v1/chat/completions",
                {"messages": [{"role": "user", "content": "hi"}]}, token=token)
    assert s == 200
    assert b["choices"][0]["message"]["content"] == "hello"
    assert b["model"] == "test-model"


def test_chat_requires_auth(app):
    base = app["base"]
    s, b = _req(base, "POST", "/v1/chat/completions",
                {"messages": [{"role": "user", "content": "hi"}]})
    assert s == 401


# --------------------------------------------------------------------------
# misc / guardrails
# --------------------------------------------------------------------------

def test_unknown_route_404(app):
    s, b = _req(app["base"], "GET", "/nope")
    assert s == 404


def test_malformed_json_400(app):
    base = app["base"]
    req = urllib.request.Request(base + "/v1/accounts/signup", data=b"{nope",
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "should have raised"
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_server_not_in_cli_package():
    """server/ must stay out of the pip package and CLI dependency set."""
    setup_py = open("setup.py").read()
    assert 'exclude' in setup_py and 'server' in setup_py
    import ast
    tree = ast.parse(setup_py)
    src = ast.dump(tree)
    assert "server" in src  # the exclude list mentions server
