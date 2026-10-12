import os
import sys
import time
import json
import subprocess
import logging as _logging

# 0. Silence third-party chatter (LiteLLM cost-map fetch, httpx, …) for normal
#    users. Must run before any other import: litellm can warn at import/first
#    use. --debug and dev mode restore full logging in main().
for _noisy in ("LiteLLM", "litellm", "httpx", "httpcore"):
    _logging.getLogger(_noisy).setLevel(_logging.ERROR)
os.environ.setdefault("LITELLM_LOG", "ERROR")
del _noisy
from typing import Optional, Dict, Any
from typing_extensions import Annotated

# 1. Self-Repairing Dependency Check (runs on CLI invocation, not on import —
#    importing this module as a library must stay side-effect free)
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from core.deps import ensure_all

# 2. Global Self-Repair & Reporting Engine
from core.repair import init_repair_engine

# 3. Auto-Update Check
from core.update import auto_update_check, CURRENT_VERSION


def run_startup_checks() -> None:
    """Heavy startup side effects: only run for real CLI invocations.

    Skipped when JADE_SKIP_STARTUP=1 (tests, library use) so that merely
    importing this module never pip-installs packages, hits the network,
    or prompts for input.
    """
    if os.environ.get("JADE_SKIP_STARTUP") == "1":
        return
    ensure_all()
    init_repair_engine()
    auto_update_check()

import typer
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from rich.prompt import Prompt, Confirm
from core import approvals
from core.approvals import confirm

# Internal Modules
from core.brain import think, think_structured, get_provider
from core.agent import debug_loop, troubleshoot_loop, forge_loop
from core.config import setup_wizard, get_env_with_config, CONFIG_FILE, load_config, save_config, is_dev_mode
from core.devmode import print_dev_banner
from core.services import repair_ollama
from core.config import start_periodic_config_maintenance, auto_config_maintenance_once
from core.logger import ErrorLogger
from core.auth import AuthManager
from core.startup import startup_check_and_login
from core.ui import (display_welcome, display_chat_message,
                     get_menu_grid, unknown_command_panel, next_steps_panel,
                     ui_error, ui_success, ui_info, ui_warning)
from core.health import check_system_health, display_health_report, auto_repair_workspace, update_all_repos
from core.repair import auto_check_on_launch
from core.nave_loop import run_nave_loop
from core.handler import CommandHandler
import core.menus as menus

# Prompt Toolkit for slash commands
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.styles import Style
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML

import logging
import warnings
warnings.simplefilter("ignore", SyntaxWarning)

_welcome_shown = False


def _show_welcome_once() -> None:
    """Welcome-first startup: splash and dev banner print exactly once,
    before any setup wizard, so first-run users see JADE before questions."""
    global _welcome_shown
    if not _welcome_shown:
        display_welcome()
        print_dev_banner()
        _welcome_shown = True


app = typer.Typer(help="🚀 JADE: The Ultimate Local AI Coding Assistant", add_completion=False)

# MCP subcommand group
mcp_app = typer.Typer(help="Model Context Protocol (MCP) client", add_completion=False)
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("servers")
def mcp_servers_cmd():
    """List configured MCP servers."""
    from core.mcp_client import cmd_servers
    cmd_servers()


@mcp_app.command("tools")
def mcp_tools_cmd(server: Annotated[Optional[str], typer.Argument(help="Server name (optional)")] = None):
    """List tools exposed by one or all MCP servers."""
    from core.mcp_client import cmd_tools
    cmd_tools(server)


@mcp_app.command("call")
def mcp_call_cmd(
    server: Annotated[str, typer.Argument(help="MCP server name")],
    tool: Annotated[str, typer.Argument(help="Tool name")],
    args_json: Annotated[str, typer.Argument(help="JSON object of arguments")] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
):
    """Call an MCP tool on a configured server."""
    from core.mcp_client import cmd_call
    cmd_call(server, tool, args_json, auto_yes=yes)


@mcp_app.command("setup")
def mcp_setup_cmd():
    """Show MCP configuration help."""
    from core.mcp_client import setup_docs
    from rich.console import Console
    Console().print(setup_docs())


console = Console()

