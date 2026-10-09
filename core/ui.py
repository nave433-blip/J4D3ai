import sys
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from rich.align import Align
from core.update import CURRENT_VERSION

console = Console()

def set_warp_status(text: str):
    """Set the terminal tab title (OSC 2) for Warp and other compatible terminals."""
    # Only if in a TTY to avoid polluting logs
    if sys.stdout.isatty():
        sys.stdout.write(f"\033]2;{text}\007")
        sys.stdout.flush()

def clear_warp_status():
    """Reset the terminal tab title."""
    set_warp_status("JADE")

def display_chat_message(role: str, text: str):
    """Display a message in a Gemini-style high-fidelity ASCII box."""
    from rich.box import ROUNDED
    from rich.panel import Panel
    from rich.markdown import Markdown

    # Clear status when message is displayed
    clear_warp_status()
    
    color = "cyan" if role.lower() == "jade" else "green"
    icon = "🧠" if role.lower() == "jade" else "👤"
    
    panel = Panel(
        Markdown(text),
        title=f"[bold {color}]{icon} {role.upper()}[/bold {color}]",
        title_align="left",
        border_style=color,
        box=ROUNDED,
        padding=(1, 2)
    )
    console.print("\n")
    console.print(panel)

def display_welcome():
    console.print(Align.center(Panel(
        Markdown(f"# JADE\nCreated by **Nave433 (Evan Shipley)**\n\nVersion: `{CURRENT_VERSION}`"),
        style="bold blue",
        border_style="cyan",
        subtitle="Type /help to see what I can do",
        expand=False,
    )))
    console.print(Align.center(
        "[dim]Tip: run [bold cyan]/connect[/bold cyan] to link your AI providers, "
        "[bold cyan]/menu[/bold cyan] for the dashboard[/dim]\n"
    ))
    console.print(Align.center("[dim italic]Wake me when you need me.[/dim italic]"))


# ---------------------------------------------------------------------------
# Shared UI helpers — one visual language for menus, tables, banners,
# error panels and help output.
# ---------------------------------------------------------------------------

def ui_error(title: str, what: str, why: str = "", next_steps: str = "") -> Panel:
    """Consistent error panel. Every error answers: what happened, why,
    and what to do next."""
    body = f"[bold red]{what}[/bold red]"
    if why:
        body += f"\n\n[white]Why:[/white] [dim]{why}[/dim]"
    if next_steps:
        body += f"\n\n[white]What to do next:[/white]\n[green]{next_steps}[/green]"
    return Panel(body, title=f"[bold red]❌ {title}[/bold red]", border_style="red")


def ui_success(message: str) -> None:
    console.print(f"[bold green]✅ {message}[/bold green]")


def ui_info(message: str) -> None:
    console.print(f"[cyan]ℹ️  {message}[/cyan]")


def ui_warning(message: str) -> None:
    console.print(f"[yellow]⚠️  {message}[/yellow]")


def unknown_command_panel(text: str, suggestions: list) -> Panel:
    """'Did you mean …?' panel for mistyped slash commands."""
    body = f"[white]Unknown command:[/white] [bold yellow]{text}[/bold yellow]"
    if suggestions:
        lines = "\n".join(f"  [bold cyan]{s}[/bold cyan]" for s in suggestions)
        body += f"\n\n[white]Did you mean:[/white]\n{lines}"
    body += "\n\n[dim]Type [bold]/help[/bold] to see all commands.[/dim]"
    return Panel(body, title="[bold yellow]Unknown command[/bold yellow]", border_style="yellow")


def next_steps_panel(steps: list, title: str = "What next?") -> Panel:
    """Numbered next-step guidance (onboarding, post-connect, post-setup)."""
    lines = "\n".join(f"  [bold cyan]{i}.[/bold cyan] {s}" for i, s in enumerate(steps, 1))
    return Panel(lines, title=f"[bold green]🎯 {title}[/bold green]", border_style="green")


def _menu_group(title: str, border: str, rows: list) -> Table:
    table = Table(title=f"[bold]{title}[/bold]", show_header=True,
                  header_style=f"bold {border}", border_style=border)
    table.add_column("Command", style="white")
    table.add_column("What it does", style="dim")
    for cmd, desc in rows:
        table.add_row(cmd, desc)
    return table


