"""Conversation time-travel: branchable conversation trees.

Most assistants give you one linear history. Jade keeps a tree: every
turn is a node with a parent pointer, so you can rewind to any earlier
point, branch off in a new direction, and diff two branches to see where
they diverged.

Persisted per session under ~/.jade/sessions/<id>/tree.json.
Slash commands: /rewind [n] · /branch <name> · /branches · /diff <a> <b>
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

console = Console()


def _sessions_dir() -> Path:
    from core.config import CONFIG_DIR
    return Path(CONFIG_DIR) / "sessions"


class ConversationTree:
    """A persistent, branchable tree of conversation turns."""

    def __init__(self, session_id: str = "default", path: Optional[Path] = None):
        self.session_id = session_id
        self.path = path or (_sessions_dir() / session_id / "tree.json")
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.branches: Dict[str, str] = {}
        self.current_branch = "main"
        self.head: Optional[str] = None  # node id of current position
        self._load()

    # -- persistence ----------------------------------------------------
    def _load(self) -> None:
        try:
            if self.path.is_file():
                data = json.loads(self.path.read_text())
                self.nodes = data.get("nodes", {})
                self.branches = data.get("branches", {})
                self.current_branch = data.get("current_branch", "main")
                self.head = self.branches.get(self.current_branch)
        except Exception:
            pass
        if "main" not in self.branches:
            self.branches["main"] = None  # type: ignore[assignment]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "nodes": self.nodes,
                "branches": self.branches,
                "current_branch": self.current_branch,
            }, indent=2))
        except Exception:
            pass

    # -- mutation ---------------------------------------------------------
    def note_turn(self, role: str, text: str) -> str:
        """Append a turn on the current branch. Returns the node id."""
        nid = uuid.uuid4().hex[:12]
        self.nodes[nid] = {
            "id": nid, "parent": self.head, "role": role,
            "text": text, "ts": time.time(), "branch": self.current_branch,
        }
        self.head = nid
        self.branches[self.current_branch] = nid
        self.save()
        return nid

    def rewind(self, n: int = 1) -> Dict[str, Any]:
        """Move the current pointer back *n* user turns (non-destructive)."""
        node = self.nodes.get(self.head or "")
        steps = 0
        while node and steps < n:
            # step back over one user turn + following assistant turns
            node = self.nodes.get(node.get("parent") or "")
            while node and node.get("role") != "user":
                node = self.nodes.get(node.get("parent") or "")
            steps += 1
        if node is None:
            return {"ok": False, "error": "already at the start of this branch"}
        self.head = node["id"]
        self.branches[self.current_branch] = self.head
        self.save()
        preview = (node.get("text", "") or "")[:80]
        return {"ok": True, "branch": self.current_branch,
                "at": f"{node.get('role')}: {preview}…"}

    def branch(self, name: str) -> Dict[str, Any]:
        """Name the current position as a new branch (or switch to it)."""
        name = name.strip()
        if not name:
            return {"ok": False, "error": "branch name required"}
        if name in self.branches:
            self.current_branch = name
            self.head = self.branches[name]
            self.save()
            return {"ok": True, "switched": True, "branch": name}
        self.branches[name] = self.head
        self.current_branch = name
        self.save()
        return {"ok": True, "switched": False, "branch": name}

    # -- inspection ---------------------------------------------------------
    def _chain(self, head: Optional[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        node = self.nodes.get(head or "")
        while node:
            out.append(node)
            node = self.nodes.get(node.get("parent") or "")
        return list(reversed(out))

    def current_chain(self) -> List[Dict[str, Any]]:
        return self._chain(self.head)

    def list_branches(self) -> List[Dict[str, Any]]:
        out = []
        for name, head in self.branches.items():
            chain = self._chain(head)
            out.append({"name": name, "turns": len(chain),
                        "current": name == self.current_branch,
                        "head_preview": (chain[-1].get("text", "")[:60] if chain else "—")})
        return out

    def diff(self, a: str, b: str) -> Dict[str, Any]:
        """Show where two branches diverged, turn by turn."""
        if a not in self.branches or b not in self.branches:
            return {"ok": False, "error": "unknown branch name"}
        ca, cb = self._chain(self.branches[a]), self._chain(self.branches[b])
        ids_a = {n["id"] for n in ca}
        common = 0
        for n in cb:
            if n["id"] in ids_a:
                common += 1
            else:
                break
        return {"ok": True, "a": a, "b": b, "common_turns": common,
                "only_in_a": ca[common:], "only_in_b": cb[common:]}

    def recent_context(self, n: int = 10) -> str:
        """Compact recent turns — used by /handoff to carry context along."""
        lines = []
        for node in self.current_chain()[-n:]:
            who = "You" if node.get("role") == "user" else "Jade"
            lines.append(f"{who}: {node.get('text', '')[:300]}")
        return "\n".join(lines)

    # -- display ------------------------------------------------------------
    def print_branches(self) -> None:
        table = Table(title="🌿 Conversation branches")
        table.add_column("Branch"); table.add_column("Turns", justify="right")
        table.add_column("Head")
        for b in self.list_branches():
            name = f"[bold green]{b['name']} ← you are here[/bold green]" if b["current"] else b["name"]
            table.add_row(name, str(b["turns"]), b["head_preview"])
        console.print(table)

    def print_diff(self, a: str, b: str) -> None:
        d = self.diff(a, b)
        if not d.get("ok"):
            console.print(f"[red]{d.get('error')}[/red]")
            return
        console.print(Panel(
            f"[bold]{a}[/bold] vs [bold]{b}[/bold] — "
            f"{d['common_turns']} shared turns, then they diverge.",
            border_style="cyan"))
        for label, key in ((f"only in {a}", "only_in_a"), (f"only in {b}", "only_in_b")):
            turns = d[key]
            if turns:
                console.print(f"\n[bold yellow]{label}:[/bold yellow]")
                for n in turns:
                    who = "You" if n.get("role") == "user" else "Jade"
                    console.print(f"  [dim]{who}:[/dim] {(n.get('text', '') or '')[:160]}")


_trees: Dict[str, ConversationTree] = {}


def current_tree(session_id: str = "default") -> ConversationTree:
    if session_id not in _trees:
        _trees[session_id] = ConversationTree(session_id)
    return _trees[session_id]


def handle_timetravel(cmd: str, args: str, tree: Optional[ConversationTree] = None) -> None:
    """Wire-up for /rewind · /branch · /branches · /diff."""
    tree = tree or current_tree()
    if cmd == "/rewind":
        n = 1
        try:
            n = max(1, int((args.split() or ["1"])[0]))
        except ValueError:
            pass
        r = tree.rewind(n)
        console.print(f"[green]⏪ rewound to:[/green] {r['at']}"
                      if r.get("ok") else f"[red]{r.get('error')}[/red]")
    elif cmd == "/branch":
        name = args.strip()
        if not name:
            from rich.prompt import Prompt
            name = Prompt.ask("Branch name")
        r = tree.branch(name)
        if r.get("ok"):
            verb = "switched to" if r.get("switched") else "created and switched to"
            console.print(f"[green]🌿 {verb} branch '{r['branch']}'[/green]")
        else:
            console.print(f"[red]{r.get('error')}[/red]")
    elif cmd == "/branches":
        tree.print_branches()
    elif cmd == "/diff":
        parts = args.split()
        if len(parts) < 2:
            console.print("[yellow]Usage: /diff <branch-a> <branch-b>[/yellow]")
        else:
            tree.print_diff(parts[0], parts[1])
