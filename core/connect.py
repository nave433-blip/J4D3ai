"""Easy-connect: interactive provider setup, secure key storage, status overview.

Key storage policy
------------------
API keys are stored in the OS keyring via the ``keyring`` package
(service ``"cortana-dev"``, account ``"cortana-<provider>"``). When no keyring
backend is available, keys fall back to ``~/.cortana/keys.json`` with 0600
permissions, and a clear warning is printed. Keys are never printed, logged,
or echoed — entry is always via ``getpass`` and a key is only stored after
it passes validation.

Migration: a key found in the old config-file store (``config["api_keys"]``)
is moved into the keyring on first successful connect and removed from the
config file.
"""

import getpass
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import keyring
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from core.approvals import confirm
from rich.table import Table

from core.auth import AuthManager
from core.config import load_config, save_config
from core.services import validate_provider_connection
from core.ui import next_steps_panel

console = Console()

KEYRING_SERVICE = "jade-dev"
# Pre-rename service names: read for compatibility, never written.
_KEYRING_SERVICE_LEGACY = "cortana-dev"
_KEYRING_SERVICE_LEGACY2 = "jarvis-dev"


def _account(provider: str) -> str:
    return f"jade-{provider.lower()}"


def _account_legacy(provider: str) -> str:
    """Pre-rename account name: read for compatibility, never written."""
    return f"cortana-{provider.lower()}"


def _account_legacy2(provider: str) -> str:
    """Pre-pre-rename account name: read for compatibility, never written."""
    return f"jarvis-{provider.lower()}"


def _keyring_backend_ok() -> bool:
    """True when a real keyring backend (not the fail/no-op one) is usable."""
    try:
        from keyring.backends import fail
        return not isinstance(keyring.get_keyring(), fail.Keyring)
    except Exception:
        return False


def _fallback_keys_path() -> Path:
    """Resolved at call time (via the config module) so tests can redirect it."""
    import core.config as cfg_mod
    return cfg_mod.CONFIG_DIR / "keys.json"


_fallback_warned = False


def _warn_fallback_once() -> None:
    global _fallback_warned
    if not _fallback_warned:
        _fallback_warned = True
        console.print(
            "[bold yellow]⚠️  No OS keyring backend is available.[/bold yellow]\n"
            f"[yellow]Falling back to {_fallback_keys_path()} with 0600 permissions. "
            "This is less secure than a system keychain — consider installing a "
            "keyring backend (e.g. Secret Service on Linux, Keychain on macOS).[/yellow]"
        )


def _read_fallback_keys() -> Dict[str, str]:
    try:
        with open(_fallback_keys_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_fallback_keys(keys: Dict[str, str]) -> None:
    path = _fallback_keys_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 0600 from the start; also chmod in case the file already existed wider.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2)
    os.chmod(str(path), 0o600)


def _old_config_key(provider: str) -> Optional[str]:
    """Key living in the legacy config-file store (services API_KEYS_KEY)."""
    return load_config().get("api_keys", {}).get(provider.lower())


def _remove_old_config_key(provider: str) -> bool:
    cfg = load_config()
    keys = cfg.get("api_keys", {})
    if provider.lower() in keys:
        del keys[provider.lower()]
        cfg["api_keys"] = keys
        save_config(cfg)
        return True
    return False


def get_key_secure(provider: str) -> Optional[str]:
    """Return the stored key, or None. Never prints key material."""
    provider = provider.lower()
    if _keyring_backend_ok():
        try:
            key = keyring.get_password(KEYRING_SERVICE, _account(provider))
            if not key:
                key = keyring.get_password(
                    _KEYRING_SERVICE_LEGACY, _account_legacy(provider))
            if not key:
                key = keyring.get_password(
                    _KEYRING_SERVICE_LEGACY2, _account_legacy2(provider))
            if key:
                return key
        except Exception:
            pass
    # Fallback-file store
    key = _read_fallback_keys().get(provider)
    if key:
        return key
    # Legacy config-file store (read for compatibility; migrated on save)
    return _old_config_key(provider)


