import sys
import traceback
import os
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from core.approvals import confirm

console = Console()

from core.config import load_config

def self_repair_hook(exctype, value, tb):
    """Global exception hook for JADE self-repair and reporting."""
    # To avoid recursion if the repair engine itself fails
    sys.excepthook = sys.__excepthook__
    
    config = load_config()
    auto_repair_enabled = config.get("self_repair", False)

    error_msg = "".join(traceback.format_exception(exctype, value, tb))
    console.print("\n[bold red]⚠️ JADE CRITICAL FAILURE DETECTED[/bold red]")
    console.print(Panel(error_msg, title="Traceback", border_style="red"))
    
    # Extract the failing file path
    last_trace = traceback.extract_tb(tb)[-1]
    failing_file = last_trace.filename
    line_no = last_trace.lineno
    
    console.print(f"\n[bold yellow]Failing Component:[/bold yellow] {failing_file} (Line {line_no})")
    
    should_repair = auto_repair_enabled or confirm("\nWould you like JADE to attempt an autonomous self-repair?")
    
    if should_repair:
        from core.brain import think
        from tools.editor import replace_in_file
        
        with open(failing_file, 'r') as f:
            source_code = f.read()
            
        repair_prompt = f"""
        I have crashed with the following error:
        {error_msg}
        
        The failing file is: {failing_file}
        
        Here is the source code of that file:
        {source_code}
        
        Your task is to provide a fix using the EDIT tool. Provide ONLY the tool block.
        """
        
        console.print("[bold cyan]Analyzing crash and generating patch...[/bold cyan]")
        res = think("", repair_prompt)
        response = res.get("text", str(res)) if isinstance(res, dict) else str(res)
        
        # Look for TOOL: EDIT in response
        if "TOOL: EDIT" in response:
            console.print("[green]Self-repair patch generated! Applying...[/green]")
            # Simplified tool parsing for emergency repair
            lines = response.split("\n")
            for i, line in enumerate(lines):
                if "TOOL: EDIT" in line and i+1 < len(lines):
                    import json
                    try:
                        args = json.loads(lines[i+1].replace("ARGS:", "").strip())
                        res = replace_in_file(args["path"], args["old"], args["new"])
                        console.print(f"[bold green]{res}[/bold green]")
                        console.print("\n[bold cyan]Please restart JADE to verify the fix.[/bold cyan]")
                        return
                    except Exception as e:
                        console.print(f"[red]Failed to parse repair patch: {e}[/red]")
        else:
            console.print("[red]Could not determine an automated fix.[/red]")

    if not auto_repair_enabled and confirm("\nWould you like to report this error log to the GitHub repository?"):
        from tools.github import github_tool
        from core.update import CURRENT_VERSION
        
        title = f"Crash Report: {exctype.__name__} in {os.path.basename(failing_file)}"
        body = f"""
        ### System Info
        - OS: {sys.platform}
        - Version: {CURRENT_VERSION}
        - File: {failing_file}
        
        ### Traceback
        ```python
        {error_msg}
        ```
        """
        console.print("[bold cyan]Opening GitHub issue...[/bold cyan]")
        # Consent already obtained by the confirm() above; skip the second prompt.
        res = github_tool.create_issue("nave433-blip/jarvis-dev", title, body, _confirm=False)
        if "Error" not in str(res):
            console.print(f"[green]Issue successfully created: {res.get('html_url')}[/green]")
        else:
            console.print(f"[red]Failed to report issue: {res}[/red]")

def auto_check_on_launch():
    """Run health checks at startup. Repairs NEVER run without explicit approval:
    the exact commands are listed and the user confirms once before anything
    executes. (Previously, self_repair=true ran `pip install` repairs silently.)
    """
    from core.health import check_system_health, display_health_report, auto_repair_workspace, REPOS
    config = load_config()

    with console.status("[bold cyan]System Diagnostics...[/bold cyan]"):
        health_results = check_system_health()

    errors_found = display_health_report(health_results)
    if not errors_found:
        return
    if not config.get("self_repair", False):
        console.print("[dim]Self-repair is disabled. Fix the issues above manually.[/dim]")
        return

    planned = [
        (res["name"], REPOS[res["name"]].get("repair"))
        for res in health_results
        if res["status"] == "ERROR" and REPOS.get(res["name"], {}).get("repair")
    ]
    if not planned:
        console.print("[dim]No automatic repairs available for these issues.[/dim]")
        return
    console.print("[bold yellow]Proposed repairs:[/bold yellow]")
    for name, cmd in planned:
        console.print(f"  • {name}: [dim]{cmd}[/dim]")
    if confirm("Run these repair commands now?"):
        auto_repair_workspace(health_results)
    else:
        console.print("[dim]Skipping repairs.[/dim]")

def init_repair_engine():
    sys.excepthook = self_repair_hook
