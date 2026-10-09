import os
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from core.approvals import confirm
from core.config import load_config, save_config, setup_wizard

console = Console()

def config_menu():
    """Manage global JADE settings, identities, and API keys with detailed guidance."""
    while True:
        config_data = load_config()
        table = Table(title="[bold cyan]Global System Configuration Control[/bold cyan]", show_header=True, header_style="bold magenta")
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="white")
        table.add_column("Functional Description", style="dim")
        
        descriptions = {
            "provider": "The primary LLM engine. Options include local Ollama, or high-performance cloud providers like Gemini, OpenAI, and Claude.",
            "jade_model": "The specific model identifier (e.g., 'gpt-4o' or 'llama3'). This string is passed directly to the active provider's API.",
            "personality": "Controls the assistant's tone, verbosity, and interaction style. Affects both chat and autonomous agent responses.",
            "active_prompt": "Your persistent system persona. This role is loaded from your Prompt Library and guides all high-level technical reasoning.",
            "github_token": "Enables JADE to autonomously push code, open Pull Requests, manage issues, and sync with your repositories.",
            "gemini_api_key": "Token for Google Gemini. Recommended for free users (1,500 req/day) seeking professional-grade reasoning.",
            "openai_api_key": "Token for OpenAI GPT series. Industry standard for technical logic and sophisticated code synthesis.",
            "anthropic_api_key": "Token for Claude 3.5. Renowned for world-class coding ability and accurate follow-through on complex tasks.",
            "nvidia_api_key": "Token for NVIDIA NIM. Provides access to massive open-source models like Llama 3.1 405B on optimized hardware.",
            "xai_api_key": "Token for xAI Grok-Beta. Focused on technical truth, edge-case detection, and witty interaction.",
            "mistral_api_key": "Token for Mistral AI. Excellent performance-to-cost ratio for technical audits and multilingual decoding.",
            "ollama_host": "The URL where your local Ollama server is running. JADE will auto-detect this during setup if you're unsure.",
            "lm_studio_host": "The URL for your local LM Studio server. Enables use of any GGUF model as the assistant's brain.",
            "llama_cpp_host": "The URL for a Llama.cpp server instance. Optimized for low-level performance on specific hardware.",
            "gpt4all_host": "The URL for the local GPT4All API. Another layer of free, private, and offline intelligence support.",
            "model_mode": "Switch between 'manual', 'auto-offline' (Local Only), 'auto-online' (Cloud Priority), or 'auto-mixed' (Connectivity-aware).",
            "self_repair": "When ENABLED, JADE will autonomously attempt to patch its own source code if it crashes or hits a critical runtime error."
        }
        
        for k, v in config_data.items():
            val = v if "api_key" not in k and "token" not in k or not v else "🔒 ****" + v[-4:]
            desc = descriptions.get(k, "General system parameter controlling core behavioral logic.")
            table.add_row(k, str(val), desc)
        
        console.print(table)
        console.print("\n[bold white]Command Options:[/bold white]")
        console.print("[w] Launch Setup Wizard - Full step-by-step configuration for beginners.")
        console.print("[e] Edit Single Key     - Direct injection of one setting (e.g. updating an API key).")
        console.print("[b] Back                - Return to the previous menu.")
        
        choice = Prompt.ask("\nSelect action", choices=["w", "e", "b"], default="b")
        if choice == "w":
            setup_wizard()
        elif choice == "e":
            key = Prompt.ask("Enter the exact key name to edit")
            if key in config_data:
                val = Prompt.ask(f"Enter new value for {key}", default=str(config_data[key]))
                config_data[key] = val
                save_config(config_data)
                console.print(f"[green]✅ Successfully updated {key}. Settings are now live.[/green]")
            else:
                console.print(f"[red]❌ Error: Key '{key}' not recognized in system configuration.[/red]")
        else:
            break

