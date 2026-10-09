import os
import webbrowser
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from core.approvals import confirm
from core.config import load_config, save_config, get_env_with_config
from core.services import get_api_key, set_api_key, validate_provider_connection, repair_ollama
from core.utils import open_url

console = Console()

class AuthManager:
    """Manages the startup login sequence and account linking for JADE."""

    # Providers with real support in core.services (validation and/or key
    # handling). Fabricated entries were removed.
    #
    # "free" labels describe legitimate free/public tiers verified against
    # provider documentation (audited 2026-09-25). Providers without a
    # verified free tier omit the label (paid API) rather than guessing.
    PROVIDERS = {
        "openai": {"display": "OpenAI", "url": "https://platform.openai.com/api-keys"},
        "gemini": {"display": "Google Gemini", "url": "https://aistudio.google.com/app/apikey",
                   "free": "Free tier via Google AI Studio (no credit card required)"},
        "anthropic": {"display": "Anthropic", "url": "https://console.anthropic.com/settings/keys"},
        "cohere": {"display": "Cohere", "url": "https://dashboard.cohere.com/api-keys",
                   "free": "Free developer trial keys (rate-limited)"},
        "mistral": {"display": "Mistral AI", "url": "https://console.mistral.ai/api-keys/",
                    "free": "Free experimentation tier on La Plateforme"},
        "deepseek": {"display": "DeepSeek", "url": "https://platform.deepseek.com/api_keys"},
        "groq": {"display": "Groq", "url": "https://console.groq.com/keys",
                 "free": "Always-free tier (no credit card, rate-limited)"},
        "together": {"display": "Together AI", "url": "https://api.together.xyz/settings/api-keys",
                     "free": "$5 in free credits on new signups"},
        "perplexity": {"display": "Perplexity", "url": "https://www.perplexity.ai/settings/api"},
        "qwen": {"display": "Alibaba Qwen", "url": "https://bailian.console.aliyun.com/?apiKey=1#/api-key"},
        "github": {"display": "GitHub", "url": "https://github.com/settings/tokens",
                   "free": "Free — personal access tokens cost nothing"},
        "ollama": {"display": "Ollama", "host_only": True,
                   "free": "Free — runs on your own hardware"},
        "vllm": {"display": "vLLM", "host_only": True,
                 "free": "Free — runs on your own hardware"},
        "sglang": {"display": "SGLang", "host_only": True,
                   "free": "Free — runs on your own hardware"},
        "gpt4all": {"display": "GPT4All", "host_only": True,
                    "free": "Free — runs on your own hardware"},
        "llama_cpp": {"display": "llama.cpp", "host_only": True,
                      "free": "Free — runs on your own hardware"},
        "nemotron": {"display": "NVIDIA NeMo", "host_only": True,
                     "free": "Free — runs on your own hardware"},
        "local": {"display": "Local server", "host_only": True,
                  "free": "Free — runs on your own hardware"},
    }

    @staticmethod
    def run_startup_login():
        """Executed on launch to ensure critical services are connected."""
        config = load_config()
        
        # 1. Check Primary Provider
        primary = config.get("provider", "ollama")
        console.print(f"[dim]Verifying primary intelligence: {primary.upper()}...[/dim]")
        
        status = validate_provider_connection(primary)
        if not status.get("ok"):
            console.print(Panel(f"[bold yellow]⚠️ Primary Provider ({primary.upper()}) Disconnected[/bold yellow]\n{status.get('error')}", border_style="yellow"))
            
            if primary == "ollama":
                if confirm("Attempt to repair/reconnect Ollama?"):
                    repair_ollama()
            else:
                if confirm(f"Link or configure your {primary.upper()} backend now?"):
                    AuthManager.link_account(primary)

        # 2. Check for missing critical cloud fallbacks
        missing_fallbacks = []
        for p in ["openai", "gemini"]:
            if not get_api_key(p):
                missing_fallbacks.append(p)

        if missing_fallbacks:
            console.print(f"\n[dim]Note: Missing cloud fallback keys for: {', '.join(missing_fallbacks)}[/dim]")
            if confirm("Would you like to link a fallback cloud account for higher reliability?"):
                for p in missing_fallbacks:
                    if confirm(f"Link {p.upper()}?"):
                        AuthManager.link_account(p)

    @staticmethod
    def link_account(provider_name):
        """Interactive flow to link a specific service account."""
        info = AuthManager.PROVIDERS.get(provider_name.lower())
        display_name = info["display"] if info else provider_name.upper()
        url = info["url"] if info and not info.get("host_only") else None

        console.print(Panel(f"🔗 [bold cyan]Linking {display_name} Account[/bold cyan]", border_style="cyan"))
        
        if info and info.get("host_only"):
            host = Prompt.ask(f"Enter host URL for {display_name} (e.g. http://localhost:8000)")
            if host:
                config = load_config()
                config[f"{provider_name.lower()}_host"] = host
                save_config(config)
                console.print(f"[green]✅ {display_name} host saved successfully![/green]")
            return

        if url:
            console.print(f"Opening developer dashboard: [link={url}]{url}[/link]")
            open_url(url)
        
        key = Prompt.ask(f"Enter API Key / Token for {display_name}", password=True)
        if key:
            res = set_api_key(provider_name, key)
            if res.get("ok"):
                console.print(f"[green]✅ {display_name} linked successfully![/green]")
                # Verify connection immediately
                v = validate_provider_connection(provider_name)
                if v.get("ok"):
                    console.print(f"[dim]Connection verified.[/dim]")
                else:
                    console.print(f"[yellow]⚠️ Key saved, but verification failed: {v.get('error')}[/yellow]")
            else:
                console.print(f"[red]❌ Failed to save key for {display_name}.[/red]")
        else:
            console.print("[yellow]Skipped.[/yellow]")
