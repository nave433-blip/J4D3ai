import subprocess
import shutil
import os
from rich.console import Console
from rich.prompt import Confirm
from core.approvals import confirm

console = Console()

TOOL_REGISTRY = {
    "claude-desktop": {"cmd": None, "install": None, "gui_app": ("Claude", "claude-desktop")},
    "claude": {"cmd": "claude", "install": "npm install -g @anthropic-ai/claude-code@latest"},
    "openclaw": {"cmd": "openclaw", "install": "npm install -g openclaw@latest"},
    "hermes": {"cmd": "hermes", "install": "curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash"},
    "opencode": {"cmd": "opencode", "install": "npm install -g opencode-ai"},
    "codex": {"cmd": "codex", "install": "npm install -g @openai/codex"},
    "copilot": {"cmd": "copilot", "install": "npm install -g @github/copilot"},
    "droid": {"cmd": "droid", "install": "npm install -g droid"},
    "pi": {"cmd": "pi", "install": "npm install -g @earendil-works/pi-coding-agent"},
    "pool": {"cmd": "pool", "install": "curl -fsSL https://downloads.poolside.ai/pool/install.sh | sh"},
    "aider": {"cmd": "aider", "install": "pip install aider-chat"},
    "interpreter": {"cmd": "interpreter", "install": "pip install open-interpreter"},
    "gpt-engineer": {"cmd": "gpt-engineer", "install": "pip install gpt-engineer"},
    "mentat": {"cmd": "mentat", "install": "pip install mentat"},
}

def is_tool_installed(cmd):
    return shutil.which(cmd) is not None

def _launch_gui_app(darwin_app_name: str, linux_bin: str) -> str:
    """Launch a GUI application via the platform's native mechanism."""
    import sys
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["open", "-a", darwin_app_name],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Launching {darwin_app_name}..."
        except Exception as e:
            return f"Error launching {darwin_app_name}: {e}"
    elif sys.platform == "linux":
        if shutil.which(linux_bin):
            subprocess.Popen([linux_bin],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Launching {linux_bin}..."
        return (f"Error: '{linux_bin}' not found. "
                f"Claude Desktop has no official Linux build.")
    return f"Error: GUI launch is not supported on {sys.platform}."

def launch_tool(tool_name):
    """
    Launch specialized AI tools via direct CLI or Ollama Cloud.
    If not installed, offers to install automatically.
    """
    if tool_name not in TOOL_REGISTRY:
        return f"Error: '{tool_name}' is not a recognized launcher command."

    info = TOOL_REGISTRY[tool_name]
    cmd = info["cmd"]
    install_cmd = info["install"]

    # GUI apps (e.g. Claude Desktop) have no CLI; use the native launcher
    if info.get("gui_app"):
        darwin_app, linux_bin = info["gui_app"]
        return _launch_gui_app(darwin_app, linux_bin)

    if not is_tool_installed(cmd):
        console.print(f"[bold yellow]⚠️ {tool_name.title()} is not installed.[/bold yellow]")
        if install_cmd and confirm(f"Would you like to install {tool_name} now?"):
            console.print(f"[bold cyan]Executing:[/bold cyan] {install_cmd}")
            os.system(install_cmd)
            # Re-check after install
            if not is_tool_installed(cmd):
                return f"Failed to install {tool_name} automatically. Please try manual install."
            console.print(f"[bold green]✅ {tool_name} installed successfully![/bold green]")
        else:
            return f"Aborted: {tool_name} is required for this action."

    console.print(f"[bold cyan]🚀 Launching {tool_name.title()}...[/bold cyan]")
    
    try:
        # Using Popen to not block the main JADE thread.
        # argv list (no shell): cmd is a plain binary name from the registry.
        subprocess.Popen([cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Successfully initiated launch for {tool_name}."
    except Exception as e:
        return f"Launch failed: {e}"
