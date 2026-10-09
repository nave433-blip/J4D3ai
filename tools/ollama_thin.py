"""Jade thin client for low-resource nodes.

A minimal-footprint way to use LLM power from a weak machine (old laptop,
Raspberry Pi, tiny VM): instead of running models locally, the thin client
talks to a remote Ollama server OR proxies through a "fat" Jade peer over
the existing P2P layer, which runs the model and streams tokens back.

Deliberately light: stdlib + requests + rich + psutil only. It never imports
core.brain, vector memory, or any heavy provider SDK. It never ships API
keys to peers — peer auth uses the existing P2P token, and remote Ollama
hosts use user-configured per-host credentials only.

Entry point: `jade-thin` (console script) or `python -m tools.ollama_thin`.
"""

import json
import os
import sys
import time
from typing import Dict, Generator, List, Optional

import psutil
import requests
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

console = Console()

# Heuristic tiers, clearly labeled as heuristics — not guarantees.
# Model RAM need ≈ parameters * bytes_per_param * ~1.3 (KV cache + overhead).
BYTES_PER_PARAM_Q4 = 0.58  # ~4-bit quant reality


def detect_resources() -> Dict[str, float]:
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(os.path.expanduser("~"))
    return {
        "ram_total_gb": round(vm.total / 1e9, 1),
        "ram_free_gb": round(vm.available / 1e9, 1),
        "cpu_count": psutil.cpu_count(logical=True) or 1,
        "disk_free_gb": round(du.free / 1e9, 1),
    }


def recommend(resources: Optional[Dict] = None) -> Dict[str, str]:
    """Resource-adaptive recommendation. Honest heuristics, labeled as such."""
    r = resources or detect_resources()
    free = r["ram_free_gb"]
    if free < 1.5:
        return {"mode": "remote",
                "reason": f"only {free}GB RAM free — too little to run even a 1B "
                          "model locally. Use a remote Ollama host or a fat peer."}
    if free < 4:
        return {"mode": "remote-or-tiny",
                "reason": f"{free}GB RAM free — remote execution recommended; "
                          "locally, only ≤1B quantized models are realistic."}
    if free < 8:
        return {"mode": "hybrid",
                "reason": f"{free}GB RAM free — ≤3B models run locally; larger "
                          "models should go to a remote host or fat peer."}
    return {"mode": "local",
            "reason": f"{free}GB RAM free — local models up to ~8B quantized "
                      "should fit; bigger still wants remote."}


def model_ram_need_gb(param_billions: float) -> float:
    return param_billions * 1e9 * BYTES_PER_PARAM_Q4 * 1.3 / 1e9


def can_fit(model_bytes: int, free_bytes: int) -> bool:
    """Refuse when the model can't fit with headroom for KV cache + OS."""
    return model_bytes <= free_bytes * 0.5


def fit_explanation(model: str, model_bytes: int, free_bytes: int) -> str:
    need = model_bytes / 1e9
    free = free_bytes / 1e9
    if can_fit(model_bytes, free_bytes):
        return (f"{model} (~{need:.1f}GB) fits in {free:.1f}GB free "
                f"(needs ≤50% of free RAM for KV cache + headroom).")
    return (f"REFUSING pull of {model}: ~{need:.1f}GB needed but only "
            f"{free:.1f}GB free — it would OOM or thrash. Point this thin "
            f"node at a remote Ollama host or fat peer instead.")


