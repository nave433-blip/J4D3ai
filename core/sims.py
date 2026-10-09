"""Jade Sims: user-creatable agents/bots.

A Sim is a portable JSON document: name, avatar emoji, personality label,
system prompt, allowed tools, model choice. Sims live in
``~/.jade/sims/<name>.json`` and can be exported/imported as plain files,
so they are trivially shareable.

Tool gating: :func:`sim_may_use_tool` is the single choke point the agent
loops must consult before a Sim touches a tool. The sandbox
(``tools/sandbox.py``) still applies to anything a Sim executes.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import CONFIG_DIR
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

console = Console()

SIMS_DIR = CONFIG_DIR / "sims"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _-]{0,31}$")
# Tools Sims may never use, even if allowlisted by mistake.
FORBIDDEN_TOOLS = {"p2p_send_file", "keyring_write", "config_delete"}


def _sims_dir() -> Path:
    SIMS_DIR.mkdir(parents=True, exist_ok=True)
    return SIMS_DIR


def _sim_path(name: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name.strip())[:40] or "sim"
    return _sims_dir() / f"{safe}.json"


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def list_sims() -> List[Dict[str, Any]]:
    out = []
    if SIMS_DIR.is_dir():
        for path in sorted(SIMS_DIR.glob("*.json")):
            try:
                doc = json.loads(path.read_text())
            except Exception:
                continue
            out.append({
                "name": doc.get("name", path.stem),
                "avatar": doc.get("avatar", "🤖"),
                "personality": doc.get("personality", ""),
                "model": doc.get("model", ""),
                "tools": len(doc.get("allowed_tools", [])),
                "file": str(path),
            })
    return out


def load_sim(name: str) -> Dict[str, Any]:
    for s in list_sims():
        if s["name"].lower() == name.lower():
            return json.loads(Path(s["file"]).read_text())
    raise KeyError(f"Sim '{name}' does not exist.")


def create_sim(name: str, avatar: str = "🤖", personality: str = "professional",
               system_prompt: str = "", allowed_tools: Optional[List[str]] = None,
               model: str = "", description: str = "") -> Dict[str, Any]:
    if not valid_name(name):
        return {"ok": False, "error": "Name must be 1-32 chars: letters, digits, space, _ or -."}
    path = _sim_path(name)
    if path.exists():
        return {"ok": False, "error": f"A sim named '{name}' already exists."}
    tools = [t for t in (allowed_tools or []) if t not in FORBIDDEN_TOOLS]
    doc = {"name": name, "avatar": avatar or "🤖", "personality": personality,
           "system_prompt": system_prompt, "allowed_tools": tools,
           "model": model, "description": description, "created": time.time(),
           "format": "jade-sim/1"}
    path.write_text(json.dumps(doc, indent=2))
    return {"ok": True, "name": name, "file": str(path)}


def delete_sim(name: str, _confirmed: bool = False) -> Dict[str, Any]:
    target = None
    for s in list_sims():
        if s["name"].lower() == name.lower():
            target = s
            break
    if target is None:
        return {"ok": False, "error": f"Sim '{name}' does not exist."}
    if not _confirmed:
        from core.approvals import confirm
        if not confirm(f"Delete sim '{target['name']}'?"):
            return {"ok": False, "error": "cancelled"}
    Path(target["file"]).unlink(missing_ok=True)
    return {"ok": True, "name": target["name"]}


def export_sim(name: str, dest: str) -> Dict[str, Any]:
    try:
        sim = load_sim(name)
    except KeyError as e:
        return {"ok": False, "error": str(e)}
    out = Path(dest).expanduser()
    out.write_text(json.dumps(sim, indent=2))
    return {"ok": True, "file": str(out)}


def import_sim(path: str) -> Dict[str, Any]:
    """Import a sim from a JSON file. Validates the document; never executes it."""
    src = Path(path).expanduser()
    if not src.exists():
        return {"ok": False, "error": f"File not found: {path}"}
    try:
        doc = json.loads(src.read_text())
    except Exception as e:
        return {"ok": False, "error": f"Not valid JSON: {e}"}
    for field in ("name", "system_prompt"):
        if field not in doc:
            return {"ok": False, "error": f"Missing required field '{field}'."}
    if doc.get("format", "").startswith("jade-sim/") is False and "format" in doc:
        return {"ok": False, "error": f"Unknown sim format '{doc.get('format')}'."}
    name = str(doc["name"])
    if not valid_name(name):
        return {"ok": False, "error": f"Invalid sim name '{name}'."}
    target = _sim_path(name)
    if target.exists():
        return {"ok": False, "error": f"A sim named '{name}' already exists."}
    # Sanitize: keep only known fields, strip forbidden tools.
    clean = {"name": name,
             "avatar": str(doc.get("avatar", "🤖"))[:8],
             "personality": str(doc.get("personality", "professional"))[:40],
             "system_prompt": str(doc.get("system_prompt", ""))[:20000],
             "allowed_tools": [t for t in doc.get("allowed_tools", [])[:64]
                               if isinstance(t, str) and t not in FORBIDDEN_TOOLS],
             "model": str(doc.get("model", ""))[:80],
             "description": str(doc.get("description", ""))[:500],
             "created": time.time(), "format": "jade-sim/1",
             "imported_from": str(src)}
    target.write_text(json.dumps(clean, indent=2))
    return {"ok": True, "name": name}


def sim_may_use_tool(sim: Dict[str, Any], tool_name: str) -> bool:
    """Choke point: must a Sim be allowed to invoke *tool_name*?

    Agent loops (incl. Round A's coding loop) must consult this before
    executing any tool on a Sim's behalf. Forbidden tools are never allowed.
    """
    if tool_name in FORBIDDEN_TOOLS:
        return False
    allowed = sim.get("allowed_tools", [])
    if not allowed:
        return False
    return tool_name in allowed


def build_sim_context(sim: Dict[str, Any], history: List[Dict[str, str]]) -> str:
    parts = [f"You are {sim['name']}, {sim.get('description') or 'a Jade sim'}.",
             f"Personality: {sim.get('personality', 'professional')}.",
             sim.get("system_prompt", ""),
             "You are a Sim running inside Jade. You have no tools unless the host grants them."]
    for turn in history[-10:]:
        parts.append(f"{turn.get('role', 'user')}: {turn.get('text', '')[:2000]}")
    return "\n".join(p for p in parts if p)


def sim_chat(name: str, message: str,
             history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """One chat turn with a Sim. Returns {"ok", "reply", "sim"}."""
    try:
        sim = load_sim(name)
    except KeyError as e:
        return {"ok": False, "error": str(e)}
    history = history or []
    context = build_sim_context(sim, history)
    try:
        from core.brain import think
        reply = think(context=context, task=message,
                      model=sim.get("model") or None)
    except Exception as e:
        return {"ok": False, "error": f"Sim chat failed: {e}"}
    return {"ok": True, "reply": reply, "sim": sim["name"],
            "avatar": sim.get("avatar", "🤖")}


def sims_table() -> Table:
    table = Table(title="Jade Sims", border_style="magenta")
    table.add_column("Sim", style="cyan", no_wrap=True)
    table.add_column("Personality", style="white")
    table.add_column("Model", style="dim")
    table.add_column("Tools", style="white")
    for s in list_sims():
        table.add_row(f"{s['avatar']} {s['name']}", s["personality"],
                      s["model"] or "default", str(s["tools"]))
    return table


def sims_menu() -> None:
    while True:
        console.print(sims_table())
        console.print("\n[bold white]Options:[/bold white] [n]ew  [c]hat  [e]xport  [i]mport  [d]elete  [b]ack")
        choice = Prompt.ask("Action", choices=["n", "c", "e", "i", "d", "b"], default="b")
        if choice == "b":
            break
        if choice == "n":
            name = Prompt.ask("Sim name").strip()
            avatar = Prompt.ask("Avatar emoji", default="🤖").strip()
            personality = Prompt.ask("Personality label", default="professional").strip()
            system_prompt = Prompt.ask("System prompt").strip()
            model = Prompt.ask("Model (empty = default)", default="").strip()
            tools_raw = Prompt.ask("Allowed tools (comma-separated, empty = none)", default="").strip()
            tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
            res = create_sim(name, avatar=avatar, personality=personality,
                             system_prompt=system_prompt, allowed_tools=tools, model=model)
            console.print(f"[green]✅ Sim '{name}' created.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
        elif choice == "c":
            name = Prompt.ask("Chat with sim").strip()
            history: List[Dict[str, str]] = []
            console.print("[dim]Chatting — empty message exits.[/dim]")
            while True:
                msg = Prompt.ask("you").strip()
                if not msg:
                    break
                history.append({"role": "user", "text": msg})
                res = sim_chat(name, msg, history)
                if not res["ok"]:
                    console.print(f"[red]❌ {res['error']}[/red]")
                    break
                console.print(f"[magenta]{res['avatar']} {res['sim']}:[/magenta] {res['reply']}")
                history.append({"role": "assistant", "text": str(res["reply"])})
        elif choice == "e":
            name = Prompt.ask("Sim name").strip()
            dest = Prompt.ask("Export destination path").strip()
            res = export_sim(name, dest)
            console.print(f"[green]✅ Exported to {res['file']}[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
        elif choice == "i":
            path = Prompt.ask("Import from path").strip()
            res = import_sim(path)
            console.print(f"[green]✅ Imported sim '{res['name']}'.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
        elif choice == "d":
            name = Prompt.ask("Delete sim").strip()
            res = delete_sim(name)
            console.print(f"[green]✅ Deleted.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
