"""Shared Google OAuth connect flow for the Google connectors.

Reuses ``~/.jade/google_client.json`` (the same file
:mod:`core.google_auth` uses) so the user configures Google credentials once.
"""
from __future__ import annotations

from typing import Any, Dict, List

from core.connectors.base import Connector
from core.connectors.oauth import run_pkce_flow, load_google_client_secrets, api_get
from rich.console import Console

console = Console()

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleConnector(Connector):
    auth_kind = "oauth2"

    def _needs_message(self) -> str:
        return (f"Run `/connector connect {self.id}`. You need Google OAuth "
                "client credentials in ~/.jade/google_client.json "
                "(same file the /google-login flow uses).")

    def connect(self) -> Dict[str, Any]:
        secrets = load_google_client_secrets()
        if not secrets:
            console.print("[yellow]Google OAuth client not configured.[/yellow]")
            console.print("Create OAuth client credentials (Desktop app) in the Google Cloud "
                          "Console and save them to [bold]~/.jade/google_client.json[/bold].")
            cid = console.input("Client ID (or Enter to abort): ").strip()
            if not cid:
                return {"ok": False, "error": "aborted"}
            csec = console.input("Client secret: ").strip()
            secrets = {"client_id": cid, "client_secret": csec or None}
            from core.config import CONFIG_DIR
            import json, os
            with open(os.path.join(str(CONFIG_DIR), "google_client.json"), "w") as f:
                json.dump({"installed": {"client_id": cid, "client_secret": csec or "",
                                         "auth_uri": GOOGLE_AUTH_URL, "token_uri": GOOGLE_TOKEN_URL,
                                         "redirect_uris": ["http://127.0.0.1"]}}, f)

        scopes = ["openid", "https://www.googleapis.com/auth/userinfo.email"] + self.scopes
        console.print(f"[cyan]Opening browser to link {self.display}…[/cyan]")
        token = run_pkce_flow(GOOGLE_AUTH_URL, GOOGLE_TOKEN_URL,
                              secrets["client_id"], scopes,
                              client_secret=secrets.get("client_secret"))
        if "error" in token:
            return {"ok": False, "error": token["error"]}
        # Identify the account.
        info = api_get("https://openidconnect.googleapis.com/v1/userinfo",
                       token["access_token"])
        token["account"] = info.get("email", "") if isinstance(info, dict) else ""
        self.save_token(token)
        return {"ok": True, "account": token.get("account", "")}
