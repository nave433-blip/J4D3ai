"""Sign in with Microsoft / Apple / Google / GitHub.

Standard OAuth2/PKCE flows that open the system browser; tokens land in the
OS keyring under ``jade_signin_<provider>`` — never in plaintext, never
over P2P.

Honesty contract: every provider needs *its own* registered OAuth client.
Jade ships no baked-in client secrets. :func:`signin_status` tells you
exactly what is missing and where to get it. GitHub uses the device flow
(no secret needed, just a client id).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Dict, List, Optional

import keyring
from core.config import load_config
from core.connectors.oauth import run_pkce_flow, load_google_client_secrets, api_get
from rich.console import Console
from rich.prompt import Prompt

console = Console()
KEYRING_PREFIX = "jade_signin_"

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "microsoft": {
        "display": "Microsoft",
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": ["openid", "profile", "email"],
        "client_id_config": "ms_client_id",
        "setup_url": "https://portal.azure.com",
        "setup_hint": "Register an app (personal Microsoft accounts supported) and copy the Application (client) ID.",
    },
    "apple": {
        "display": "Apple",
        "auth_url": "https://appleid.apple.com/auth/authorize",
        "token_url": "https://appleid.apple.com/auth/token",
        "scopes": ["name", "email"],
        "client_id_config": "apple_client_id",
        "setup_url": "https://developer.apple.com/account",
        "setup_hint": ("Create a Services ID, then generate a client secret (JWT signed "
                       "with your private key) and store it as apple_client_secret."),
        "client_secret_config": "apple_client_secret",
        "extra_auth_params": {"response_mode": "form_post"},
    },
    "google": {
        "display": "Google",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["openid", "https://www.googleapis.com/auth/userinfo.email"],
        "client_via": "google_client.json",
        "setup_hint": "Uses ~/.jade/google_client.json (same file as /google-login).",
    },
    "github": {
        "display": "GitHub",
        "device_flow": True,
        "device_code_url": "https://github.com/login/device/code",
        "device_token_url": "https://github.com/login/oauth/access_token",
        "scopes": ["read:user", "user:email"],
        "client_id_config": "github_client_id",
        "setup_url": "https://github.com/settings/developers",
        "setup_hint": "Create an OAuth App; the device flow needs only the Client ID (no secret).",
    },
}


def _service(provider: str) -> str:
    return f"{KEYRING_PREFIX}{provider}"


def get_signin(provider: str) -> Optional[Dict[str, Any]]:
    try:
        raw = keyring.get_password(_service(provider), "token")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def is_signed_in(provider: str) -> bool:
    tok = get_signin(provider)
    return bool(tok and tok.get("access_token"))


def _client_id(provider: str) -> str:
    info = PROVIDERS[provider]
    if info.get("client_via") == "google_client.json":
        secrets = load_google_client_secrets()
        return (secrets or {}).get("client_id", "")
    return (load_config().get(info["client_id_config"], "") or "").strip()


def signin_status() -> List[Dict[str, Any]]:
    out = []
    for pid, info in PROVIDERS.items():
        tok = get_signin(pid)
        cid = _client_id(pid)
        out.append({
            "provider": pid,
            "display": info["display"],
            "signed_in": is_signed_in(pid),
            "account": (tok or {}).get("account", ""),
            "client_configured": bool(cid),
            "needs": "" if (is_signed_in(pid) or cid) else
                   f"Set {info.get('client_id_config', 'client credentials')}: {info.get('setup_hint', '')}",
        })
    return out


def _github_device_flow(client_id: str, scopes: List[str]) -> Dict[str, Any]:
    data = urllib.parse.urlencode({"client_id": client_id,
                                   "scope": " ".join(scopes)}).encode()
    try:
        req = urllib.request.Request(
            PROVIDERS["github"]["device_code_url"], data=data,
            headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            dev = json.loads(r.read().decode())
    except Exception as e:
        return {"error": f"Device flow start failed: {e}"}
    console.print(f"[cyan]Go to [bold]{dev['verification_uri']}[/bold] and enter code: "
                  f"[bold yellow]{dev['user_code']}[/bold][/cyan]")
    try:
        webbrowser.open(dev["verification_uri"])
    except Exception:
        pass
    interval = int(dev.get("interval", 5))
    deadline = time.time() + int(dev.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        payload = urllib.parse.urlencode(
            {"client_id": client_id, "device_code": dev["device_code"],
             "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}).encode()
        try:
            req = urllib.request.Request(
                PROVIDERS["github"]["device_token_url"], data=payload,
                headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                tok = json.loads(r.read().decode())
        except Exception:
            continue
        if tok.get("access_token"):
            return tok
        if tok.get("error") not in ("authorization_pending", "slow_down"):
            return {"error": f"Device flow failed: {tok.get('error_description') or tok.get('error')}"}
    return {"error": "Device flow timed out."}


def signin(provider: str) -> Dict[str, Any]:
    """Interactive sign-in. Returns {"ok", "account"} or {"ok": False, "error"}."""
    if provider not in PROVIDERS:
        return {"ok": False, "error": f"Unknown provider '{provider}'."}
    info = PROVIDERS[provider]

    if provider == "github":
        cid = _client_id(provider)
        if not cid:
            console.print(f"[yellow]{info['setup_hint']}[/yellow]")
            cid = Prompt.ask("GitHub OAuth App Client ID (empty aborts)", default="").strip()
            if not cid:
                return {"ok": False, "error": "aborted"}
        tok = _github_device_flow(cid, info["scopes"])
        if "error" in tok:
            return {"ok": False, "error": tok["error"]}
        me = api_get("https://api.github.com/user", tok["access_token"])
        tok["account"] = me.get("login", "") if isinstance(me, dict) else ""
    elif info.get("client_via") == "google_client.json":
        secrets = load_google_client_secrets()
        if not secrets:
            return {"ok": False, "error": "google_client.json not configured — run /google-login first."}
        console.print("[cyan]Opening browser for Google sign-in…[/cyan]")
        tok = run_pkce_flow(info["auth_url"], info["token_url"],
                            secrets["client_id"], info["scopes"],
                            client_secret=secrets.get("client_secret"))
        if "error" in tok:
            return {"ok": False, "error": tok["error"]}
        uinfo = api_get("https://openidconnect.googleapis.com/v1/userinfo", tok["access_token"])
        tok["account"] = uinfo.get("email", "") if isinstance(uinfo, dict) else ""
    else:
        cid = _client_id(provider)
        if not cid:
            console.print(f"[yellow]{info['setup_hint']}[/yellow]")
            console.print(f"Register at: {info['setup_url']}")
            return {"ok": False, "error": f"{info.get('client_id_config')} not set"}
        csec = None
        if info.get("client_secret_config"):
            csec = (load_config().get(info["client_secret_config"], "") or "").strip() or None
            if not csec:
                console.print(f"[yellow]{info['setup_hint']}[/yellow]")
                return {"ok": False, "error": f"{info['client_secret_config']} not set"}
        console.print(f"[cyan]Opening browser for {info['display']} sign-in…[/cyan]")
        tok = run_pkce_flow(info["auth_url"], info["token_url"], cid,
                            info["scopes"], client_secret=csec,
                            extra_auth_params=info.get("extra_auth_params"))
        if "error" in tok:
            return {"ok": False, "error": tok["error"]}
        tok["account"] = ""  # Apple/Microsoft userinfo handled below; JWTs are not parsed locally.
        # Best-effort account label via userinfo where available.
        if provider == "microsoft":
            me = api_get("https://graph.microsoft.com/v1.0/me", tok["access_token"],
                         {"$select": "mail,userPrincipalName"})
            if isinstance(me, dict):
                tok["account"] = me.get("mail") or me.get("userPrincipalName", "")

    tok["provider"] = provider
    tok["ts"] = time.time()
    try:
        keyring.set_password(_service(provider), "token", json.dumps(tok))
    except Exception as e:
        return {"ok": False, "error": f"Could not store token in keyring: {e}"}
    # Link into the local Jade Account record.
    try:
        from core.jade_account import link_signin
        link_signin(provider, tok.get("account", ""))
    except Exception:
        pass
    return {"ok": True, "account": tok.get("account", "")}


def signout(provider: str, _confirmed: bool = False) -> Dict[str, Any]:
    if provider not in PROVIDERS:
        return {"ok": False, "error": f"Unknown provider '{provider}'."}
    if not is_signed_in(provider):
        return {"ok": True, "message": "Already signed out."}
    if not _confirmed:
        from core.approvals import confirm
        if not confirm(f"Sign out of {PROVIDERS[provider]['display']}?"):
            return {"ok": False, "error": "cancelled"}
    try:
        keyring.delete_password(_service(provider), "token")
    except Exception:
        pass
    try:
        from core.jade_account import unlink_signin
        unlink_signin(provider)
    except Exception:
        pass
    return {"ok": True, "message": f"Signed out of {PROVIDERS[provider]['display']}."}


def signin_menu() -> None:
    from rich.table import Table
    while True:
        table = Table(title="Sign in", border_style="cyan")
        table.add_column("Provider", style="cyan", no_wrap=True)
        table.add_column("Status", style="white")
        table.add_column("Account / next step", style="dim")
        for s in signin_status():
            status = "🟢 signed in" if s["signed_in"] else "⚪ signed out"
            detail = s["account"] if s["signed_in"] else s["needs"]
            table.add_row(s["display"], status, (detail or "")[:90])
        console.print(table)
        console.print("\n[bold white]Options:[/bold white] [i] sign in  [o] sign out  [b]ack")
        choice = Prompt.ask("Action", choices=["i", "o", "b"], default="b")
        if choice == "b":
            break
        pid = Prompt.ask("Provider", choices=list(PROVIDERS)).strip()
        if choice == "i":
            res = signin(pid)
            console.print(f"[green]✅ Signed in as {res.get('account', '')}.[/green]"
                          if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
        else:
            res = signout(pid)
            console.print(f"[green]✅ {res.get('message', '')}[/green]"
                          if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
