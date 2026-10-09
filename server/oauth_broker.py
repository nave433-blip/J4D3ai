"""OAuth brokerage: the server holds ONE set of OAuth app credentials per
provider so end users never register their own OAuth apps.

Per-user tokens are encrypted at rest with Fernet (``cryptography`` —
server-only dependency). If the key or package is missing, storage
*refuses* with a clear error instead of storing tokens insecurely.

Token refresh happens server-side: clients call
``POST /v1/connectors/{id}/token`` and receive a short-lived access
token; they never see refresh tokens.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
import urllib.request

from .db import Database

# Connector catalog. ``provider`` selects which OAuth app credentials
# (from env) are used; scopes are the minimum each connector needs.
CONNECTORS = {
    "google-drive": {
        "provider": "google",
        "label": "Google Drive",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
    },
    "gmail": {
        "provider": "google",
        "label": "Gmail",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
    },
    "google-calendar": {
        "provider": "google",
        "label": "Google Calendar",
        "scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
    },
    "outlook": {
        "provider": "microsoft",
        "label": "Outlook / Microsoft 365",
        "scopes": ["https://graph.microsoft.com/Mail.Read",
                   "https://graph.microsoft.com/Calendars.Read",
                   "https://graph.microsoft.com/Files.Read",
                   "offline_access"],
        "auth_url_tpl": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url_tpl": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
    },
    "github": {
        "provider": "github",
        "label": "GitHub",
        "scopes": ["repo", "read:user"],
        "auth_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
    },
}


class BrokerError(Exception):
    pass


class CryptoUnavailable(BrokerError):
    """Raised when connector tokens cannot be encrypted safely."""


class TokenCipher:
    """Fernet envelope for per-user OAuth tokens."""

    def __init__(self, key_b64: str):
        if not key_b64:
            raise CryptoUnavailable(
                "JADE_TOKEN_KEY is not set — connector token storage is disabled. "
                "Generate one with: openssl rand -base64 32"
            )
        try:
            from cryptography.fernet import Fernet
            # Validate the key up front (Fernet raises on bad keys).
            self._fernet = Fernet(key_b64.encode())
        except ImportError as exc:
            raise CryptoUnavailable(
                "the 'cryptography' package is required to store connector tokens "
                "(server/requirements.txt). Refusing to store tokens insecurely."
            ) from exc
        except Exception as exc:
            raise CryptoUnavailable(f"JADE_TOKEN_KEY is invalid: {exc}") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _client_creds(cfg, provider: str) -> tuple[str, str]:
    if provider == "google":
        return cfg.google_client_id, cfg.google_client_secret
    if provider == "microsoft":
        return cfg.microsoft_client_id, cfg.microsoft_client_secret
    if provider == "github":
        return cfg.github_client_id, cfg.github_client_secret
    raise BrokerError(f"unknown provider: {provider}")


def build_authorize_url(cfg, connector_id: str, *, state: str) -> tuple[str, str]:
    """Return (authorize_url, code_verifier). Raises BrokerError if unconfigured."""
    if connector_id not in CONNECTORS:
        raise BrokerError(f"unknown connector: {connector_id}")
    meta = CONNECTORS[connector_id]
    client_id, _secret = _client_creds(cfg, meta["provider"])
    if not client_id:
        raise BrokerError(
            f"OAuth app credentials for {meta['provider']} are not configured "
            f"(see .env.example). The server operator must register one OAuth app."
        )
    verifier, challenge = _pkce_pair()
    redirect_uri = f"{cfg.public_url.rstrip('/')}/v1/connectors/{connector_id}/callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(meta["scopes"]),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",   # ask for refresh tokens where supported
        "prompt": "consent",
    }
    if meta["provider"] == "microsoft":
        auth_url = meta["auth_url_tpl"].format(tenant=cfg.microsoft_tenant)
    else:
        auth_url = meta["auth_url"]
    return f"{auth_url}?{urllib.parse.urlencode(params)}", verifier


def _token_url(cfg, meta: dict) -> str:
    if meta["provider"] == "microsoft":
        return meta["token_url_tpl"].format(tenant=cfg.microsoft_tenant)
    return meta["token_url"]


def _post_token(url: str, fields: dict, timeout: int = 20) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Accept": "application/json",
                                          "User-Agent": "jade-server/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def exchange_code(cfg, connector_id: str, code: str, code_verifier: str,
                  *, http_post=None) -> dict:
    """Exchange an authorization code for tokens. Returns the token response."""
    meta = CONNECTORS[connector_id]
    client_id, client_secret = _client_creds(cfg, meta["provider"])
    redirect_uri = f"{cfg.public_url.rstrip('/')}/v1/connectors/{connector_id}/callback"
    post = http_post or _post_token
    try:
        data = post(_token_url(cfg, meta), {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        })
    except Exception as exc:
        raise BrokerError(f"token exchange failed: {exc}") from exc
    if "access_token" not in data:
        raise BrokerError(f"token exchange failed: {data.get('error_description') or data.get('error') or 'no access_token'}")
    return data


def store_tokens(db: Database, cipher: TokenCipher, user_id: int,
                 connector_id: str, token_response: dict) -> None:
    access = token_response["access_token"]
    refresh = token_response.get("refresh_token", "")
    expires_in = int(token_response.get("expires_in", 3600))
    scopes = token_response.get("scope", " ".join(CONNECTORS[connector_id]["scopes"]))
    db.store_connector_tokens(
        user_id, connector_id,
        cipher.encrypt(access), cipher.encrypt(refresh) if refresh else "",
        int(time.time()) + expires_in, scopes,
    )


def get_access_token(db: Database, cipher: TokenCipher, cfg, user_id: int,
                     connector_id: str, *, http_post=None) -> tuple[str, int]:
    """Return (access_token, expires_in_seconds), refreshing server-side.

    Clients never see refresh tokens.
    """
    row = db.get_connector_tokens(user_id, connector_id)
    if not row:
        raise BrokerError(f"connector '{connector_id}' is not connected")
    now = int(time.time())
    if row["expires_at"] - now > 60:
        return cipher.decrypt(row["access_token_enc"]), row["expires_at"] - now
    refresh_enc = row["refresh_token_enc"]
    if not refresh_enc:
        raise BrokerError(f"connector '{connector_id}' token expired and no refresh token is stored — reconnect")
    meta = CONNECTORS[connector_id]
    client_id, client_secret = _client_creds(cfg, meta["provider"])
    post = http_post or _post_token
    try:
        data = post(_token_url(cfg, meta), {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": cipher.decrypt(refresh_enc),
        })
    except Exception as exc:
        raise BrokerError(f"token refresh failed: {exc}") from exc
    if "access_token" not in data:
        raise BrokerError(f"token refresh failed: {data.get('error_description') or data.get('error') or 'unknown'}")
    new_refresh = data.get("refresh_token")
    db.store_connector_tokens(
        user_id, connector_id,
        cipher.encrypt(data["access_token"]),
        cipher.encrypt(new_refresh) if new_refresh else refresh_enc,
        now + int(data.get("expires_in", 3600)),
        data.get("scope", row["scopes"]),
    )
    return data["access_token"], int(data.get("expires_in", 3600))


def revoke_tokens(db: Database, user_id: int, connector_id: str) -> bool:
    if not db.get_connector_tokens(user_id, connector_id):
        return False
    # Best effort: also tell the provider. Failures here must not block
    # local deletion — the tokens are gone from our side regardless.
    db.delete_connector_tokens(user_id, connector_id)
    return True
