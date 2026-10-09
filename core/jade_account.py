"""Local Cortana Account record.

A Cortana Account is the user's identity inside Cortana: a display name plus
the set of linked third-party sign-ins (Microsoft, Apple, Google, GitHub).
This module is the *client-side slot* for that identity — a local record in
``~/.cortana/account.json``. There is deliberately no server here.

See ``docs/CORTANA_ACCOUNT.md`` for the protocol sketch of the future hosted
Cortana cloud service. That document is explicit: the cloud backend does not
exist yet; this round delivers the local record and the sign-in plumbing that
a future backend would consume.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from core.config import CONFIG_DIR

ACCOUNT_FILE = CONFIG_DIR / "account.json"


def _blank() -> Dict[str, Any]:
    return {"display_name": "", "email": "", "linked": {},
            "created": time.time(), "format": "cortana-account/1"}


def get_account() -> Dict[str, Any]:
    if ACCOUNT_FILE.exists():
        try:
            data = json.loads(ACCOUNT_FILE.read_text())
            merged = _blank()
            merged.update(data)
            return merged
        except Exception:
            pass
    return _blank()


def _save(doc: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNT_FILE.write_text(json.dumps(doc, indent=2))


def set_display_name(name: str) -> Dict[str, Any]:
    doc = get_account()
    doc["display_name"] = name.strip()[:80]
    _save(doc)
    return {"ok": True, "display_name": doc["display_name"]}


def link_signin(provider: str, account: str) -> Dict[str, Any]:
    doc = get_account()
    doc["linked"][provider] = {"account": account, "linked_at": time.time()}
    if not doc["email"] and "@" in (account or ""):
        doc["email"] = account
    _save(doc)
    return {"ok": True, "provider": provider}


def unlink_signin(provider: str) -> Dict[str, Any]:
    doc = get_account()
    doc["linked"].pop(provider, None)
    _save(doc)
    return {"ok": True, "provider": provider}


def linked_providers() -> List[str]:
    return list(get_account().get("linked", {}))


def account_menu() -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt
    console = Console()
    while True:
        doc = get_account()
        linked = ", ".join(f"{p} ({d.get('account', '')})"
                           for p, d in doc.get("linked", {}).items()) or "none"
        console.print(Panel(
            f"Display name: [bold]{doc.get('display_name') or '(not set)'}[/bold]\n"
            f"Email: {doc.get('email') or '(not set)'}\n"
            f"Linked sign-ins: {linked}\n\n"
            "[dim]Local record only — the hosted Cortana cloud does not exist yet. "
            "See docs/CORTANA_ACCOUNT.md.[/dim]",
            title="Cortana Account", border_style="cyan"))
        console.print("[n] set display name   [b]ack")
        choice = Prompt.ask("Action", choices=["n", "b"], default="b")
        if choice == "b":
            break
        name = Prompt.ask("Display name").strip()
        set_display_name(name)
        console.print("[green]✅ Saved.[/green]")
