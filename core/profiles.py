"""Named profiles: work, personal, etc.

Each profile holds a *settings override subset* plus an identity block and
its own memory scope. The global config in ``~/.jade/config.json`` stays
the source of truth; the active profile's overrides layer on top whenever
:func:`core.settings.get_setting` is used.

Profiles live in ``~/.jade/profiles/<name>.json``. The built-in
``default`` profile is implicit (no file needed).

GUI hook (for the desktop round): ``list_profiles()``,
``get_active_profile()`` and ``switch_profile(name)`` are the whole API.
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import CONFIG_DIR, load_config, save_config
from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.table import Table

console = Console()

PROFILES_DIR = CONFIG_DIR / "profiles"
DEFAULT_PROFILE = "default"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _-]{0,31}$")


def _profiles_dir() -> Path:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    return PROFILES_DIR


def _profile_path(name: str) -> Path:
    return _profiles_dir() / f"{name}.json"


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def list_profiles() -> List[Dict[str, Any]]:
    """All profiles, with the active one flagged. ``default`` always exists."""
    profiles = [{"name": DEFAULT_PROFILE, "builtin": True,
                 "description": "Built-in profile (global settings as-is)."}]
    if PROFILES_DIR.is_dir():
        for path in sorted(PROFILES_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except Exception:
                continue
            profiles.append({
                "name": data.get("name", path.stem),
                "builtin": False,
                "description": data.get("description", ""),
                "overrides": len(data.get("settings", {})),
            })
    active = get_active_profile()
    for p in profiles:
        p["active"] = (p["name"] == active)
    return profiles


def get_active_profile() -> str:
    return load_config().get("active_profile", DEFAULT_PROFILE) or DEFAULT_PROFILE


def load_profile(name: str) -> Dict[str, Any]:
    """Raw profile document. ``default`` returns an empty override set."""
    if name == DEFAULT_PROFILE:
        return {"name": DEFAULT_PROFILE, "settings": {}, "identity": {}}
    path = _profile_path(name)
    if not path.exists():
        raise KeyError(f"Profile '{name}' does not exist.")
    return json.loads(path.read_text())


def profile_override(key: str) -> Optional[Any]:
    """Override value for *key* from the active profile, or None.

    Consumed by :func:`core.settings.get_setting`. Only keys present in the
    settings schema may be overridden.
    """
    active = get_active_profile()
    if active == DEFAULT_PROFILE:
        return None
    try:
        from core.settings import _find as _schema_find
    except Exception:
        return None
    try:
        data = load_profile(active)
    except (KeyError, OSError, ValueError):
        return None
    overrides = data.get("settings", {})
    if key not in overrides or _schema_find(key) is None:
        return None
    return overrides[key]


def create_profile(name: str, description: str = "",
                   settings: Optional[Dict[str, Any]] = None,
                   identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not valid_name(name):
        return {"ok": False, "error": "Name must be 1-32 chars: letters, digits, space, _ or -."}
    if name == DEFAULT_PROFILE:
        return {"ok": False, "error": "'default' is built-in and cannot be created."}
    path = _profile_path(name)
    if path.exists():
        return {"ok": False, "error": f"Profile '{name}' already exists."}
    # Validate override keys against the settings schema.
    from core.settings import _find as _schema_find, coerce
    clean: Dict[str, Any] = {}
    for k, v in (settings or {}).items():
        entry = _schema_find(k)
        if entry is None:
            return {"ok": False, "error": f"'{k}' is not a known setting."}
        try:
            clean[k] = coerce(entry, v)
        except ValueError as e:
            return {"ok": False, "error": f"Invalid value for '{k}': {e}"}
    doc = {"name": name, "description": description, "settings": clean,
           "identity": identity or {}, "created": time.time()}
    path.write_text(json.dumps(doc, indent=2))
    return {"ok": True, "name": name}


def delete_profile(name: str, _confirmed: bool = False) -> Dict[str, Any]:
    if name == DEFAULT_PROFILE:
        return {"ok": False, "error": "The built-in 'default' profile cannot be deleted."}
    path = _profile_path(name)
    if not path.exists():
        return {"ok": False, "error": f"Profile '{name}' does not exist."}
    if not _confirmed:
        from core.approvals import confirm
        if not confirm(f"Delete profile '{name}' and its overrides?"):
            return {"ok": False, "error": "cancelled"}
    if get_active_profile() == name:
        cfg = load_config(); cfg["active_profile"] = DEFAULT_PROFILE; save_config(cfg)
    path.unlink()
    return {"ok": True, "name": name}


def switch_profile(name: str) -> Dict[str, Any]:
    if name != DEFAULT_PROFILE and not _profile_path(name).exists():
        return {"ok": False, "error": f"Profile '{name}' does not exist."}
    cfg = load_config()
    cfg["active_profile"] = name
    save_config(cfg)
    return {"ok": True, "name": name}


def set_profile_setting(name: str, key: str, value: Any) -> Dict[str, Any]:
    """Set one settings override inside a profile."""
    from core.settings import _find as _schema_find, coerce
    entry = _schema_find(key)
    if entry is None:
        return {"ok": False, "error": f"Unknown setting '{key}'."}
    if name == DEFAULT_PROFILE:
        return {"ok": False, "error": "The 'default' profile has no overrides — use `jade settings set` instead."}
    try:
        doc = load_profile(name)
    except KeyError as e:
        return {"ok": False, "error": str(e)}
    try:
        coerced = coerce(entry, value)
    except ValueError as e:
        return {"ok": False, "error": f"Invalid value for '{key}': {e}"}
    doc["settings"][key] = coerced
    _profile_path(name).write_text(json.dumps(doc, indent=2))
    return {"ok": True, "name": name, "key": key, "value": coerced}


def memory_scope() -> str:
    """Memory namespace for the active profile (keeps work/personal apart)."""
    return f"profile:{get_active_profile()}"


def profiles_table() -> Table:
    table = Table(title="Jade Profiles", border_style="cyan")
    table.add_column("Profile", style="cyan", no_wrap=True)
    table.add_column("Active", style="green")
    table.add_column("Overrides", style="white")
    table.add_column("Description", style="dim")
    for p in list_profiles():
        table.add_row(p["name"], "●" if p["active"] else "",
                      str(p.get("overrides", "—")), p.get("description", ""))
    return table


def profiles_menu() -> None:
    while True:
        console.print(profiles_table())
        console.print("\n[bold white]Options:[/bold white] [n]ew  [s]witch  [e]dit override  [d]elete  [b]ack")
        choice = Prompt.ask("Action", choices=["n", "s", "e", "d", "b"], default="b")
        if choice == "b":
            break
        if choice == "n":
            name = Prompt.ask("Profile name (e.g. work)").strip()
            desc = Prompt.ask("Description", default="").strip()
            res = create_profile(name, description=desc)
            console.print(f"[green]✅ Profile '{name}' created.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
        elif choice == "s":
            name = Prompt.ask("Switch to profile").strip()
            res = switch_profile(name)
            console.print(f"[green]✅ Now using profile '{name}'.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
        elif choice == "e":
            name = Prompt.ask("Profile", default=get_active_profile()).strip()
            key = Prompt.ask("Setting key").strip()
            val = Prompt.ask(f"Override value for {key} (empty clears)")
            if not val:
                try:
                    doc = load_profile(name); doc["settings"].pop(key, None)
                    _profile_path(name).write_text(json.dumps(doc, indent=2))
                    console.print(f"[green]✅ Override cleared.[/green]")
                except Exception as e:
                    console.print(f"[red]❌ {e}[/red]")
            else:
                res = set_profile_setting(name, key, val)
                console.print(f"[green]✅ {name}.{key} = {res['value']!r}[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
        elif choice == "d":
            name = Prompt.ask("Delete profile").strip()
            res = delete_profile(name)
            console.print(f"[green]✅ Profile '{name}' deleted.[/green]" if res["ok"] else f"[red]❌ {res['error']}[/red]")