def network_menu():
    """Advanced networking and discovery suite with detailed tool output."""
    from tools.network import scan_network, scan_ports
    while True:
        info_panel = """
        [bold cyan]Network & Connectivity Suite[/bold cyan]
        
        [1] [bold white]Subnet Discovery (Fing-style):[/bold white]
            Scans your local network to map out every active IP address and hostname. 
            Useful for finding local LLM servers or shared storage devices.
        
        [2] [bold white]Port Analytics (IP Scanner):[/bold white]
            Probes a target IP for open TCP ports (e.g. 11434 for Ollama, 22 for SSH).
            Essential for debugging server connections and remote accessibility.
            
        [b] Back to Main Menu
        """
        console.print(Panel(info_panel, title="DevOps: Networking", border_style="cyan"))
        choice = Prompt.ask("Select tool", choices=["1", "2", "b"], default="b")
        if choice == "1":
            console.print(scan_network())
            input("\nScan complete. Press Enter to continue...")
        elif choice == "2":
            ip = Prompt.ask("Target IP Address")
            ports = scan_ports(ip)
            if ports:
                console.print(f"[green]✅ Found {len(ports)} open ports on {ip}:[/green] {ports}")
            else:
                console.print(f"[yellow]⚠️ No open ports detected on {ip} within the standard range.[/yellow]")
            input("\nPress Enter to continue...")
        else: break

def server_menu():
    """Real-time system health and process management dashboard."""
    from tools.server import list_listening_ports, get_process_stats, kill_process
    while True:
        stats = get_process_stats()
        status = f"CPU Load: [bold]{stats['cpu_usage']}%[/bold] | RAM Usage: [bold]{stats['memory_usage']}%[/bold] | Active PIDs: [bold]{stats['process_count']}[/bold]"
        console.print(Panel(status, title="Node Health Status", border_style="green"))
        
        console.print("\n[bold white]Process Controls:[/bold white]")
        console.print("[1] List Port Map - Identify which services are occupying which network ports.")
        console.print("[2] Terminate Task - Force shutdown a process by its PID to free up system resources.")
        console.print("[b] Back           - Return to the previous menu.")
        
        choice = Prompt.ask("Select action", choices=["1", "2", "b"], default="b")
        if choice == "1":
            console.print(list_listening_ports())
            input("\nEnter to continue...")
        elif choice == "2":
            pid = Prompt.ask("Enter the PID to terminate")
            try:
                console.print(f"[yellow]{kill_process(int(pid))}[/yellow]")
            except Exception: console.print("[red]Invalid PID format.[/red]")
        else: break

def memory_menu():
    """Vector database management for long-term assistant knowledge."""
    from memory.vector import get_stats, search, clear
    from rich.prompt import Confirm
    while True:
        stats = get_stats()
        mem_info = f"""
        [bold cyan]Neural Knowledge Base (FAISS)[/bold cyan]
        Stored Insights: [bold white]{stats['count']}[/bold white]
        
        [1] Search Brain - Query past conversations and technical insights by semantic meaning.
        [2] Wipe Database - Erase all stored context and start fresh with a clean slate.
        [b] Back         - Exit memory management.
        """
        console.print(Panel(mem_info, title="Core Memory", border_style="magenta"))
        choice = Prompt.ask("Choice", choices=["1", "2", "b"], default="b")
        if choice == "1":
            q = Prompt.ask("Semantic search query")
            results = search(q)
            if results: 
                console.print(Panel("\n".join([f"→ {r}" for r in results]), title=f"Relevant Insights: {q}"))
            else: 
                console.print("[yellow]⚠️ No relevant insights found in existing vector storage.[/yellow]")
            input("\nPress Enter to continue...")
        elif choice == "2":
            console.print("[dim italic]Don't make a girl a promise you can't keep.[/dim italic]")
            if confirm("[bold red]DANGER: Are you sure you want to permanently erase all JADE memories?[/bold red]"):
                console.print(f"[green]✅ {clear()}[/green]")
        else: break

