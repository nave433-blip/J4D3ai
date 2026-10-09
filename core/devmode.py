"""Developer mode: local-only diagnostics and personal instructions.

Dev mode is a neutral mechanism for the developer's own machine. When
enabled it changes *visibility only*:

- DEBUG log level
- per-request timing plus which provider/model served each LLM call
- a clear DEV MODE banner at REPL startup
- optional personal instructions appended to the system prompt, loaded from
  ``~/.jade/dev_instructions.md`` — a file that lives outside the repo and
  is never committed

It never changes tool-execution semantics: no new capabilities, no bypassed
confirmations, no altered safety behavior. The contents of the personal
instructions file are the developer's own business; this loader never logs
or prints them.
"""

import logging
import time
from contextlib import contextmanager
from pathlib import Path

from core.config import CONFIG_DIR, is_dev_mode

logger = logging.getLogger("jade.devmode")

DEV_INSTRUCTIONS_PATH = CONFIG_DIR / "dev_instructions.md"


def dev_instructions_path() -> Path:
    """Where personal dev-mode instructions live (outside the repo)."""
    return DEV_INSTRUCTIONS_PATH


def load_dev_instructions() -> str:
    """Return the developer's personal instructions, or "" when dev mode is
    off or the file is absent. File contents are never logged."""
    if not is_dev_mode():
        return ""
    try:
        text = DEV_INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug("dev mode on; no personal instructions file at %s", DEV_INSTRUCTIONS_PATH)
        return ""
    except OSError as e:
        logger.debug("dev mode on; could not read %s: %s", DEV_INSTRUCTIONS_PATH, e)
        return ""
    logger.debug("dev mode on; loaded personal instructions (%d chars) from %s",
                 len(text), DEV_INSTRUCTIONS_PATH)
    return text


def effective_system_prompt(base: str) -> str:
    """System prompt actually sent to the model: base plus the developer's
    personal instructions when dev mode is on and the file exists."""
    if not is_dev_mode():
        return base
    extra = load_dev_instructions()
    if not extra.strip():
        return base
    return f"{base}\n\n---\nDeveloper notes (local dev mode):\n{extra}"


def print_dev_banner(console=None) -> bool:
    """Print the DEV MODE banner. Returns True when dev mode is on."""
    if not is_dev_mode():
        return False
    from rich.console import Console
    from rich.panel import Panel
    status = "loaded" if DEV_INSTRUCTIONS_PATH.exists() else "none"
    (console or Console()).print(Panel(
        "[bold yellow]\U0001f6e0 DEV MODE[/bold yellow]\n"
        "[dim]Local development build — diagnostics on. "
        f"Personal instructions: {status}.[/dim]",
        border_style="yellow",
        expand=False,
    ))
    return True


@contextmanager
def timed_request(provider: str, model: str):
    """Log provider/model/elapsed for one LLM call at DEBUG level."""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        logger.debug("llm call served by provider=%s model=%s elapsed=%.2fs",
                     provider, model, elapsed)
