"""Inspectable memory cores: facts, preferences, project memories, episodic.

Unlike the opaque FAISS vector store, cores are plain JSONL files the user
can read, export and delete at will::

    ~/.jade/memory/cores/<core>.jsonl   # one JSON object per line

Rules (pinned by the project's security posture):
- Everything is opt-in. Auto-capture defaults OFF (settings key
  ``memory_auto_capture``).
- Cores are user-deletable at any granularity (one entry, one core, all).
- Cores are NEVER transmitted over P2P — local only.
- Destructive wipes require confirmation.

Recall is keyword-based (no embedding dependency), so cores work fully
offline. An optional vector-index hook is documented for later.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import CONFIG_DIR
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

console = Console()

CORES = {
    "facts": "Durable facts about the user (name, hardware, projects).",
    "preferences": "How the user likes things done.",
    "projects": "Per-project memories, scoped by project slug.",
    "episodic": "Summaries of past sessions and events.",
}

CORES_DIR = CONFIG_DIR / "memory" / "cores"


def _cores_dir() -> Path:
    CORES_DIR.mkdir(parents=True, exist_ok=True)
    return CORES_DIR


def _core_path(core: str) -> Path:
    if core not in CORES:
        raise KeyError(f"Unknown memory core '{core}'. Choose from: {', '.join(CORES)}")
    return _cores_dir() / f"{core}.jsonl"


def _read_all(core: str) -> List[Dict[str, Any]]:
    path = _core_path(core)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return entries


def _write_all(core: str, entries: List[Dict[str, Any]]) -> None:
    path = _core_path(core)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def remember(core: str, text: str, source: str = "user",
             scope: Optional[str] = None) -> Dict[str, Any]:
    """Store one memory in a core. Returns the stored entry."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Empty memory not stored."}
    if len(text) > 4000:
        return {"ok": False, "error": "Memory too long (max 4000 chars)."}
    try:
        entries = _read_all(core)
    except KeyError as e:
        return {"ok": False, "error": str(e)}
    entry = {"id": uuid.uuid4().hex[:12], "text": text, "source": source,
             "scope": scope or "", "ts": time.time()}
    entries.append(entry)
    _write_all(core, entries)
    return {"ok": True, "entry": entry}


def recall(core: str, query: str = "", scope: Optional[str] = None,
           limit: int = 20) -> List[Dict[str, Any]]:
    """Keyword recall over a core (case-insensitive, all terms must match)."""
    entries = _read_all(core)
    if scope:
        entries = [e for e in entries if e.get("scope") == scope]
    terms = [t.lower() for t in query.split() if t]
    if terms:
        entries = [e for e in entries
                   if all(t in e.get("text", "").lower() for t in terms)]
    entries.sort(key=lambda e: e.get("ts", 0), reverse=True)
    return entries[:limit]


def forget(core: str, entry_id: str) -> Dict[str, Any]:
    entries = _read_all(core)
    kept = [e for e in entries if e.get("id") != entry_id]
    if len(kept) == len(entries):
        # Also try prefix match on the text for convenience.
        kept2 = [e for e in entries if entry_id.lower() not in e.get("text", "").lower()]
        if len(kept2) == len(entries):
            return {"ok": False, "error": f"No entry '{entry_id}' in core '{core}'."}
        kept = kept2
    _write_all(core, kept)
    return {"ok": True, "removed": len(entries) - len(kept)}


def export_core(core: str, dest: Optional[str] = None) -> Dict[str, Any]:
    entries = _read_all(core)
    payload = {"core": core, "exported": time.time(), "entries": entries}
    text = json.dumps(payload, indent=2)
    if dest:
        Path(dest).expanduser().write_text(text)
        return {"ok": True, "file": str(Path(dest).expanduser()), "count": len(entries)}
    return {"ok": True, "count": len(entries), "json": text}


def clear_core(core: str, _confirmed: bool = False) -> Dict[str, Any]:
    if not _confirmed:
        from core.approvals import confirm
        if not confirm(f"Permanently erase ALL memories in core '{core}'?"):
            return {"ok": False, "error": "cancelled"}
    path = _core_path(core)
    n = len(_read_all(core))
    if path.exists():
        path.unlink()
    return {"ok": True, "removed": n}


def stats() -> Dict[str, Any]:
    out = {}
    for core in CORES:
        try:
            out[core] = len(_read_all(core))
        except Exception:
            out[core] = 0
    try:
        from memory.vector import get_stats as vector_stats
        out["vector"] = vector_stats().get("count", 0)
    except Exception:
        out["vector"] = 0
    return out


def auto_capture_enabled() -> bool:
    try:
        from core.settings import get_setting
        return bool(get_setting("memory_enabled") and get_setting("memory_auto_capture"))
    except Exception:
        return False


def maybe_auto_capture(text: str, source: str = "auto") -> Optional[Dict[str, Any]]:
    """No-op unless the user explicitly enabled memory_auto_capture.

    When enabled, stores the text in the episodic core. The chat layer may
    call this after each turn; it never captures when the toggle is off.
    """
    if not auto_capture_enabled():
        return None
    return remember("episodic", text, source=source)


def cores_table() -> Table:
    table = Table(title="Memory Cores", border_style="magenta")
    table.add_column("Core", style="cyan", no_wrap=True)
    table.add_column("Entries", style="white")
    table.add_column("Description", style="dim")
    s = stats()
    for core, desc in CORES.items():
        table.add_row(core, str(s.get(core, 0)), desc)
    table.add_row("vector", str(s.get("vector", 0)), "Legacy FAISS semantic store.")
    return table


def memory_cores_menu() -> None:
    while True:
        console.print(cores_table())
        console.print("\n[bold white]Options:[/bold white] [s]how  [a]dd  [f]orget  [e]xport  [w]ipe core  [b]ack")
        choice = Prompt.ask("Action", choices=["s", "a", "f", "e", "w", "b"], default="b")
        if choice == "b":
            break
        core = Prompt.ask("Core", choices=list(CORES), default="facts")
        if choice == "s":
            q = Prompt.ask("Search (empty = all)", default="").strip()
            entries = recall(core, q)
            if not entries:
                console.print("[yellow]No entries.[/yellow]")
            for e in entries:
                ts = time.strftime("%Y-%m-%d", time.localtime(e.get("ts", 0)))
                console.print(f"[dim]{e['id']} · {ts}[/dim] {e['text']}")
        elif choice == "a":
            text = Prompt.ask("Memory text").strip()
            res = remember(core, text)
            console.print(f"[green]✅ Stored ({res['entry']['id']}).[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
        elif choice == "f":
            ident = Prompt.ask("Entry id (or text fragment)").strip()
            res = forget(core, ident)
            console.print(f"[green]✅ Removed {res['removed']}.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
        elif choice == "e":
            dest = Prompt.ask("Export path (empty = print)", default="").strip()
            res = export_core(core, dest or None)
            if res["ok"] and dest:
                console.print(f"[green]✅ Exported {res['count']} entries to {res['file']}[/green]")
            elif res["ok"]:
                console.print(res["json"])
            else:
                console.print(f"[red]❌ {res['error']}[/red]")
        elif choice == "w":
            res = clear_core(core)
            console.print(f"[green]✅ Core '{core}' wiped ({res['removed']} entries).[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
