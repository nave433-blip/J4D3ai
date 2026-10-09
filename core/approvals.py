"""Central approval plumbing for Jade's confirmation prompts.

Routine confirmations go through :func:`confirm`, a drop-in replacement for
``rich.prompt.Confirm.ask``. Auto-approve ("allow all") is an explicit,
never-by-default opt-in via any of:

- config ``"auto_approve": true`` in ``~/.jade/config.json``
- env var ``JADE_AUTO_APPROVE=1`` (deprecated fallback ``JARVIS_AUTO_APPROVE``)
- the ``--yes`` / ``-y`` CLI flag (current session only)

When enabled, routine prompts are answered "yes" automatically: a warning
banner prints once at startup and every auto-approved action is appended to
``~/.jade/logs/auto_approve.log`` (mode ``0600``).

Credential and trust decisions are never auto-approved: pass
``sensitive=True`` and the user is always asked, no matter what. The sandbox
itself is untouched — auto-approve never disables sandboxing, it only skips
the human confirmation step around routine side effects.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

console = Console()

_TRUE_VALUES = {"1", "true", "yes", "on"}

# Set by the --yes / -y CLI flag for the current session.
_session_yes = False


def set_session_yes(value: bool = True) -> None:
    """Enable (or disable) auto-approve for the current session."""
    global _session_yes
    _session_yes = bool(value)


def session_yes_enabled() -> bool:
    return _session_yes


def is_auto_approve(cfg=None) -> bool:
    """True when the user explicitly opted into auto-approve. Never on by default."""
    if _session_yes:
        return True
    if os.environ.get("JADE_AUTO_APPROVE", "").strip().lower() in _TRUE_VALUES:
        return True
    # Deprecated pre-rename fallback.
    if os.environ.get("JARVIS_AUTO_APPROVE", "").strip().lower() in _TRUE_VALUES:
        return True
    try:
        if cfg is None:
            from core.config import load_config
            cfg = load_config()
        return bool(cfg.get("auto_approve", False))
    except Exception:
        return False


def _strip_markup(text: str) -> str:
    return re.sub(r"\[[^\]]*\]", "", str(text)).strip()


def audit_log_path() -> Path:
    from core.config import CONFIG_DIR
    return CONFIG_DIR / "logs" / "auto_approve.log"


def log_auto_approved(prompt: str) -> Path:
    """Append one line per auto-approved action. Best effort; never raises."""
    path = audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} AUTO-APPROVED: {_strip_markup(prompt)}\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return path


def confirm(prompt, *, default: bool = False, sensitive: bool = False, **kwargs) -> bool:
    """Drop-in replacement for ``Confirm.ask`` with auto-approve support.

    ``sensitive=True`` marks credential/trust decisions that must always ask.
    Everything else is answered "yes" (and logged) when auto-approve is on.
    """
    if sensitive or not is_auto_approve():
        return Confirm.ask(prompt, default=default, **kwargs)
    log_auto_approved(prompt)
    console.print(f"[dim]auto-approve → yes: {_strip_markup(prompt)}[/dim]")
    return True


def startup_banner() -> bool:
    """Print the auto-approve warning banner. Returns True when shown."""
    if not is_auto_approve():
        return False
    console.print(Panel(
        "[bold yellow]⚠️ AUTO-APPROVE ON — all routine prompts will be answered yes[/bold yellow]\n"
        "[dim]Credential and trust prompts still ask. "
        "Every auto-approved action is logged.[/dim]",
        title="Dev toggle", border_style="yellow", expand=False,
    ))
    return True