def personality_menu():
    """Behavioral profile configuration for AI interaction style."""
    from core.personalities import list_personalities, resolve_personality
    config = load_config()
    current = resolve_personality(config.get("personality")).name

    lines = ["", "    [bold white]Persona Selection Suite[/bold white]", ""]
    for i, p in enumerate(list_personalities(), start=1):
        lines.append(f"    [{i}] [bold cyan]{p.title}:[/bold cyan] {p.description}")
    lines.append("")
    info = "\n".join(lines)
    console.print(Panel(info, title=f"Current: {current.upper()}", border_style="cyan"))
    choices = [str(i) for i in range(1, len(list_personalities()) + 1)] + ["b"]
    choice = Prompt.ask("Select personality", choices=choices, default="b")

    mapping = {str(i): p.name for i, p in enumerate(list_personalities(), start=1)}
    if choice in mapping:
        config["personality"] = mapping[choice]
        save_config(config)
        p = resolve_personality(mapping[choice])
        console.print(f"[green]✅ Identity updated. {p.greeting}[/green]")

def models_menu():
    """Intelligent orchestration and manual selection of LLM providers."""
    config = load_config()
    current_p = config.get("provider", "ollama")
    current_m = config.get("jade_model", "llama3")
    current_mode = config.get("model_mode", "manual")
    
    header = f"""
    Mode: [bold green]{current_mode.upper()}[/bold green]
    Active Brain: [bold cyan]{current_p.upper()}[/bold cyan]
    Intelligence Model: [bold yellow]{current_m}[/bold yellow]
    """
    console.print(Panel(header, title="LLM Intelligence & Routing", border_style="magenta"))
    
    modes_info = """
    [bold white]Operation Modes:[/bold white]
    [a] Auto-Offline - Automatically find and use the best local runner (Ollama, LM Studio).
    [s] Auto-Online  - Prioritize world-class cloud models (Gemini Flash, GPT-4o).
    [x] Auto-Mixed   - Smart routing. Uses Cloud when online, pivots to Local when offline.
    [m] Manual Select - Override all automation and pick a specific provider below.
    """
    console.print(modes_info)

    specialties = Table(title="Model Specialties", show_header=True, header_style="bold magenta")
    specialties.add_column("Model/Provider", style="cyan")
    specialties.add_column("Specialty", style="white")
    specialties.add_row("DeepSeek R1/V3", "High-reasoning, complex logic, mathematics")
    specialties.add_row("Qwen2.5 72B", "Top-tier coding, multilingual, large context")
    specialties.add_row("Llama 3.3 70B", "General-purpose power, creative writing")
    specialties.add_row("GPT-4o / Mini", "Reliable logic, instruction following, versatility")
    specialties.add_row("Claude 3.5 Sonnet", "Nuanced reasoning, coding, professional tone")
    specialties.add_row("Gemini 2.0 Flash", "Fast, multimodal, web-connected research")
    console.print(specialties)

    # Providers shown here must have real support in core.services
    # (connection validation and/or API-key handling).
    p_mapping = {

        "1": "ollama", "2": "openai", "3": "anthropic", "4": "gemini",
        "5": "mistral", "6": "deepseek", "7": "groq", "8": "together",
        "9": "cohere", "0": "perplexity",
        "g": "gpt4all", "l": "llama_cpp", "v": "vllm", "y": "sglang",
        "n": "nemotron", "q": "qwen", "o": "local",
    }

    # Generate Status Table (shared builder — same as the /model command view)
    from core.connect import is_configured
    from core.ui import build_provider_status_table
    keys = list(p_mapping.keys())
    entries = [(p_mapping[k].upper(), is_configured(p_mapping[k])) for k in keys]
    console.print(build_provider_status_table(entries))
    
    choice = Prompt.ask("Select mode or provider", choices=["a", "s", "x", "m", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "g", "l", "v", "y", "n", "q", "o", "b"], default="b")
    
    if choice == "a":
        config["model_mode"] = "auto-offline"
        save_config(config)
        console.print("[green]✅ Switched to Auto-Offline mode. JADE will now stay local.[/green]")
        return
    elif choice == "s":
        config["model_mode"] = "auto-online"
        save_config(config)
        console.print("[green]✅ Switched to Auto-Online mode. Prioritizing cloud intelligence.[/green]")
        return
    elif choice == "x":
        config["model_mode"] = "auto-mixed"
        save_config(config)
        console.print("[green]✅ Switched to Auto-Mixed mode. JADE will now manage connectivity.[/green]")
        return
    elif choice == "m":
        config["model_mode"] = "manual"
        save_config(config)
        console.print("[green]✅ Switched to Manual mode. Please select your provider below.[/green]")
        return

    # Providers offered here must have real support in core.services
    # (connection validation and/or API-key handling). Fabricated providers
    # and speculative model names were removed.
    p_mapping = {
        "1": "ollama", "2": "openai", "3": "anthropic", "4": "gemini",
        "5": "mistral", "6": "deepseek", "7": "groq", "8": "together",
        "9": "cohere", "0": "perplexity",
        "g": "gpt4all", "l": "llama_cpp", "v": "vllm", "y": "sglang",
        "n": "nemotron", "q": "qwen", "o": "local",
    }
    if choice in p_mapping:
        config["model_mode"] = "manual"
        provider = p_mapping[choice]
        config["provider"] = provider
        models = {
            "ollama": ["llama3.3", "llama3.2", "phi4", "qwen3", "deepseek-r1"],
            "openai": ["gpt-4o", "gpt-4o-mini", "o1-preview", "o1-mini"],
            "anthropic": ["claude-3-5-sonnet-20241022", "claude-3-5-sonnet-20240620",
                          "claude-3-opus-20240229"],
            "gemini": ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
            "mistral": ["mistral-large-latest", "mistral-medium-latest"],
            "deepseek": ["deepseek-chat", "deepseek-reasoner"],
            "groq": ["llama-3.3-70b-versatile"],
            "together": ["meta-llama/Llama-3.3-70B-Instruct-Turbo",
                         "Qwen/Qwen2.5-72B-Instruct"],
            "cohere": ["command-r-plus", "command-r"],
            "perplexity": ["llama-3.1-sonar-large-128k-online"],
            "gpt4all": ["default"],
            "llama_cpp": ["default"],
            "vllm": ["meta-llama/Meta-Llama-3-70B-Instruct"],
            "sglang": ["meta-llama/Meta-Llama-3-8B-Instruct"],
            "nemotron": ["default"],
            "qwen": ["qwen3", "qwen2.5"],
            "local": ["default"],
        }
        console.print(f"\n[bold white]Recommended Models for {provider.upper()}:[/bold white]")
        for m in models.get(provider, ["default"]): console.print(f"→ {m}")
        new_model = Prompt.ask("Enter exact model identifier", default=models.get(provider, ["default"])[0])
        config["jade_model"] = new_model
        if provider == "gemini": config["gemini_model"] = new_model
        save_config(config)
        console.print(f"[green]✅ Manual Setup Complete: Brain switched to {provider.upper()} ({new_model})[/green]")
        if not is_configured(provider):
            from rich.prompt import Confirm
            from core.ui import ui_warning
            from core.connect import connect_provider_cli
            ui_warning(f"{provider.upper()} isn't connected yet — it won't answer until you link it.")
            if confirm(f"Set up {provider.upper()} now?", default=True):
                connect_provider_cli(provider)

