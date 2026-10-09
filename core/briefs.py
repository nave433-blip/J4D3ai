"""Opt-in proactive briefs: a "while you were away" digest.

Strictly opt-in and OFF by default. When enabled (`/brief on`), a watchdog
observer records file events under your chosen watch paths; `/brief` (or the
next REPL start) compiles them into a digest where EVERY item links to its
source path. Nothing leaves the machine — this is purely local.

Slash commands: /brief on [paths…] · /brief off · /brief status · /brief now
"""

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from core.approvals import confirm
from rich.table import Table

console = Console()

STORE_NAME = "briefs.json"
DEFAULT_IGNORE = {".git", "__pycache__", ".venv", "node_modules", ".jade"}


def _store_path() -> Path:
    from core.config import CONFIG_DIR
    return Path(CONFIG_DIR) / STORE_NAME


def _load_store() -> Dict[str, Any]:
    try:
        p = _store_path()
        if p.is_file():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {"enabled": False, "watch_paths": [], "events": [], "last_brief_ts": 0.0}


def _save_store(s: Dict[str, Any]) -> None:
    try:
        p = _store_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def is_enabled() -> bool:
    return bool(_load_store().get("enabled"))


def _record_event(kind: str, path: str) -> None:
    s = _load_store()
    if not s.get("enabled"):
        return
    p = Path(path)
    if any(part in DEFAULT_IGNORE for part in p.parts):
        return
    events = s.setdefault("events", [])
    events.append({"ts": time.time(), "kind": kind, "path": str(p)})
    del events[:-500]  # cap
    _save_store(s)


class _Handler:
    def on_created(self, event):  # watchdog API
        if not event.is_directory:
            _record_event("created", event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            _record_event("modified", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            _record_event("deleted", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            _record_event("moved", f"{event.src_path} → {event.dest_path}")


_watcher_thread: threading.Thread | None = None
_watcher_stop = threading.Event()


def _watch_loop(paths: List[str]) -> None:
    try:
        from watchdog.observers import Observer
    except ImportError:
        return
    observer = Observer()
    handler = _Handler()
    for p in paths:
        try:
            if Path(p).expanduser().is_dir():
                observer.schedule(handler, str(Path(p).expanduser()), recursive=True)
        except Exception:
            pass
    observer.start()
    try:
        while not _watcher_stop.wait(1.0):
            if not is_enabled():
                break
    finally:
        observer.stop()
        observer.join(timeout=5)


def ensure_watcher() -> None:
    """Start the background watcher if briefs are enabled. Idempotent."""
    global _watcher_thread
    s = _load_store()
    if not s.get("enabled") or not s.get("watch_paths"):
        return
    if _watcher_thread and _watcher_thread.is_alive():
        return
    _watcher_stop.clear()
    _watcher_thread = threading.Thread(target=_watch_loop, args=(s["watch_paths"],),
                                       daemon=True, name="jade-briefs")
    _watcher_thread.start()


def pending_events() -> List[Dict[str, Any]]:
    s = _load_store()
    last = s.get("last_brief_ts", 0.0)
    # collapse to the latest event per path
    latest: Dict[str, Dict[str, Any]] = {}
    for e in s.get("events", []):
        if e.get("ts", 0) > last:
            latest[e["path"]] = e
    return sorted(latest.values(), key=lambda e: e["ts"])


def compile_brief(mark_seen: bool = True) -> List[Dict[str, Any]]:
    items = pending_events()
    if mark_seen:
        s = _load_store()
        s["last_brief_ts"] = time.time()
        _save_store(s)
    return items


def print_brief() -> None:
    items = compile_brief()
    if not items:
        console.print("[dim]Nothing new since your last brief.[/dim]")
        return
    table = Table(title=f"☕ While you were away ({len(items)} changes)")
    table.add_column("When"); table.add_column("What"); table.add_column("Source")
    now = time.time()
    for e in items:
        ago = int(now - e["ts"])
        when = f"{ago // 60}m ago" if ago >= 60 else f"{ago}s ago"
        table.add_row(when, e["kind"], e["path"])
    console.print(table)
    console.print("[dim]Every item links to its source path above.[/dim]")


def maybe_show_brief_on_startup() -> None:
    """Called once at REPL startup. Silent unless there's something to show."""
    if not is_enabled():
        return
    ensure_watcher()
    items = pending_events()
    if items:
        console.print(Panel(
            f"[bold cyan]☕ While you were away:[/bold cyan] {len(items)} file "
            f"change(s) under your watch paths. Run [green]/brief[/green] for the digest.",
            border_style="cyan"))


def handle_brief(args: str) -> None:
    parts = args.split()
    sub = parts[0].lower() if parts else ""
    s = _load_store()
    if sub == "on":
        paths = [str(Path(p).expanduser().resolve()) for p in parts[1:]]
        if not paths:
            console.print("[yellow]Usage: /brief on <path> [path…] — which directories should I watch?[/yellow]")
            return
        bad = [p for p in paths if not Path(p).is_dir()]
        if bad:
            console.print(f"[red]Not directories: {', '.join(bad)}[/red]")
            return
        console.print(Panel(
            "[bold]Proactive briefs — what this does:[/bold]\n"
            f"• Watches: {', '.join(paths)}\n"
            "• Records file creates/modifies/deletes locally\n"
            "• Shows you a digest with `/brief` or at next startup\n"
            "• Nothing leaves this machine. Ever.\n"
            "• Turn off anytime with `/brief off`.",
            border_style="cyan"))
        if not confirm("Enable proactive briefs?"):
            console.print("[yellow]Not enabled.[/yellow]")
            return
        s["enabled"] = True
        s["watch_paths"] = paths
        _save_store(s)
        ensure_watcher()
        console.print("[green]✅ Briefs enabled. I'll keep an eye on those paths.[/green]")
    elif sub == "off":
        s["enabled"] = False
        _save_store(s)
        _watcher_stop.set()
        console.print("[dim]Briefs disabled and watcher stopped.[/dim]")
    elif sub == "status":
        console.print(f"Briefs: {'[green]ON[/green]' if s.get('enabled') else '[dim]OFF[/dim]'}")
        if s.get("watch_paths"):
            console.print("Watching: " + ", ".join(s["watch_paths"]))
        console.print(f"Pending items: {len(pending_events())}")
    else:  # /brief or /brief now
        if not s.get("enabled"):
            console.print("[dim]Briefs are off. `/brief on <paths>` to enable.[/dim]")
            return
        print_brief()
