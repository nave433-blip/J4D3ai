"""Projects (Claude/ChatGPT-style): named workspaces grouping chats,
attached files/folders, custom instructions and scoped memory.

Stored under ``~/.jade/projects/<slug>/``::

    project.json      # name, instructions, attachments, created
    chats/<ts>.jsonl  # appended chat turns while the project is open

Attachments are *references* to paths on disk (not copies) so large repos
stay light; missing paths are reported, never fatal.

The active project is stored in config ``active_project``. Call
:func:`project_context_block` to get the injectable context string, or
:func:`inject_into_system_prompt` to prepend it to any system prompt.
``core/brain.py`` calls this automatically for the active project.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import CONFIG_DIR, load_config, save_config
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

console = Console()

PROJECTS_DIR = CONFIG_DIR / "projects"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_MAX_PREVIEW_BYTES = 4000


def _projects_dir() -> Path:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    return PROJECTS_DIR


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:48]
    return slug or "project"


def _project_path(slug: str) -> Path:
    return _projects_dir() / slug


def _doc_path(slug: str) -> Path:
    return _project_path(slug) / "project.json"


def list_projects() -> List[Dict[str, Any]]:
    out = []
    if PROJECTS_DIR.is_dir():
        for path in sorted(PROJECTS_DIR.iterdir()):
            doc_file = path / "project.json"
            if not path.is_dir() or not doc_file.exists():
                continue
            try:
                doc = json.loads(doc_file.read_text())
            except Exception:
                continue
            out.append({
                "slug": path.name,
                "name": doc.get("name", path.name),
                "instructions": bool(doc.get("instructions")),
                "attachments": len(doc.get("attachments", [])),
                "chats": len(list((path / "chats").glob("*.jsonl"))) if (path / "chats").is_dir() else 0,
                "active": path.name == get_active_project(),
            })
    return out


def get_active_project() -> str:
    return load_config().get("active_project", "") or ""


def load_project(slug: str) -> Dict[str, Any]:
    doc_file = _doc_path(slug)
    if not doc_file.exists():
        raise KeyError(f"Project '{slug}' does not exist.")
    return json.loads(doc_file.read_text())


def create_project(name: str, instructions: str = "",
                   attachments: Optional[List[str]] = None) -> Dict[str, Any]:
    slug = slugify(name)
    base = slug
    i = 2
    while _doc_path(slug).exists():
        slug = f"{base}-{i}"
        i += 1
    _project_path(slug).mkdir(parents=True, exist_ok=True)
    (_project_path(slug) / "chats").mkdir(exist_ok=True)
    doc = {"name": name, "slug": slug, "instructions": instructions,
           "attachments": attachments or [], "created": time.time()}
    _doc_path(slug).write_text(json.dumps(doc, indent=2))
    return {"ok": True, "slug": slug, "name": name}


def open_project(slug: str) -> Dict[str, Any]:
    if not _doc_path(slug).exists():
        return {"ok": False, "error": f"Project '{slug}' does not exist."}
    cfg = load_config()
    cfg["active_project"] = slug
    save_config(cfg)
    return {"ok": True, "slug": slug}


def close_project() -> Dict[str, Any]:
    cfg = load_config()
    cfg["active_project"] = ""
    save_config(cfg)
    return {"ok": True}


def delete_project(slug: str, _confirmed: bool = False) -> Dict[str, Any]:
    import shutil
    if not _doc_path(slug).exists():
        return {"ok": False, "error": f"Project '{slug}' does not exist."}
    if not _confirmed:
        from core.approvals import confirm
        if not confirm(f"Delete project '{slug}' and its chat logs?"):
            return {"ok": False, "error": "cancelled"}
    if get_active_project() == slug:
        close_project()
    shutil.rmtree(_project_path(slug), ignore_errors=True)
    return {"ok": True, "slug": slug}


def set_instructions(slug: str, instructions: str) -> Dict[str, Any]:
    try:
        doc = load_project(slug)
    except KeyError as e:
        return {"ok": False, "error": str(e)}
    doc["instructions"] = instructions
    _doc_path(slug).write_text(json.dumps(doc, indent=2))
    return {"ok": True, "slug": slug}


def add_attachment(slug: str, path: str) -> Dict[str, Any]:
    try:
        doc = load_project(slug)
    except KeyError as e:
        return {"ok": False, "error": str(e)}
    p = str(Path(path).expanduser())
    if p in doc["attachments"]:
        return {"ok": False, "error": "Already attached."}
    if not Path(p).exists():
        return {"ok": False, "error": f"Path does not exist: {p}"}
    doc["attachments"].append(p)
    _doc_path(slug).write_text(json.dumps(doc, indent=2))
    return {"ok": True, "slug": slug, "path": p}


def remove_attachment(slug: str, path: str) -> Dict[str, Any]:
    try:
        doc = load_project(slug)
    except KeyError as e:
        return {"ok": False, "error": str(e)}
    p = str(Path(path).expanduser())
    if p not in doc["attachments"]:
        return {"ok": False, "error": "Not attached."}
    doc["attachments"].remove(p)
    _doc_path(slug).write_text(json.dumps(doc, indent=2))
    return {"ok": True, "slug": slug}


def log_chat_turn(slug: str, role: str, text: str) -> None:
    """Append one chat turn to today's project chat log (best-effort)."""
    try:
        chats = _project_path(slug) / "chats"
        chats.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        with open(chats / f"{day}.jsonl", "a") as f:
            f.write(json.dumps({"ts": time.time(), "role": role,
                                "text": text[:8000]}) + "\n")
    except Exception:
        pass