def get_menu_grid() -> Table:
    """Grouped dashboard tables shared by /menu and /help."""
    grid = Table.grid(expand=True)
    grid.add_column(justify="center")
    grid.add_column(justify="center")

    agents = _menu_group("🤖 Core AI Agents", "magenta", [
        ("/chat", "Consult JADE — technical advice, explanations"),
        ("/fix", "Autonomous research & repair loop for bugs"),
        ("/forge", "Code synthesis & creation"),
        ("/plan", "Strategic engineering roadmaps"),
        ("/troubleshoot", "Run a command, auto-fix errors"),
        ("/analyze", "Project health audit: lines, complexity, hotspots"),
        ("/nave", "Multi-model reasoning & refinement"),
        ("/hive", "Hive mind: ask all connected AIs, get one consensus"),
        ("/swarm", "Agent swarm: parallel planner → workers → reviewer"),
        ("/research", "Deep research: multi-query web research w/ citations"),
        ("/council", "Council: providers propose, critique, converge (experimental)"),
        ("/code", "Agentic coding: propose diff, confirm, apply, rewind"),
        ("/suggest", "Suggest a shell command for a task (never auto-runs)"),
        ("/explain", "Explain a shell command in plain language"),
        ("/schedule", "Task scheduler: cron/interval/one-shot jobs in-Jade"),
        ("/rewind", "Conversation time-travel: rewind, branch, diff"),
    ])
    devops = _menu_group("🛠️ DevOps & Utilities", "green", [
        ("/doctor", "System health check & self-repair"),
        ("/network", "Local network discovery & port scanning"),
        ("/server", "Monitor ports, processes, services"),
        ("/hardware", "USB & physical port probing"),
        ("/ssh", "Run commands on remote servers"),
        ("/memory", "Search your persistent knowledge base"),
        ("/dashboard", "Local web dashboard: status, chat, logs"),
        ("/mcp", "External tool servers via MCP (stdio)"),
        ("/github", "GitHub: repos, issues, PRs, Actions, code search"),
        ("/api", "OpenAI-compatible API server for external clients"),
        ("/ollama", "Ollama fleet: prune, bench, stats, auto-pull"),
        ("/thin", "Thin client for low-resource nodes"),
    ])
    accounts = _menu_group("🔌 Accounts & Setup", "cyan", [
        ("/connect", "Link AI providers (API keys, Ollama, …)"),
        ("/connections", "See what's connected & reachable"),
        ("/models", "Switch provider / model"),
        ("/config", "Settings & configuration"),
        ("/personality", "Assistant tone: Professional, Mentor, …"),
        ("/prompts", "Saved system-prompt roles"),
        ("/skill", "Share & install verified skill packs"),
        ("/brief", "Proactive 'while you were away' briefs"),
        ("/handoff", "Hand a live session to another node"),
        ("/help", "Full command reference"),
        ("/exit", "Shut down cleanly"),
    ])
    grid.add_row(agents, devops)
    grid.add_row(accounts, _menu_group("⚡ Quick Starts", "yellow", [
        ("/connect", "First run? Start here to link an AI"),
        ("/chat hello", "Talk to your active provider"),
        ("/fix .", "Let JADE audit this directory"),
        ("/doctor", "Check everything is healthy"),
    ]))
    return grid


def build_provider_status_table(entries: list) -> Table:
    """Two-column provider status table shared by /models views.

    entries: list of (label, is_linked_bool) tuples.
    """
    table = Table(title="Intelligence Provider Status", border_style="dim")
    table.add_column("Provider", style="cyan")
    table.add_column("Status", justify="center")
    table.add_column("Provider", style="cyan")
    table.add_column("Status", justify="center")
    for i in range(0, len(entries), 2):
        row = []
        for j in range(2):
            if i + j < len(entries):
                label, linked = entries[i + j]
                status = "[bold green]✓ connected[/bold green]" if linked else "[dim]○ not linked[/dim]"
                row.extend([label, status])
            else:
                row.extend(["", ""])
        table.add_row(*row)
    return table

def get_main_menu_table():
    table = Table(show_header=False, box=None)
    table.add_column("Command", style="cyan", justify="right")
    table.add_column("Description", style="white")
    
    table.add_row("/chat", "Consult JADE for technical advice or code explanation")
    table.add_row("/fix", "Autonomous research & repair loop for project bugs")
    table.add_row("/analyze", "Deep health audit: lines, complexity, and file hotspots")
    table.add_row("/analyze-file", "Focused security and performance audit on a single file")
    table.add_row("/locate", "Global system search for files and directories")
    table.add_row("/troubleshoot", "Warp-style Agent Mode: Run command and auto-fix errors")
    table.add_row("/network", "Fing-style local network discovery and port scanning")
    table.add_row("/ssh", "Execute commands on remote servers via agentic SSH")
    table.add_row("/server", "Monitor local ports, process stats, and manage services")
    table.add_row("/undo", "Safety rollback: Revert the last file change made by JADE")
    table.add_row("/dashboard", "Launch live multi-window system monitoring interface")
    table.add_row("/memory", "Search or manage the persistent vector knowledge base")
    table.add_row("/personality", "Switch between Professional, Sarcastic, Concise, or Mentor vibes")
    table.add_row("/models", "Intelligent provider switcher (Ollama, NVIDIA, OpenAI, etc.)")
    table.add_row("/launch", "Spin up specialized AI agents (Claude Code, Hermes, Copilot CLI)")
    table.add_row("/model", "Detailed status of active model, provider, and current quota")
    table.add_row("/cloud", "Bridge to Google Drive, Dropbox, and iCloud storage")
    table.add_row("/focus", "Set a specific path as the primary work context for the agent")
    table.add_row("/help", "Access detailed system documentation and role guide")
    table.add_row("/clippy", "A familiar little helper has questions about what you're doing")
    table.add_row("/exit", "Secure shutdown of all background threads and exit")
    
    return Panel(table, title="[bold white]System Commands & Capabilities[/bold white]", border_style="blue", expand=False)
