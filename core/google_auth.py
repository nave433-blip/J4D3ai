"""Google OAuth for JADE AI — clean-room implementation (stdlib only).

This module replaces the old ``google-auth-oauthlib`` /
``google-api-python-client`` path with :mod:`core.connectors.oauth`
(PKCE authorization-code flow + direct REST calls). No vendor SDK, no
binary blobs — just ``urllib`` and the OAuth2 standard.

Public API is unchanged from the legacy module so callers (``cli.py``)
keep working:
  - ``GoogleAuth.get_credentials()`` -> token dict or None
  - ``GoogleAuth.run_flow()`` -> token dict or None
  - ``GoogleAuth.get_user_email(token)`` -> email str
  - ``GoogleAuth.auto_register_flow()``

Token storage: JSON dict in the OS keychain under service
``jade_google_auth`` / key ``google_token``::
    {"access_token": ..., "refresh_token": ..., "expires_in": 3600,
     "obtained_at": 169..., "token_type": "Bearer",
     "client_id": ..., "client_secret": ...}

Tokens written by the legacy ``google.oauth2`` path (``Credentials.to_json``
format, key ``"token"``) are detected and migrated to the new format on
first read. The legacy keyring service ``jarvis_google_auth`` is still read
for compatibility, never written.
"""
from __future__ import annotations

import json
import os
import time
import webbrowser
from typing import Any, Dict, Optional

from rich.console import Console
from rich.panel import Panel
import keyring

from core.config import CONFIG_DIR
from core.connectors.oauth import (
    api_get,
    load_google_client_secrets,
    refresh_access_token,
    run_pkce_flow,
)
from core.connectors._google import GOOGLE_AUTH_URL, GOOGLE_TOKEN_URL
from core.utils import open_url

console = Console()

# Scopes for Google Auth - userinfo for account identification, plus the
# generative-language scope used for Gemini access.
SCOPES = [
    'https://www.googleapis.com/auth/userinfo.email',
    'openid',
    'https://www.googleapis.com/auth/generative-language',  # Gemini access
]

USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

KEYRING_SERVICE = "jade_google_auth"
# Pre-rename service name: read for compatibility, never written.
_KEYRING_SERVICE_LEGACY = "jarvis_google_auth"
TOKEN_KEY = "google_token"

_EXPIRY_SKEW = 60  # refresh a minute before the token actually dies


def _now() -> float:
    return time.time()


