"""Connector registry. Importing this module registers all connectors."""
from __future__ import annotations

from typing import Dict, List

from core.connectors.base import Connector
from core.connectors.google_drive import GoogleDriveConnector
from core.connectors.gmail import GmailConnector
from core.connectors.google_calendar import GoogleCalendarConnector
from core.connectors.google_tasks import GoogleTasksConnector
from core.connectors.google_contacts import GoogleContactsConnector
from core.connectors.google_docs import GoogleDocsConnector
from core.connectors.youtube import YouTubeConnector
from core.connectors.outlook import OutlookConnector
from core.connectors.onedrive import OneDriveConnector
from core.connectors.teams import TeamsConnector
from core.connectors.onenote import OneNoteConnector
from core.connectors.azure_mgmt import AzureConnector

_REGISTRY: Dict[str, Connector] = {}


def _register(conn: Connector) -> None:
    _REGISTRY[conn.id] = conn


for _cls in (GoogleDriveConnector, GmailConnector, GoogleCalendarConnector,
             GoogleTasksConnector, GoogleContactsConnector,
             GoogleDocsConnector, YouTubeConnector,
             OutlookConnector, OneDriveConnector, TeamsConnector,
             OneNoteConnector, AzureConnector):
    _register(_cls())


def list_connectors() -> List[Connector]:
    return list(_REGISTRY.values())


def get_connector(conn_id: str) -> Connector:
    if conn_id not in _REGISTRY:
        raise KeyError(f"Unknown connector '{conn_id}'. "
                       f"Available: {', '.join(sorted(_REGISTRY))}")
    return _REGISTRY[conn_id]


def connector_status_all() -> List[Dict]:
    return [c.status() for c in list_connectors()]


def connectors_menu() -> None:
    """Interactive connector manager."""
    from rich.console import Console
    from rich.prompt import Prompt
    from rich.table import Table
    console = Console()
    while True:
        table = Table(title="App Connectors", border_style="cyan")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("App", style="white")
        table.add_column("Status", style="green")
        table.add_column("Account / Next step", style="dim")
        for c in list_connectors():
            st = c.status()
            status = "🟢 connected" if st["connected"] else "⚪ not connected"
            detail = st["account"] if st["connected"] else st["needs"]
            table.add_row(c.id, c.display, status, detail[:80])
        console.print(table)
        console.print("\n[bold white]Options:[/bold white] [c]onnect  [d]isconnect  [a]ctions  [b]ack")
        choice = Prompt.ask("Action", choices=["c", "d", "a", "b"], default="b")
        if choice == "b":
            break
        cid = Prompt.ask("Connector id").strip()
        try:
            conn = get_connector(cid)
        except KeyError as e:
            console.print(f"[red]❌ {e}[/red]")
            continue
        if choice == "c":
            res = conn.connect()
            console.print(f"[green]✅ Connected as {res.get('account', '')}.[/green]"
                          if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
        elif choice == "d":
            res = conn.disconnect()
            console.print(f"[green]✅ {res.get('message', '')}[/green]"
                          if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
        elif choice == "a":
            acts = conn.actions()
            if not acts:
                console.print("[yellow]No actions exposed.[/yellow]")
                continue
            for name, info in acts.items():
                console.print(f"  [cyan]{name}[/cyan] — {info['description']}")
            name = Prompt.ask("Action to run (empty = cancel)", default="").strip()
            if not name:
                continue
            if name not in acts:
                console.print(f"[red]Unknown action '{name}'.[/red]")
                continue
            from core.approvals import confirm
            if not confirm(f"Run {conn.display} action '{name}'?"):
                console.print("[yellow]Cancelled.[/yellow]")
                continue
            import inspect
            sig = inspect.signature(acts[name]["run"])
            kwargs = {}
            for p in sig.parameters.values():
                if p.default is inspect.Parameter.empty:
                    kwargs[p.name] = Prompt.ask(f"  {p.name}").strip()
            res = conn.run_action(name, **kwargs)
            console.print_json(data=res)