def prompts_menu():
    """Custom prompt library management and system instruction control."""
    from core.prompts import load_prompts, save_prompt, delete_prompt, list_prompts
    while True:
        console.print(list_prompts())
        config = load_config()
        current = config.get("active_prompt", "default")
        console.print(Panel(f"Active Role: [bold cyan]{current.upper()}[/bold cyan]", border_style="magenta"))
        
        opts = """
        [1] Set Active - Apply a stored role as the primary instruction set.
        [2] Import Role - Create a new persona or custom system instruction.
        [3] Delete Role - Permanently remove a custom role from your library.
        [b] Back       - Exit prompt management.
        """
        console.print(opts)
        choice = Prompt.ask("Action", choices=["1", "2", "3", "b"], default="b")
        if choice == "1":
            name = Prompt.ask("Enter role name to activate")
            prompts = load_prompts()
            if name in prompts:
                config["active_prompt"] = name
                save_config(config)
                console.print(f"[green]✅ Active system instructions set to '{name}'.[/green]")
            else: console.print(f"[red]❌ Error: Role '{name}' not found in library.[/red]")
        elif choice == "2":
            name = Prompt.ask("New Role Name"); text = Prompt.ask("System Instruction Text")
            console.print(f"[green]✅ {save_prompt(name, text)}[/green]")
        elif choice == "3":
            name = Prompt.ask("Role Name to Delete"); res = delete_prompt(name)
            if "Error" in res: console.print(f"[red]❌ {res}[/red]")
            else: console.print(f"[green]✅ {res}[/green]")
        else: break

