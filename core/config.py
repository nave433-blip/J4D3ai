import copy
import json
import os
import shutil
import threading
import datetime
import webbrowser
import time
import sys
from typing import Callable
from pathlib import Path
from rich.console import Console
from rich.prompt import Prompt, Confirm
from core.approvals import confirm

console = Console()

def _resolve_config_dir():
    """Return the config dir, migrating ``~/.cortana`` -> ``~/.jade`` once
    (and legacy ``~/.jarvis`` before that).

    The old directory is copied (never moved or deleted); a notice is printed
    to stderr so the user knows what happened. Migration still runs when
    ``~/.jade`` exists but has no ``config.json`` yet (e.g. only
    auto-created cache dirs) — an existing ``config.json`` always wins.
    """
    new = Path.home() / ".jade"
    for old, label in ((Path.home() / ".cortana", "Cortana"),
                       (Path.home() / ".jarvis", "Jarvis")):
        if old.is_dir() and not (new / "config.json").exists():
            try:
                shutil.copytree(old, new, dirs_exist_ok=True)
                print(f"Migrated your {label} config (~/{old.name}) to ~/.jade. "
                      "The old directory was left untouched.", file=sys.stderr)
            except Exception as e:
                print(f"Could not migrate ~/{old.name} to ~/.jade ({e}); "
                      f"using ~/{old.name}.", file=sys.stderr)
                return old
    return new

CONFIG_DIR = _resolve_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "provider": "ollama",
    "ollama_host": "http://localhost:11434",
    "ollama_hosts": ["http://localhost:11434"],
    "ollama_cloud_host": "https://ollama.com/api",
    "ollama_token": "", # Added for account connectivity
    "lm_studio_host": "http://localhost:1234",
    "llama_cpp_host": "http://localhost:8080",
    "gpt4all_host": "http://localhost:4891",
    "jade_model": "llama3",
    "jade_name": "Jade",
    "auto_approve": False,  # dev toggle: auto-answer routine confirmations (never on by default)
    "gemini_api_key": "",
    "anthropic_api_key": "",
    "xai_api_key": "",
    "openai_api_key": "",
    "mistral_api_key": "",
    "nvidia_api_key": "",
    "deepseek_api_key": "",
    "moonshot_api_key": "",
    "dropbox_token": "",
    "gdrive_token": "",
    "github_token": "",
    "personality": "jade",
    "active_prompt": "default",
    "model_mode": "manual",
    "self_repair": True,
    # P2P TLS opt-in (plaintext HTTP by default). When p2p_use_tls is true,
    # the P2P server wraps its socket with an SSLContext using these PEM files.
    "p2p_use_tls": False,
    "p2p_tls_certfile": "",
    "p2p_tls_keyfile": "",
    # Shell allowlist mode: empty/missing = disabled (legacy blocklist only).
    # When set to a list of command prefixes, tools.shell.run only executes
    # commands starting with one of them.
    "shell_allowlist": [],
    # Dev mode: local-only diagnostics (debug logging, request timing,
    # DEV MODE banner) + optional personal instructions from
    # ~/.jade/dev_instructions.md. Also enabled via JADE_DEV_MODE=1
    # (JARVIS_DEV_MODE still works as a deprecated alias).
    "dev_mode": False,
    # --- Maximal settings schema (core/settings.py). Kept in sync with
    # SETTINGS_SCHEMA there; a test pins the two together. ---
    # appearance
    "theme": "dark",
    "compact_mode": False,
    "show_banner": True,
    "easter_eggs": True,
    # chat
    "chat_streaming": True,
    "enter_to_send": True,
    "show_token_counts": False,
    "confirm_destructive": True,
    "chat_history_limit": 200,
    # models
    "model_chat": "",
    "model_code": "",
    "model_reasoning": "",
    "model_routing": False,
    # privacy
    "telemetry": False,
    "p2p_discovery": True,
    "p2p_enabled": True,
    # notifications
    "notify_on_long_task": True,
    "notify_sound": False,
    # hotkeys
    "overlay_hotkey": "ctrl+alt+c",
    "push_to_talk_key": "ctrl+shift+space",
    # voice
    "voice_enabled": False,
    "wake_word_enabled": False,
    "voice_profile": "jade-default",
    "stt_engine": "auto",
    "tts_engine": "auto",
    # network
    "dashboard_port": 0,
    "proxy_url": "",
    # sandbox
    "sandbox_enabled": True,
    "sandbox_network": False,
    # data
    "retention_days": 0,
    "auto_backup": False,
    # memory
    "memory_enabled": True,
    "memory_auto_capture": False,
    # connectors
    "connector_auto_refresh": True,
    "ms_client_id": "",
    "apple_client_id": "",
    "apple_client_secret": "",
    "github_client_id": "",
    # active profile / project (core/profiles.py, core/projects.py)
    "active_profile": "default",
    "active_project": "",
}

