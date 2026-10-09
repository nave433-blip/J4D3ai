"""Detection + explicit opt-in installation of third-party agent CLIs.

Design rules (see startup auto-install redesign):
- Detection (`check_agents`) is pure: no side effects, no network, no installs.
- Installation NEVER happens without explicit per-item user approval.
- "Don't ask again" choices persist in the Jade config under
  "agent_install_declined" so declined agents stay silent on later startups.
- Non-interactive callers pass no prompter (or auto-mode) and get a
  report of what is missing without any install being attempted.
"""
import subprocess
import shutil
from typing import Callable, Dict, List, Optional

from rich.console import Console
from rich.prompt import Prompt

console = Console()

# NOTE: install commands use each tool's real distribution channel
# (npm / curl / gh extension) — not PyPI.
AGENT_REGISTRY = {
    "Hermes": {
        "install": "curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash",
        "check": "hermes --version",
    },
    "OpenClaw": {
        "install": "npm install -g openclaw@latest",
        "check": "openclaw --version",
    },
    "OpenCode": {
        "install": "npm install -g opencode-ai",
        "check": "opencode --version",
    },
    "Codex": {
        "install": "npm install -g @openai/codex",
        "check": "codex --version",
    },
    "Copilot CLI": {
        "install": "gh extension install github/gh-copilot",
        "check": "gh copilot --version",
    },
    "Droid": {
        "install": "npm install -g droid",
        "check": "droid --version",
    },
    "Pi": {
        "install": "npm install -g @earendil-works/pi-coding-agent",
        "check": "pi --version",
    },
    "Pool": {
        "install": "curl -fsSL https://downloads.poolside.ai/pool/install.sh | sh",
        "check": "pool --version",
    },
}

DECLINED_CONFIG_KEY = "agent_install_declined"


def check_agents() -> List[Dict]:
    """Detect which registered agent CLIs are present. Pure: no side effects."""
    results = []
    for name, cmds in AGENT_REGISTRY.items():
        binary = cmds["check"].split()[0]
        results.append(
            {
                "name": name,
                "installed": bool(shutil.which(binary)),
                "check": cmds["check"],
                "install": cmds["install"],
            }
        )
    return results


def install_agent(name: str, runner: Optional[Callable[[str], None]] = None) -> bool:
    """Install a single registered agent by name. Returns True on success.

    Only call after explicit user approval — this runs the registry's
    install command (npm / curl|bash / gh extension).
    """
    cmds = AGENT_REGISTRY.get(name)
    if not cmds:
        console.print(f"[red]Unknown agent '{name}'.[/red]")
        return False
    run = runner or (lambda cmd: subprocess.check_call(cmd, shell=True))
    try:
        run(cmds["install"])
        console.print(f"[green]✅ Agent '{name}' installed.[/green]")
        return True
    except Exception as e:
        console.print(f"[red]❌ Failed to install agent '{name}': {e}[/red]")
        return False


def _default_prompter(prompt_text: str) -> str:
    return Prompt.ask(
        prompt_text, choices=["y", "n", "never"], default="n", show_choices=True
    )


def prompt_and_install_agents(
    statuses: Optional[List[Dict]] = None,
    config: Optional[Dict] = None,
    save_config_fn: Optional[Callable[[Dict], None]] = None,
    prompter: Optional[Callable[[str], str]] = None,
    runner: Optional[Callable[[str], None]] = None,
) -> Dict[str, List[str]]:
    """Walk missing agents with per-item opt-in. Never installs silently.

    Each missing agent offers: y = install now, n = skip this time,
    never = don't ask about this agent again (persisted in config when a
    config dict + save function are provided).

    Returns a report: {"installed", "failed", "skipped", "declined",
    "already_declined"}.
    """
    if statuses is None:
        statuses = check_agents()
    ask = prompter or _default_prompter
    declined = set((config or {}).get(DECLINED_CONFIG_KEY, []) or [])
    report: Dict[str, List[str]] = {
        "installed": [],
        "failed": [],
        "skipped": [],
        "declined": [],
        "already_declined": [],
    }

    for st in statuses:
        name = st["name"]
        if st["installed"]:
            continue
        if name in declined:
            report["already_declined"].append(name)
            continue
        console.print(f"[yellow]⚠️ Agent '{name}' not found.[/yellow]")
        console.print(f"[dim]Would run: {st['install']}[/dim]")
        answer = ""
        while answer not in ("y", "n", "never"):
            answer = (ask(f"Install '{name}' now? [y/n/never]") or "n").strip().lower()
        if answer == "y":
            if install_agent(name, runner=runner):
                report["installed"].append(name)
            else:
                report["failed"].append(name)
        elif answer == "never":
            declined.add(name)
            report["declined"].append(name)
        else:
            report["skipped"].append(name)

    if report["declined"] and config is not None and save_config_fn is not None:
        config[DECLINED_CONFIG_KEY] = sorted(declined)
        try:
            save_config_fn(config)
        except Exception as e:
            console.print(f"[red]Could not persist declined installs: {e}[/red]")

    return report
