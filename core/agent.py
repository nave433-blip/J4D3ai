import json
import os
import sys
from core.brain import think_structured
from tools.shell import run, run_simple
from tools.search import grep, list_files, system_find
from tools.editor import replace_in_file, read_section
from tools.installer import brew_install, git_install, curl_install
from tools.github import github_tool
from tools.analytics import analyze_complexity, project_summary
from tools.cloud import list_dropbox, list_gdrive, list_icloud
from tools.network import scan_network, scan_ports
from tools.server import list_listening_ports, get_process_stats, kill_process
# tools.ssh pulls paramiko (~110ms) — import lazily at the SSH dispatch site.
from tools.hardware import list_usb_devices, probe_ports
from tools.launcher import launch_tool
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from core.approvals import confirm
from rich.markdown import Markdown

console = Console()

from core.ui import display_chat_message


def _sandbox_enabled():
    """Opt-in Linux code sandbox for the SHELL tool.

    Enabled with "code_sandbox": true in ~/.jade/config.json. Default off —
    plain execution stays the default; the sandbox only ever restricts.
    """
    try:
        from core.config import load_config
        return bool(load_config().get("code_sandbox", False))
    except Exception:
        return False

def dispatch_tool(line, next_line):
    if not line.startswith("TOOL:"): return None
    
    tool_name = line.replace("TOOL:", "").strip()
    args_str = next_line.replace("ARGS:", "").strip()
    
    try:
        args = json.loads(args_str)
        
        # GEMINI-STYLE: Execute Tool box
        console.print(Panel(f"[bold green]▶ EXECUTING {tool_name}[/bold green]\n[dim]{args_str}[/dim]", border_style="green", box=None))

        if tool_name == "SEARCH":
            if "grep" in args: return grep(args["grep"])
            return list_files(args.get("glob", "**/*"))
        elif tool_name == "SYSTEM_SEARCH":
            return system_find(args["name"], args.get("root", "/"))
        elif tool_name == "WEB_SEARCH":
            from tools.search import web_search
            return web_search(args["query"])
        elif tool_name == "READ":
            return read_section(args["path"], args.get("start", 1), args.get("end", 100))
        elif tool_name == "EDIT":
            return replace_in_file(args["path"], args["old"], args["new"])
        elif tool_name == "SHELL":
            use_sandbox = _sandbox_enabled()
            res = run(args["command"], sandbox=use_sandbox)
            if isinstance(res, dict) and res.get("status") == "needs_confirmation":
                console.print(Panel(f"[bold red]⚠️ POTENTIALLY UNSAFE COMMAND DETECTED[/bold red]\n\n[white]{res['command']}[/white]", border_style="red"))
                if confirm("Do you want to authorize this command?"):
                    return run_simple(res['command'], confirm=True, sandbox=use_sandbox)
                else:
                    return "ABORTED: Command authorization denied by user."
            return run_simple(args["command"], sandbox=use_sandbox)
        elif tool_name == "INSTALLER":
            method = args.get("method")
            if method == "brew": return brew_install(args["package"])
            if method == "git": return git_install(args["repo"], args.get("dest", "."))
            if method == "curl": return curl_install(args["url"], args["output"])
        elif tool_name == "GITHUB":
            action = args.get("action")
            repo = args.get("repo")
            if action == "info": return github_tool.get_repo_info(repo)
            if action == "issue": return github_tool.create_issue(repo, args["title"], args["body"])
            if action == "list_prs": return github_tool.list_pull_requests(repo)
            if action == "create_pr": return github_tool.create_pr(repo, args["title"], args["body"], args["head"], args.get("base", "main"))
        elif tool_name == "ANALYTICS":
            action = args.get("action", "summary")
            if action == "file": return analyze_complexity(args["path"])
            return project_summary(args.get("path", "."))
        elif tool_name == "CLOUD":
            platform = args.get("platform")
            action = args.get("action", "list")
            if platform == "dropbox": return list_dropbox(args.get("path", ""))
            if platform == "gdrive": return list_gdrive(args.get("query", "'root' in parents"))
            if platform == "icloud": return list_icloud(args.get("path", ""))
        elif tool_name == "NETWORK":
            action = args.get("action", "scan")
            if action == "scan": return scan_network()
            if action == "ports": return scan_ports(args["ip"], args.get("range", (1, 1024)))
        elif tool_name == "SSH":
            from tools.ssh import run_remote  # lazy: paramiko is slow to import
            return run_remote(args["host"], args["username"], args["command"], args.get("password"), args.get("key"))
        elif tool_name == "SERVER":
            action = args.get("action", "ports")
            if action == "ports": return list_listening_ports()
            if action == "stats": return get_process_stats()
            if action == "kill": return kill_process(args["pid"])
        elif tool_name == "LAUNCHER":
            return launch_tool(args.get("tool"))
        elif tool_name == "COPILOT":
            from tools.copilot import copilot_suggest, copilot_explain
            action = args.get("action", "suggest")
            if action == "suggest": return copilot_suggest(args.get("query"))
            if action == "explain": return copilot_explain(args.get("command"))
        elif tool_name == "HARDWARE":
            action = args.get("action", "usb")
            if action == "usb": return list_usb_devices()
            if action == "probe": return probe_ports()
        elif tool_name == "GEMINI_REASON":
            from core.brain import GeminiProvider
            model = args.get("model", "gemini-1.5-pro")
            prompt = args.get("prompt")
            if not prompt: return "Error: Missing prompt for Gemini."
            return GeminiProvider(model=model).ask(prompt)
        elif tool_name == "CODEX_CLI":
            import subprocess
            cmd = ["codex", "exec"]
            if "prompt" in args: cmd.append(args["prompt"])
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                return f"Codex Output:\n{res.stdout}\nErrors:\n{res.stderr}"
            except Exception as e:
                return f"Codex Execution Error: {e}"
        elif tool_name == "BOX_CREATE":
            from core.gemini_box import session
            return f"Box creation success: {session.create_box(args['name'])}"
        elif tool_name == "BOX_RUN":
            from core.gemini_box import session
            res = session.run_in_box(args['name'], args['command'])
            return f"Box Run Result:\nSTDOUT: {res.get('stdout')}\nSTDERR: {res.get('stderr')}\nRC: {res.get('exit_code')}"
        elif tool_name == "BOX_TAIL":
            from core.gemini_box import session
            return session.tail_box(args['name'], args.get('lines', 50))
        elif tool_name == "PYTHON_EXAMINE":
            from tools.python_agent import file_summary
            return file_summary(args["path"])
        elif tool_name == "PYTHON_PATCH":
            from tools.python_agent import suggest_patch_via_llm, safe_apply_new_content
            res = suggest_patch_via_llm(args["path"], args["instruction"])
            if res["ok"]:
                console.print(Panel(res["unified_diff"], title="Suggested Patch", border_style="yellow"))
                if confirm("Apply this patch?"):
                    ok, msg = safe_apply_new_content(args["path"], res["suggested"])
                    return msg
                return "Patch rejected by user."
            return f"Patch Error: {res.get('error')}"
            
    except Exception as e:
        return f"Tool Error: {e}"
    
    return "Unknown tool"

