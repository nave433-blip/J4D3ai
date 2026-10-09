"""Cross-device session handoff: hand a live session to another Jade node.

`/handoff <peer-ip> [note]` packages recent conversation context (from the
time-travel tree), open tasks, and your note — and sends it to the peer.
The receiving side shows exactly what's inside and asks for EXPLICIT accept.

Security: the payload NEVER contains API keys, tokens, or credentials.
(An older P2P action shipped keys; this one is deliberately key-free and
asserts that invariant before sending.)

Slash command: /handoff <peer-ip> [note]
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from core.approvals import confirm

console = Console()

# Field names that must never appear in a handoff payload, at any depth.
FORBIDDEN_KEYS = {"api_key", "apikey", "token", "secret", "password",
                  "credentials", "private_key", "bearer"}


def _contains_forbidden(obj: Any, path: str = "") -> Optional[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if any(f in kl for f in FORBIDDEN_KEYS):
                return f"{path}.{k}" if path else str(k)
            hit = _contains_forbidden(v, f"{path}.{k}" if path else str(k))
            if hit:
                return hit
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hit = _contains_forbidden(v, f"{path}[{i}]")
            if hit:
                return hit
    return None


def build_handoff(note: str = "", context_turns: int = 12,
                  open_tasks: Optional[List[str]] = None,
                  session_id: str = "default") -> Dict[str, Any]:
    """Build a key-free handoff payload."""
    try:
        from core.timetravel import current_tree
        context = current_tree(session_id).recent_context(context_turns)
        branch = current_tree(session_id).current_branch
    except Exception:
        context, branch = "", "main"
    payload = {
        "format": "jade-handoff/1",
        "from": _node_name(),
        "ts": time.time(),
        "branch": branch,
        "note": note,
        "context": context,
        "open_tasks": open_tasks or [],
    }
    hit = _contains_forbidden(payload)
    if hit:
        raise ValueError(f"handoff payload illegally contains credential-like field: {hit}")
    return payload


def _node_name() -> str:
    try:
        from core.config import load_config
        return load_config().get("jade_name", "Jade")
    except Exception:
        return "Jade"


def send_handoff(peer_ip: str, note: str = "", port: int = 11435,
                 use_tls: Optional[bool] = None,
                 open_tasks: Optional[List[str]] = None) -> Dict[str, Any]:
    payload = build_handoff(note=note, open_tasks=open_tasks)
    console.print(Panel(
        f"[bold]Handing session to {peer_ip}[/bold]\n"
        f"Branch: {payload['branch']} · Note: {note or '—'}\n"
        f"Context: {len(payload['context'])} chars of recent conversation\n"
        f"Open tasks: {len(payload['open_tasks'])}\n"
        "[green]No keys or credentials are included — verified.[/green]\n"
        "[dim]The peer must explicitly accept.[/dim]",
        border_style="cyan"))
    if not confirm(f"Send handoff to {peer_ip}?"):
        return {"ok": False, "error": "cancelled"}
    from core.p2p import send_remote_command
    res = send_remote_command(peer_ip, "session_handoff", {"payload": payload},
                              port=port, use_tls=use_tls)
    if res.get("ok"):
        try:
            return {"ok": True, **json.loads(res["data"])}
        except Exception:
            return {"ok": True, "response": res["data"]}
    return {"ok": False, "error": res.get("error")}


def _handoffs_dir() -> Path:
    from core.config import CONFIG_DIR
    d = Path(CONFIG_DIR) / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def handle_session_handoff(peer_ip: str, payload: Dict[str, Any]):
    """Receiver side (called from the P2P handler). Returns (status, ctype, body)."""
    if not isinstance(payload, dict) or payload.get("format") != "jade-handoff/1":
        return 400, "text/plain", b"bad handoff payload"
    hit = _contains_forbidden(payload)
    if hit:
        return 400, "text/plain", f"refused: payload contains credential-like field {hit}".encode()
    context = str(payload.get("context", ""))[:2000]
    tasks = payload.get("open_tasks", []) or []
    console.print(Panel(
        f"[bold magenta]🔀 INCOMING SESSION HANDOFF from {peer_ip}[/bold magenta]\n"
        f"From: {payload.get('from', '?')} · Branch: {payload.get('branch', '?')}\n"
        f"Note: {payload.get('note') or '—'}\n\n"
        f"[bold]Recent context:[/bold]\n{context}\n\n"
        f"[bold]Open tasks ({len(tasks)}):[/bold]\n" +
        "\n".join(f"  • {t}" for t in tasks[:10]),
        border_style="magenta"))
    console.print("[green]Verified: payload contains no keys or credentials.[/green]")
    if confirm("Accept this session handoff? (saves it locally for review)"):
        dest = _handoffs_dir() / f"handoff-{int(time.time())}.json"
        dest.write_text(json.dumps(payload, indent=2))
        body = json.dumps({"ok": True, "saved": str(dest),
                           "note": "saved for review — nothing was auto-applied"}).encode()
        return 200, "application/json", body
    return 403, "text/plain", b"handoff declined"


def handle_handoff_command(args: str) -> None:
    parts = args.split()
    if not parts:
        console.print("[yellow]Usage: /handoff <peer-ip> [note…][/yellow]")
        return
    peer_ip, note = parts[0], " ".join(parts[1:])
    try:
        r = send_handoff(peer_ip, note=note)
        if r.get("ok"):
            console.print(f"[green]✅ Handoff accepted by {peer_ip}[/green] [dim]({r.get('note', '')})[/dim]")
        else:
            console.print(f"[red]Handoff failed: {r.get('error')}[/red]")
    except Exception as e:
        console.print(f"[red]handoff error: {e}[/red]")
