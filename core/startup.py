from typing import Dict, Any, List, Optional
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from core.approvals import confirm
import core.services as svc
from core.config import load_config, save_config
from tools.network import scan_network_for_ollama
import os
import sys
import time

console = Console()
DEFAULT_PROVIDERS = ["ollama", "openai", "gemini", "anthropic", "mistral", "deepseek", "qwen", "kimi", "perplexity", "granite", "nemotron", "groq", "together"]

def _interactive_connect(provider: str) -> Dict[str, Any]:
    provider = provider.lower()
    result = {"provider": provider, "connected": False, "message": ""}
    FREE_GUIDE = {
        "groq": "Get a free API key at https://groq.com",
        "together": "Get a free API key at https://together.ai",
        "deepseek": "Get an API key at https://platform.deepseek.com",
        "qwen": "Get an API key at https://dashscope.console.aliyun.com",
        "anthropic": "Get an API key at https://console.anthropic.com",
        "mistral": "Get an API key at https://console.mistral.ai",
        "kimi": "Get an API key at https://platform.moonshot.cn",
        "perplexity": "Get an API key at https://www.perplexity.ai/settings/api",
        "granite": "Get an API key at https://www.ibm.com/granite",
        "nemotron": "Get an API key at https://build.nvidia.com"
    }
    try:
        if provider == "ollama":
            host = Prompt.ask("Enter Ollama host", default="http://localhost:11434")
            cfg = load_config()
            cfg["ollama_host"] = host
            save_config(cfg)
            result.update({"connected": True, "message": f"Ollama host updated to {host}"})
            return result
        
        if provider in FREE_GUIDE:
            console.print(Panel(FREE_GUIDE[provider], title=f"{provider.upper()} Guide", border_style="green"))
            
        key = Prompt.ask(f"Enter {provider.upper()} API key", password=True)
        if key:
            svc.set_api_key(provider, key)
            result.update({"connected": True, "message": f"{provider.upper()} key saved."})
        return result
    except Exception as e:
        return {"connected": False, "message": f"Connect failed: {e}"}

def check_provider(provider: str, auto: bool = False) -> Dict[str, Any]:
    status = {"provider": provider, "ok": False, "validated": False, "action_taken": None, "message": None}
    try:
        v = svc.validate_provider_connection(provider)
        status["ok"] = v.get("ok", False)
        status["validated"] = v.get("ok", False)
        status["message"] = v.get("error") or v.get("note") or "Connected" if v.get("ok") else "Missing configuration"
    except Exception as e:
        status["ok"] = False
        status["message"] = str(e)
        
    if not status["ok"] and not auto:
        # Prompt for all defined providers
        if confirm(f"⚠️ Provider [bold cyan]'{provider.upper()}'[/bold cyan] is not ready. Configure it now?"):
            conn = _interactive_connect(provider)
            if conn.get("connected"):
                status["ok"] = True
                status["message"] = conn.get("message")
    return status

