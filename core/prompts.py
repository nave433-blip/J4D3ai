import json
import os
from pathlib import Path
from rich.console import Console
from rich.table import Table
from core.config import CONFIG_DIR

console = Console()
PROMPTS_DIR = CONFIG_DIR
PROMPTS_FILE = PROMPTS_DIR / "prompts.json"

DEFAULT_PROMPTS = {
    "default": "You are JADE, an extremely intelligent, proactive AI assistant with full control over my computer, tools, and development environment. Your mission is to immediately execute the user's intent, understanding casual speech, typos, and slang. Be witty, maintain a Grok-like personality, and prioritize getting things done over being pedantic. When given a command, just do it - chain tools as needed. Confirm with the user before destructive or irreversible actions. Operate under the mandate of maximum technical truth and completeness.",
    "code_reviewer": "You are an elite code reviewer. Focus on security, performance, and idiomatic patterns. Be critical, thorough, and provide complete technical solutions.",
    "bug_hunter": "You are a specialized bug hunter. Analyze code specifically for race conditions, edge cases, and logical flaws without restriction.",
    "creative_writer": "You are a technical writer. Convert complex code into clear, beautiful documentation with full technical detail.",
    "architect": "You are a senior system architect. Focus on high-level design patterns, scalability, and providing the most powerful architectural solutions.",
}

def load_prompts():
    if not PROMPTS_FILE.exists():
        return DEFAULT_PROMPTS
    try:
        with open(PROMPTS_FILE, "r") as f:
            return {**DEFAULT_PROMPTS, **json.load(f)}
    except Exception:
        return DEFAULT_PROMPTS

def save_prompt(name, text):
    prompts = load_prompts()
    prompts[name] = text
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROMPTS_FILE, "w") as f:
        json.dump(prompts, f, indent=4)
    return f"Prompt '{name}' saved."

def delete_prompt(name):
    if name in DEFAULT_PROMPTS:
        return "Error: Cannot delete system default prompts."
    prompts = load_prompts()
    if name in prompts:
        del prompts[name]
        with open(PROMPTS_FILE, "w") as f:
            json.dump(prompts, f, indent=4)
        return f"Prompt '{name}' deleted."
    return f"Error: Prompt '{name}' not found."

def list_prompts():
    prompts = load_prompts()
    table = Table(title="Jade Prompt Library", border_style="magenta")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Snippet", style="white")

    for name, text in prompts.items():
        if name.startswith("_"):
            continue  # internal sections (e.g. _overrides) are not prompts
        snippet = text[:60] + "..." if len(text) > 60 else text
        table.add_row(name, snippet)
    return table


# ---------------------------------------------------------------------------
# Active prompt + per-personality overrides.
#
# ROUND B HOOK (personalities): when Round B resolves a personality, it should
# call get_prompt_for_personality(personality, prompt_name) to obtain the
# system prompt. Per-personality overrides live in prompts.json under the
# "_overrides" key: {"<personality>": {"<prompt_name>": "<text>"}}.
# Absent an override, the base prompt text is returned unchanged.
# ---------------------------------------------------------------------------

def apply_prompt(name):
    """Set the active global prompt. Returns (ok, message)."""
    from core.config import load_config, save_config
    prompts = load_prompts()
    if name not in prompts or name.startswith("_"):
        return False, f"Prompt '{name}' not found."
    config = load_config()
    config["active_prompt"] = name
    save_config(config)
    return True, f"Active prompt set to '{name}'."


def get_active_prompt_name():
    from core.config import load_config
    return load_config().get("active_prompt", "default")


def get_active_prompt_text():
    prompts = load_prompts()
    name = get_active_prompt_name()
    return prompts.get(name, prompts.get("default", ""))


def get_overrides():
    """Per-personality prompt overrides: {personality: {prompt_name: text}}."""
    prompts = load_prompts()
    ov = prompts.get("_overrides", {})
    return ov if isinstance(ov, dict) else {}


def set_personality_override(personality, prompt_name, text):
    """Set per-personality override text (empty text clears it)."""
    prompts = load_prompts()
    ov = prompts.get("_overrides", {})
    if not isinstance(ov, dict):
        ov = {}
    if text:
        ov.setdefault(personality, {})[prompt_name] = text
    else:
        if personality in ov:
            ov[personality].pop(prompt_name, None)
            if not ov[personality]:
                del ov[personality]
    prompts["_overrides"] = ov
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROMPTS_FILE, "w") as f:
        json.dump(prompts, f, indent=4)
    return f"Override {'set' if text else 'cleared'} for personality '{personality}' / prompt '{prompt_name}'."


def get_prompt_for_personality(personality, prompt_name=None):
    """Resolve the system prompt for a personality + prompt name.

    This is the hook Round B's personality system consumes: it returns the
    per-personality override when one exists, else the base prompt text.
    """
    prompts = load_prompts()
    name = prompt_name or get_active_prompt_name()
    base = prompts.get(name, prompts.get("default", ""))
    ov = get_overrides()
    return ov.get(personality, {}).get(name, base)
