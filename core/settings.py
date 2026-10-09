"""Maximal typed settings schema for Jade.

The schema is the union of what major competitors expose (ChatGPT, Claude,
Copilot, Gemini, Perplexity): appearance, chat, models, privacy,
notifications, hotkeys, voice, network, sandbox, data, memory, connectors.

Backed by ``~/.jade/config.json`` through :mod:`core.config`.
Profile overrides (:mod:`core.profiles`) layer on top via :func:`get_setting`.

``live: True`` marks a setting that takes effect without a restart
(it is read fresh wherever it is used). Everything else applies on the
next launch of the affected component.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from core.config import load_config, save_config
from core.approvals import confirm
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

console = Console()

# key, type, default, category, description, live, extras
SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    # -- appearance -------------------------------------------------------
    {"key": "theme", "type": "choice", "choices": ["dark", "light", "auto"],
     "default": "dark", "category": "appearance", "live": True,
     "description": "Color theme for the CLI, dashboard and desktop UI."},
    {"key": "compact_mode", "type": "bool", "default": False,
     "category": "appearance", "live": True,
     "description": "Compact output: shorter banners, denser tables."},
    {"key": "show_banner", "type": "bool", "default": True,
     "category": "appearance", "live": True,
     "description": "Show the Jade banner on startup."},
    {"key": "easter_eggs", "type": "bool", "default": True,
     "category": "appearance", "live": True,
     "description": "Show tasteful Halo / retro-computing easter eggs."},
    # -- chat -------------------------------------------------------------
    {"key": "chat_streaming", "type": "bool", "default": True,
     "category": "chat", "live": True,
     "description": "Stream model responses token-by-token instead of waiting."},
    {"key": "enter_to_send", "type": "bool", "default": True,
     "category": "chat", "live": True,
     "description": "Enter sends the message (vs. multiline editing)."},
    {"key": "show_token_counts", "type": "bool", "default": False,
     "category": "chat", "live": True,
     "description": "Show token usage after each response."},
    {"key": "confirm_destructive", "type": "bool", "default": True,
     "category": "chat", "live": True,
     "description": "Always confirm before destructive or irreversible actions. "
                    "Strongly recommended to leave on."},
    {"key": "chat_history_limit", "type": "int", "default": 200, "min": 0,
     "max": 10000, "category": "chat", "live": True,
     "description": "Max chat turns kept per conversation (0 = unlimited)."},
    # -- models -----------------------------------------------------------
    {"key": "model_chat", "type": "str", "default": "",
     "category": "models", "live": True,
     "description": "Default model for chat (empty = follow jade_model)."},
    {"key": "model_code", "type": "str", "default": "",
     "category": "models", "live": True,
     "description": "Default model for coding tasks (empty = follow jade_model)."},
    {"key": "model_reasoning", "type": "str", "default": "",
     "category": "models", "live": True,
     "description": "Default model for deep reasoning (empty = follow jade_model)."},
    {"key": "model_routing", "type": "bool", "default": False,
     "category": "models", "live": True,
     "description": "Auto-route tasks to the best model for the job."},
    # -- privacy ----------------------------------------------------------
    {"key": "telemetry", "type": "bool", "default": False,
     "category": "privacy", "live": True,
     "description": "Anonymous usage telemetry. Off by default; nothing leaves "
                    "your machine unless you enable it."},
    {"key": "p2p_discovery", "type": "bool", "default": True,
     "category": "privacy", "live": True,
     "description": "Allow local-network discovery of this Jade node."},
    {"key": "p2p_enabled", "type": "bool", "default": True,
     "category": "privacy", "live": False,
     "description": "Enable the P2P server entirely (restart the server to apply)."},
    # -- notifications ----------------------------------------------------
    {"key": "notify_on_long_task", "type": "bool", "default": True,
     "category": "notifications", "live": True,
     "description": "Desktop notification when a long task finishes."},
    {"key": "notify_sound", "type": "bool", "default": False,
     "category": "notifications", "live": True,
     "description": "Play a sound with notifications."},
    # -- hotkeys ----------------------------------------------------------
    {"key": "overlay_hotkey", "type": "str", "default": "ctrl+alt+c",
     "category": "hotkeys", "live": False,
     "description": "Global hotkey that summons the overlay (desktop app)."},
    {"key": "push_to_talk_key", "type": "str", "default": "ctrl+shift+space",
     "category": "hotkeys", "live": False,
     "description": "Push-to-talk key for voice input (desktop app)."},
    # -- voice ------------------------------------------------------------
    {"key": "voice_enabled", "type": "bool", "default": False,
     "category": "voice", "live": True,
     "description": "Enable voice input/output (opt-in)."},
    {"key": "wake_word_enabled", "type": "bool", "default": False,
     "category": "voice", "live": True,
     "description": "\"Hey Jade\" hotword detection (opt-in; mic stays local)."},
    {"key": "voice_profile", "type": "str", "default": "jade-default",
     "category": "voice", "live": True,
     "description": "TTS voice profile name (see /voice to list)."},
    {"key": "stt_engine", "type": "str", "default": "auto",
     "category": "voice", "live": True,
     "description": "Speech-to-text engine: auto, system, or a plugin name."},
    {"key": "tts_engine", "type": "str", "default": "auto",
     "category": "voice", "live": True,
     "description": "Text-to-speech engine: auto, system, or a plugin name."},
    # -- network ----------------------------------------------------------
    {"key": "dashboard_port", "type": "int", "default": 0, "min": 0,
     "max": 65535, "category": "network", "live": False,
     "description": "Dashboard port (0 = random free port)."},
    {"key": "proxy_url", "type": "str", "default": "",
     "category": "network", "live": True,
     "description": "HTTP(S) proxy URL (empty = none)."},
    # -- sandbox ----------------------------------------------------------
    {"key": "sandbox_enabled", "type": "bool", "default": True,
     "category": "sandbox", "live": True,
     "description": "Run generated code in the resource-limited sandbox."},
    {"key": "sandbox_network", "type": "bool", "default": False,
     "category": "sandbox", "live": True,
     "description": "Allow network access inside the sandbox (risky)."},
    # -- data -------------------------------------------------------------
    {"key": "retention_days", "type": "int", "default": 0, "min": 0,
     "max": 3650, "category": "data", "live": True,
     "description": "Auto-prune chats/logs older than N days (0 = keep forever)."},
    {"key": "auto_backup", "type": "bool", "default": False,
     "category": "data", "live": True,
     "description": "Automatically back up config, prompts and sims daily."},
    # -- memory -----------------------------------------------------------
    {"key": "memory_enabled", "type": "bool", "default": True,
     "category": "memory", "live": True,
     "description": "Enable long-term memory (vector store + cores)."},
    {"key": "memory_auto_capture", "type": "bool", "default": False,
     "category": "memory", "live": True,
     "description": "Auto-capture facts from conversations (opt-in; off by default)."},
    # -- connectors -------------------------------------------------------
    {"key": "connector_auto_refresh", "type": "bool", "default": True,
     "category": "connectors", "live": True,
     "description": "Silently refresh OAuth tokens before they expire."},
    {"key": "ms_client_id", "type": "str", "default": "",
     "category": "connectors", "live": True,
     "description": "Azure app (client) ID for Outlook connector + Microsoft sign-in. "
                    "Register at https://portal.azure.com."},
    {"key": "apple_client_id", "type": "str", "default": "",
     "category": "connectors", "live": True,
     "description": "Apple Services ID for Apple sign-in. Register at https://developer.apple.com."},
    {"key": "apple_client_secret", "type": "str", "default": "",
     "category": "connectors", "live": True,
     "description": "Apple client secret (JWT). Stored in config; prefer keyring via /signin."},
    {"key": "github_client_id", "type": "str", "default": "",
     "category": "connectors", "live": True,
     "description": "GitHub OAuth App client ID (device flow needs no secret)."},
]

# Keys that must never be flipped silently by another feature.
SENSITIVE_KEYS = {"confirm_destructive", "sandbox_enabled", "telemetry"}


def get_schema() -> List[Dict[str, Any]]:
    """Return a deep copy of the settings schema."""
    return copy.deepcopy(SETTINGS_SCHEMA)


def get_categories() -> List[str]:
    seen = []
    for entry in SETTINGS_SCHEMA:
        if entry["category"] not in seen:
            seen.append(entry["category"])
    return seen


def _find(key: str) -> Optional[Dict[str, Any]]:
    for entry in SETTINGS_SCHEMA:
        if entry["key"] == key:
            return entry
    return None


def coerce(entry: Dict[str, Any], value: Any) -> Any:
    """Coerce/validate a raw value for a schema entry. Raises ValueError."""
    t = entry["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"'{value}' is not a boolean (use true/false)")
    if t == "int":
        try:
            iv = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"'{value}' is not an integer")
        if "min" in entry and iv < entry["min"]:
            raise ValueError(f"must be >= {entry['min']}")
        if "max" in entry and iv > entry["max"]:
            raise ValueError(f"must be <= {entry['max']}")
        return iv
    if t == "choice":
        s = str(value).strip()
        if s not in entry["choices"]:
            raise ValueError(f"must be one of: {', '.join(entry['choices'])}")
        return s
    # str
    return str(value)


def get_setting(key: str, config: Optional[Dict[str, Any]] = None) -> Any:
    """Effective value for a setting: profile overrides win over global config.

    Raises KeyError for unknown keys.
    """
    entry = _find(key)
    if entry is None:
        raise KeyError(f"Unknown setting: {key}")
    cfg = config if config is not None else load_config()
    try:
        from core.profiles import profile_override
        override = profile_override(key)
    except Exception:
        override = None
    if override is not None:
        return coerce(entry, override)
    if key in cfg:
        return coerce(entry, cfg[key])
    return entry["default"]


def set_setting(key: str, value: Any, _confirm_sensitive: bool = True) -> Dict[str, Any]:
    """Validate, coerce and persist a setting. Returns {"ok", ...} dict."""
    entry = _find(key)
    if entry is None:
        return {"ok": False, "error": f"Unknown setting '{key}'. "
                "Run `jade settings list` to see all settings."}
    try:
        coerced = coerce(entry, value)
    except ValueError as e:
        return {"ok": False, "error": f"Invalid value for '{key}': {e}"}
    if key in SENSITIVE_KEYS and _confirm_sensitive:
        old = get_setting(key)
        if not confirm(f"Change sensitive setting '{key}' from {old!r} to {coerced!r}?"):
            return {"ok": False, "error": "cancelled"}
    cfg = load_config()
    cfg[key] = coerced
    save_config(cfg)
    live_note = " (live — applied immediately)" if entry.get("live") else " (applies on next launch of the affected component)"
    return {"ok": True, "key": key, "value": coerced, "message": f"✅ {key} = {coerced!r}{live_note}"}


def reset_setting(key: str) -> Dict[str, Any]:
    entry = _find(key)
    if entry is None:
        return {"ok": False, "error": f"Unknown setting '{key}'."}
    cfg = load_config()
    cfg[key] = entry["default"]
    save_config(cfg)
    return {"ok": True, "key": key, "value": entry["default"]}


def export_settings() -> Dict[str, Any]:
    """All effective settings as a plain dict (for dashboard / backup)."""
    return {entry["key"]: get_setting(entry["key"]) for entry in SETTINGS_SCHEMA}


def settings_table(category: Optional[str] = None) -> Table:
    table = Table(title="Jade Settings", border_style="cyan")
    table.add_column("Setting", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")
    table.add_column("Live", style="dim", no_wrap=True)
    table.add_column("Description", style="dim")
    for entry in SETTINGS_SCHEMA:
        if category and entry["category"] != category:
            continue
        try:
            val = get_setting(entry["key"])
        except Exception:
            val = entry["default"]
        if isinstance(val, bool):
            val = "✅ on" if val else "❌ off"
        table.add_row(entry["key"], str(val),
                      "⚡" if entry.get("live") else "",
                      f"[{entry['category']}] {entry['description']}")
    return table


def settings_menu(category: Optional[str] = None) -> None:
    """Interactive settings browser/editor."""
    while True:
        console.print(settings_table(category))
        console.print("\n[bold white]Options:[/bold white] [s]et  [r]eset  [c]ategory  [b]ack")
        choice = Prompt.ask("Action", choices=["s", "r", "c", "b"], default="b")
        if choice == "b":
            break
        if choice == "c":
            cats = get_categories()
            console.print("Categories: " + ", ".join(cats))
            picked = Prompt.ask("Category (empty = all)", default="").strip()
            category = picked if picked in cats else None
            continue
        key = Prompt.ask("Setting key").strip()
        if _find(key) is None:
            console.print(f"[red]Unknown setting '{key}'.[/red]")
            continue
        if choice == "r":
            res = reset_setting(key)
            console.print(f"[green]✅ {key} reset to default ({res['value']!r}).[/green]" if res["ok"] else f"[red]{res['error']}[/red]")
        else:
            entry = _find(key)
            hint = ""
            if entry["type"] == "choice":
                hint = f" [{'/'.join(entry['choices'])}]"
            elif entry["type"] == "bool":
                hint = " [true/false]"
            raw = Prompt.ask(f"New value for {key}{hint}")
            res = set_setting(key, raw)
            console.print(f"[green]{res['message']}[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")


# ---------------------------------------------------------------------------
# Defaults mirror for core.config.DEFAULT_CONFIG (kept in sync by tests).
# ---------------------------------------------------------------------------
def schema_defaults() -> Dict[str, Any]:
    return {entry["key"]: entry["default"] for entry in SETTINGS_SCHEMA}