def _attachment_preview(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return f"- {path} (missing)"
    try:
        if p.is_dir():
            entries = sorted(x.name for x in p.iterdir())[:40]
            more = "…" if len(entries) == 40 else ""
            return f"- {path}/ [dir: {', '.join(entries)}{more}]"
        data = p.read_bytes()[:_MAX_PREVIEW_BYTES]
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return f"- {path} (binary, {p.stat().st_size} bytes)"
        if len(text) == _MAX_PREVIEW_BYTES:
            text += "\n…(truncated)"
        return f"- {path}:\n```\n{text}\n```"
    except Exception as e:
        return f"- {path} (unreadable: {e})"


def project_context_block(slug: Optional[str] = None) -> str:
    """Render the active project's injectable context (instructions + files)."""
    slug = slug or get_active_project()
    if not slug:
        return ""
    try:
        doc = load_project(slug)
    except (KeyError, OSError, ValueError):
        return ""
    parts = [f"[Project: {doc.get('name', slug)}]"]
    if doc.get("instructions"):
        parts.append(f"Project instructions: {doc['instructions']}")
    if doc.get("attachments"):
        parts.append("Attached files/folders:")
        parts.extend(_attachment_preview(a) for a in doc["attachments"][:12])
        if len(doc["attachments"]) > 12:
            parts.append(f"…and {len(doc['attachments']) - 12} more attachments")
    return "\n".join(parts)


def inject_into_system_prompt(base_prompt: str, slug: Optional[str] = None) -> str:
    """Prepend active-project context to a system prompt (no-op if none)."""
    block = project_context_block(slug)
    if not block:
        return base_prompt
    return f"{block}\n\n{base_prompt}"


def projects_table() -> Table:
    table = Table(title="Jade Projects", border_style="cyan")
    table.add_column("Slug", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")
    table.add_column("Active", style="green")
    table.add_column("Files", style="white")
    table.add_column("Chats", style="white")
    for p in list_projects():
        table.add_row(p["slug"], p["name"], "●" if p["active"] else "",
                      str(p["attachments"]), str(p["chats"]))
    return table


def projects_menu() -> None:
    while True:
        console.print(projects_table())
        console.print("\n[bold white]Options:[/bold white] [n]ew  [o]pen  [c]lose  [a]ttach  [i]nstructions  [d]elete  [b]ack")
        choice = Prompt.ask("Action", choices=["n", "o", "c", "a", "i", "d", "b"], default="b")
        if choice == "b":
            break
        if choice == "n":
            name = Prompt.ask("Project name").strip()
            instr = Prompt.ask("Custom instructions (empty = none)", default="").strip()
            res = create_project(name, instructions=instr)
            console.print(f"[green]✅ Project '{res['name']}' created (slug: {res['slug']}).[/green]")
        elif choice == "o":
            slug = Prompt.ask("Open project (slug)").strip()
            res = open_project(slug)
            console.print(f"[green]✅ Project '{slug}' is now active.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
        elif choice == "c":
            close_project()
            console.print("[green]✅ No active project.[/green]")
        elif choice == "a":
            slug = get_active_project() or Prompt.ask("Project slug").strip()
            path = Prompt.ask("File/folder path to attach").strip()
            res = add_attachment(slug, path)
            console.print(f"[green]✅ Attached.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
        elif choice == "i":
            slug = get_active_project() or Prompt.ask("Project slug").strip()
            instr = Prompt.ask("Custom instructions").strip()
            res = set_instructions(slug, instr)
            console.print(f"[green]✅ Instructions saved.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
        elif choice == "d":
            slug = Prompt.ask("Delete project (slug)").strip()
            res = delete_project(slug)
            console.print(f"[green]✅ Deleted.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
