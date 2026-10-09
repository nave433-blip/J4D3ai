"""Server-side "Sign in with X" validation.

Supported providers and how each is verified:

- ``google`` / ``apple`` / ``microsoft`` — standard OIDC: the client
  sends the provider's ``id_token`` (JWT); the server fetches the
  provider's JWKS, verifies the RS256 signature with ``cryptography``,
  and checks ``iss`` / ``aud`` / ``exp`` / ``iat``. No shortcuts: an
  unverifiable token is rejected.
- ``github`` — GitHub has no user-facing OIDC ID token. Instead the
  client sends the OAuth ``code``; the server exchanges it for an access
  token and calls ``GET https://api.github.com/user``. The GitHub numeric
  user id becomes the linked subject.

The ``cryptography`` package is required for signature verification
(server-only dependency — never needed by the CLI). JWKS fetching uses
urllib; pass ``jwks_fetcher`` in tests to avoid network.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.request

OIDC_PROVIDERS = {
    "google": {
        "issuer": "https://accounts.google.com",
        "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
    },
    "apple": {
        "issuer": "https://appleid.apple.com",
        "jwks_uri": "https://appleid.apple.com/auth/keys",
    },
    "microsoft": {
        # {tenant} is substituted from config (default "common").
        "issuer_prefix": "https://login.microsoftonline.com/",
        "jwks_uri_tpl": "https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
    },
}


class OIDCError(Exception):
    pass


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _default_jwks_fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "jade-server/0.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _rsa_public_key_from_jwk(jwk: dict):
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    return RSAPublicNumbers(e, n).public_key()


def verify_id_token(provider: str, id_token: str, client_id: str, *,
                    tenant: str = "common", leeway: int = 120,
                    jwks_fetcher=None) -> dict:
    """Verify an OIDC ID token. Returns claims ``{sub, email, name}``.

    Raises OIDCError on any failure. Never returns unverified claims.
    """
    if provider not in OIDC_PROVIDERS:
        raise OIDCError(f"unsupported OIDC provider: {provider}")
    try:
        header_b64, payload_b64, sig_b64 = id_token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except Exception as exc:
        raise OIDCError(f"malformed token: {exc}") from exc

    if header.get("alg") != "RS256" or not header.get("kid"):
        raise OIDCError("unsupported token algorithm (need RS256 with kid)")

    meta = OIDC_PROVIDERS[provider]
    if provider == "microsoft":
        expected_iss = f"{meta['issuer_prefix']}{tenant}/v2.0"
        jwks_uri = meta["jwks_uri_tpl"].format(tenant=tenant)
    else:
        expected_iss = meta["issuer"]
        jwks_uri = meta["jwks_uri"]

    fetcher = jwks_fetcher or _default_jwks_fetch
    try:
        jwks = fetcher(jwks_uri)
    except Exception as exc:
        raise OIDCError(f"could not fetch JWKS: {exc}") from exc
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == header["kid"]), None)
    if not key:
        raise OIDCError("signing key not found in JWKS")

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.exceptions import InvalidSignature
    public_key = _rsa_public_key_from_jwk(key)
    signed = f"{header_b64}.{payload_b64}".encode()
    try:
        public_key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise OIDCError("invalid token signature") from exc

    now = int(time.time())
    if claims.get("iss") != expected_iss:
        raise OIDCError("wrong token issuer")
    aud = claims.get("aud")
    if aud != client_id and not (isinstance(aud, list) and client_id in aud):
        raise OIDCError("token audience mismatch")
    if not isinstance(claims.get("exp"), int) or claims["exp"] < now - leeway:
        raise OIDCError("token expired")
    if isinstance(claims.get("iat"), int) and claims["iat"] > now + leeway:
        raise OIDCError("token issued in the future")
    if not claims.get("sub"):
        raise OIDCError("token has no subject")

    return {
        "sub": str(claims["sub"]),
        "email": str(claims.get("email", "")),
        "name": str(claims.get("name", "")),
    }


def github_user_from_code(code: str, client_id: str, client_secret: str,
                          *, http_post=None, http_get=None, timeout: int = 15) -> dict:
    """Exchange a GitHub OAuth code and return ``{sub, email, name}``.

    ``sub`` is the numeric GitHub user id (stable, unlike logins).
    ``http_post``/``http_get`` are injectable for tests.
    """
    import urllib.parse
    post = http_post or _post_form
    data = post(
        "https://github.com/login/oauth/access_token",
        {"client_id": client_id, "client_secret": client_secret, "code": code},
        {"Accept": "application/json"},
        timeout=timeout,
    )
    access_token = data.get("access_token")
    if not access_token:
        raise OIDCError(f"github code exchange failed: {data.get('error_description') or data.get('error') or 'unknown'}")
    get = http_get or _get_json
    user = get("https://api.github.com/user", {"Authorization": f"Bearer {access_token}",
                                               "Accept": "application/vnd.github+json",
                                               "User-Agent": "jade-server/0.1"},
               timeout=timeout)
    if "id" not in user:
        raise OIDCError("github user lookup failed")
    email = user.get("email") or ""
    return {"sub": str(user["id"]), "email": str(email), "name": str(user.get("name") or user.get("login") or "")}


def _post_form(url: str, fields: dict, headers: dict, timeout: int) -> dict:
    import urllib.parse
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read().decode()
    if "json" in ctype:
        return json.loads(raw)
    return dict(urllib.parse.parse_qsl(raw))


def _get_json(url: str, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())