def generate_plan(task, model=None):
    """Generate a high-level engineering plan for a task.

    Returns the structured {"ok", "text", "error"} dict from
    think_structured so callers (cli.plan -> process_think_res) get the
    failure panel and setup-wizard offer instead of a bare string.
    """
    return think_structured("", f"Create a step-by-step engineering plan for this task. Do not execute tools, just design the strategy: {task}", model=model, prompt_name="architect")

def debug_loop(issue, model=None, prompt=None, ui_hint=None, on_turn=None):
    """on_turn(role, text): optional callback fired for user/assistant turns
    (used by conversation time-travel). Never required; never raises."""
    context = f"Original Issue: {issue}"
    if on_turn:
        try: on_turn("user", issue)
        except Exception: pass
    
    # If a UI hint is provided, display it first
    if ui_hint:
        title = ui_hint.get("title", "Task Initialization")
        subtitle = ui_hint.get("subtitle", "")
        markdown = ui_hint.get("markdown", "")
        panel_text = f"[bold white]{title}[/bold white]\n[dim]{subtitle}[/dim]\n\n{markdown}"
        console.print(Panel(panel_text, title="💎 GEMINI UI HINT", border_style="cyan"))

    # Auto-Research: If the issue looks like a project name, try to locate it first
    if len(issue.split()) == 1 and not os.path.exists(issue):
        from tools.search import system_find
        console.print(Panel(f"🔍 [bold cyan]ISOLATING TARGET:[/bold cyan] {issue}\n[dim]Scanning system for project root...[/dim]", border_style="cyan"))
        locations = system_find(issue)
        if locations and "error" not in locations.lower():
            context += f"\nNote: I found potential locations for this project:\n{locations}"
            console.print(f"[green]Found potential project path: {locations.splitlines()[0]}[/green]")

    for i in range(10): 
        from rich.status import Status
        
        with Status(f"[bold cyan]Cycle {i+1}:[/bold cyan] JADE is reasoning...", spinner="dots"):
            # think_structured returns {"ok", "text", "error"}; plain think()
            # returns only the text str, which would fail the dict check below
            # and abort the loop on the very first cycle.
            res = think_structured(context, "Resolve the issue using tools if necessary. If previous tools failed, try a different approach.", model=model, prompt_name=prompt)
        
        if not isinstance(res, dict) or not res.get("ok"):
            # think() or NAVE-RL failed, let the CLI handle the setup prompt
            # but we need to display the error here if we're in the loop
            error_msg = res.get("error") if isinstance(res, dict) else str(res)
            console.print(f"[bold red]❌ Brain Loop Interrupted:[/bold red] {error_msg}")
            return

        response = res.get("text", "")
        
        # GEMINI-STYLE: Separate thoughts from tool calls
        thoughts = ""
        tool_lines = []
        for line in response.splitlines():
            if line.startswith("TOOL:") or line.startswith("ARGS:"):
                tool_lines.append(line)
            else:
                thoughts += line + "\n"

        if thoughts.strip():
            # If there are no tool calls, this is a final answer
            if not tool_lines:
                display_chat_message("JADE", thoughts)
                if on_turn:
                    try: on_turn("assistant", thoughts)
                    except Exception: pass
            else:
                console.print(Panel(Markdown(thoughts), title=f"💭 THOUGHTS: Cycle {i+1}", border_style="blue", subtitle="[dim]Gemini-Style Reasoning[/dim]"))
        
        tool_result = None
        for j, line in enumerate(tool_lines):
            if line.startswith("TOOL:") and j + 1 < len(tool_lines):
                tool_result = dispatch_tool(line, tool_lines[j+1])
                break
        
        if tool_result:
            # GEMINI-STYLE: Output shell box for result
            res_panel = Panel(str(tool_result), title="📡 TOOL OUTPUT", border_style="dim", box=None)
            console.print(res_panel)
            context += f"\nStep {i+1} Tool Output:\n{tool_result}"
        else:
            if thoughts.strip() and not tool_lines:
                # We already displayed the final message
                return
            ok = Prompt.ask("\nJADE seems to have finished. Exit?", choices=["y", "n"], default="y")
            if ok.lower() == "y":
                return
            context += f"\nUser feedback: Please continue."