class ThinClient:
    """Chat/generate against a remote Ollama host or a fat Jade peer."""

    def __init__(self, remote_host: Optional[str] = None,
                 host_token: Optional[str] = None,
                 peer_ip: Optional[str] = None, peer_port: int = 11435,
                 use_tls: Optional[bool] = None):
        self.remote_host = (remote_host or "").rstrip("/") or None
        self.host_token = host_token
        self.peer_ip = peer_ip
        self.peer_port = peer_port
        self.use_tls = use_tls

    # -- discovery ------------------------------------------------------
    def probe_remote(self, host: Optional[str] = None) -> Dict:
        host = (host or self.remote_host or "").rstrip("/")
        if not host:
            return {"ok": False, "error": "no remote host configured"}
        try:
            headers = {"Authorization": f"Bearer {self.host_token}"} if self.host_token else {}
            r = requests.get(f"{host}/api/version", headers=headers, timeout=5)
            r.raise_for_status()
            return {"ok": True, "host": host, "version": r.json().get("version", "?")}
        except Exception as e:
            return {"ok": False, "host": host, "error": str(e)}

    def probe_peers(self, timeout: float = 1.5) -> List[Dict]:
        from core.p2p import scan_for_jade_peers
        try:
            return [{"ip": ip} for ip in scan_for_jade_peers(timeout=timeout)]
        except Exception:
            return []

    def remote_models(self) -> List[Dict]:
        if not self.remote_host:
            return []
        try:
            headers = {"Authorization": f"Bearer {self.host_token}"} if self.host_token else {}
            r = requests.get(f"{self.remote_host}/api/tags", headers=headers, timeout=10)
            r.raise_for_status()
            return r.json().get("models", [])
        except Exception:
            return []

    def peer_models(self) -> List[str]:
        if not self.peer_ip:
            return []
        from core.p2p import send_remote_command
        res = send_remote_command(self.peer_ip, "status", {}, port=self.peer_port,
                                  use_tls=self.use_tls)
        if res.get("ok"):
            try:
                return json.loads(res["data"]).get("local_models", [])
            except Exception:
                pass
        return []

    # -- inference ------------------------------------------------------
    def chat_remote(self, model: str, prompt: str) -> Generator[str, None, None]:
        """Stream tokens from a remote Ollama host."""
        headers = {"Authorization": f"Bearer {self.host_token}"} if self.host_token else {}
        r = requests.post(f"{self.remote_host}/api/chat",
                          json={"model": model,
                                "messages": [{"role": "user", "content": prompt}],
                                "stream": True},
                          headers=headers, stream=True, timeout=300)
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            chunk = (d.get("message") or {}).get("content", "")
            if chunk:
                yield chunk
            if d.get("done"):
                break

    def chat_peer(self, model: str, prompt: str) -> Generator[str, None, None]:
        """Stream tokens from a fat peer (it runs the model, we render).

        No API keys are ever sent — peer auth uses the configured P2P token.
        """
        from core.p2p import send_remote_command
        res = send_remote_command(self.peer_ip, "think",
                                  {"task": prompt, "model": model, "stream": True},
                                  port=self.peer_port, use_tls=self.use_tls)
        if not res.get("ok"):
            yield f"\n[peer error: {res.get('error')}]"
            return
        buf = b""
        for chunk in res["stream"]:
            buf += chunk if isinstance(chunk, bytes) else str(chunk).encode()
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    yield line.decode("utf-8", "replace")

    def chat(self, model: str, prompt: str) -> Generator[str, None, None]:
        if self.remote_host:
            yield from self.chat_remote(model, prompt)
        elif self.peer_ip:
            yield from self.chat_peer(model, prompt)
        else:
            yield ("[thin client has nowhere to run this: configure a remote "
                   "Ollama host (`/remote <url>`) or pick a fat peer (`/peers`).]")

    def wake(self, model: str) -> Dict:
        """Ask the fat side to load *model* now; report readiness honestly."""
        if self.remote_host:
            try:
                headers = {"Authorization": f"Bearer {self.host_token}"} if self.host_token else {}
                r = requests.post(
                    f"{self.remote_host}/api/chat",
                    json={"model": model, "messages": [{"role": "user", "content": "ping"}],
                          "stream": False, "keep_alive": "30m"},
                    headers=headers, timeout=180)
                r.raise_for_status()
                return {"ok": True, "via": self.remote_host, "ready": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        if self.peer_ip:
            from core.p2p import send_remote_command
            res = send_remote_command(self.peer_ip, "think",
                                      {"task": "Reply with exactly: READY", "model": model},
                                      port=self.peer_port, use_tls=self.use_tls)
            if res.get("ok"):
                return {"ok": True, "via": self.peer_ip, "ready": True}
            return {"ok": False, "error": res.get("error")}
        return {"ok": False, "error": "no remote host or peer configured"}


# ---------------------------------------------------------------------------
# Thin REPL
# ---------------------------------------------------------------------------

def _guarded_pull(client: ThinClient, model: str, model_bytes: int) -> None:
    free = psutil.virtual_memory().available
    console.print(fit_explanation(model, model_bytes, free))
    if not can_fit(model_bytes, free):
        return
    console.print("[dim]Fits — but thin nodes should prefer remote execution. "
                  "Pull it on the fat side instead.[/dim]")


def main() -> None:
    console.print(Panel("[bold cyan]🪶 JADE THIN CLIENT[/bold cyan]\n"
                        "Minimal footprint — models run elsewhere, tokens stream here.",
                        border_style="cyan"))
    res = detect_resources()
    rec = recommend(res)
    console.print(f"[dim]RAM: {res['ram_free_gb']}GB free / {res['ram_total_gb']}GB total · "
                  f"CPUs: {res['cpu_count']} · Disk free: {res['disk_free_gb']}GB[/dim]")
    console.print(f"[yellow]Recommendation ({rec['mode']}):[/yellow] {rec['reason']}")

    try:
        from core.config import load_config
        cfg_host = (load_config().get("thin_remote_host") or "").strip()
        cfg_token = (load_config().get("ollama_host_tokens") or {}).get(cfg_host)
    except Exception:
        cfg_host, cfg_token = "", None
    client = ThinClient(remote_host=cfg_host or None, host_token=cfg_token)
    model = "llama3"

    if client.remote_host:
        probe = client.probe_remote()
        console.print(f"[green]✓ remote {probe['host']} (v{probe.get('version', '?')})[/green]"
                      if probe.get("ok") else f"[red]✗ remote unreachable: {probe.get('error')}[/red]")
    else:
        console.print("[dim]No remote host configured. `/remote <url>` or `/peers` to find a fat peer.[/dim]")
    console.print("[dim]Commands: /remote <url> · /peers · /use peer <ip> · /wake <model> · "
                  "/models · /model <name> · /exit[/dim]")

    while True:
        try:
            text = Prompt.ask(f"[cyan]thin:{model}>[/cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Goodbye.[/yellow]")
            break
        if not text:
            continue
        low = text.lower()
        if low in ("/exit", "exit", "quit"):
            break
        if low == "/peers":
            peers = client.probe_peers()
            if not peers:
                console.print("[yellow]No fat peers found on the LAN. "
                              "Is another Jade running with P2P enabled?[/yellow]")
            else:
                for p in peers:
                    console.print(f"  🖥️ {p['ip']}")
            continue
        if low.startswith("/remote "):
            url = text.split(None, 1)[1].rstrip("/")
            probe = client.probe_remote(url)
            if probe.get("ok"):
                client.remote_host, client.peer_ip = url, None
                console.print(f"[green]✓ using remote {url}[/green]")
            else:
                console.print(f"[red]✗ {probe.get('error')}[/red]")
            continue
        if low.startswith("/use peer "):
            ip = text.split(None, 2)[2]
            client.peer_ip, client.remote_host = ip, None
            models = client.peer_models()
            console.print(f"[green]✓ proxying through peer {ip}[/green]"
                          + (f" [dim](models: {', '.join(models[:5])})[/dim]" if models else ""))
            continue
        if low.startswith("/wake "):
            m = text.split(None, 1)[1]
            r = client.wake(m)
            console.print(f"[green]✅ {m} ready via {r.get('via')}[/green]"
                          if r.get("ok") else f"[red]✗ {r.get('error')}[/red]")
            continue
        if low == "/models":
            models = client.remote_models() if client.remote_host else []
            names = [m.get("name") for m in models]
            if not names and client.peer_ip:
                names = client.peer_models()
            console.print(", ".join(names) if names else "[yellow]No models visible.[/yellow]")
            continue
        if low.startswith("/model "):
            model = text.split(None, 1)[1]
            console.print(f"[dim]Model set to {model}[/dim]")
            continue
        # plain text → chat
        try:
            for tok in client.chat(model, text):
                console.print(tok, end="")
            console.print()
        except Exception as e:
            console.print(f"\n[red]chat failed: {e}[/red]")


if __name__ == "__main__":
    main()