def load_config():
    """Load config merged over defaults. Always returns an isolated deep copy:
    mutating the result (including nested lists) never affects DEFAULT_CONFIG
    or any previously returned config."""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r") as f:
                raw = json.load(f)
        else:
            raw = {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        # Valid JSON, wrong shape (e.g. a list or string) — fall back to
        # defaults instead of crashing the merge below.
        raw = {}
    merged = {**DEFAULT_CONFIG, **raw}
    # Migrate pre-rename keys so existing configs keep working. (Check the
    # raw user file, not the merged dict — defaults already contain the new
    # keys, which would otherwise shadow the old values.) Chain:
    # jarvis_* -> jade_* -> jade_*; newest present name wins.
    for new_key, old_keys in (("jade_model", ("cortana_model", "jarvis_model")),
                              ("jade_name", ("cortana_name", "jarvis_name"))):
        if new_key not in raw:
            for old_key in old_keys:
                if old_key in raw:
                    merged[new_key] = raw[old_key]
                    break
    return copy.deepcopy(merged)

def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f: json.dump(config, f, indent=4)

def detect_ollama(max_attempts: int = 3):
    import requests  # lazy: ~0.4s import; only needed for Ollama probing
    hosts = ["http://localhost:11434", "http://127.0.0.1:11434"]
    for host in hosts:
        try:
            r = requests.get(f"{host}/api/tags", timeout=0.5)
            if r.status_code == 200: return host
        except Exception: continue

    # Auto-Launch Attempt (bounded retries; previously unbounded recursion)
    if max_attempts > 0:
        if sys.platform == "darwin":
            console.print("[dim]Ollama not detected. Attempting to launch Ollama.app...[/dim]")
            os.system("open -a Ollama &")
        elif sys.platform == "linux":
            console.print("[dim]Ollama not detected. Attempting to start ollama serve...[/dim]")
            os.system("ollama serve &")
        else:
            return None
        time.sleep(3) # Wait for startup
        return detect_ollama(max_attempts=max_attempts - 1)
    return None

def verify_and_fix_local_llm():
    """Proactively check local LLM settings. Do not auto-change provider or auto-run setup."""
    config = load_config()
    if config.get("provider") != "ollama":
        return True

    host = config.get("ollama_host", "http://localhost:11434")
    try:
        import requests
        r = requests.get(f"{host}/api/tags", timeout=1.0)
        if r.status_code == 200:
            return True
    except Exception:
        console.print("[yellow]⚠️ Ollama unreachable at configured host.[/yellow]")
        console.print("[dim]JADE will not auto-switch providers. You can run '/repair-ollama-cmd' or '/setup' to reconfigure.[/dim]")
        return False
    return True

def smart_input(label, default_val, auto_detect_func=None):
    if confirm(f"Do you have the specific {label} details (e.g. URL or Key)?"):
        return Prompt.ask(f"Enter {label}", default=default_val)
    if auto_detect_func:
        console.print(f"[dim]Attempting to auto-configure {label}...[/dim]")
        detected = auto_detect_func()
        if detected:
            console.print(f"[green]✅ Auto-detected: {detected}[/green]")
            return detected
    console.print(f"[yellow]⚠️ No specific {label} details provided. Using default/empty.[/yellow]")
    return default_val

def _print_first_use():
    """Concrete first-use instruction shown at the end of every setup path."""
    from core.ui import next_steps_panel
    from rich.console import Console
    Console().print(next_steps_panel(
        ["`/connections --test` — verify your providers are reachable",
         "`/chat hello` — have your first conversation",
         "`/fix .` — let JADE audit this directory"],
        title="You're set up — try these",
    ))

def setup_wizard():
    console.print("[bold cyan]Welcome to JADE Setup[/bold cyan]\n")
    console.print("[dim]Takes about a minute. API keys are validated, then stored in your "
                  "OS keyring — never in plain text.[/dim]\n")

    if confirm("Use [bold green]Automation Mode[/bold green]? (Auto-detects everything)", default=True):
        quick_setup()
        return

    config = load_config()
    console.print("[dim]Step 1 of 3 — choose your brain[/dim]")
    config["provider"] = Prompt.ask("Select your primary LLM provider", choices=["ollama", "openai", "anthropic", "gemini", "mistral", "deepseek", "groq", "together", "cohere", "perplexity"], default=config["provider"])

    if config["provider"] == "ollama":
        config["ollama_host"] = smart_input("Ollama Host URL", config["ollama_host"], auto_detect_func=detect_ollama)
        config["jade_model"] = Prompt.ask("Ollama Model Name", default=config["jade_model"])
    else:
        # Credentials go through the secure /connect flow (keyring + validation),
        # never into the plaintext config file.
        from core.connect import connect_provider_cli
        console.print("\n[dim]Step 2 of 3 — connect your provider[/dim]")
        connect_provider_cli(config["provider"])

    console.print("\n[dim]Step 3 of 3 — finishing touches[/dim]")
    if confirm("Configure external integrations (GitHub, Cloud Storage)?"):
        config["github_token"] = Prompt.ask("GitHub Personal Access Token", default=config.get("github_token", ""), password=True)
        config["dropbox_token"] = Prompt.ask("Dropbox API Token", default=config.get("dropbox_token", ""), password=True)
        config["gdrive_token"] = Prompt.ask("Google Drive API Token", default=config.get("gdrive_token", ""), password=True)

    config["self_repair"] = confirm("Enable autonomous self-repair?", default=config.get("self_repair", True))
    save_config(config)
    console.print("\n[green]Configuration saved successfully.[/green]")
    _print_first_use()

def quick_setup():
    """Hyper-automated setup for JADE."""
    console.print("[bold cyan]🚀 Initializing JADE Automation Setup...[/bold cyan]")
    config = load_config()

    # 1. Detect Ollama
    host = detect_ollama()
    if host:
        config["provider"] = "ollama"
        config["ollama_host"] = host
        config["jade_model"] = "llama3"
        console.print(f"[green]✅ Local Ollama detected at {host}[/green]")
    else:
        # No silent Gemini default (that dead-ended: no key, no guidance).
        # Take the user straight to the connection center instead.
        console.print("[yellow]⚠️ No local Ollama found.[/yellow]")
        console.print("[dim]JADE needs an AI provider to think. Let's link one now — "
                      "your key is validated, then stored in the OS keyring.[/dim]\n")
        from core.auth import AuthManager
        from core.connect import is_configured, run_connect_wizard
        run_connect_wizard()
        for name in AuthManager.PROVIDERS:
            if is_configured(name):
                config["provider"] = name
                console.print(f"[green]✅ Using {name.upper()} as your provider.[/green]")
                break
        else:
            config["provider"] = "ollama"
            console.print("[yellow]No provider was linked yet. Run /connect any time to set one up.[/yellow]")

    # 2. Cloud Configuration
    if os.getenv("OLLAMA_TOKEN"):
        config["ollama_token"] = os.getenv("OLLAMA_TOKEN")
        config["ollama_cloud_host"] = "https://ollama.com/api"
        console.print("[green]✅ Ollama Cloud auto-configured via OLLAMA_TOKEN environment variable.[/green]")

    # 3. Set defaults for everything else
    config["self_repair"] = True
    config["model_mode"] = "auto-mixed"

    save_config(config)
    console.print("[bold green]✅ Automation Complete![/bold green]")
    _print_first_use()

def get_env_with_config(key):
    """Config lookup with env override; honors pre-rename ``CORTANA_*`` /
    ``JARVIS_*`` spellings.

    Precedence: new env var, legacy env vars (newest rename first), config
    file (which itself migrates legacy keys on load).
    """
    config = load_config()
    env_val = os.getenv(key.upper())
    if env_val:
        return env_val
    kl = key.lower()
    if kl.startswith("jade_"):
        for prefix in ("CORTANA_", "JARVIS_"):
            legacy_val = os.getenv(prefix + kl[len("jade_"):].upper())
            if legacy_val:
                return legacy_val
    return config.get(kl, "")

def is_dev_mode(config=None):
    """Dev-mode toggle for the developer's own machine.

    JADE_DEV_MODE env var wins when set to a recognized value:
    1/true/yes/on enables, 0/false/no/off disables. JARVIS_DEV_MODE is
    honored as a deprecated fallback. When no env var is set (or
    unrecognized), the `dev_mode` config key decides.
    """
    env = os.getenv("JADE_DEV_MODE", "").strip().lower()
    if not env:
        env = os.getenv("JARVIS_DEV_MODE", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    cfg = config if config is not None else load_config()
    return bool(cfg.get("dev_mode", False))

def auto_config_maintenance_once():
    """
    One-off maintenance pass:
    - Ensure required keys exist in config
    - Attempt to detect Ollama and set ollama_host
    - If Ollama down and OpenAI key present, set provider to openai (non-forced)
    - Save config if updates made
    Returns report dict.
    """
    cfg = load_config()
    changed = False
    report = {"timestamp": datetime.datetime.utcnow().isoformat(), "actions": []}

    if "api_keys" not in cfg:
        cfg["api_keys"] = {}
        changed = True
        report["actions"].append("Initialized api_keys entry.")

    if cfg.get("provider") == "ollama" or not cfg.get("ollama_host"):
        try:
            from core.services import validate_ollama
            candidates = ["http://localhost:11434", "http://127.0.0.1:11434"]
            if cfg.get("ollama_host") not in candidates:
                candidates.insert(0, cfg.get("ollama_host"))
            candidates = [c for c in candidates if c]
            for h in candidates:
                if not h: continue
                v = validate_ollama(h, timeout=1.5)
                report["actions"].append({"probe": h, "result": v})
                if v.get("ok"):
                    cfg["ollama_host"] = h
                    changed = True
                    report["actions"].append(f"Auto-set ollama_host={h}")
                    break
                if v.get("error_type") == "auth":
                    cfg["ollama_host"] = h
                    changed = True
                    report["actions"].append(f"Saved Ollama host (auth required): {h}")
                    break
        except Exception as e:
            report["actions"].append({"error": str(e)})

    if cfg.get("provider") == "ollama":
        try:
            from core.services import validate_ollama
            h = cfg.get("ollama_host")
            v = validate_ollama(h, timeout=1.0) if h else None
            if not v or not v.get("ok"):
                if cfg.get("api_keys", {}).get("openai") or os.getenv("OPENAI_API_KEY"):
                    cfg["provider"] = "openai"
                    changed = True
                    report["actions"].append("Ollama unreachable; switched provider to openai.")
        except Exception as e:
            report["actions"].append({"error": str(e)})

    if changed:
        save_config(cfg)
        report["saved"] = True
    else:
        report["saved"] = False
    return report

def start_periodic_config_maintenance(interval_hours: int = 24):
    """
    Start a background daemon thread that runs auto_config_maintenance_once every interval_hours.
    Non-blocking; returns the Thread object.
    """
    def _loop():
        while True:
            try:
                r = auto_config_maintenance_once()
                console.print(f"[dim]Auto-config maintenance run: {r.get('timestamp')} saved={r.get('saved')}[/dim]")
            except Exception as e:
                console.print(f"[red]Auto-config maintenance error: {e}[/red]")
            time.sleep(interval_hours * 3600)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
