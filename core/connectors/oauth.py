"""OAuth2 / PKCE helper built on stdlib only.

Flow: build an authorization URL, open the system browser, listen on
127.0.0.1 for the redirect, exchange the code for tokens. Used by the
connector framework and by :mod:`core.signin`.

Nothing is faked: if the provider needs a registered OAuth client (client
id/secret), the caller must supply it — see each connector's docs.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import threading
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Dict, List, Optional


def pkce_pair() -> tuple:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    code: Optional[str] = None
    error: Optional[str] = None

    def do_GET(self):  # noqa: N802
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = qs.get("code", [None])[0]
        _CallbackHandler.error = qs.get("error", [None])[0]
        body = ("<h2>✅ Connected — you can close this tab and return to Cortana.</h2>"
                if _CallbackHandler.code else
                f"<h2>❌ Authorization failed: {_CallbackHandler.error}</h2>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def run_pkce_flow(auth_url: str, token_url: str, client_id: str,
                  scopes: List[str], client_secret: Optional[str] = None,
                  extra_auth_params: Optional[Dict[str, str]] = None,
                  timeout: int = 180) -> Dict[str, Any]:
    """Run a full PKCE authorization-code flow. Returns token dict or {"error"}."""
    verifier, challenge = pkce_pair()
    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    params = {"client_id": client_id, "redirect_uri": redirect_uri,
              "response_type": "code", "scope": " ".join(scopes),
              "code_challenge": challenge, "code_challenge_method": "S256",
              "state": secrets.token_urlsafe(16)}
    if extra_auth_params:
        params.update(extra_auth_params)
    url = auth_url + "?" + urllib.parse.urlencode(params)

    _CallbackHandler.code = None
    _CallbackHandler.error = None
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print(f"If your browser didn't open, visit:\n{url}\n")
    thread.join(timeout)
    server.server_close()

    if _CallbackHandler.error or not _CallbackHandler.code:
        return {"error": f"Authorization failed: {_CallbackHandler.error or 'no code received (timed out?)'}"}

    data = {"client_id": client_id, "code": _CallbackHandler.code,
            "code_verifier": verifier, "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"}
    if client_secret:
        data["client_secret"] = client_secret
    try:
        req = urllib.request.Request(token_url,
                                     data=urllib.parse.urlencode(data).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            token = json.loads(r.read().decode())
    except Exception as e:
        return {"error": f"Token exchange failed: {e}"}
    if "access_token" not in token:
        return {"error": f"Token exchange failed: {token.get('error_description') or token.get('error') or token}"}
    return token


def refresh_access_token(token_url: str, client_id: str, refresh_token: str,
                         client_secret: Optional[str] = None) -> Dict[str, Any]:
    data = {"client_id": client_id, "refresh_token": refresh_token,
            "grant_type": "refresh_token"}
    if client_secret:
        data["client_secret"] = client_secret
    try:
        req = urllib.request.Request(token_url,
                                     data=urllib.parse.urlencode(data).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": f"Token refresh failed: {e}"}


def api_request(method: str, url: str, access_token: str,
                payload: Optional[Dict[str, Any]] = None,
                params: Optional[Dict[str, str]] = None,
                timeout: int = 30) -> Dict[str, Any]:
    """General JSON REST call with a bearer token (GET/POST/PATCH/DELETE).

    Returns parsed JSON, ``{"ok": True}`` for empty 2xx bodies, or
    ``{"error": ...}``. Failures never raise.
    """
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {access_token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method.upper())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:500]
        except Exception:
            detail = str(e)
        return {"error": f"HTTP {e.code}: {detail}"}
    except Exception as e:
        return {"error": str(e)}
    if not body.strip():
        return {"ok": True}
    try:
        return json.loads(body)
    except Exception as e:
        return {"error": f"Could not parse JSON response: {e}"}


def api_get(url: str, access_token: str, params: Optional[Dict[str, str]] = None,
            timeout: int = 30, raw: bool = False) -> Dict[str, Any]:
    """GET a REST endpoint with a bearer token.

    Returns parsed JSON, or with ``raw=True`` the response text as
    ``{"ok": True, "text": ...}``. Failures come back as ``{"error": ...}``.
    """
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:500]
        except Exception:
            detail = str(e)
        return {"error": f"HTTP {e.code}: {detail}"}
    except Exception as e:
        return {"error": str(e)}
    if raw:
        return {"ok": True, "text": body}
    try:
        return json.loads(body)
    except Exception as e:
        return {"error": f"Could not parse JSON response: {e}"}


def load_google_client_secrets() -> Optional[Dict[str, str]]:
    """Read OAuth client credentials from ~/.cortana/google_client.json.

    Shared with core.google_auth's flow so users configure Google once.
    Returns {"client_id", "client_secret"} or None.
    """
    from core.config import CONFIG_DIR
    path = os.path.join(str(CONFIG_DIR), "google_client.json")
    if not os.path.exists(path):
        return None
    try:
        data = json.load(open(path))
        installed = data.get("installed", data)
        cid = installed.get("client_id")
        sec = installed.get("client_secret")
        if cid:
            return {"client_id": cid, "client_secret": sec}
    except Exception:
        pass
    return None