@app.callback(invoke_without_command=True)
def main(ctx: typer.Context,
         debug: bool = typer.Option(False, "--debug", help="Enable debug logging"),
         skip_startup: bool = typer.Option(False, "--skip-startup", help="Skip dependency/update startup checks"),
         yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve confirmation prompts (dev only; credential/trust prompts still ask)")):
    """JADE: Your local AI engineer."""
    if skip_startup:
        os.environ["JADE_SKIP_STARTUP"] = "1"
    if yes:
        approvals.set_session_yes(True)
    approvals.startup_banner()
    run_startup_checks()
    if debug:
        import logging
        logging.basicConfig(level=logging.DEBUG)
        os.environ["LITELLM_LOG"] = "DEBUG"
        for name in ("LiteLLM", "litellm", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.DEBUG)
        console.print("[dim]Debug mode enabled.[/dim]")
    elif is_dev_mode():
        import logging
        logging.basicConfig(level=logging.DEBUG)
        os.environ["LITELLM_LOG"] = "DEBUG"
        for name in ("LiteLLM", "litellm", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.DEBUG)
        console.print("[dim]\U0001f6e0 Dev mode: debug logging enabled.[/dim]")
    if ctx.invoked_subcommand is None:
        if not CONFIG_FILE.exists():
            # Welcome first, then environment detection + /connect flow.
            _show_welcome_once()
            console.print("[yellow]No configuration found. Let's get you set up.[/yellow]")
            setup_wizard()
        try:
            from core.scheduler import maybe_start_scheduler
            maybe_start_scheduler()  # background thread; jobs persist in ~/.jade/
        except Exception:
            pass
        interactive()

# Initialize Advanced Handler
handler = CommandHandler()

from core.gemini_box import session as gemini_session

COMMANDS = [
    "/chat", "/fix", "/forge", "/decode", "/lookup", "/hardware", "/voice", "/watch", "/config", "/init", "/analyze", "/analyze-file",
    "/locate", "/undo", "/dashboard", "/memory", "/personality", "/models", "/focus", "/copilot",
    "/cloud", "/network", "/ssh", "/server", "/troubleshoot", "/free", "/model", "/brain", "/doctor", "/box",
    "/examine-py", "/patch-py", "/ollama",

    "/git", "/nave", "/sync", "/upgrade", "/update", "/connect", "/connections", "/launch", "/plan", "/restart", "/reinstall", "/menu", "/exit",
    "/prompts", "/search", "/clear", "/health", "/google-login", "/google-sync", "/google-register",
    "/google-connect", "/webask", "/multibrain", "/scan-ollama", "/ollama-login", "/p2p-scan", "/p2p-status", "/p2p-edit", "/p2p-read", "/p2p-server", "/p2p-tokens", "/p2p-set-token", "/optimize", "/refine", "/stress-test",
    "/hive", "/swarm", "/mcp", "/research",
    "/council", "/code", "/suggest", "/explain", "/github", "/api",
    "/settings", "/profile", "/project", "/sim", "/prompt", "/memories",
    "/connector", "/signin", "/account",
    "/help", "/t", "/thin", "/clippy",
    "/rewind", "/branch", "/branches", "/diff", "/skill", "/brief", "/handoff",
    "/schedule", "/voice-profile", "/gui", "/overlay", "/tray", "/voice-wake",
]

# ... (omitted)

@app.command()
def ollama_cmd(args: Annotated[Optional[str], typer.Argument(help="Ollama CLI arguments")] = None):
    """Direct interface to the local Ollama CLI."""
    import shlex
    import subprocess
    cmd = ["ollama"] + (shlex.split(args) if args else [])
    console.print(f"[dim]Executing: {' '.join(cmd)}[/dim]")
    try:
        subprocess.run(cmd, shell=False)
    except FileNotFoundError:
        console.print("[red]'ollama' executable not found in PATH.[/red]")
    except Exception as e:
        console.print(f"[red]Ollama CLI failed: {e}[/red]")

@app.command()
def scan_ollama():
    """Scan the local network for Ollama instances."""
    from tools.network import scan_network_for_ollama
    from core.config import load_config, save_config
    
    console.print("[cyan]Scanning network for Ollama instances...[/cyan]")
    hosts = scan_network_for_ollama()
    
    if not hosts:
        console.print("[red]No Ollama instances found.[/red]")
        return
        
    console.print(f"[green]Found: {', '.join(hosts)}[/green]")
    if confirm("Add these to your configuration?"):
        cfg = load_config()
        cfg["ollama_hosts"] = list(set(cfg.get("ollama_hosts", []) + hosts))
        save_config(cfg)
        console.print("[green]✅ Configuration updated.[/green]")

# ... (omitted)

@app.command()
def multibrain(task: Annotated[str, typer.Argument(help="The task or query to reason about using multiple AIs")]):
    """Reason about a task using all connected high-performance AIs."""
    from core.brain import multibrain_think
    res = multibrain_think(task)
    if res.get("ok"):
        display_chat_message("JADE", res.get("text"))
    else:
        console.print(f"[red]Error: {res.get('error')}[/red]")

# Register commands for fuzzy matching
for cmd in COMMANDS:
    if cmd != "/exit":
        handler.register(cmd, lambda x: None, help="System command")

def get_bottom_toolbar():
    try:
        cwd = os.getcwd()
        config = load_config()
        model = config.get("jade_model", "unknown")
        provider = config.get("provider", "ollama")
        return HTML(f'<style fg="cyan">📁 {cwd}</style> | <style fg="magenta">🧠 {provider.upper()} ({model})</style>')
    except Exception:
        return HTML('<style fg="red">System Initializing...</style>')

@app.command()
def menu():
    """Launch the high-fidelity JADE Dashboard Menu."""
    console.clear()
    header = Panel(Markdown(f"# JADE SYSTEM INTERFACE\nVersion: `{CURRENT_VERSION}`"), style="bold cyan", border_style="cyan")
    console.print(header)
    console.print(get_menu_grid())
    footer = Panel("[bold white]Settings:[/bold white] /config  [bold white]Accounts:[/bold white] /connect /connections", border_style="dim")
    console.print(footer)
    console.print("\n[dim]*Type any command or natural language request below.*[/dim]")

CLIPPY_ART = r"""
   +-----+
   |     |
   |     |
   +-----+
     |_|
"""

@app.command()
def clippy():
    """A familiar little helper with questions about what you're doing."""
    from core.config import load_config
    cfg = load_config()
    provider = (cfg.get("provider") or "").strip()
    if not provider:
        line = "It looks like you're trying to give me a brain. Shall we run /connect?"
    elif provider == "ollama":
        line = "It looks like you're running local. Need a model tuned up? Try /ollama bench."
    else:
        line = "It looks like you're getting things done. Need a hand? Try /menu to see what I can do."
    console.print(Panel(
        f"[bold yellow]{CLIPPY_ART}[/bold yellow]\n{line}\n[dim]— Clippy (definitely not watching you type)[/dim]",
        title="📎 Clippy", border_style="yellow", expand=False,
    ))

def process_think_res(res: Any, fallback_text: str = "") -> str:
    """Processes the structured dictionary from think() and handles failures."""
    if not isinstance(res, dict):
        return str(res)
        
    if res.get("ok"):
        # Display provider info if available
        if res.get("provider"):
            prov_info = f"Provider: {res.get('provider').upper()}"
            if res.get("model"): prov_info += f" ({res.get('model')})"
            console.print(f"[dim]{prov_info}[/dim]")
        return res.get("text", "")
        
    # Failure case — human-friendly. Raw provider errors stay hidden unless
    # the user is debugging (dev mode / --debug).
    debug = os.environ.get("JADE_DEV_MODE") in ("1", "true", "yes", "on") or is_dev_mode()
    why = res.get("error", "no provider could answer")
    next_steps = "Run /connect to link a provider, /connections --test to check them, or /models to switch."
    console.print(ui_error("No answer", "JADE could not get an answer from any provider.",
                           why=why if debug else "", next_steps=next_steps))

    # Show short history only when debugging
    if debug:
        hist = res.get("history", [])
        if hist:
            summary = []
            for h in hist:
                if isinstance(h, dict) and "attempt" in h:
                    summary.append(f"Primary ({h.get('provider')}): {h.get('result', {}).get('error', 'Unknown error')}")
                elif isinstance(h, dict) and "fallback" in h:
                    for f in h.get("history", []):
                        summary.append(f"Fallback ({f.get('provider')}): {f.get('result', {}).get('error', 'Unknown error')}")
            if summary:
                console.print(Panel("\n".join(summary[-6:]), title="Recent Attempts (debug)", border_style="magenta"))

    # Offer the secure connect flow (not the legacy plaintext setup wizard)
    if confirm("Would you like to set up a provider now?"):
        from core.connect import run_connect_wizard
        run_connect_wizard()
        return "[yellow]Setup complete. Please try your request again.[/yellow]"
    else:
        ui_info("Skipping setup. Run /connect any time to link a provider.")
        return fallback_text or "[red]Task failed due to provider disconnection.[/red]"

@app.command()
def interactive():
    """Launch the main interactive Gemini-style prompt."""
    from core.config import verify_and_fix_local_llm
    # JADE_SKIP_STARTUP=1 (tests, CI, piped use) also skips interactive()'s
    # own startup routines so piped stdin reaches the REPL instead of being
    # eaten by setup prompts. Real interactive use is unchanged.
    skip_startup = os.environ.get("JADE_SKIP_STARTUP") == "1"
    try:
        _show_welcome_once()
        if not skip_startup:
            verify_and_fix_local_llm()
            auto_check_on_launch()
            startup_check_and_login(auto=False)
    except (EOFError, KeyboardInterrupt):
        # Startup prompts need a TTY. With piped/closed stdin (CI, `echo | jade`)
        # or Ctrl+C during startup, exit cleanly instead of tracebacking.
        # Interactive TTY behavior is unchanged.
        console.print("\n[yellow]Startup input unavailable — exiting.[/yellow]")
        return

    # Opt-in proactive briefs: silent unless there's something to show.
    try:
        from core.briefs import maybe_show_brief_on_startup
        maybe_show_brief_on_startup()
    except Exception:
        pass

    completer = WordCompleter(COMMANDS, ignore_case=True)
    kb = KeyBindings()
    @kb.add('escape')
    def _(event): event.app.exit(result="/exit")

    session = PromptSession(completer=completer, key_bindings=kb, bottom_toolbar=get_bottom_toolbar)
    style = Style.from_dict({'prompt': 'ansicyan bold', 'bottom-toolbar': 'bg:#1e1e1e #888888'})
    last_ctrl_c = 0

    # Command classification
    PLAIN_COMMANDS = {cmd[1:] for cmd in COMMANDS if cmd.startswith("/")}
    DANGEROUS_PLAIN_CMDS = {"reinstall", "update", "upgrade", "patch-py", "repair-ollama-cmd", "setup", "sync"}

    while True:
        try:
            text = session.prompt('JADE > ', style=style).strip()
            last_ctrl_c = 0 
            if not text: continue
            if text in ["/exit", "exit", "quit"]:
                console.print("[yellow]Goodbye, Sir.[/yellow]")
                console.print("[dim]It is now safe to turn off your computer.[/dim]"); break
            
            try:
                # 1. Intelligent Input Interpretation
                tokens = text.split()
                first_word = tokens[0].lower() if tokens else ""
                
                # Check for "no-slash" command invocation
                if not text.startswith("/") and first_word in PLAIN_COMMANDS:
                    # Guard dangerous commands
                    if first_word in DANGEROUS_PLAIN_CMDS:
                        console.print(Panel(f"[bold yellow]⚠️ DANGEROUS COMMAND DETECTED[/bold yellow]\n\nYou invoked '{first_word}' without a leading '/'.", border_style="yellow"))
                        if not confirm(f"Are you sure you want to execute '{first_word}'?"):
                            console.print("[yellow]Cancelled.[/yellow]")
                            continue
                    
                    # Transform to slash command for the handler
                    text = "/" + text

                # 2. Slash commands: resolve locally first (exact + fuzzy).
                # Unknown ones get a "did you mean?" panel — no LLM call burned.
                if text.startswith("/"):
                    matched, args = handler.resolve(text)
                    if matched is None:
                        first = text.split()[0] if text.split() else text
                        console.print(unknown_command_panel(first, handler.suggest(first)))
                        continue
                    res = {"ok": True, "type": "internal", "command": matched,
                           "args": " ".join(args), "ui": None}
                else:
                    # 3. Natural language: use the advanced handler (LLM intent parse)
                    res = handler.handle(text)
                ui_hint = res.get("ui")
                
                if res.get("type") == "chat":
                    display_chat_message("User", res.get("args", text))
                    debug_loop(res.get("args", text), on_turn=_timetravel_cb)
                    continue

                if res.get("type") == "internal":
                    cmd = res["command"]
                    args = res.get("args", "")
                    
                    prompt_name = None
                    if "@" in args:
                        parts = args.split("@", 1)
                        args = parts[0].strip()
                        prompt_name = parts[1].split()[0]
                    
                    if cmd == "/chat": chat(args or Prompt.ask("Question"), prompt=prompt_name)
                    elif cmd == "/fix": debug_loop(args or Prompt.ask("Issue to fix"), prompt=prompt_name, ui_hint=ui_hint)
                    elif cmd == "/plan": plan(args or Prompt.ask("Task for strategy"))
                    elif cmd == "/forge": forge(args or Prompt.ask("Task to forge"))
                    elif cmd == "/decode": decode(args or Prompt.ask("Content to decode"))
                    elif cmd == "/lookup": lookup(args or Prompt.ask("What to find?"))
                    elif cmd == "/hardware": hardware_menu()
                    elif cmd == "/voice":
                        if args:
                            set_voice_profile(args)
                        else:
                            voice()
                    elif cmd == "/watch": watch()
                    elif cmd == "/config": menus.config_menu()
                    elif cmd == "/init": init()
                    elif cmd == "/analyze": analyze(args or ".")
                    elif cmd == "/analyze-file": analyze_file(args or Prompt.ask("Path to file"), prompt=prompt_name)
                    elif cmd == "/locate": locate(args or Prompt.ask("Search name"))
                    elif cmd == "/network": menus.network_menu()
                    elif cmd == "/ssh": menus.ssh_command(args)
                    elif cmd == "/server": menus.server_menu()
                    elif cmd == "/undo": undo(args or Prompt.ask("Path to undo"))
                    elif cmd == "/dashboard": dashboard()
                    elif cmd == "/gui":
                        from core.desktop import launch_gui
                        launch_gui()
                    elif cmd == "/overlay":
                        from core.desktop import launch_overlay
                        launch_overlay()
                    elif cmd == "/tray":
                        from core.tray import run_tray
                        run_tray()
                    elif cmd == "/voice-wake":
                        voice_wake(args or None)
                    elif cmd == "/memory": menus.memory_menu()
                    elif cmd == "/memories":
                        from core import memory_cores as _mc
                        parts = args.split()
                        sub = parts[0] if parts else "menu"
                        if sub == "menu":
                            _mc.memory_cores_menu()
                        elif sub in ("show", "add", "forget", "export", "stats"):
                            core = parts[1] if len(parts) > 1 and parts[1] in _mc.CORES else "facts"
                            rest = " ".join(parts[2:] if len(parts) > 1 and parts[1] in _mc.CORES else parts[1:])
                            if sub == "show":
                                for e in _mc.recall(core, rest):
                                    console.print(f"[dim]{e['id']}[/dim] {e['text']}")
                            elif sub == "stats":
                                console.print(_mc.cores_table())
                            elif sub == "add":
                                res = _mc.remember(core, rest or Prompt.ask("Memory text"))
                                console.print(f"[green]✅ Stored.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
                            elif sub == "forget":
                                res = _mc.forget(core, rest or Prompt.ask("Entry id or text"))
                                console.print(f"[green]✅ Removed.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
                            elif sub == "export":
                                res = _mc.export_core(core)
                                console.print(res.get("json", ""))
                        else:
                            console.print("[red]Usage: /memories [menu|show|add|forget|export|stats] [core] [text][/red]")
                    elif cmd == "/personality":
                        if args:
                            set_personality(args)
                        else:
                            menus.personality_menu()
                    elif cmd == "/voice-profile":
                        if args:
                            set_voice_profile(args)
                        else:
                            voice_profile_menu()
                    elif cmd == "/models": menus.models_menu()
                    elif cmd == "/multibrain": multibrain(args or Prompt.ask("Task for multi-brain reasoning"))
                    elif cmd == "/hive":
                        from core.hive import hive_ask, display_hive_result
                        display_hive_result(hive_ask(args or Prompt.ask("Question for the hive")))
                    elif cmd == "/swarm":
                        from core.swarm import run_swarm, display_swarm_result
                        display_swarm_result(run_swarm(args or Prompt.ask("Task for the swarm")))
                    elif cmd == "/mcp":
                        from core.mcp_client import handle_mcp_command
                        handle_mcp_command(args or "")
                    elif cmd == "/research":
                        from core.research import run_research, display_research_result
                        display_research_result(run_research(args or Prompt.ask("Research topic")))
                    elif cmd == "/council":
                        from core.council import handle_council_command
                        handle_council_command(args or Prompt.ask("Question for the council"))
                    elif cmd == "/code":
                        from core.codeops import handle_code_command
                        handle_code_command(args or Prompt.ask("Coding task"))
                    elif cmd == "/suggest":
                        from core.codeops import handle_suggest_command
                        handle_suggest_command(args or Prompt.ask("Task to suggest a command for"))
                    elif cmd == "/explain":
                        from core.codeops import handle_explain_command
                        handle_explain_command(args or Prompt.ask("Command to explain"))
                    elif cmd == "/github":
                        from tools.github import handle_github_command
                        handle_github_command(args)
                    elif cmd == "/api":
                        api_server()
                    elif cmd == "/scan-ollama": scan_ollama()
                    elif cmd == "/ollama": ollama_cli(args)
                    elif cmd in ("/rewind", "/branch", "/branches", "/diff"):
                        from core.timetravel import handle_timetravel
                        handle_timetravel(cmd, args)
                    elif cmd == "/skill":
                        from core.skillshare import handle_skill_command
                        handle_skill_command(args)
                    elif cmd == "/brief":
                        from core.briefs import handle_brief
                        handle_brief(args)
                    elif cmd == "/handoff":
                        from core.handoff import handle_handoff_command
                        handle_handoff_command(args)
                    elif cmd == "/schedule":
                        from core.scheduler import handle_schedule
                        handle_schedule(args)
                    elif cmd == "/thin":
                        from tools.ollama_thin import main as thin_main
                        thin_main()
                    elif cmd == "/refine":
                        target = args.split()[0] if args else Prompt.ask("File to refine")
                        test = " ".join(args.split()[1:]) if len(args.split()) > 1 else Prompt.ask("Test command")
                        from core.refinement import refine_loop
                        refine_loop(target, test)
                    elif cmd == "/stress-test":
                        from core.validator import Validator
                        v = Validator()
                        v.run_tests()
                    elif cmd == "/ollama-login": ollama_login()
                    elif cmd == "/p2p-scan": p2p_scan()
                    elif cmd == "/p2p-status": p2p_status()
                    elif cmd == "/p2p-edit":

                        p_ip = args.split()[0] if args else Prompt.ask("Peer IP")
                        p_path = args.split()[1] if args and len(args.split()) > 1 else Prompt.ask("File Path")
                        p_content = " ".join(args.split()[2:]) if args and len(args.split()) > 2 else Prompt.ask("Content")
                        p2p_edit(p_ip, p_path, p_content)
                    elif cmd == "/p2p-read":
                        p_ip = args.split()[0] if args else Prompt.ask("Peer IP")
                        p_path = args.split()[1] if args and len(args.split()) > 1 else Prompt.ask("File Path")
                        p2p_read(p_ip, p_path)
                    elif cmd == "/p2p-server": p2p_server()
                    elif cmd == "/p2p-tokens": p2p_tokens()
                    elif cmd == "/prompts": menus.prompts_menu()
                    elif cmd == "/prompt":
                        from core import prompts as _pr
                        parts = args.split()
                        sub = parts[0] if parts else "list"
                        if sub == "list":
                            console.print(_pr.list_prompts())
                        elif sub == "save" and len(parts) >= 2:
                            text = " ".join(parts[2:]) or Prompt.ask("System prompt text")
                            console.print(f"[green]✅ {_pr.save_prompt(parts[1], text)}[/green]")
                        elif sub == "apply" and len(parts) >= 2:
                            ok, msg = _pr.apply_prompt(parts[1])
                            console.print(f"[green]✅ {msg}[/green]" if ok else f"[red]❌ {msg}[/red]")
                        elif sub == "delete" and len(parts) >= 2:
                            console.print(f"[green]✅ {_pr.delete_prompt(parts[1])}[/green]")
                        elif sub == "show":
                            console.print(_pr.load_prompts().get(parts[1] if len(parts) > 1 else _pr.get_active_prompt_name(), "Not found."))
                        elif sub == "override" and len(parts) >= 3:
                            text = " ".join(parts[3:]) or Prompt.ask("Override text (empty clears)", default="")
                            console.print(f"[green]✅ {_pr.set_personality_override(parts[1], parts[2], text)}[/green]")
                        else:
                            console.print("[red]Usage: /prompt [list|save <n> [text]|apply <n>|delete <n>|show [n]|override <personality> <n> [text]][/red]")
                    elif cmd == "/settings":
                        from core.settings import settings_menu
                        settings_menu()
                    elif cmd == "/profile":
                        from core import profiles as _pf
                        parts = args.split()
                        if not parts:
                            _pf.profiles_menu()
                        elif parts[0] == "switch" and len(parts) > 1:
                            res = _pf.switch_profile(parts[1])
                            console.print(f"[green]✅ Now using profile '{parts[1]}'.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
                        elif parts[0] == "list":
                            console.print(_pf.profiles_table())
                        else:
                            console.print("[red]Usage: /profile [list|switch <name>] (empty opens the menu)[/red]")
                    elif cmd == "/project":
                        from core import projects as _pj
                        parts = args.split()
                        if not parts:
                            _pj.projects_menu()
                        elif parts[0] == "new" and len(parts) > 1:
                            res = _pj.create_project(" ".join(parts[1:]))
                            console.print(f"[green]✅ Project created (slug: {res['slug']}).[/green]")
                        elif parts[0] == "open" and len(parts) > 1:
                            res = _pj.open_project(parts[1])
                            console.print(f"[green]✅ Project '{parts[1]}' active.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
                        elif parts[0] == "close":
                            _pj.close_project(); console.print("[green]✅ No active project.[/green]")
                        elif parts[0] == "list":
                            console.print(_pj.projects_table())
                        elif parts[0] == "add" and len(parts) > 1:
                            slug = _pj.get_active_project()
                            res = _pj.add_attachment(slug, " ".join(parts[1:])) if slug else {"ok": False, "error": "No active project."}
                            console.print(f"[green]✅ Attached.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
                        else:
                            console.print("[red]Usage: /project [list|new <name>|open <slug>|close|add <path>] (empty opens the menu)[/red]")
                    elif cmd == "/sim":
                        from core import sims as _sm
                        parts = args.split()
                        if not parts:
                            _sm.sims_menu()
                        elif parts[0] == "list":
                            console.print(_sm.sims_table())
                        elif parts[0] == "chat" and len(parts) > 1:
                            from rich.prompt import Prompt as _Prompt
                            sname = parts[1]; history = []
                            console.print("[dim]Chatting — empty message exits.[/dim]")
                            while True:
                                msg = _Prompt.ask("you").strip()
                                if not msg: break
                                history.append({"role": "user", "text": msg})
                                res = _sm.sim_chat(sname, msg, history)
                                if not res["ok"]:
                                    console.print(f"[red]❌ {res['error']}[/red]"); break
                                console.print(f"[magenta]{res['avatar']} {res['sim']}:[/magenta] {res['reply']}")
                                history.append({"role": "assistant", "text": str(res["reply"])})
                        elif parts[0] == "new":
                            _sm.sims_menu()
                        else:
                            console.print("[red]Usage: /sim [list|chat <name>|new] (empty opens the menu)[/red]")
                    elif cmd == "/connector":
                        from core import connectors as _cx
                        parts = args.split()
                        if not parts:
                            _cx.connectors_menu()
                        elif parts[0] in ("list", "status"):
                            for s in _cx.connector_status_all():
                                mark = "🟢" if s["connected"] else "⚪"
                                console.print(f"{mark} [cyan]{s['id']}[/cyan] — {s['display']}: {s['account'] or s['needs']}")
                        elif parts[0] == "connect" and len(parts) > 1:
                            try:
                                res = _cx.get_connector(parts[1]).connect()
                                console.print(f"[green]✅ Connected.[/green]" if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
                            except KeyError as e:
                                console.print(f"[red]❌ {e}[/red]")
                        elif parts[0] == "disconnect" and len(parts) > 1:
                            try:
                                res = _cx.get_connector(parts[1]).disconnect()
                                console.print(f"[green]✅ {res.get('message', '')}[/green]" if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
                            except KeyError as e:
                                console.print(f"[red]❌ {e}[/red]")
                        else:
                            console.print("[red]Usage: /connector [list|status|connect <id>|disconnect <id>] (empty opens the menu)[/red]")
                    elif cmd == "/signin":
                        from core import signin as _si
                        parts = args.split()
                        if not parts:
                            _si.signin_menu()
                        elif parts[0] == "in" and len(parts) > 1:
                            res = _si.signin(parts[1])
                            console.print(f"[green]✅ Signed in.[/green]" if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
                        elif parts[0] == "out" and len(parts) > 1:
                            res = _si.signout(parts[1])
                            console.print(f"[green]✅ {res.get('message', '')}[/green]" if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
                        elif parts[0] == "status":
                            for s in _si.signin_status():
                                mark = "🟢" if s["signed_in"] else "⚪"
                                console.print(f"{mark} {s['display']}: {s['account'] or s['needs'] or 'not configured'}")
                        else:
                            console.print("[red]Usage: /signin [in|out <provider>|status] (empty opens the menu)[/red]")
                    elif cmd == "/account":
                        from core import jade_account as _ca
                        _ca.account_menu()
                    elif cmd == "/cloud": menus.cloud_menu()
                    elif cmd == "/connect": menus.connect_menu()
                    elif cmd == "/connections": connections()
                    elif cmd == "/google-login":
                        from core.google_auth import GoogleAuth
                        GoogleAuth.get_credentials()
                    elif cmd == "/google-sync":
                        from core.google_auth import GoogleAuth
                        GoogleAuth.auto_register_flow()
                    elif cmd == "/google-register":
                        from core.google_auth import GoogleAuth
                        GoogleAuth.auto_register_flow()
                    elif cmd == "/google-connect":
                        from core.google_auth import GoogleAuth
                        GoogleAuth.run_flow()
                    elif cmd == "/webask":
                        webask(args or Prompt.ask("Search query"))
                    elif cmd == "/menu": menu()
                    elif cmd == "/reinstall": reinstall()
                    elif cmd == "/model": models_command()
                    elif cmd == "/brain": menus.models_menu()
                    elif cmd == "/launch":
                        from tools.launcher import TOOL_REGISTRY
                        t = args or Prompt.ask("AI tool", choices=list(TOOL_REGISTRY.keys()))
                        launch(tool=t)
                    elif cmd == "/focus": focus(args or Prompt.ask("Path"))
                    elif cmd in ["/troubleshoot", "/t"]: troubleshoot(args or Prompt.ask("Command"), prompt=prompt_name)
                    elif cmd == "/free": show_free_providers()
                    elif cmd == "/doctor": run_doctor()
                    elif cmd == "/git": ai_git(args or Prompt.ask("Git task?"))
                    elif cmd == "/restart": restart()
                    elif cmd == "/search":
                        q = args or Prompt.ask("Search history")
                        results = session.history.get_strings()
                        matches = [s for s in results if q.lower() in s.lower()]
                        console.print(Panel("\n".join(matches[-10:]), title=f"History: {q}"))
                    elif cmd == "/clear": console.clear()
                    elif cmd == "/help": menus.robust_help()
                    elif cmd == "/clippy": clippy()
                    elif cmd == "/health": display_health_report(check_system_health())
                    elif cmd in ["/upgrade", "/update"]:
                        from core.update import manual_upgrade
                        manual_upgrade()
                    elif cmd == "/sync": update_all_repos()
                    elif cmd == "/box":
                        box_menu(args)
                    elif cmd == "/examine-py":
                        examine_py(args or Prompt.ask("Path to Python file"))
                    elif cmd == "/patch-py":
                        patch_py(args or Prompt.ask("Path to Python file"))
                    elif cmd == "/nave":
                        from core.nave_loop import run_nave_loop
                        res = run_nave_loop(args or Prompt.ask("Task for NAVE"))
                        if res.get("ok"):
                            console.print(Markdown(res.get("final_answer", "")))
                            if confirm("Show reasoning summary?"):
                                console.print(Panel(res.get("integrator_json", {}).get("reasoning_summary", "No summary available."), title="Reasoning", border_style="dim"))
                        else:
                            pass # run_nave_loop handled the panel
                    elif cmd == "/copilot":
                        copilot(args or Prompt.ask("Query"))
                    else: console.print(f"[red]Routing error for: {cmd}[/red]")
                
                elif res.get("type") == "shell":
                    cmd = res["command"]
                    if res.get("confirm", True):
                        console.print(Panel(f"[bold red]⚠️ EXECUTE SHELL COMMAND?[/bold red]\n\n[white]{cmd}[/white]", border_style="red"))
                        if confirm("Authorize?"):
                            from tools.shell import run_simple
                            console.print(run_simple(cmd))
                    else:
                        from tools.shell import run_simple
                        console.print(run_simple(cmd))
                
                else:
                    display_chat_message("User", res.get("args", text))
                    debug_loop(res.get("args", text), on_turn=_timetravel_cb)
            except EOFError:
                # Input stream closed in the middle of a command (piped stdin
                # exhausted, terminal closed): exit the REPL the same clean way
                # as EOF at the prompt instead of reporting a "System Error".
                break
            except KeyboardInterrupt:
                # Ctrl+C during a command cancels back to the prompt; the
                # double-press-to-exit logic at the prompt is unchanged.
                console.print("\n[yellow]Cancelled.[/yellow]")
                continue
            except Exception as e:
                entry = ErrorLogger.log_error(e, context=f"Command: {text}")
                console.print(f"[bold red]❌ System Error:[/bold red] {e}")
                if confirm("Initiate autonomous debug analysis?"):
                    ErrorLogger.auto_debug(f"Error: {e}\nStack: {entry['stack_trace']}")

        except KeyboardInterrupt:
            now = time.time()
            if now - last_ctrl_c < 2: 
                console.print("\n[yellow]Shutdown complete.[/yellow]"); break
            else:
                console.print("\n[bold red]Press Ctrl+C again to exit.[/bold red]")
                last_ctrl_c = now
        except EOFError: break

# (single callback defined above; duplicate removed)

def _timetravel_cb(role, text):
    """Record chat turns into the branchable conversation tree (best-effort)."""
    try:
        from core.timetravel import current_tree
        current_tree().note_turn(role, text)
    except Exception:
        pass


def ollama_cli(args: str):
    """Managed Ollama subcommands (/ollama ps|list|prune|stats|…) + raw passthrough."""
    # Jade-managed subcommands first; anything else falls through to the
    # raw `ollama` binary below. `handle_ollama_args("")` shows managed help.
    from core.ollama_mgmt import handle_ollama_args
    if handle_ollama_args(args or ""):
        return
    import subprocess
    import os
    
    from core.config import load_config
    cfg = load_config()
    host = cfg.get("ollama_host", "http://localhost:11434")
    env = os.environ.copy()
    env["OLLAMA_HOST"] = host
    
    import shlex
    cmd = ["ollama"] + shlex.split(args)
    console.print(f"[dim]Running: {' '.join(cmd)} (Host: {host})[/dim]")
    try:
        subprocess.run(cmd, env=env, shell=False)
    except FileNotFoundError:
        console.print("[red]❌ 'ollama' executable not found in PATH.[/red]")
    except Exception as e:
        console.print(f"[red]❌ Error executing ollama: {e}[/red]")

@app.command()
def ollama(args: str = typer.Argument(None, help="Arguments to pass to the ollama CLI")):
    """Pass-through CLI for Ollama."""
    ollama_cli(args)

@app.command()
def ollama_login():
    """Sign in to Ollama Cloud to enable cloud models."""
    from core.utils import open_url
    from core.config import load_config, save_config
    
    console.print("[bold cyan]Ollama Cloud Login[/bold cyan]")
    console.print("Opening Ollama website. Please log in and find your API token in your account settings/dashboard.")
    open_url("https://ollama.com")

    token = Prompt.ask("Enter your Ollama account token", password=True)
    if token:
        cfg = load_config()
        cfg["ollama_token"] = token
        cfg["ollama_cloud_host"] = "https://ollama.com/api"
        save_config(cfg)
        console.print("[bold green]✅ Ollama Cloud configured successfully![/bold green]")
        console.print("You can now use cloud models by adding '-cloud' to the model name.")

@app.command()
def setup(): setup_wizard()

@app.command()
def p2p_scan():
    """Scan local network for other JADE instances."""
    from core.p2p import scan_for_jade_peers
    console.print("[cyan]Searching for JADE peers on local network...[/cyan]")
    peers = scan_for_jade_peers()
    if not peers:
        console.print("[yellow]No other JADE instances found.[/yellow]")
    else:
        console.print(f"[green]Found JADE peers at: {', '.join(peers)}[/green]")

@app.command()
def p2p_status():
    """Scan and display a detailed report of all JADE peers on the network."""
    from core.p2p import p2p_status_report
    p2p_status_report()

@app.command()
def p2p_edit(peer_ip: str, path: str, old_string: str, new_string: str):
    """Request to edit a file on a remote JADE instance."""
    from core.p2p import send_remote_command
    console.print(f"[cyan]Requesting to edit '{path}' on {peer_ip}...[/cyan]")
    res = send_remote_command(peer_ip, "edit_file", {"path": path, "old_string": old_string, "new_string": new_string})
    if res["ok"]:
        console.print(f"[green]✅ {res['data']}[/green]")
    else:
        console.print(f"[red]❌ {res['error']}[/red]")

@app.command()
def p2p_set_token(token: str):
    """Set the P2P shared secret token for swarm trust."""
    from core.config import load_config, save_config
    cfg = load_config()
    cfg["p2p_token"] = token
    save_config(cfg)
    console.print(f"[green]✅ P2P Token set successfully.[/green]")

@app.command()
def p2p_read(peer_ip: str, path: str):
    """Request to read a file from a remote JADE instance."""
    from core.p2p import send_remote_command
    console.print(f"[cyan]Requesting to read '{path}' from {peer_ip}...[/cyan]")
    res = send_remote_command(peer_ip, "read_file", {"path": path})
    if res["ok"]:
        console.print(Panel(res["data"], title=f"File: {path} (from {peer_ip})"))
    else:
        console.print(f"[red]❌ {res['error']}[/red]")

@app.command()
def p2p_server():
    """Start the P2P server to allow remote requests."""
    from core.p2p import run_p2p_server
    run_p2p_server()

@app.command()
def p2p_tokens():
    """Interactive menu for discovering and requesting tokens from peers."""
    from core.p2p import p2p_token_menu
    p2p_token_menu()

@app.command()
def p2p_discovery(port: int = typer.Option(11437, "--port", help="TCP port to listen on"),
                  ttl: int = typer.Option(90, "--ttl", help="Peer registration TTL in seconds"),
                  secret: str = typer.Option("", "--secret", help="Optional shared secret required of registering nodes"),
                  persist: str = typer.Option("", "--persist", help="Optional JSON file to persist the peer registry")):
    """Run the self-hosted P2P discovery (rendezvous) server for global P2P."""
    from core.p2p_discovery_server import run_discovery_server
    run_discovery_server(port=port, ttl=ttl, secret=secret or None,
                         persist_path=persist or None)

@app.command()
def p2p_pair():
    """Print a pairing code so another Jade on your LAN can join you."""
    from core.p2p_pairing import make_pairing_code, render_pairing_qr
    code, info = make_pairing_code()
    if info["token_generated"]:
        console.print("[yellow]No P2P token was set — generated a fresh one for pairing.[/yellow]")
    console.print(f"\n[bold cyan]Pairing code for '{info['name']}'[/bold cyan] "
                  f"(token fingerprint [dim]{info['token_fingerprint']}[/dim], LAN endpoint {info['endpoint']}):")
    console.print(f"\n[bold green]{code}[/bold green]\n")
    console.print("On the other machine, run: [bold]jade p2p-join <code>[/bold]")
    qr = render_pairing_qr(code)
    if qr:
        console.print("[dim]Or scan:[/dim]")
        console.print(qr)
    else:
        console.print("[dim](Install the 'qrcode' package to also show a scannable QR code.)[/dim]")

@app.command()
def p2p_join(code: str = typer.Argument(..., help="Pairing code from 'jade p2p-pair'"),
             yes: bool = typer.Option(False, "--yes", help="Replace existing P2P token without asking")):
    """Join a peer using its pairing code (LAN discovery; see p2p-pair)."""
    from core.p2p_pairing import join_pairing
    res = join_pairing(code, assume_yes=yes)
    if res["ok"]:
        console.print(f"[green]✅ Paired with '{res['peer']}' at {res['ip']}:{res['port']} "
                      f"(token fingerprint {res['token_fingerprint']}).[/green]")
    else:
        console.print(f"[red]❌ {res['error']}[/red]")
        if res.get("hint"):
            console.print(f"[yellow]{res['hint']}[/yellow]")

@app.command()
def p2p_message(peer_ip: str = typer.Argument(..., help="Peer IP address"),
                text: str = typer.Argument(..., help="Message text"),
                port: int = typer.Option(11435, "--port", help="Peer P2P port")):
    """Send a direct text message to a peer's inbox."""
    from core.p2p import p2p_send_message
    res = p2p_send_message(peer_ip, text, port=port)
    if res["ok"]:
        console.print("[green]✅ Message delivered.[/green]")
    else:
        console.print(f"[red]❌ {res.get('error', res)}[/red]")

@app.command()
def p2p_inbox(clear: bool = typer.Option(False, "--clear", help="Empty the inbox after listing"),
              limit: int = typer.Option(20, "--limit", help="How many messages to show")):
    """List messages peers have sent to this node's inbox."""
    from core.p2p import p2p_inbox as read_inbox, p2p_inbox_clear
    import datetime
    msgs = read_inbox(limit=limit)
    if not msgs:
        console.print("[dim]Inbox is empty.[/dim]")
    else:
        for m in msgs:
            ts = datetime.datetime.fromtimestamp(m.get("timestamp", 0)).strftime("%Y-%m-%d %H:%M:%S")
            console.print(f"[cyan]{ts}[/cyan] [magenta]{m.get('from_ip')}[/magenta] "
                          f"[dim]({str(m.get('from_node_id'))[:8]}…)[/dim]: {m.get('text')}")
    if clear:
        n = p2p_inbox_clear()
        console.print(f"[yellow]Cleared {n} message(s).[/yellow]")

@app.command(name="p2p-peers")
def p2p_peers_cmd():
    """Show merged LAN + global peer presence (deduped by node id)."""
    from core.p2p import p2p_peers
    from rich.table import Table
    peers = p2p_peers()
    if not peers:
        console.print("[yellow]No peers seen. (Start 'jade p2p-server' on another machine, or configure a discovery server.)[/yellow]")
        return
    table = Table(title="JADE P2P Presence", border_style="cyan")
    table.add_column("Node"); table.add_column("Name"); table.add_column("Version")
    table.add_column("Sources"); table.add_column("Endpoint"); table.add_column("Last seen")
    import datetime
    for p in peers:
        if "lan" in p["sources"] and p["lan_endpoint"]:
            ep = f"{p['lan_endpoint']['ip']}:{p['lan_endpoint']['port']}"
        elif p["global_endpoint"]:
            ep = f"{p['global_endpoint']['host']}:{p['global_endpoint']['port']}"
        else:
            ep = "?"
        ls = p["last_seen"]
        ls_s = datetime.datetime.fromtimestamp(ls).strftime("%H:%M:%S") if ls else "—"
        table.add_row(p["node_id"][:12], p["name"], p["version"],
                      ",".join(p["sources"]), ep, ls_s)
    console.print(table)

@app.command(name="p2p-sync-memory")
def p2p_sync_memory_cmd(peer_ip: str = typer.Argument(..., help="Peer IP address"),
                        core: str = typer.Argument(..., help="Memory core: facts, preferences, projects, episodic"),
                        direction: str = typer.Argument(..., help="'push' (send mine) or 'pull' (fetch theirs)"),
                        port: int = typer.Option(11435, "--port", help="Peer P2P port")):
    """Sync a memory core with one of your own devices over P2P.

    Both nodes need "p2p_memory_sync": true in config, and the receiving
    node must grant the sender the "sync_memory" permission. A peer with
    that permission can read AND write your memory cores — only use this
    between machines you fully trust.
    """
    from core.p2p import p2p_push_memory, p2p_pull_memory
    if direction == "push":
        res = p2p_push_memory(peer_ip, core, port=port)
    elif direction == "pull":
        res = p2p_pull_memory(peer_ip, core, port=port)
    else:
        console.print("[red]direction must be 'push' or 'pull'[/red]")
        return
    if res["ok"]:
        console.print(f"[green]✅ Memory sync {direction} complete: {res}[/green]")
    else:
        console.print(f"[red]❌ {res.get('error', res)}[/red]")

@app.command()
def restart():
    console.print("[yellow]🔄 Restarting JADE...[/yellow]")
    os.execv(sys.executable, ['python3'] + sys.argv)

@app.command()
def reinstall():
    """Perform a clean sovereign reinstallation of the JADE ecosystem."""
    if confirm("[bold red]⚠️ DANGER: This will wipe your local installation and start fresh. Proceed?[/bold red]"):
        console.print("[bold yellow]🚀 Initiating Sovereign Reinstallation...[/bold yellow]")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        os.system(f"cd {base_dir} && bash install.sh")
        console.print("[bold green]✅ Reinstallation complete. Please restart JADE.[/bold green]")
        sys.exit(0)

@app.command()
def chat(q: str, 
         model: Annotated[Optional[str], typer.Option("--model", "-m")] = None, 
         prompt: Annotated[Optional[str], typer.Option("--prompt", "-p")] = None):
    display_chat_message("User", q)
    res = think_structured("", q, model=model, prompt_name=prompt)
    txt = process_think_res(res)
    if txt: console.print(Markdown(txt))

@app.command()
def fix(issue: str, model: Annotated[Optional[str], typer.Option("--model", "-m")] = None, prompt: Annotated[Optional[str], typer.Option("--prompt", "-p")] = None):
    display_chat_message("Fix Request", issue)
    debug_loop(issue, model=model, prompt=prompt)

@app.command()
def plan(task: str, model: Annotated[Optional[str], typer.Option("--model", "-m")] = None):
    from core.planning import save_plan, display_plan
    from core.agent import generate_plan
    
    display_chat_message("Strategy Phase", task)
    res = generate_plan(task, model=model)
    txt = process_think_res(res)
    
    if txt:
        plan_path = save_plan("active_plan", txt)
        display_plan("active_plan")
        
        if confirm("[bold yellow]Accept this plan and transition to Execution Mode?[/bold yellow]"):
            console.print("[green]✅ Plan approved. Execution initiated.[/green]")
            # Proceed to execution (forge loop or similar)
        else:
            console.print("[yellow]Plan rejected. Staying in Plan Mode.[/yellow]")
            # Logic to keep the CLI in a "Plan" sub-prompt loop would go here


@app.command()
def forge(task: str, model: Annotated[Optional[str], typer.Option("--model", "-m")] = None):
    display_chat_message("Forge Task", task)
    forge_loop(task, model=model)

@app.command()
def decode(content: str):
    res = think_structured("", f"Decode and explain with maximum depth: {content}")
    txt = process_think_res(res)
    if txt: console.print(Markdown(txt))

@app.command()
def lookup(request: str):
    res = think_structured("", f"Suggest best command for: {request}")
    txt = process_think_res(res)
    if txt: console.print(Panel(Markdown(txt), title="Lookup"))

@app.command()
def hardware_menu():
    from tools.hardware import get_hardware_summary, list_usb_devices, probe_ports
    while True:
        console.print(get_hardware_summary())
        console.print("\n[1] USB | [2] All Ports | [b] Back")
        c = Prompt.ask("Choice", choices=["1", "2", "b"], default="b")
        if c == "1": console.print(Panel(list_usb_devices()))
        elif c == "2": console.print(Panel(probe_ports()))
        else: break

@app.command()
def analyze_file(path: str, model: Annotated[Optional[str], typer.Option("--model", "-m")] = None, prompt: Annotated[Optional[str], typer.Option("--prompt", "-p")] = None):
    if not os.path.exists(path): console.print(f"[red]Error: Not found:[/red] {path}"); return
    with open(path, 'r') as f: content = f.read()
    console.print(f"[bold cyan]Auditing:[/bold cyan] {path}...")
    res = think_structured(f"Path: {path}\nContent:\n{content}", "Deep security and performance audit.", model=model, prompt_name=prompt)
    txt = process_think_res(res)
    if txt: console.print(Markdown(txt))

@app.command()
def locate(name: str, root: str = "/"):
    from tools.search import system_find
    console.print(f"[bold cyan]Searching for '{name}'...[/bold cyan]")
    console.print(Panel(system_find(name, root), title="Locate"))

@app.command()
def launch(tool: str):
    from tools.launcher import launch_tool
    console.print(f"[bold cyan]Launching:[/bold cyan] {tool}")
    console.print(f"[green]{launch_tool(tool)}[/green]")

def show_free_providers():
    """Show providers usable without an API key (local models) and their status."""
    from core.services import validate_ollama
    from core.config import load_config
    cfg = load_config()
    host = cfg.get("ollama_host", "http://localhost:11434")
    table = Table(title="Key-free providers", border_style="green")
    table.add_column("Provider", style="cyan")
    table.add_column("Status", style="white")
    # Local providers that need no API key
    res = validate_ollama(host)
    ollama_ok = res.get("ok")
    table.add_row("ollama (local)", "[green]reachable[/green]" if ollama_ok else f"[red]not reachable at {host}[/red]")
    for name in ("llama_cpp", "gpt4all", "vllm", "sglang", "local"):
        table.add_row(name, "[dim]configure a local server, then select via /model[/dim]")
    console.print(table)
    console.print("[dim]Tip: use /model to switch providers, /doctor to repair Ollama.[/dim]")

@app.command()
def troubleshoot(command: str, model: Annotated[Optional[str], typer.Option("--model", "-m")] = None, prompt: Annotated[Optional[str], typer.Option("--prompt", "-p")] = None):
    troubleshoot_loop(command, model=model, prompt=prompt)

@app.command()
def undo(path: str):
    from tools.editor import undo_last_edit
    console.print(f"[green]{undo_last_edit(path)}[/green]")

@app.command()
def dashboard(port: int = typer.Option(0, "--port", help="Port to serve (0 = random free port)"),
              lan: bool = typer.Option(False, "--lan", help="Bind all interfaces (LAN). Prints a warning."),
              open_browser: bool = typer.Option(False, "--open", help="Open the dashboard in a browser")):
    """Local web dashboard: live status, chat, read-only commands, logs."""
    from core.dashboard import run_dashboard
    run_dashboard(port=port, bind_lan=lan, open_browser=open_browser)

@app.command(name="api")
def api_server(port: int = typer.Option(0, "--port", help="Port to serve (0 = random free port)"),
               lan: bool = typer.Option(False, "--lan", help="Bind all interfaces (LAN). Prints a warning.")):
    """OpenAI-compatible API server: point Copilot-style clients at Jade."""
    from core.apiserver import run_api_server
    server, thread, url, token = run_api_server(port=port, bind_lan=lan)
    console.print(f"[green]Serving OpenAI-compatible API at {url}[/green] — press Ctrl+C to stop.")
    console.print("[dim]Endpoints: POST /v1/chat/completions, GET /v1/models (Bearer token required)[/dim]")
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()

@app.command()
def focus(path: str):
    if os.path.exists(path): console.print(Panel(f"[green]Focus set:[/green] {os.path.abspath(path)}", border_style="green"))
    else: console.print(f"[red]Error: Missing path:[/red] {path}")

@app.command()
def voice():
    try:
        from voice.voice import run_voice
    except ImportError as e:
        console.print(f"[red]Voice support needs extra packages: {e}.[/red]")
        console.print("[dim]Install with: pip install sounddevice scipy SpeechRecognition[/dim]")
        return
    run_voice()


def set_personality(name: str):
    """Set the assistant personality directly (``/personality <name>``)."""
    from core.personalities import is_known_personality, list_personalities, resolve_personality
    from core.config import load_config, save_config
    if not is_known_personality(name):
        known = ", ".join(p.name for p in list_personalities())
        console.print(f"[red]Unknown personality '{name}'. Choose from: {known}[/red]")
        return
    config = load_config()
    config["personality"] = resolve_personality(name).name
    save_config(config)
    console.print(f"[green]✅ Personality set to {resolve_personality(name).title}. "
                  f"{resolve_personality(name).greeting}[/green]")


@app.command(name="personality")
def personality_cmd(name: str = typer.Argument(None, help="Personality name (jade, witty, clippy, professional). Omit for the picker.")):
    """Set or pick Jade's personality."""
    if name:
        set_personality(name)
    else:
        menus.personality_menu()


def voice_profile_menu():
    """Pick a TTS voice profile from the available list."""
    from core.voice.profiles import list_profiles
    from core.config import load_config, save_config
    profiles = list_profiles()
    config = load_config()
    current = config.get("voice_profile", "default")
    table_lines = [f"  [bold]{'→' if p.name == current else ' '} {p.name}[/bold] — {p.description}"
                   for p in profiles]
    console.print(Panel("\n".join(table_lines), title="Voice profiles", border_style="cyan"))
    choice = Prompt.ask("Voice profile", default=current)
    set_voice_profile(choice)


def set_voice_profile(name: str):
    """Set the TTS voice profile (``/voice <profile>``)."""
    from core.voice.profiles import get_profile, list_profiles
    from core.config import load_config, save_config
    profile = get_profile(name)
    if profile is None:
        known = ", ".join(p.name for p in list_profiles())
        console.print(f"[red]Unknown voice profile '{name}'. Available: {known}[/red]")
        return
    config = load_config()
    config["voice_profile"] = profile.name
    save_config(config)
    console.print(f"[green]✅ Voice profile set to '{profile.name}' ({profile.engine}).[/green]")
    if profile.note:
        console.print(f"[dim]{profile.note}[/dim]")


@app.command(name="voice-profile")
def voice_profile_cmd(name: str = typer.Argument(None, help="Voice profile name. Omit for the picker.")):
    """Set or pick Jade's TTS voice profile."""
    if name:
        set_voice_profile(name)
    else:
        voice_profile_menu()


@app.command()
def gui():
    """Launch the Jade desktop app (chat window + overlay + tray)."""
    from core.desktop import launch_gui
    launch_gui()


@app.command()
def overlay():
    """Launch the floating Jade overlay (summon with the global hotkey)."""
    from core.desktop import launch_overlay
    launch_overlay()


@app.command()
def tray():
    """Run the Jade system-tray / menu-bar / panel icon."""
    from core.tray import run_tray
    run_tray()


@app.command(name="voice-wake")
def voice_wake(state: str = typer.Argument(None, help="'on' to listen for 'Hey Jade', 'off' to disable.")):
    """Opt-in 'Hey Jade' wake-word listener (summons the overlay)."""
    from core.voice import wakeword as ww
    from core.config import load_config, save_config
    if state is None or state.strip().lower() not in ("on", "off"):
        cfg = load_config()
        cur = "on" if cfg.get("wakeword_enabled") else "off"
        console.print(f"[dim]Wake-word listening is {cur}. Usage: jade voice-wake [on|off][/dim]")
        console.print(f"[dim]{ww.WAKEWORD_NOTICE}[/dim]")
        return
    if state.strip().lower() == "off":
        cfg = load_config()
        cfg["wakeword_enabled"] = False
        save_config(cfg)
        console.print("[green]Wake-word listening disabled.[/green]")
        return
    if not ww.notice_acknowledged():
        console.print(f"[yellow]Privacy notice:[/yellow] {ww.WAKEWORD_NOTICE}")
        if not Confirm.ask("Enable 'Hey Jade' wake-word listening?", default=False):
            console.print("[dim]Wake-word listening stays off.[/dim]")
            return
        ww.acknowledge_notice()
    cfg = load_config()
    cfg["wakeword_enabled"] = True
    save_config(cfg)
    from core.desktop import launch_overlay
    ww.run_listener(on_wake=lambda: launch_overlay())

@app.command()
def watch():
    try:
        from watcher.monitor import start_monitor
    except ImportError as e:
        console.print(f"[red]Watcher needs the watchdog package: {e}.[/red]")
        console.print("[dim]Install with: pip install watchdog[/dim]")
        return
    start_monitor()

@app.command()
def config(): menus.config_menu()

@app.command()
def analyze(path: Annotated[str, typer.Argument(help="Path to analyze")] = "."):
    from tools.analytics import project_summary
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        if not os.path.exists(path):
            console.print(f"[bold red]Error:[/bold red] Path '[yellow]{path}[/yellow]' does not exist.")
            return
        s = project_summary(path)

    t = Table(title=f"Project Health: {path}", border_style="cyan")
    t.add_column("Metric", style="white"); t.add_column("Value", style="bold cyan")
    t.add_row("Total Files", str(s["total_files"])); t.add_row("Total Lines", str(s["total_lines"]))
    lang_str = ", ".join([f"{k}: {v}" for k, v in sorted(s["languages"].items(), key=lambda x: x[1], reverse=True)[:3]])
    t.add_row("Top Languages", lang_str)
    console.print(t)
    if s["hotspots"]:
        h_table = Table(title="Complexity Hotspots (Refactor Recommended)", border_style="red")
        h_table.add_column("Path", style="white"); h_table.add_column("Score", style="bold red")
        for h in sorted(s["hotspots"], key=lambda x: x["score"], reverse=True)[:5]:
            h_table.add_row(h["path"], str(h["score"]))
        console.print(h_table)

@app.command()
def run_doctor():
    from core.deps import ensure_all; ensure_all()
    display_health_report(check_system_health())

@app.command()
def ai_git(task: str):
    res = think_structured("", f"Git task: {task}")
    txt = process_think_res(res)
    if txt: console.print(Markdown(txt))

@app.command()
def copilot(query: str, action: Annotated[str, typer.Option("--action", "-a")] = "suggest"):
    from tools.copilot import copilot_suggest, copilot_explain
    if action == "suggest": res = copilot_suggest(query)
    else: res = copilot_explain(query)
    console.print(Panel(res, title=f"GitHub Copilot ({action.capitalize()})", border_style="cyan"))

@app.command()
def box_menu(args: str = ""):
    tokens = args.split()
    if not tokens:
        console.print("[bold cyan]Gemini Multi-Box Engine[/bold cyan]")
        boxes = gemini_session.list_boxes()
        if not boxes: console.print("[dim]No active boxes.[/dim]")
        else:
            t = Table(title="Active Boxes", border_style="cyan")
            t.add_column("Box Name"); t.add_column("Last CMD Status")
            for b in boxes:
                logs = gemini_session.show_box_output(b)
                status = f"[green]Success[/green]" if logs and logs[-1]["returncode"] == 0 else f"[red]Error ({logs[-1]['returncode']})[/red]" if logs else "[dim]Idle[/dim]"
                t.add_row(b, status)
            console.print(t)
        console.print("\nUsage: /box create [name] | /box run [name] [cmd] | /box tail [name]")
        return
    sub = tokens[0]
    if sub == "create" and len(tokens) > 1:
        name = tokens[1]
        if gemini_session.create_box(name): console.print(f"[green]Box '{name}' initialized.[/green]")
        else: console.print(f"[red]Box '{name}' already exists.[/red]")
    elif sub == "run" and len(tokens) > 2:
        name = tokens[1]; cmd = " ".join(tokens[2:])
        res = gemini_session.run_in_box(name, cmd)
        if res["ok"]: console.print(Panel(res["stdout"] or "[dim]No output[/dim]", title=f"Box '{name}' Output", border_style="green"))
        else:
            console.print(f"[red]Execution failed in box '{name}': {res.get('error')}[/red]")
            if res.get("stderr"): console.print(Panel(res["stderr"], title="Error Log", border_style="red"))
    elif sub == "tail" and len(tokens) > 1:
        name = tokens[1]; console.print(Panel(gemini_session.tail_box(name), title=f"Tail: {name}", border_style="dim"))
    elif sub == "close" and len(tokens) > 1:
        name = tokens[1]
        if gemini_session.close_box(name): console.print(f"[yellow]Box '{name}' closed.[/yellow]")

@app.command()
def examine_py(path: str):
    from tools.python_agent import file_summary
    if not os.path.exists(path):
        console.print(f"[red]Error: File not found: {path}[/red]"); return
    console.print(Panel(file_summary(path), title=f"Examination: {path}", border_style="cyan"))

@app.command()
def patch_py(path: str, instruction: str = ""):
    from tools.python_agent import suggest_patch_via_llm, safe_apply_new_content
    if not os.path.exists(path):
        console.print(f"[red]Error: File not found: {path}[/red]"); return
    if not instruction: instruction = Prompt.ask("Instruction for patch")
    with console.status(f"[bold yellow]Analyzing and generating patch for {path}..."):
        res = suggest_patch_via_llm(path, instruction)
    if not res["ok"]:
        console.print(f"[red]Patch Error: {res.get('error')}[/red]"); return
    console.print(Panel(res["unified_diff"], title="Suggested Patch (Unified Diff)", border_style="yellow"))
    if confirm("Apply this patch?"):
        ok, msg = safe_apply_new_content(path, res["suggested"])
        if ok: console.print(f"[bold green]{msg}[/bold green]")
        else: console.print(f"[bold red]Failed to apply: {msg}[/bold red]")
    else: console.print("[yellow]Patch aborted by user.[/yellow]")

@app.command()
def init():
    if os.path.exists("JARVIS.md"): console.print("[yellow]Exists.[/yellow]")
    else:
        with open("JARVIS.md", "w") as f: f.write("# JADE Rules")
        console.print("[green]Created.[/green]")

def show_model_status():
    p_obj = get_provider(); p_name = get_env_with_config("provider") or "ollama"
    console.print(Panel(f"Brain: {p_name.upper()}\nModel: {p_obj.model}", title="Status", border_style="magenta"))

@app.command()
def brain():
    """Switch LLM brain (provider) via interactive menu."""
    menus.models_menu()

@app.command()
def model(name: Optional[str] = None):
    """Switch LLM model for the current brain."""
    models_command(name)

def models_command(name: Optional[str] = None):
    config = load_config()
    provider = config.get("provider", "ollama")
    tier = config.get("machine_tier", "medium")
    
    # Advanced Model & Tool Mapping
    # Providers/models here must have real support in core.services.
    model_map = {
        "ollama": ["llama3.3", "llama3.2", "phi4", "qwen3", "deepseek-r1"],
        "openai": ["gpt-4o", "gpt-4o-mini", "o1-preview", "o1-mini"],
        "anthropic": ["claude-3-5-sonnet-20241022", "claude-3-5-sonnet-20240620",
                      "claude-3-opus-20240229"],
        "gemini": ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
        "mistral": ["mistral-large-latest", "mistral-medium-latest"],
        "deepseek": ["deepseek-chat", "deepseek-reasoner"],
        "groq": ["llama-3.3-70b-versatile"],
        "together": ["meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-72B-Instruct"],
        "cohere": ["command-r-plus", "command-r"],
        "perplexity": ["llama-3.1-sonar-large-128k-online"],
        "qwen": ["qwen3", "qwen2.5"],
        "gpt4all": ["default"],
        "llama_cpp": ["local-model"],
        "vllm": ["meta-llama/Meta-Llama-3-70B-Instruct", "mistralai/Mixtral-8x7B-Instruct-v0.1"],
        "sglang": ["meta-llama/Meta-Llama-3-8B-Instruct"],
        "nemotron": ["default"],
        "local": ["default"],
    }

    # Must match tools.launcher.TOOL_REGISTRY — entries here launch for real.
    ai_tools = {
        "claude-desktop": "Anthropic's official desktop client",
        "claude": "Anthropic's Claude Code (with subagents)",
        "hermes": "Nous Research Hermes Agent",
        "openclaw": "OpenClaw Personal AI",
        "opencode": "OpenCode terminal agent",
        "copilot": "GitHub Copilot CLI",
        "aider": "High-speed CLI pair programming agent",
        "droid": "Factory's coding agent",
        "pi": "Minimal AI agent toolkit",
        "pool": "Poolside's coding agent",
        "codex": "OpenAI's open-source coding agent",
        "interpreter": "Open Interpreter",
        "gpt-engineer": "GPT Engineer",
        "mentat": "Mentat coding assistant",
    }

    if name:
        if name in ai_tools:
            launch(tool=name)
            return
        
        # Support for -cloud suffix logic
        config["jade_model"] = name
        save_config(config)
        
        if name.endswith("-cloud"):
            console.print(f"[green]✅ Model switched to Cloud: {name}[/green]")
            console.print("[dim]Requests will be routed to https://ollama.com/api with your bearer token.[/dim]")
        else:
            console.print(f"[green]✅ Model switched to: {name}[/green]")
        return

    # Generate Status Table (shared builder — same as /models menu)
    from core.connect import is_configured
    from core.services import KNOWN_PROVIDERS
    from core.ui import build_provider_status_table
    entries = [(p.upper(), is_configured(p)) for p in KNOWN_PROVIDERS]
    console.print(build_provider_status_table(entries))

    console.print("\n[bold cyan]Intelligence Control Center[/bold cyan]")
    console.print("[1] Switch Models (Current Provider)")
    console.print("[2] Launch AI Tools (Specialized Agents)")
    console.print("[b] Back")

    top_choice = Prompt.ask("Select category", choices=["1", "2", "b"], default="1")
    
    if top_choice == "1":
        _provider_key = provider.lower()
        if _provider_key == "claude":
            _provider_key = "anthropic"  # legacy config value
        options = model_map.get(_provider_key, ["llama3"])
        
        # Hardware Tiering Filter
        if tier == "low" and provider == "ollama":
            console.print("[yellow]⚠️ Low-end hardware detected. Heavy local models hidden.[/yellow]")
            options = [m for m in options if "70b" not in m and "dbrx" not in m]

        console.print(f"\n[bold white]Models for {provider.upper()} ({tier.upper()} TIER):[/bold white]")
        for i, m in enumerate(options):
            console.print(f"[{i+1}] {m}")
        
        choice = Prompt.ask("Choice", choices=[str(i+1) for i in range(len(options))] + ["b"], default="1")
        if choice != "b":
            selected = options[int(choice)-1]
            config["jade_model"] = selected
            save_config(config)
            console.print(f"[green]✅ Now using {selected}[/green]")
            
    elif top_choice == "2":
        t_list = list(ai_tools.keys())
        console.print("\n[bold white]Specialized AI Tools:[/bold white]")
        for i, t in enumerate(t_list):
            console.print(f"[{i+1}] {t} - [dim]{ai_tools[t]}[/dim]")
        
        choice = Prompt.ask("Choice", choices=[str(i+1) for i in range(len(t_list))] + ["b"], default="1")
        if choice != "b":
            selected = t_list[int(choice)-1]
            launch(tool=selected)

@app.command()
def update():
    from core.update import manual_upgrade; manual_upgrade()

@app.command()
def upgrade():
    from core.update import manual_upgrade; manual_upgrade()

@app.command()
def sync():
    from core.health import update_all_repos; update_all_repos()

@app.command()
def repair_ollama_cmd(host: Optional[str] = None):
    """Attempt to repair or reconfigure Ollama automatically."""
    console.print("[bold cyan]Repairing Ollama...[/bold cyan]")
    report = repair_ollama(host=host)
    t = Table(title="Ollama Repair Report")
    t.add_column("Key"); t.add_column("Value")
    t.add_row("Fixed", str(report.get("fixed")))
    t.add_row("Details", "\n".join(report.get("details", [])[:10]))
    attempts = report.get("attempts", [])
    if attempts: t.add_row("Attempts", "\n".join([str(a) for a in attempts[:6]]))
    console.print(t)

@app.command()
def config_maintain():
    """Run one-off auto-config maintenance."""
    console.print("[cyan]Running one-off config maintenance...[/cyan]")
    r = auto_config_maintenance_once()
    console.print(Panel(json.dumps(r, indent=2), title="Config Maintenance"))

@app.command()
def start_config_maintenance(interval_hours: int = 24):
    """Start background periodic config maintenance."""
    start_periodic_config_maintenance(interval_hours=interval_hours)
    console.print(f"[green]Started periodic config maintenance every {interval_hours} hours.[/green]")

@app.command()
def connect_provider(provider: str, host: Optional[str] = None, key: Optional[str] = None):
    """Connect and configure a provider (delegates to the easy-connect module)."""
    from core.connect import connect_provider_cli
    connect_provider_cli(provider, host=host, key=key)

@app.command()
def connections(test: bool = typer.Option(False, "--test", help="Probe reachability of each configured provider")):
    """Show a connection-status table for all AI providers. Keys are never printed."""
    from core.connect import connection_status, render_status_table, render_next_steps
    rows = connection_status(test=test)
    console.print(render_status_table(rows))
    panel = render_next_steps(rows)
    if panel is not None:
        console.print(panel)
    elif test:
        ui_success("All configured providers are reachable.")

# ---------------------------------------------------------------------------
# Platform round (Round C): settings, profiles, projects, sims, prompts,
# memory cores, connectors, sign-in, account.
# ---------------------------------------------------------------------------

@app.command()
def settings(action: str = typer.Argument("list", help="list|get|set|reset|export"),
             key: Optional[str] = typer.Argument(None, help="Setting key"),
             value: Optional[str] = typer.Argument(None, help="New value"),
             category: Optional[str] = typer.Option(None, "--category", "-c", help="Filter by category")):
    """Manage Jade's typed settings."""
    from core.settings import settings_table, get_setting, set_setting, reset_setting, export_settings
    import json as _json
    if action == "list":
        console.print(settings_table(category))
    elif action == "get":
        if not key:
            console.print("[red]Usage: jade settings get <key>[/red]"); return
        try:
            console.print(f"{key} = {get_setting(key)!r}")
        except KeyError as e:
            console.print(f"[red]❌ {e}[/red]")
    elif action == "set":
        if not key or value is None:
            console.print("[red]Usage: jade settings set <key> <value>[/red]"); return
        res = set_setting(key, value)
        console.print(f"[green]{res['message']}[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    elif action == "reset":
        if not key:
            console.print("[red]Usage: jade settings reset <key>[/red]"); return
        res = reset_setting(key)
        console.print(f"[green]✅ {key} reset to {res['value']!r}[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    elif action == "export":
        console.print(_json.dumps(export_settings(), indent=2))
    else:
        console.print(f"[red]Unknown action '{action}'.[/red]")

@app.command()
def profile(action: str = typer.Argument("list", help="list|switch|new|delete"),
            name: Optional[str] = typer.Argument(None, help="Profile name")):
    """Manage named profiles (work, personal, …)."""
    from core import profiles as _p
    if action == "list":
        console.print(_p.profiles_table())
    elif action == "switch":
        if not name:
            console.print("[red]Usage: jade profile switch <name>[/red]"); return
        res = _p.switch_profile(name)
        console.print(f"[green]✅ Now using profile '{name}'.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    elif action == "new":
        if not name:
            console.print("[red]Usage: jade profile new <name>[/red]"); return
        res = _p.create_profile(name)
        console.print(f"[green]✅ Profile '{name}' created.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    elif action == "delete":
        if not name:
            console.print("[red]Usage: jade profile delete <name>[/red]"); return
        res = _p.delete_profile(name)
        console.print(f"[green]✅ Profile '{name}' deleted.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    else:
        console.print(f"[red]Unknown action '{action}'.[/red]")

@app.command()
def project(action: str = typer.Argument("list", help="list|new|open|close|delete"),
            name: Optional[str] = typer.Argument(None, help="Project name or slug")):
    """Manage projects (grouped chats, files, instructions)."""
    from core import projects as _pr
    if action == "list":
        console.print(_pr.projects_table())
    elif action == "new":
        if not name:
            console.print("[red]Usage: jade project new <name>[/red]"); return
        res = _pr.create_project(name)
        console.print(f"[green]✅ Project '{res['name']}' created (slug: {res['slug']}).[/green]")
    elif action == "open":
        if not name:
            console.print("[red]Usage: jade project open <slug>[/red]"); return
        res = _pr.open_project(name)
        console.print(f"[green]✅ Project '{name}' is now active.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    elif action == "close":
        _pr.close_project()
        console.print("[green]✅ No active project.[/green]")
    elif action == "delete":
        if not name:
            console.print("[red]Usage: jade project delete <slug>[/red]"); return
        res = _pr.delete_project(name)
        console.print(f"[green]✅ Deleted.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    else:
        console.print(f"[red]Unknown action '{action}'.[/red]")

@app.command()
def sim(action: str = typer.Argument("list", help="list|chat|export|import|delete"),
        name: Optional[str] = typer.Argument(None, help="Sim name or path")):
    """Manage Jade Sims (portable agents)."""
    from core import sims as _s
    if action == "list":
        console.print(_s.sims_table())
    elif action == "chat":
        if not name:
            console.print("[red]Usage: jade sim chat <name>[/red]"); return
        from rich.prompt import Prompt as _Prompt
        history = []
        console.print("[dim]Chatting — empty message exits.[/dim]")
        while True:
            msg = _Prompt.ask("you").strip()
            if not msg:
                break
            history.append({"role": "user", "text": msg})
            res = _s.sim_chat(name, msg, history)
            if not res["ok"]:
                console.print(f"[red]❌ {res['error']}[/red]"); break
            console.print(f"[magenta]{res['avatar']} {res['sim']}:[/magenta] {res['reply']}")
            history.append({"role": "assistant", "text": str(res["reply"])})
    elif action == "export":
        if not name:
            console.print("[red]Usage: jade sim export <name> <dest>[/red]"); return
        dest = typer.prompt("Destination path")
        res = _s.export_sim(name, dest)
        console.print(f"[green]✅ Exported to {res['file']}[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    elif action == "import":
        if not name:
            console.print("[red]Usage: jade sim import <path>[/red]"); return
        res = _s.import_sim(name)
        console.print(f"[green]✅ Imported sim '{res['name']}'.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    elif action == "delete":
        if not name:
            console.print("[red]Usage: jade sim delete <name>[/red]"); return
        res = _s.delete_sim(name)
        console.print(f"[green]✅ Deleted.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    else:
        console.print(f"[red]Unknown action '{action}'.[/red]")

@app.command()
def prompt(action: str = typer.Argument("list", help="list|save|apply|delete|show|override"),
           name: Optional[str] = typer.Argument(None, help="Prompt name")):
    """Manage the prompt template library."""
    from core import prompts as _pr
    if action == "list":
        console.print(_pr.list_prompts())
    elif action == "save":
        if not name:
            console.print("[red]Usage: jade prompt save <name>[/red]"); return
        text = typer.prompt("System prompt text")
        console.print(f"[green]✅ {_pr.save_prompt(name, text)}[/green]")
    elif action == "apply":
        if not name:
            console.print("[red]Usage: jade prompt apply <name>[/red]"); return
        ok, msg = _pr.apply_prompt(name)
        console.print(f"[green]✅ {msg}[/green]" if ok else f"[red]❌ {msg}[/red]")
    elif action == "delete":
        if not name:
            console.print("[red]Usage: jade prompt delete <name>[/red]"); return
        console.print(f"[green]✅ {_pr.delete_prompt(name)}[/green]")
    elif action == "show":
        if not name:
            console.print(_pr.get_active_prompt_text())
        else:
            prompts = _pr.load_prompts()
            console.print(prompts.get(name, f"[red]Prompt '{name}' not found.[/red]"))
    elif action == "override":
        # jade prompt override <personality> <prompt-name>  (text via prompt)
        if not name:
            console.print("[red]Usage: jade prompt override <personality> <prompt-name>[/red]"); return
        parts = name.split(None, 1)
        if len(parts) != 2:
            console.print("[red]Usage: jade prompt override <personality> <prompt-name>[/red]"); return
        text = typer.prompt("Override text (empty clears)", default="")
        console.print(f"[green]✅ {_pr.set_personality_override(parts[0], parts[1], text)}[/green]")
    else:
        console.print(f"[red]Unknown action '{action}'.[/red]")

@app.command(name="memories")
def memories_cmd(action: str = typer.Argument("show", help="show|add|forget|export|stats"),
                 core: str = typer.Argument("facts", help="facts|preferences|projects|episodic"),
                 text: Optional[str] = typer.Argument(None, help="Text for add/forget")):
    """Manage inspectable memory cores."""
    from core import memory_cores as _mc
    import json as _json
    if action == "stats":
        console.print(_mc.cores_table())
    elif action == "show":
        entries = _mc.recall(core, text or "")
        if not entries:
            console.print("[yellow]No entries.[/yellow]"); return
        for e in entries:
            console.print(f"[dim]{e['id']}[/dim] {e['text']}")
    elif action == "add":
        if not text:
            console.print("[red]Usage: jade memories add <core> <text>[/red]"); return
        res = _mc.remember(core, text)
        console.print(f"[green]✅ Stored ({res['entry']['id']}).[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    elif action == "forget":
        if not text:
            console.print("[red]Usage: jade memories forget <core> <id-or-text>[/red]"); return
        res = _mc.forget(core, text)
        console.print(f"[green]✅ Removed {res['removed']}.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
    elif action == "export":
        res = _mc.export_core(core)
        console.print(res.get("json", "") if res["ok"] else f"[red]❌ {res['error']}[/red]")
    else:
        console.print(f"[red]Unknown action '{action}'.[/red]")

@app.command()
def connector(action: str = typer.Argument("list", help="list|status|connect|disconnect"),
              conn_id: Optional[str] = typer.Argument(None, help="Connector id")):
    """Manage app connectors (Google + Microsoft services)."""
    from core import connectors as _cx
    if action == "list":
        for c in _cx.list_connectors():
            st = "🟢" if c.status()["connected"] else "⚪"
            console.print(f"{st} [cyan]{c.id}[/cyan] — {c.display}: {c.description}")
    elif action == "status":
        if conn_id:
            console.print_json(data=_cx.get_connector(conn_id).status())
        else:
            for s in _cx.connector_status_all():
                console.print_json(data=s)
    elif action == "connect":
        if not conn_id:
            console.print("[red]Usage: jade connector connect <id>[/red]"); return
        res = _cx.get_connector(conn_id).connect()
        console.print(f"[green]✅ Connected.[/green]" if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
    elif action == "disconnect":
        if not conn_id:
            console.print("[red]Usage: jade connector disconnect <id>[/red]"); return
        res = _cx.get_connector(conn_id).disconnect()
        console.print(f"[green]✅ {res.get('message', '')}[/green]" if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
    else:
        console.print(f"[red]Unknown action '{action}'.[/red]")

@app.command()
def signin(action: str = typer.Argument("status", help="status|in|out"),
           provider: Optional[str] = typer.Argument(None, help="microsoft|apple|google|github")):
    """Sign in with Microsoft / Apple / Google / GitHub (OAuth2)."""
    from core import signin as _si
    if action == "status":
        for s in _si.signin_status():
            mark = "🟢" if s["signed_in"] else "⚪"
            detail = s["account"] if s["signed_in"] else (s["needs"] or "client not configured")
            console.print(f"{mark} [cyan]{s['display']}[/cyan] — {detail}")
    elif action == "in":
        if not provider:
            console.print("[red]Usage: jade signin in <microsoft|apple|google|github>[/red]"); return
        res = _si.signin(provider)
        console.print(f"[green]✅ Signed in as {res.get('account', '')}.[/green]" if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
    elif action == "out":
        if not provider:
            console.print("[red]Usage: jade signin out <provider>[/red]"); return
        res = _si.signout(provider)
        console.print(f"[green]✅ {res.get('message', '')}[/green]" if res.get("ok") else f"[red]❌ {res.get('error')}[/red]")
    else:
        console.print(f"[red]Unknown action '{action}'.[/red]")

@app.command()
def account(action: str = typer.Argument("show", help="show|set-name")):
    """Local Jade Account record (see docs/JADE_ACCOUNT.md)."""
    from core import jade_account as _ca
    if action == "show":
        import json as _json
        console.print(_json.dumps(_ca.get_account(), indent=2))
    elif action == "set-name":
        name = typer.prompt("Display name")
        res = _ca.set_display_name(name)
        console.print(f"[green]✅ Display name: {res['display_name']}[/green]")
    else:
        console.print(f"[red]Unknown action '{action}'.[/red]")

@app.command()
def webask(query: str, provider: Optional[str] = None):
    """Perplexity-like web assistant: search web and synthesize answer with citations."""
    from core.perplexity_like import bing_search, extract_search_snippets, synthesize_answer
    console.print(f"[dim]Searching web for:[/dim] {query}")
    s = bing_search(query)
    if not s.get("ok"):
        console.print(f"[red]Search failed: {s.get('error')}[/red]"); return
    snippets = extract_search_snippets(s["raw"])
    res = synthesize_answer(query, snippets, provider=provider)
    if not res.get("ok"):
        console.print(f"[red]Summarization failed: {res.get('error')}[/red]"); return
    console.print(Panel(res["answer"], title=f"Answer ({res.get('provider')}/{res.get('model')})", border_style="cyan"))
    if res["sources"]:
        console.print(Panel("\n".join(res["sources"]), title="Sources", border_style="magenta"))

@app.command()
def optimize():
    """Optimize JADE and system environment for peak performance."""
    from core.deps import ensure_all
    import shutil
    
    console.print("[bold cyan]🚀 Starting Optimization Suite...[/bold cyan]")
    
    # 1. Clear __pycache__
    console.print("[dim]Cleaning Python bytecode cache...[/dim]")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for root, dirs, files in os.walk(base_dir):
        if "__pycache__" in dirs:
            shutil.rmtree(os.path.join(root, "__pycache__"))
    
    # 2. Refresh Dependency Cache
    console.print("[dim]Refreshing dependency status...[/dim]")
    ensure_all(force=True)
    
    # 3. System Cleanup Suggestions (Info only)
    if sys.platform == "darwin":
        if shutil.which("brew"):
            console.print("[dim]Note: Run 'brew upgrade' to keep system tools updated.[/dim]")
    elif shutil.which("apt"):
        console.print("[dim]Note: Run 'sudo apt update && sudo apt upgrade' to keep system tools updated.[/dim]")

    console.print("[bold green]✅ Optimization complete![/bold green]")

def jarvis_shim():
    """Deprecated ``jarvis`` entry point: notifies about the rename, then forwards."""
    import sys
    print("Note: 'jarvis' has been renamed to 'jade' (JADE AI). Please use 'jade' going forward.",
          file=sys.stderr)
    sys.argv[0] = "jade"
    app()


def cortana_shim():
    """Deprecated ``cortana`` entry point: notifies about the rename, then forwards."""
    import sys
    print("Note: 'cortana' has been renamed to 'jade' (JADE AI). Please use 'jade' going forward.",
          file=sys.stderr)
    sys.argv[0] = "jade"
    app()


def cortana_thin_shim():
    """Deprecated ``cortana-thin`` entry point: notifies about the rename, then forwards."""
    import sys
    print("Note: 'cortana-thin' has been renamed to 'jade-thin'. Please use 'jade-thin' going forward.",
          file=sys.stderr)
    from tools.ollama_thin import main
    sys.argv[0] = "jade-thin"
    main()


if __name__ == "__main__": app()