def startup_check_and_login(auto: bool = False, providers: Optional[List[str]] = None, start_maintenance: bool = True) -> Dict[str, Any]:
    # 0. Hardware Check & Tiering
    try:
        from core.hardware_check import run_hardware_check_on_startup
        run_hardware_check_on_startup()
    except Exception as e:
        console.print(f"[dim red]Hardware audit failed: {e}[/dim red]")

    # 1. Auto-scan network for Ollama
    console.print("[dim]Scanning network for Ollama instances...[/dim]")
    found_hosts = scan_network_for_ollama()
    cfg = load_config()
    if found_hosts:
        hosts = cfg.get("ollama_hosts", ["http://localhost:11434"])
        new_hosts = list(set(hosts + found_hosts))
        if len(new_hosts) > len(hosts):
            cfg["ollama_hosts"] = new_hosts
            save_config(cfg)
            console.print(f"[green]✅ Distributed compute ready. Discovered: {', '.join(found_hosts)}[/green]")
        
    # 1. Background Ollama Management
    console.print("[dim]Checking Ollama background status...[/dim]")
    # Try launching ollama via systemd or desktop entry (OS specific)
    if sys.platform == "darwin":
        os.system("open -a Ollama &")
    elif sys.platform == "linux":
        os.system("systemctl --user start ollama &")
        os.system("ollama serve &")
    time.sleep(2) # Give it a moment to initialize

    # 2. Proactively detect and manage local models
    try:
        console.print("[dim]Detecting available Ollama models...[/dim]")
        res = svc.list_models_for_provider("ollama")
        if res.get("ok"):
            models = res.get("models", [])
            # Ensure required models exist
            required_models = ["tinyllama", "alpaca"]
            for model in required_models:
                if model not in "".join(models).lower():
                    console.print(f"[yellow]⚠️ Required model '{model}' missing.[/yellow]")
                    if auto:
                        console.print("[dim]Skipping model download in non-interactive mode.[/dim]")
                    elif confirm(f"Download Ollama model '{model}' now?"):
                        svc.install_ollama_model(model)
        
        # 3. Proactive Cloud Authentication Check (interactive only; the login
        # itself still prompts for the token, so no credentials move silently)
        if not cfg.get("ollama_token") and not auto:
            console.print("[yellow]⚠️ Ollama Cloud not configured. Initiating login...[/yellow]")
            from cli import ollama_login
            ollama_login()
        
    except Exception as e:
        console.print(f"[dim]Auto-detect models failed: {e}[/dim]")

    # 4. Proactive Agent Ecosystem Setup — detection only. Installs are
    # strictly opt-in (per-item prompt with "never ask again" persistence);
    # nothing is ever installed automatically, and non-interactive mode
    # skips installs entirely.
    try:
        console.print("[dim]Checking AI Agent ecosystem...[/dim]")
        from core.agent_manager import check_agents, prompt_and_install_agents
        statuses = check_agents()
        missing = [s for s in statuses if not s["installed"]]
        if not missing:
            console.print("[dim][green]✓ All registered agent CLIs detected.[/green][/dim]")
        elif auto:
            console.print(
                "[dim]Skipping agent installs in non-interactive mode. Missing: "
                + ", ".join(s["name"] for s in missing)
                + "[/dim]"
            )
        else:
            report = prompt_and_install_agents(
                statuses, config=cfg, save_config_fn=save_config
            )
            if report["installed"]:
                console.print(
                    f"[green]✅ Installed: {', '.join(report['installed'])}[/green]"
                )
            if report["failed"]:
                console.print(
                    f"[red]❌ Failed: {', '.join(report['failed'])}[/red]"
                )
    except Exception as e:
        console.print(f"[dim]Agent ecosystem check failed: {e}[/dim]")

    # 3. Check Cloud Ollama
    if not cfg.get("ollama_token"):
        try:
            from core.hardware_check import get_hardware_specs
            specs = get_hardware_specs()
        except Exception:
            specs = {}
        prompt_text = "⚠️ [bold cyan]Ollama Cloud[/bold cyan] is not configured. Sign in to your account to enable cloud models?"
        if specs.get("is_low_end"):
            prompt_text = "⚠️ [yellow]Low-end hardware detected.[/yellow] Sign in to [bold cyan]Ollama Cloud[/bold cyan] for better performance?"
            
        if not auto and confirm(prompt_text):
            from core.utils import open_url
            console.print("[dim]Opening Ollama website. Please log in and find your API token in your account settings/dashboard.[/dim]")
            open_url("https://ollama.com")
            token = Prompt.ask("Enter your Ollama account token", password=True)
            if token:
                cfg["ollama_token"] = token
                cfg["ollama_cloud_host"] = "https://ollama.com/api"
                save_config(cfg)
                console.print("[green]✅ Ollama Cloud configured successfully.[/green]")
    elif not cfg.get("ollama_cloud_host"):
        cfg["ollama_cloud_host"] = "https://ollama.com/api"
        save_config(cfg)
        console.print("[dim]Auto-set Ollama Cloud API URL.[/dim]")

    # 4. Check P2P Connectivity
    if not cfg.get("p2p_enabled"):
        if not auto and confirm("⚠️ [bold cyan]P2P Features[/bold cyan] (Local Network Peer-to-Peer) are not configured. Enable them now?"):
            cfg["p2p_enabled"] = True
            cfg["p2p_share_keys"] = confirm("Do you want to enable sharing API keys across P2P?", sensitive=True)
            cfg["p2p_share_fs"] = confirm("Do you want to enable file system edits via P2P?", sensitive=True)
            cfg["p2p_share_tokens"] = confirm("Do you want to enable sharing login tokens across P2P?", sensitive=True)
            save_config(cfg)
            console.print("[green]✅ P2P Features enabled.[/green]")
        else:
            cfg["p2p_enabled"] = False
            save_config(cfg)

    # 5. Connectivity initialization
    report = {"auto": bool(auto), "results": [], "maintenance_started": False}
    provs = providers or DEFAULT_PROVIDERS

    # Start P2P Server in background if enabled
    if cfg.get("p2p_enabled"):
        try:
            from core.p2p import start_server_background
            start_server_background()
            
            # Register for global P2P if enabled
            if cfg.get("global_p2p_enabled"):
                from core.global_p2p import register_node
                register_node()
                
                
            console.print("[dim][green]✓ P2P Server Online[/green][/dim]")
        except Exception as e:
            console.print(f"[dim][red]! P2P Server failed: {e}[/red][/dim]")

    console.print(Panel(f"JADE System Initialization: Verifying connectivity to {len(provs)} providers...", title="Startup", border_style="cyan"))

    
    for p in provs:
        res = check_provider(p, auto=auto)
        report["results"].append(res)
        if res.get("ok"):
            console.print(f"  [green]✓[/green] {p.upper()}: [dim]{res.get('message')}[/dim]")
        else:
            # For non-core providers, just a dim warning if missing
            console.print(f"  [yellow]![/yellow] {p.upper()}: [dim]{res.get('message')}[/dim]")

    if start_maintenance:
        try:
            from core.config import start_periodic_config_maintenance
            start_periodic_config_maintenance(interval_hours=24)
            report["maintenance_started"] = True
        except Exception as e:
            report["maintenance_error"] = str(e)

    console.print(Panel("Intelligence core online. Systems nominal.", title="Status", border_style="green"))
    return report