def save_key_secure(provider: str, key: str) -> Dict:
    """Store key in the keyring, else the 0600 fallback file.

    Migrates any key in the legacy config-file store into the keyring and
    removes it from the config file. Returns {"ok", "stored_in"}.
    Never prints or logs key material.
    """
    provider = provider.lower()
    if not key:
        return {"ok": False, "error": "empty key"}

    if _keyring_backend_ok():
        try:
            keyring.set_password(KEYRING_SERVICE, _account(provider), key)
            migrated = _remove_old_config_key(provider)
            # Drop any stale fallback-file copy now the keyring owns the key.
            keys = _read_fallback_keys()
            if provider in keys:
                del keys[provider]
                _write_fallback_keys(keys)
            return {"ok": True, "stored_in": "keyring", "migrated": migrated}
        except Exception as e:
            console.print(f"[yellow]Keyring write failed ({e}); using fallback file.[/yellow]")

    _warn_fallback_once()
    keys = _read_fallback_keys()
    keys[provider] = key
    _write_fallback_keys(keys)
    return {"ok": True, "stored_in": "fallback_file", "migrated": False}


def unset_key_secure(provider: str) -> Dict:
    """Remove a stored key from every store."""
    provider = provider.lower()
    for service, acct in ((KEYRING_SERVICE, _account(provider)),
                          (_KEYRING_SERVICE_LEGACY, _account_legacy(provider)),
                          (_KEYRING_SERVICE_LEGACY2, _account_legacy2(provider))):
        try:
            keyring.delete_password(service, acct)
        except Exception:
            pass
    keys = _read_fallback_keys()
    if provider in keys:
        del keys[provider]
        _write_fallback_keys(keys)
    _remove_old_config_key(provider)
    return {"ok": True, "provider": provider}


def test_connection(provider: str) -> Dict:
    """Real validation via core.services; reports the actual result.

    Never fabricates success — the returned dict is the validator's own.
    """
    provider = provider.lower()
    try:
        result = validate_provider_connection(provider)
    except Exception as e:
        result = {"ok": False, "error": str(e), "error_type": "other"}
    result = dict(result)
    result.setdefault("provider", provider)
    result.setdefault("ok", False)
    return result


def get_provider_host(provider: str) -> Optional[str]:
    return load_config().get(f"{provider.lower()}_host")


def _env_var_for(provider: str) -> Optional[str]:
    from core.services import ENV_KEY_VARS
    for var in ENV_KEY_VARS.get(provider.lower(), ()):
        if os.getenv(var):
            return var
    return None


def is_configured(provider: str) -> bool:
    """Single definition of 'connected' used by status tables, the models
    menu, and post-switch warnings. Never exposes key material."""
    provider = provider.lower()
    info = AuthManager.PROVIDERS.get(provider, {})
    if info.get("host_only"):
        return bool(get_provider_host(provider))
    return get_key_secure(provider) is not None or _env_var_for(provider) is not None


def connection_status(test: bool = False) -> List[Dict]:
    """Provider connection overview.

    Each row: {provider, display, configured, reachable, needs_attention, reason}.
    `reachable` is None when untested (no network call is made unless test=True).
    Key values are never included — callers must render "set"/"not set" only.
    """
    rows: List[Dict] = []
    for name, info in AuthManager.PROVIDERS.items():
        display = info.get("display", name)
        configured = is_configured(name)

        reachable: Optional[bool] = None
        needs_attention = False
        reason = ""
        hint = ""
        if not configured:
            needs_attention = True
            reason = "not configured"
        elif test:
            result = test_connection(name)
            reachable = bool(result.get("ok"))
            hint = str(result.get("hint", ""))
            if not reachable:
                needs_attention = True
                reason = str(result.get("error", "validation failed"))

        rows.append(
            {
                "provider": name,
                "display": display,
                "configured": configured,
                "reachable": reachable,
                "needs_attention": needs_attention,
                "reason": reason,
                "hint": hint,
            }
        )
    return rows


def render_next_steps(rows: List[Dict]) -> Optional[Panel]:
    """Actionable advice for every provider needing attention.

    Each line answers: what's wrong and the one command that fixes it.
    Returns None when everything is fine.
    """
    from core.ui import next_steps_panel

    attention = [r for r in rows if r.get("needs_attention")]
    if not attention:
        return None
    steps = []
    for r in attention:
        name = r["provider"]
        display = r.get("display", name)
        reason = r.get("reason", "")
        hint = r.get("hint", "")
        free = AuthManager.PROVIDERS.get(name, {}).get("free", "")
        if r.get("configured") and r.get("reachable") is False:
            step = (
                f"{display}: key/host saved but unreachable ({reason}). "
                f"Run `/connect`, pick {display}, and re-enter the key or host."
            )
        else:
            step = (
                f"{display}: {reason}. Run `/connect` and choose {display} to set it up."
            )
        if hint:
            step += f" {hint}"
        if free and not r.get("configured"):
            step += f" Free tier: {free}."
        steps.append(step)
    return next_steps_panel(steps, title="Needs attention")


