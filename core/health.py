import os
import subprocess
import sys
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()

REPOS = {
    "jarvis-dev": {"path": "~/.jade-app", "check": f"{sys.executable} -m cli --help", "repair": f"{sys.executable} -m pip install -e ."},
    "navescript": {"path": "~/navescript", "check": "cargo --version", "repair": "cargo build --release"},
    "wikiproxy": {"path": "~/wikiproxy", "check": f"{sys.executable} -m wikiproxy --help", "repair": f"{sys.executable} -m pip install -e ."},
}

def check_system_health():
    """Check health of JADE and related engineering repos."""
    results = []
    
    if not REPOS:
        return results

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task(description="Scanning workspace for issues...", total=None)
        
        for name, info in REPOS.items():
            path = os.path.expanduser(info["path"])
            if not os.path.exists(path):
                results.append({"name": name, "status": "MISSING", "error": "Path not found"})
                continue
            
            try:
                # Run check command with 60s timeout to prevent startup hangs
                cmd = info["check"]
                res = subprocess.run(cmd, shell=True, cwd=path, capture_output=True, text=True, timeout=60.0)
                
                if res.returncode == 0:
                    results.append({"name": name, "status": "ONLINE", "error": None})
                else:
                    error_summary = res.stderr.splitlines()[-1] if res.stderr else "Unknown error"
                    results.append({"name": name, "status": "ERROR", "error": error_summary})
            except subprocess.TimeoutExpired:
                results.append({"name": name, "status": "TIMEOUT", "error": "Check command timed out"})
            except Exception as e:
                results.append({"name": name, "status": "CRITICAL", "error": str(e)})

    return results

def display_health_report(results):
    if not results:
        return False

    table = Table(title="Workspace System Health", show_header=True, header_style="bold magenta")
    table.add_column("Repository", style="cyan")
    table.add_column("Status", justify="center")
    table.add_column("Diagnostics / Action Required", style="dim")
    
    errors_found = False
    for res in results:
        status_style = "green" if res["status"] == "ONLINE" else "red"
        if res["status"] != "ONLINE":
            errors_found = True
        
        table.add_row(
            res["name"],
            f"[{status_style}]{res['status']}[/]",
            res["error"] or "[grey]Operational[/]"
        )
    
    console.print(table)
    return errors_found

def auto_repair_workspace(results):
    """Attempt to fix common issues in the workspace."""
    for res in results:
        if res["status"] == "ERROR":
            console.print(f"[yellow]Attempting to repair {res['name']}...[/yellow]")
            repair_cmd = REPOS[res["name"]].get("repair")
            if repair_cmd:
                path = os.path.expanduser(REPOS[res["name"]]["path"])
                subprocess.run(repair_cmd, shell=True, cwd=path)
            
            # Re-verify
            path = os.path.expanduser(REPOS[res["name"]]["path"])
            cmd = REPOS[res["name"]]["check"]
            final = subprocess.run(cmd, shell=True, cwd=path, capture_output=True)
            if final.returncode == 0:
                console.print(f"[green]✅ {res['name']} repaired successfully![/green]")
            else:
                console.print(f"[red]❌ Failed to repair {res['name']}. Manual intervention required.[/red]")

def update_all_repos():
    """Pull latest changes for all repos and reinstall."""
    for name, info in REPOS.items():
        path = os.path.expanduser(info["path"])
        if os.path.exists(os.path.join(path, ".git")):
            console.print(f"[bold cyan]Updating {name}...[/bold cyan]")
            subprocess.run(["git", "pull"], cwd=path)
        
        repair_cmd = info.get("repair")
        if repair_cmd:
            console.print(f"[dim]Reinstalling {name}...[/dim]")
            subprocess.run(repair_cmd, shell=True, cwd=path)
    console.print("[bold green]✅ All repositories synchronized and reinstalled.[/bold green]")