def troubleshoot_loop(command, model=None, prompt=None):
    console.print(f"[bold cyan]Troubleshooting command:[/bold cyan] {command}")
    result = run(command, confirm=True) 
    
    if result.get("return_code") == 0:
        console.print("[green]Command succeeded on first try. No troubleshooting needed.[/green]")
        return
    
    error_context = f"COMMAND FAILED: {command}\nRETURN CODE: {result.get('return_code')}\nSTDERR: {result.get('stderr')}\nSTDOUT: {result.get('stdout')}"
    console.print(Panel(error_context, title="Error Captured", border_style="red"))
    debug_loop(f"Fix the error caused by this command: {command}. Error context: {error_context}", model=model, prompt=prompt)

def forge_loop(task, model=None):
    """Hardcore code synthesis loop."""
    from core.nave_loop import run_nave_loop
    console.print(Panel(f"🛠 [bold cyan]FORGING CODE:[/bold cyan] {task}", border_style="cyan"))
    
    # Use Nave Loop for advanced reasoning
    res = run_nave_loop(f"Create a workaround or new code for this task: {task}. Ensure it is advanced and human-unprecedented.")
    refined_plan = res.get("final_answer", str(res))
    
    # Feed refined plan back into agent loop for execution
    debug_loop(f"Execute the following refined engineering plan: {refined_plan}", model=model, prompt="architect")
