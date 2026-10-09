import subprocess
import json
from rich.console import Console

console = Console()

def query_ollama_cloud(prompt: str) -> str:
    """Queries Ollama Cloud via ModelManager logic."""
    from core.brain import ModelManager
    mgr = ModelManager()
    mgr.current_model = "ollama/llama3-cloud" # Force cloud trigger
    return mgr.chat(prompt)

def query_gemini_cli(prompt: str) -> str:
    """Executes a prompt via Gemini CLI (assuming 'gemini' executable exists)."""
    try:
        cmd = ["gemini", "generate", prompt]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return result.stdout if result.returncode == 0 else ""
    except Exception as e:
        console.print(f"[dim]Gemini CLI failed: {e}[/dim]")
        return ""

def query_ollama_cli(prompt: str, model: str = "llama3") -> str:
    """Executes a prompt via the Ollama CLI (assuming 'ollama' executable exists)."""
    try:
        cmd = ["ollama", "run", model, prompt]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return result.stdout if result.returncode == 0 else ""
    except Exception as e:
        console.print(f"[dim]Ollama CLI failed: {e}[/dim]")
        return ""

def query_hive_mind(task: str) -> str:
    """Queries remote P2P nodes for task resolution."""
    from core.p2p import scan_for_jade_peers, send_remote_command
    peers = scan_for_jade_peers()
    for peer in peers:
        res = send_remote_command(peer, "think", {"task": task})
        if res.get("ok"):
            data = json.loads(res.get("data", "{}"))
            return data.get("text", "")
    return ""
