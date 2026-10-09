"""cortana_core — the importable Cortana library, decoupled from the CLI.

This package is Cortana's **stable public API** for embedding: everything
below is importable without the Typer CLI, the Rich UI, or any interactive
prompts. Imports are lazy (PEP 562), so ``import cortana_core`` stays fast
and never pulls in heavy provider deps until you actually call something.

Stable API
----------
- ``think(context, task, model=None) -> str``
    One-shot reasoning: returns the assistant's reply text.
- ``think_structured(context, task, model=None) -> dict``
    Same, but the raw result ``{"ok", "text", "provider", "error"?}``.
- ``hive_ask(question, providers=None, timeout=90.0, use_cache=True,
  max_models=5) -> dict``
    Ask several configured providers and synthesize a consensus answer.
- ``load_config() -> dict`` / ``save_config(config) -> None``
    Read/write Cortana's JSON config (``~/.cortana/config.json``).
- ``run_sandboxed(command, timeout=30, ...) -> SandboxResult``
    Run a shell command in an isolated Linux sandbox (bubblewrap when
    available). See ``tools.sandbox`` for the exact isolation guarantees.
- ``confirm(prompt, *, default=False, sensitive=False) -> bool``
    Approval gate that honors auto-approve mode; ``sensitive=True`` always
    asks the human.

Quickstart
----------
>>> import cortana_core
>>> cortana_core.load_config()["provider"]
'ollama'
>>> reply = cortana_core.think("greeter", "Say hello in one short line.")

The REST equivalent of this API is served by ``core.apiserver``
(OpenAI-compatible ``/v1/chat/completions``); the VS Code driver in
``extensions/vscode/`` talks to that server.
"""

from __future__ import annotations

__all__ = [
    "think",
    "think_structured",
    "hive_ask",
    "load_config",
    "save_config",
    "run_sandboxed",
    "confirm",
]

_LAZY = {
    "think": ("core.brain", "think"),
    "think_structured": ("core.brain", "think_structured"),
    "hive_ask": ("core.hive", "hive_ask"),
    "load_config": ("core.config", "load_config"),
    "save_config": ("core.config", "save_config"),
    "run_sandboxed": ("tools.sandbox", "run_sandboxed"),
    "confirm": ("core.approvals", "confirm"),
}


def __getattr__(name: str):
    if name in _LAZY:
        module_name, attr = _LAZY[name]
        import importlib

        value = getattr(importlib.import_module(module_name), attr)
        globals()[name] = value  # cache after first lookup
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals().keys()) + __all__)