def _convert_legacy_format(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a google.oauth2 Credentials.to_json() dict to our format."""
    if "access_token" in data or "token" not in data:
        return None
    token: Dict[str, Any] = {
        "access_token": data.get("token"),
        "refresh_token": data.get("refresh_token"),
        "token_type": data.get("token_type", "Bearer"),
        "client_id": data.get("client_id"),
        "client_secret": data.get("client_secret"),
        "scope": " ".join(data.get("scopes", []) or []),
        "_migrated_from": "google.oauth2",
    }
    expiry = data.get("expiry")
    if expiry:
        try:
            # "2026-10-09T20:00:00" — may carry a trailing Z.
            token["expires_at"] = _now() + 3600
            from datetime import datetime, timezone
            exp = expiry.replace("Z", "+00:00")
            token["expires_at"] = datetime.fromisoformat(exp).replace(
                tzinfo=timezone.utc).timestamp()
        except Exception:
            pass
    return token


def _is_expired(token: Dict[str, Any]) -> bool:
    exp = token.get("expires_at")
    if exp:
        try:
            return _now() > float(exp) - _EXPIRY_SKEW
        except (TypeError, ValueError):
            return False
    obtained = token.get("obtained_at")
    lifetime = token.get("expires_in")
    if obtained and lifetime:
        try:
            return _now() > float(obtained) + float(lifetime) - _EXPIRY_SKEW
        except (TypeError, ValueError):
            return False
    return False  # no expiry info: assume usable, let the API say otherwise


def _read_stored_token() -> Optional[Dict[str, Any]]:
    for service in (KEYRING_SERVICE, _KEYRING_SERVICE_LEGACY):
        try:
            raw = keyring.get_password(service, TOKEN_KEY)
        except Exception:
            continue
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if "access_token" not in data and "token" in data:
            data = _convert_legacy_format(data)
            if data:
                _save_token(data)  # migrate to the new format once
        if data and data.get("access_token"):
            return data
    return None


def _save_token(token: Dict[str, Any]) -> None:
    token = dict(token)
    token.setdefault("obtained_at", _now())
    try:
        keyring.set_password(KEYRING_SERVICE, TOKEN_KEY,
                             json.dumps(token))
    except Exception as e:
        console.print(f"[dim]Note: could not save Google token to keychain: {e}[/dim]")


class GoogleAuth:
    @staticmethod
    def get_credentials() -> Optional[Dict[str, Any]]:
        """Retrieve credentials from keychain, refreshing or re-running the
        flow as needed. Returns a token dict or None."""
        token = _read_stored_token()

        if token and not _is_expired(token):
            return token

        if token and token.get("refresh_token"):
            refreshed = refresh_access_token(
                GOOGLE_TOKEN_URL,
                token.get("client_id") or "",
                token["refresh_token"],
                client_secret=token.get("client_secret"),
            )
            if "access_token" in refreshed:
                refreshed["refresh_token"] = refreshed.get("refresh_token") or token.get("refresh_token")
                refreshed["client_id"] = token.get("client_id")
                refreshed["client_secret"] = token.get("client_secret")
                _save_token(refreshed)
                return refreshed
            console.print("[dim]Note: token refresh failed; starting a new login flow.[/dim]")

        token = GoogleAuth.run_flow()
        if token:
            _save_token(token)
        return token

    @staticmethod
    def _ensure_client_secrets() -> Optional[Dict[str, str]]:
        secrets = load_google_client_secrets()
        if secrets:
            return secrets
        client_secrets_path = str(CONFIG_DIR / "google_client.json")
        console.print("[yellow]⚠️ Warning: Google OAuth Client Secrets not found at ~/.jade/google_client.json[/yellow]")
        console.print("Please provide your Google Cloud Console Client ID and Secret.")
        client_id = console.input("Client ID: ").strip()
        client_secret = console.input("Client Secret: ").strip()
        if not client_id or not client_secret:
            console.print("[red]Aborted: Missing credentials.[/red]")
            return None
        secrets_data = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": GOOGLE_AUTH_URL,
                "token_uri": GOOGLE_TOKEN_URL,
                "redirect_uris": ["http://127.0.0.1"],
            }
        }
        try:
            with open(client_secrets_path, "w") as f:
                json.dump(secrets_data, f)
        except OSError as e:
            console.print(f"[red]Could not write {client_secrets_path}: {e}[/red]")
            return None
        return {"client_id": client_id, "client_secret": client_secret}

    @staticmethod
    def run_flow() -> Optional[Dict[str, Any]]:
        """Run the OAuth2 PKCE flow to get a token. Returns a token dict."""
        console.print(Panel(
            "🌐 [bold cyan]Initiating Google Login Flow[/bold cyan]\n\n"
            "A browser window will open. Please login with your Google account "
            "to link your AI services.", border_style="cyan"))

        secrets = GoogleAuth._ensure_client_secrets()
        if not secrets:
            return None

        # Prefer the robust open_url (Chrome on Linux, `open` on macOS).
        original_open = webbrowser.open
        webbrowser.open = open_url
        try:
            token = run_pkce_flow(
                GOOGLE_AUTH_URL, GOOGLE_TOKEN_URL,
                secrets["client_id"], SCOPES,
                client_secret=secrets.get("client_secret"),
                extra_auth_params={"access_type": "offline", "prompt": "consent"},
            )
        finally:
            webbrowser.open = original_open

        if "error" in token:
            console.print(f"[bold red]❌ Google Login Failed:[/bold red] {token['error']}")
            return None
        token["client_id"] = secrets["client_id"]
        token["client_secret"] = secrets.get("client_secret")
        return token

    @staticmethod
    def get_user_email(token: Dict[str, Any]) -> Optional[str]:
        """Fetch the Google account email for a token dict."""
        access = token.get("access_token") if isinstance(token, dict) else None
        if not access:
            return None
        info = api_get(USERINFO_URL, access)
        if isinstance(info, dict):
            return info.get("email")
        return None

    @staticmethod
    def auto_register_flow():
        """Orchestrate a semi-automated registration flow across all
        Google-compatible AI providers."""
        token = GoogleAuth.get_credentials()
        if not token:
            return

        email = GoogleAuth.get_user_email(token)
        console.print(Panel(
            f"🚀 [bold cyan]Starting AI Registration Orchestrator[/bold cyan]\n\n"
            f"Logged in as: [bold green]{email}[/bold green]\n\n"
            "JADE will now open registration pages for all major AI services. \n"
            "[bold yellow]Requirement:[/bold yellow] Click 'Continue with Google' on each page, "
            "create your API key, and paste it back here.", border_style="cyan"))

        from core.services import set_api_key

        target_services = {
            "openai": {"url": "https://platform.openai.com/api-keys", "display": "OpenAI"},
            "anthropic": {"url": "https://console.anthropic.com/settings/keys", "display": "Anthropic"},
            "perplexity": {"url": "https://www.perplexity.ai/settings/api", "display": "Perplexity"},
            "gemini": {"url": "https://aistudio.google.com/app/apikey", "display": "Google Gemini"},
            "mistral": {"url": "https://console.mistral.ai/api-keys/", "display": "Mistral AI"},
            "groq": {"url": "https://console.groq.com/keys", "display": "Groq"},
            "deepseek": {"url": "https://platform.deepseek.com/api_keys", "display": "DeepSeek"},
            "qwen": {"url": "https://dashscope.console.aliyun.com/apiKey", "display": "Alibaba Qwen"},
            "together": {"url": "https://api.together.xyz/settings/api-keys", "display": "Together AI"},
        }

        for name, info in target_services.items():
            console.print(f"\n[bold magenta]Step: Registering for {info['display']}...[/bold magenta]")
            console.print(f"Opening: {info['url']}")
            open_url(info['url'])

            console.print(f"[dim]Instruction: Sign up with {email}, generate an API key, and paste it below.[/dim]")
            key = console.input(f"Paste {info['display']} API Key (or press Enter to skip): ").strip()

            if key:
                res = set_api_key(name, key)
                if res.get("ok"):
                    console.print(f"[bold green]✅ {info['display']} secured in Keychain.[/bold green]")
                else:
                    console.print(f"[red]❌ Failed to secure {info['display']} key.[/red]")
            else:
                console.print(f"[yellow]⏩ Skipped {info['display']}.[/yellow]")

        console.print(Panel(
            "🏁 [bold green]All targeted accounts processed![/bold green]\n\n"
            "Your JADE ecosystem is now fully powered and authenticated.",
            border_style="green"))
