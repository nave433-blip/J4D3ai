"""Shared Microsoft Graph client for Cortana's Microsoft connectors.

Bring-your-own Azure app registration: the user registers an app at
https://portal.azure.com and stores the application (client) id with
``cortana settings set ms_client_id <id>``. No Microsoft credentials are
ever shipped with Cortana.

Auth: OAuth2 device-code flow (works on headless/SSH machines where a
localhost browser redirect is awkward) plus refresh-token flow. Tokens are
cached as JSON in ``~/.cortana/`` with 0600 permissions — never in
plaintext config, never transmitted over P2P.

Features: paged GET helper (follows ``@odata.nextLink``), 429
rate-limit backoff honoring ``Retry-After``, single automatic retry on
401 via refresh token, constant-time comparison for clientState/secret
checks.
"""
from __future__ import annotations

import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from core.config import CONFIG_DIR, load_config
from rich.console import Console

console = Console()

MS_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MS_DEVICE_CODE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0"

_TOKEN_FILE = os.path.join(str(CONFIG_DIR), "ms_graph_token.json")


# ---------------------------------------------------------------- token cache
def load_ms_token() -> Optional[Dict[str, Any]]:
    try:
        with open(_TOKEN_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_ms_token(token: Dict[str, Any]) -> None:
    os.makedirs(str(CONFIG_DIR), exist_ok=True)
    tmp = _TOKEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(token, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _TOKEN_FILE)


def clear_ms_token() -> None:
    try:
        os.remove(_TOKEN_FILE)
    except Exception:
        pass


def safe_compare(a: str, b: str) -> bool:
    """Constant-time string comparison for secrets / clientState values."""
    return hmac.compare_digest(str(a), str(b))


# ---------------------------------------------------------------- HTTP layer
def _http(method: str, url: str, access_token: Optional[str] = None,
           payload: Optional[Dict[str, Any]] = None,
           params: Optional[Dict[str, str]] = None,
           content_type: str = "application/json",
           timeout: int = 30) -> "tuple[int, Dict[str, str], bytes]":
    """Low-level request. Returns (status, headers, body). Separated so tests
    can monkeypatch it without touching the network."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        if content_type == "application/json":
            data = json.dumps(payload).encode()
        else:
            data = urllib.parse.urlencode(payload).encode()
        headers["Content-Type"] = content_type
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, dict(e.headers or {}), body
    except Exception as e:  # network-level failure
        return 0, {}, str(e).encode()


def _parse_graph_error(status: int, body: bytes) -> str:
    try:
        err = json.loads(body.decode() or "{}").get("error", {})
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
    except Exception:
        msg = body.decode(errors="replace")[:300]
    return f"Graph HTTP {status}: {msg or 'request failed'}"


# ---------------------------------------------------------------- auth flows
def device_code_flow(client_id: str, scopes: List[str],
                     timeout: int = 600) -> Dict[str, Any]:
    """Run the OAuth2 device-code flow. Returns token dict or {"error": ...}."""
    status, _, body = _http(
        "POST", MS_DEVICE_CODE_URL, payload={"client_id": client_id,
                                             "scope": " ".join(scopes)},
        content_type="application/x-www-form-urlencoded")
    if status != 200:
        return {"error": f"Device-code request failed: {_parse_graph_error(status, body)}"}
    try:
        dc = json.loads(body.decode())
    except Exception:
        return {"error": "Device-code request returned non-JSON."}
    console.print(f"[cyan]To link your Microsoft account, visit "
                  f"[bold]{dc.get('verification_uri')}[/bold] and enter code "
                  f"[bold]{dc.get('user_code')}[/bold][/cyan]")
    interval = int(dc.get("interval", 5))
    deadline = time.time() + min(int(dc.get("expires_in", 900)), timeout)
    device_code = dc["device_code"]
    while time.time() < deadline:
        time.sleep(interval)
        status, _, body = _http(
            "POST", MS_TOKEN_URL,
            payload={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                     "client_id": client_id, "device_code": device_code},
            content_type="application/x-www-form-urlencoded")
        if status == 200:
            token = json.loads(body.decode())
            token["obtained_at"] = time.time()
            return token
        try:
            err = json.loads(body.decode() or "{}").get("error", "")
        except Exception:
            err = ""
        if err not in ("authorization_pending", "slow_down"):
            return {"error": f"Device-code poll failed: {err or _parse_graph_error(status, body)}"}
        if err == "slow_down":
            interval += 5
    return {"error": "Device-code flow timed out waiting for authorization."}


def refresh_ms_token(token: Dict[str, Any], client_id: str) -> Dict[str, Any]:
    """Refresh an MS token dict. Returns new token dict or {"error": ...}."""
    rt = token.get("refresh_token")
    if not rt:
        return {"error": "No refresh token stored; reconnect the account."}
    status, _, body = _http(
        "POST", MS_TOKEN_URL,
        payload={"grant_type": "refresh_token", "client_id": client_id,
                 "refresh_token": rt, "scope": token.get("scope", "")},
        content_type="application/x-www-form-urlencoded")
    if status != 200:
        return {"error": f"Token refresh failed: {_parse_graph_error(status, body)}"}
    new = json.loads(body.decode())
    new["obtained_at"] = time.time()
    if "refresh_token" not in new:
        new["refresh_token"] = rt  # AAD sometimes omits it
    return new


def _token_expired(token: Dict[str, Any], skew: int = 120) -> bool:
    exp = token.get("expires_in")
    obtained = token.get("obtained_at", 0)
    if not exp:
        return False
    return time.time() > obtained + int(exp) - skew


# ---------------------------------------------------------------- Graph client
class GraphClient:
    """Authenticated Graph caller with paging, 429 backoff and auto-refresh."""

    def __init__(self, token: Dict[str, Any], client_id: str,
                 on_token: Optional[Callable[[Dict[str, Any]], None]] = None,
                 max_retries: int = 4):
        self.token = token
        self.client_id = client_id
        self.on_token = on_token
        self.max_retries = max_retries

    def _auth(self) -> str:
        if _token_expired(self.token):
            new = refresh_ms_token(self.token, self.client_id)
            if "error" not in new:
                self.token = new
                if self.on_token:
                    self.on_token(new)
        return self.token.get("access_token", "")

    def request(self, method: str, path_or_url: str,
                params: Optional[Dict[str, str]] = None,
                payload: Optional[Dict[str, Any]] = None,
                raw: bool = False) -> Dict[str, Any]:
        """Call Graph. ``path_or_url`` may be a ``/me/...`` path or full URL
        (e.g. an ``@odata.nextLink``). Returns parsed JSON or
        ``{"error": ...}``. ``raw=True`` returns ``{"ok": True, "bytes": ...}``
        for downloads."""
        url = (path_or_url if path_or_url.startswith("http")
               else GRAPH + path_or_url)
        attempted_refresh = False
        for attempt in range(self.max_retries + 1):
            status, headers, body = _http(method, url, self._auth(),
                                          payload=payload,
                                          params=params if attempt == 0 else None)
            params = None  # only send query params on the first attempt
            if status == 401 and not attempted_refresh:
                attempted_refresh = True
                new = refresh_ms_token(self.token, self.client_id)
                if "error" not in new:
                    self.token = new
                    if self.on_token:
                        self.on_token(new)
                    continue
                return {"error": new["error"]}
            if status == 429 and attempt < self.max_retries:
                wait = headers.get("Retry-After")
                try:
                    delay = min(int(wait), 60)
                except (TypeError, ValueError):
                    delay = min(2 ** attempt, 30)
                time.sleep(delay)
                continue
            if status in (200, 201, 202, 204):
                if raw:
                    return {"ok": True, "bytes": body}
                if not body:
                    return {"ok": True}
                try:
                    return json.loads(body.decode())
                except Exception:
                    return {"ok": True, "text": body.decode(errors="replace")}
            return {"error": _parse_graph_error(status, body)}
        return {"error": "Graph request failed after retries."}

    def get(self, path_or_url: str,
            params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        return self.request("GET", path_or_url, params=params)

    def get_paged(self, path: str, params: Optional[Dict[str, str]] = None,
                  limit: int = 200) -> Dict[str, Any]:
        """Follow @odata.nextLink until exhausted or ``limit`` items."""
        items: List[Any] = []
        url: Optional[str] = path
        while url and len(items) < limit:
            res = self.get(url, params=params)
            params = None
            if "error" in res:
                return {"error": res["error"], "items": items}
            items.extend(res.get("value", []))
            url = res.get("@odata.nextLink")
        return {"ok": True, "items": items[:limit]}


# ---------------------------------------------------------------- connector base
class GraphConnectorBase:
    """Mixin giving a Connector a GraphClient backed by the shared token file.

    Subclasses set ``id``/``display``/``scopes`` like any Connector.
    """

    def _client_id(self) -> str:
        return (load_config().get("ms_client_id", "") or "").strip()

    def _needs_message(self) -> str:
        if not self._client_id():
            return ("Not configured: register an app at https://portal.azure.com "
                    "(personal Microsoft accounts supported), then run "
                    "`cortana settings set ms_client_id <application-id>`.")
        missing = ", ".join(getattr(self, "scopes", []))
        return (f"Run `/connector connect {self.id}` to grant these "
                f"permissions: {missing}.")

    def _required_scopes(self) -> set:
        return ({"openid", "profile", "email", "offline_access"}
                | set(getattr(self, "scopes", [])))

    def get_token(self):  # overrides Connector.get_token
        return load_ms_token()

    def save_token(self, token):  # overrides Connector.save_token
        save_ms_token(token)

    def clear_token(self):  # overrides Connector.clear_token
        clear_ms_token()

    def is_connected(self) -> bool:
        # All Microsoft connectors share one token file; a connector is only
        # "connected" when the granted scopes cover everything it needs.
        tok = load_ms_token()
        if not (tok and tok.get("access_token")):
            return False
        granted = set((tok.get("scope") or "").split())
        return self._required_scopes() <= granted

    def graph(self) -> Optional[GraphClient]:
        tok = load_ms_token()
        cid = self._client_id()
        if not tok or not cid:
            return None
        return GraphClient(tok, cid, on_token=save_ms_token)

    def connect(self) -> Dict[str, Any]:
        cid = self._client_id()
        if not cid:
            console.print(f"[yellow]{self._needs_message()}[/yellow]")
            return {"ok": False, "error": "ms_client_id not set"}
        scopes = ["openid", "profile", "email", "offline_access"] + list(
            getattr(self, "scopes", []))
        # De-duplicate while preserving order.
        scopes = list(dict.fromkeys(scopes))
        token = device_code_flow(cid, scopes)
        if "error" in token:
            return {"ok": False, "error": token["error"]}
        client = GraphClient(token, cid)
        me = client.get("/me", {"$select": "mail,userPrincipalName,displayName"})
        token["account"] = (me.get("mail") or me.get("userPrincipalName", "")
                            if isinstance(me, dict) else "")
        save_ms_token(token)
        return {"ok": True, "account": token.get("account", "")}