def ssh_command(args):
    """Secure shell remote command execution engine."""
    from tools.ssh import run_remote
    if not args:
        host = Prompt.ask("Remote Host (e.g. 192.168.1.50)"); user = Prompt.ask("Username"); cmd = Prompt.ask("Command to execute")
    else:
        parts = args.split(" ", 2)
        if len(parts) < 3:
            console.print("[red]⚠️ Usage: /ssh <host> <user> <command>[/red]")
            return
        host, user, cmd = parts
    
    console.print(f"[bold cyan]🔗 Establishing secure channel to {host} as {user}...[/bold cyan]")
    res = run_remote(host, user, cmd)
    console.print(Panel(str(res), title=f"Remote Execution Result: {host}", border_style="cyan"))

def connect_menu():
    """Account Connection Center — delegates to the easy-connect wizard."""
    from core.connect import run_connect_wizard
    run_connect_wizard()

def robust_help():
    """Grouped, scannable command reference."""
    from core.ui import get_menu_grid, next_steps_panel
    console.print(Panel(
        "[bold green]JADE Command Reference[/bold green]\n"
        "[dim]Everything you can type. Natural language works too — "
        "just describe what you want.[/dim]",
        title="[bold green]Help[/bold green]", border_style="green",
    ))
    console.print(get_menu_grid())
    console.print(next_steps_panel(
        ["`/connect` — link an AI provider (first run)",
         "`/chat hello` — talk to your active provider",
         "`/fix .` — autonomous audit & repair of this directory"],
        title="New here? Start with these",
    ))
    console.print("[dim]Psst… there's a [bold]/clippy[/bold] hiding in here somewhere.[/dim]")

def cloud_menu():
    """Interactive management for cloud storage platforms."""
    from tools.cloud import list_gdrive, list_dropbox, list_icloud
    info = """
    [bold white]Unified Cloud Bridge[/bold white]

    [1] [bold cyan]Google Drive:[/bold cyan] Browse files from your G-Drive storage.
    [2] [bold cyan]Dropbox:[/bold cyan] List files stored in your Dropbox.
    [3] [bold cyan]iCloud Drive:[/bold cyan] Direct access to Apple Cloud files (macOS only).
    """
    console.print(Panel(info, title="Cloud Storage", border_style="cyan"))
    choice = Prompt.ask("Select a provider", choices=["1", "2", "3"], default="1")
    if choice == "1":
        files = list_gdrive()
        title = "Google Drive"
    elif choice == "2":
        files = list_dropbox()
        title = "Dropbox"
    else:
        files = list_icloud()
        title = "iCloud Drive"
    if isinstance(files, str):
        # tools/cloud.py returns an error string when a provider is unavailable
        console.print(f"[red]{files}[/red]")
    else:
        body = "\n".join(files) if files else "[dim](empty)[/dim]"
        console.print(Panel(body, title=title, border_style="green"))
