import subprocess
from rich.console import Console

console = Console()

def copilot_suggest(query):
    """
    Get command suggestions from GitHub Copilot CLI.
    """
    try:
        # Note: 'gh copilot suggest' is interactive by default.
        # We wrap it to get the best technical advice for the CLI.
        cmd = ["gh", "copilot", "suggest", "-t", "shell", query]
        console.print(f"[bold cyan]🔍 Querying GitHub Copilot CLI for suggestion...[/bold cyan]")

        # We run it and let the user interact with the shell if needed
        # but for JADE tool use, we'll try to get the explanation if possible.
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.stdout if res.stdout else "Copilot suggested a command (check terminal)."
    except Exception as e:
        return f"Copilot Suggest Error: {e}"

def copilot_explain(command):
    """
    Explain a command using GitHub Copilot CLI.
    """
    try:
        cmd = ["gh", "copilot", "explain", command]
        console.print(f"[bold cyan]💡 Asking Copilot to explain: {command}[/bold cyan]")
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.stdout or "Copilot provided an explanation (check terminal)."
    except Exception as e:
        return f"Copilot Explain Error: {e}"