def render_status_table(rows: List[Dict]) -> Table:
    """Rich table for connection_status() rows. Never renders key values."""
    table = Table(title="AI Provider Connections")
    table.add_column("Provider", style="cyan")
    table.add_column("Configured", justify="center")
    table.add_column("Reachable", justify="center")
    table.add_column("Attention", style="yellow")
    for r in rows:
        configured = "[green]✓[/green]" if r["configured"] else "[red]✗[/red]"
        if r["reachable"] is None:
            reachable = "[dim]—[/dim]"
        elif r["reachable"]:
            reachable = "[green]✓[/green]"
        else:
            reachable = "[red]✗[/red]"
        table.add_row(r["display"], configured, reachable, r.get("reason", ""))
    return table


# --------------------
# Interactive wizard
# --------------------

def _probe_ollama_host(host: str, timeout: float = 1.5) -> bool:
    """Lightweight probe: GET <host>/api/tags (Ollama's canonical endpoint)."""
    try:
        import requests

        r = requests.get(host.rstrip("/") + "/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _detect_ollama() -> Optional[str]:
    """Return a live Ollama host, or None. No auto-launch here (opt-in)."""
    cfg = load_config()
    candidates = [
        cfg.get("ollama_host"),
        "http://localhost:11434",
        "http://127.0.0.1:11434",
    ]
    for host in dict.fromkeys(filter(None, candidates)):
        if _probe_ollama_host(host):
            return host
    return None


def _save_host(provider: str, host: str) -> None:
    cfg = load_config()
    cfg[f"{provider.lower()}_host"] = host.rstrip("/")
    save_config(cfg)


def _setup_host_provider(name: str, info: Dict) -> None:
    display = info.get("display", name)
    console.print(Panel(f"🔗 [bold cyan]Connecting {display}[/bold cyan]", border_style="cyan"))

    if name == "ollama":
        detected = _detect_ollama()
        if detected:
            console.print(f"[green]✅ Ollama detected at {detected}[/green]")
            if confirm(f"Use Ollama at {detected}?", default=True):
                _save_host(name, detected)
                console.print(f"[green]✅ {display} connected.[/green]")
                return
        # Not detected: offer auto-repair (opt-in; may launch the Ollama app)
        from core.services import repair_ollama

        if confirm("Ollama not detected. Attempt auto-repair (may launch Ollama)?", default=False):
            report = repair_ollama(open_app_if_mac=True, prompt_for_host=False)
            if report.get("fixed"):
                console.print(f"[green]✅ Ollama repaired at {report.get('host')}.[/green]")
                return
            console.print(f"[yellow]Auto-repair did not find Ollama: {report.get('error', 'no host responded')}[/yellow]")

    default_host = get_provider_host(name) or ("http://localhost:11434" if name == "ollama" else "")
    host = Prompt.ask(f"Enter host URL for {display}", default=default_host).strip()
    if not host:
        console.print("[yellow]Skipped.[/yellow]")
        return

    result = validate_provider_connection(name, extra={"host": host})
    if result.get("ok"):
        _save_host(name, host)
        console.print(f"[green]✅ {display} connected at {host}.[/green]")
    else:
        console.print(f"[red]Could not reach {display} at {host}: {result.get('error')}[/red]")
        if confirm("Save the host anyway?", default=False, sensitive=True):
            _save_host(name, host)
            console.print(f"[yellow]Saved {display} host (unverified).[/yellow]")
        else:
            console.print("[yellow]Aborted: host not saved.[/yellow]")


def _setup_key_provider(name: str, info: Dict) -> None:
    display = info.get("display", name)
    url = info.get("url")
    console.print(Panel(f"🔗 [bold cyan]Connecting {display}[/bold cyan]", border_style="cyan"))

    if url:
        console.print(f"Get your API key here: [link={url}]{url}[/link]")
        if confirm(f"Open {display} key page in your browser?", default=False):
            from core.utils import open_url

            open_url(url)

    while True:
        try:
            key = getpass.getpass(f"Paste your {display} API key (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Aborted.[/yellow]")
            return
        if not key:
            console.print("[yellow]No key entered; aborting.[/yellow]")
            return

        console.print("[dim]Validating key...[/dim]")
        result = validate_provider_connection(name, extra={"key": key})
        if result.get("ok"):
            saved = save_key_secure(name, key)
            storage = "OS keychain" if saved.get("stored_in") == "keyring" else "secure local file"
            msg = f"[green]✅ {display} connected — key saved to {storage}.[/green]"
            if saved.get("migrated"):
                msg += "\n[dim]Migrated legacy config-file key into the keychain.[/dim]"
            console.print(msg)
            return

        console.print(f"[red]❌ Validation failed: {result.get('error')}[/red]")
        console.print("[dim]The key was NOT saved.[/dim]")
        if not confirm("Try again?", default=True):
            console.print("[yellow]Aborted.[/yellow]")
            return


def run_connect_wizard() -> None:
    """Interactive provider setup: numbered list with status dots, per-provider flow."""
    console.print(Panel(
        "🌐 [bold cyan]Account Connection Center[/bold cyan]\n"
        "[dim]Link an AI provider so CORTANA has a brain. Nothing is saved until it validates.[/dim]",
        border_style="cyan",
    ))

    providers = list(AuthManager.PROVIDERS.items())
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Status", justify="center")
    table.add_column("Provider", style="cyan")
    table.add_column("How it connects", style="dim")
    table.add_column("Free tier", style="green")

    for i, (name, info) in enumerate(providers, start=1):
        dot = "[green]●[/green]" if is_configured(name) else "[dim]○[/dim]"
        method = "local host" if info.get("host_only") else "API key"
        free = info.get("free", "—")
        table.add_row(str(i), dot, info.get("display", name), method, free)

    console.print(table)
    console.print("[dim]● connected   ○ not connected   — type [bold]b[/bold] or Ctrl+C to go back[/dim]\n")

    choices = [str(i) for i in range(1, len(providers) + 1)] + ["b"]
    choice = Prompt.ask("Step 1 of 2 — choose a provider", choices=choices, default="b")
    if choice == "b":
        console.print("[dim]Back.[/dim]")
        return

    name, info = providers[int(choice) - 1]
    console.print(f"[dim]Step 2 of 2 — setting up [bold cyan]{info.get('display', name)}[/bold cyan][/dim]")
    if info.get("host_only"):
        _setup_host_provider(name, info)
    else:
        _setup_key_provider(name, info)

    if is_configured(name):
        console.print(next_steps_panel(
            [f"Run `/connections` to verify all providers",
             f"Switch to it any time with `/models`",
             f"Then just ask: `/chat hello`"],
            title=f"{info.get('display', name)} is ready — what next?",
        ))


def connect_provider_cli(provider: str, host: Optional[str] = None,
                         key: Optional[str] = None) -> None:
    """Non-interactive entry point used by the `connect-provider` typer command.

    Delegates to the same secure storage and validation as the wizard.
    """
    provider = provider.lower()
    info = AuthManager.PROVIDERS.get(provider)
    if not info:
        supported = ", ".join(AuthManager.PROVIDERS.keys())
        console.print(f"[red]Unknown provider '{provider}'. Supported: {supported}[/red]")
        return

    display = info.get("display", provider)

    if info.get("host_only"):
        if not host:
            host = Prompt.ask(f"Enter host URL for {display}", default="").strip()
        if not host:
            console.print("[yellow]No host provided; aborting.[/yellow]")
            return
        result = validate_provider_connection(provider, extra={"host": host})
        _save_host(provider, host)
        if result.get("ok"):
            console.print(f"[green]✅ {display} connected at {host}.[/green]")
        else:
            console.print(f"[yellow]⚠️ Saved {provider}_host = {host}, but validation failed: "
                          f"{result.get('error')}[/yellow]")
        return

    if key:
        console.print("[dim]Note: passing a key on the command line may expose it "
                      "in shell history; interactive entry is preferred.[/dim]")
    else:
        try:
            key = getpass.getpass(f"Enter API key for {display} (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Aborted.[/yellow]")
            return
    if not key:
        console.print("[yellow]No key entered; aborting.[/yellow]")
        return

    result = validate_provider_connection(provider, extra={"key": key})
    if not result.get("ok"):
        console.print(f"[red]❌ Validation failed: {result.get('error')}[/red]")
        console.print("[dim]The key was NOT saved.[/dim]")
        return
    saved = save_key_secure(provider, key)
    storage = "OS keychain" if saved.get("stored_in") == "keyring" else "secure local file"
    console.print(f"[green]✅ API key for {display} validated and saved to {storage}.[/green]")
