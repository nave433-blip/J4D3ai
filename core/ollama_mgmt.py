"""Ollama fleet management — capabilities Ollama itself doesn't ship.

Library management with real per-model disk accounting (parsed from the
Ollama models directory manifests, not guessed), stale-model detection,
multi-host routing with failover, warm/keep-alive policies, per-model usage
stats, cross-model benchmarks, Modelfile generation, GGUF import, and
tracked auto-updates.

All HTTP goes through :class:`OllamaHost` (stdlib ``requests``). Nothing
here invents data: when a host is unreachable the caller gets a clear
error, never fabricated numbers.
"""

import hashlib
import json
import os
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from core.approvals import confirm
from rich.table import Table

console = Console()

DEFAULT_HOST = "http://localhost:11434"
STATE_NAME = "ollama_mgmt.json"
STALE_DAYS = 90
BENCH_PROMPT = "Explain quantum entanglement in one short paragraph."
BENCH_TOKENS = 64
MAX_USAGE_ENTRIES = 1000


# ---------------------------------------------------------------------------
# Config / state helpers (isolated for tests via monkeypatching CONFIG_DIR)
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    from core.config import CONFIG_DIR
    return Path(CONFIG_DIR)


def _state_path() -> Path:
    return _config_dir() / STATE_NAME


def _load_state() -> Dict[str, Any]:
    try:
        p = _state_path()
        if p.is_file():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {"models": {}, "usage": [], "tracked": []}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _host_tokens() -> Dict[str, str]:
    """User-configured per-host bearer tokens. Never sent to P2P peers."""
    try:
        from core.config import load_config
        return dict(load_config().get("ollama_host_tokens", {}) or {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Low-level Ollama REST client
# ---------------------------------------------------------------------------

class OllamaError(Exception):
    pass


class OllamaHost:
    """Thin wrapper around one Ollama server's REST API."""

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        headers = self._headers()
        if "headers" in kw:
            headers.update(kw.pop("headers"))
        kw["headers"] = headers
        try:
            r = requests.request(method, self.base_url + path, **kw)
        except requests.RequestException as e:
            raise OllamaError(f"{self.base_url} unreachable: {e}")
        return r

    def version(self) -> str:
        r = self._req("GET", "/api/version")
        r.raise_for_status()
        return r.json().get("version", "?")

    def tags(self) -> List[Dict[str, Any]]:
        r = self._req("GET", "/api/tags")
        r.raise_for_status()
        return r.json().get("models", [])

    def ps(self) -> List[Dict[str, Any]]:
        r = self._req("GET", "/api/ps")
        r.raise_for_status()
        return r.json().get("models", [])

    def show(self, name: str) -> Dict[str, Any]:
        r = self._req("POST", "/api/show", json={"name": name})
        r.raise_for_status()
        return r.json()

    def delete(self, name: str) -> None:
        r = self._req("DELETE", "/api/delete", json={"name": name})
        if r.status_code not in (200, 404):
            r.raise_for_status()

    def generate(self, name: str, prompt: str, options: Optional[Dict] = None,
                 keep_alive: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"model": name, "prompt": prompt, "stream": False}
        if options:
            payload["options"] = options
        if keep_alive:
            payload["keep_alive"] = keep_alive
        r = self._req("POST", "/api/generate", json=payload, timeout=120)
        r.raise_for_status()
        return r.json()

    def chat(self, name: str, messages: List[Dict[str, str]],
             keep_alive: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"model": name, "messages": messages, "stream": False}
        if keep_alive:
            payload["keep_alive"] = keep_alive
        r = self._req("POST", "/api/chat", json=payload, timeout=120)
        r.raise_for_status()
        return r.json()

    def pull_stream(self, name: str, on_line: Callable[[Dict], None]) -> None:
        r = self._req("POST", "/api/pull", json={"name": name, "stream": True},
                      stream=True, timeout=600)
        r.raise_for_status()
        for line in r.iter_lines():
            if line:
                try:
                    on_line(json.loads(line))
                except Exception:
                    pass

    def create_stream(self, name: str, modelfile: Optional[str] = None,
                      from_path: Optional[str] = None,
                      on_line: Callable[[Dict], None] = lambda d: None) -> None:
        payload: Dict[str, Any] = {"name": name, "stream": True}
        if modelfile is not None:
            payload["modelfile"] = modelfile
        if from_path is not None:
            payload["from"] = from_path
        r = self._req("POST", "/api/create", json=payload, stream=True, timeout=900)
        r.raise_for_status()
        for line in r.iter_lines():
            if line:
                try:
                    on_line(json.loads(line))
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Real disk accounting from the Ollama models directory
# ---------------------------------------------------------------------------

def models_dir() -> Path:
    return Path(os.environ.get("OLLAMA_MODELS", "~/.ollama/models")).expanduser()


def _iter_manifests(base: Path):
    manifests = base / "manifests" / "registry.ollama.ai" / "library"
    if not manifests.is_dir():
        return
    for mf in manifests.rglob("*"):
        if mf.is_file() and not mf.name.startswith("."):
            # name = library/<ns_path>/<model>, tag = filename
            try:
                rel = mf.relative_to(manifests)
                tag = mf.name
                name = str(rel.parent).replace(os.sep, "/")
                yield f"{name}:{tag}", mf
            except Exception:
                continue


def manifest_disk_usage(base: Optional[Path] = None) -> Dict[str, int]:
    """Bytes actually on disk per model, from manifests + blobs.

    Returns {} when the models directory isn't present (e.g. remote host).
    """
    base = base or models_dir()
    blobs = base / "blobs"
    usage: Dict[str, int] = {}
    for model_ref, mf in _iter_manifests(base):
        try:
            data = json.loads(mf.read_text())
        except Exception:
            continue
        total = 0
        digests = []
        cfg = (data.get("config") or {}).get("digest", "")
        if cfg:
            digests.append(cfg)
        for layer in data.get("layers", []):
            d = layer.get("digest", "")
            if d:
                digests.append(d)
        for digest in digests:
            hexpart = digest.split(":", 1)[-1]
            blob = blobs / f"sha256-{hexpart}"
            try:
                total += blob.stat().st_size if blob.is_file() else 0
            except Exception:
                pass
        usage[model_ref] = total
    return usage


def _parse_ts(ts: str) -> float:
    try:
        # Ollama emits RFC3339 with nanoseconds; trim to microseconds
        t = ts.split(".")[0]
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# High-level manager
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


class OllamaManager:
    """Fleet-level Ollama operations across one or more hosts."""

    def __init__(self, hosts: Optional[List[str]] = None):
        from core.config import load_config
        cfg = load_config()
        self.hosts = hosts or cfg.get("ollama_hosts") or [DEFAULT_HOST]
        tokens = _host_tokens()
        self._clients = {h: OllamaHost(h, token=tokens.get(h)) for h in self.hosts}
        self.state = _load_state()

    # -- persistence ------------------------------------------------------
    def _save(self) -> None:
        _save_state(self.state)

    def _model_state(self, name: str) -> Dict[str, Any]:
        return self.state.setdefault("models", {}).setdefault(name, {})

    # -- hosts ------------------------------------------------------------
    def client(self, host: str) -> OllamaHost:
        if host not in self._clients:
            self._clients[host] = OllamaHost(host, token=_host_tokens().get(host))
        return self._clients[host]

    def add_host(self, url: str) -> Dict[str, Any]:
        url = url.rstrip("/")
        try:
            ver = self.client(url).version()
        except OllamaError as e:
            return {"ok": False, "error": str(e)}
        if url not in self.hosts:
            self.hosts.append(url)
            self._persist_hosts()
        return {"ok": True, "version": ver, "host": url}

    def remove_host(self, url: str) -> Dict[str, Any]:
        url = url.rstrip("/")
        if url in self.hosts:
            self.hosts.remove(url)
            self._clients.pop(url, None)
            self._persist_hosts()
            return {"ok": True}
        return {"ok": False, "error": "host not configured"}

    def _persist_hosts(self) -> None:
        try:
            from core.config import load_config, save_config
            cfg = load_config()
            cfg["ollama_hosts"] = self.hosts
            save_config(cfg)
        except Exception:
            pass

    def prefer_host(self, url: str) -> Dict[str, Any]:
        url = url.rstrip("/")
        if url not in self.hosts:
            return {"ok": False, "error": "host not configured — add it first"}
        self.hosts.remove(url)
        self.hosts.insert(0, url)
        self._persist_hosts()
        return {"ok": True, "hosts": self.hosts}

    def test_hosts(self) -> List[Dict[str, Any]]:
        out = []
        for h in self.hosts:
            try:
                out.append({"host": h, "ok": True, "version": self.client(h).version()})
            except OllamaError as e:
                out.append({"host": h, "ok": False, "error": str(e)})
        return out

    def route(self, model: str) -> Optional[str]:
        """First configured host that actually has *model*. None if nowhere."""
        for h in self.hosts:
            try:
                names = [m.get("name", "") for m in self.client(h).tags()]
                if model in names or any(n.split(":")[0] == model.split(":")[0] for n in names):
                    return h
            except OllamaError:
                continue
        return None

    # -- library ----------------------------------------------------------
    def library(self, host: Optional[str] = None) -> List[Dict[str, Any]]:
        """Per-model rows with real disk usage, load state, and staleness."""
        hosts = [host] if host else self.hosts
        disk = manifest_disk_usage()
        rows: List[Dict[str, Any]] = []
        now = time.time()
        for h in hosts:
            try:
                tags = self.client(h).tags()
                loaded = {m.get("name") for m in self.client(h).ps()}
            except OllamaError as e:
                rows.append({"host": h, "error": str(e)})
                continue
            for m in tags:
                name = m.get("name", "?")
                size = disk.get(name) or m.get("size", 0)
                ms = self._model_state(name)
                modified = _parse_ts(m.get("modified_at", ""))
                last_activity = max(ms.get("last_used", 0), ms.get("last_pull", 0), modified)
                stale = (now - last_activity) > STALE_DAYS * 86400 and not ms.get("tracked")
                rows.append({
                    "host": h, "name": name, "size": size,
                    "size_h": _fmt_bytes(size), "digest": (m.get("digest", "") or "")[:19],
                    "modified": m.get("modified_at", "")[:10],
                    "loaded": name in loaded,
                    "stale": stale, "tracked": bool(ms.get("tracked")),
                    "last_activity": last_activity,
                })
        return rows

    def print_library(self, host: Optional[str] = None) -> None:
        rows = self.library(host)
        table = Table(title="🦙 Ollama Library (real disk usage)", border_style="cyan")
        for col in ("Host", "Model", "Size", "Loaded", "Stale", "Tracked", "Modified"):
            table.add_column(col)
        for r in rows:
            if "error" in r:
                table.add_row(r["host"], f"[red]{r['error']}[/red]", "", "", "", "", "")
                continue
            table.add_row(
                r["host"].replace("http://", ""),
                r["name"], r["size_h"],
                "[green]●[/green]" if r["loaded"] else "[dim]○[/dim]",
                "[yellow]stale[/yellow]" if r["stale"] else "",
                "📌" if r["tracked"] else "",
                r["modified"],
            )
        console.print(table)

    def prune(self, dry_run: bool = True, auto_yes: bool = False) -> List[str]:
        """Delete stale models. Dry-run by default; real deletes need confirm."""
        candidates = [r for r in self.library() if r.get("stale")]
        if not candidates:
            console.print("[green]No stale models — nothing to prune.[/green]")
            return []
        table = Table(title="🧹 Prune candidates (unused > 90d, untracked)")
        table.add_column("Model"); table.add_column("Size"); table.add_column("Host")
        total = 0
        for c in candidates:
            table.add_row(c["name"], c["size_h"], c["host"].replace("http://", ""))
            total += c["size"]
        console.print(table)
        console.print(f"Would free [bold]{_fmt_bytes(total)}[/bold]")
        if dry_run:
            console.print("[dim]Dry run — nothing deleted. Re-run with --yes to delete.[/dim]")
            return [c["name"] for c in candidates]
        if not auto_yes and not confirm(f"Delete these {len(candidates)} models?"):
            console.print("[yellow]Cancelled.[/yellow]")
            return []
        deleted = []
        for c in candidates:
            try:
                self.client(c["host"]).delete(c["name"])
                deleted.append(c["name"])
                console.print(f"[green]✓ deleted {c['name']}[/green]")
            except OllamaError as e:
                console.print(f"[red]✗ {c['name']}: {e}[/red]")
        return deleted

    # -- warm / keep-alive --------------------------------------------------
    def ps_all(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for h in self.hosts:
            try:
                for m in self.client(h).ps():
                    rows.append({"host": h, **m})
            except OllamaError as e:
                rows.append({"host": h, "error": str(e)})
        return rows

    def print_ps(self) -> None:
        rows = self.ps_all()
        table = Table(title="🔥 Loaded models (/api/ps)", border_style="magenta")
        table.add_column("Host"); table.add_column("Model")
        table.add_column("Size"); table.add_column("VRAM"); table.add_column("Expires")
        for r in rows:
            if "error" in r:
                table.add_row(r["host"], f"[red]{r['error']}[/red]", "", "", "")
                continue
            table.add_row(
                r["host"].replace("http://", ""), r.get("name", "?"),
                _fmt_bytes(r.get("size", 0)), _fmt_bytes(r.get("size_vram", 0)),
                (r.get("expires_at", "") or "")[:19].replace("T", " "),
            )
        console.print(table)

    def warm(self, model: str, keep_alive: str = "30m") -> Dict[str, Any]:
        """Pre-load *model* into memory on the host that has it."""
        host = self.route(model)
        if not host:
            return {"ok": False, "error": f"model '{model}' not found on any configured host"}
        try:
            res = self.client(host).chat(
                model, [{"role": "user", "content": "ping"}], keep_alive=keep_alive)
            self._model_state(model)["last_used"] = time.time()
            self._save()
            return {"ok": True, "host": host,
                    "eval_ms": round(res.get("total_duration", 0) / 1e6, 1)}
        except OllamaError as e:
            return {"ok": False, "error": str(e)}

    # -- usage stats ----------------------------------------------------------
    def note_usage(self, model: str, host: str, payload: Dict[str, Any]) -> None:
        """Record one inference's token/timing counters (defensive)."""
        try:
            entry = {
                "ts": time.time(), "model": model, "host": host,
                "prompt_tokens": int(payload.get("prompt_eval_count", 0) or 0),
                "completion_tokens": int(payload.get("eval_count", 0) or 0),
                "ms": round(float(payload.get("total_duration", 0) or 0) / 1e6, 1),
            }
        except (TypeError, ValueError):
            return
        usage = self.state.setdefault("usage", [])
        usage.append(entry)
        del usage[:-MAX_USAGE_ENTRIES]
        self._model_state(model)["last_used"] = entry["ts"]
        self._save()

    def stats(self, model: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        agg: Dict[str, Dict[str, Any]] = {}
        for u in self.state.get("usage", []):
            if model and u.get("model") != model:
                continue
            a = agg.setdefault(u["model"], {"calls": 0, "prompt": 0, "completion": 0, "ms": 0.0})
            a["calls"] += 1
            a["prompt"] += u.get("prompt_tokens", 0)
            a["completion"] += u.get("completion_tokens", 0)
            a["ms"] += u.get("ms", 0.0)
        for a in agg.values():
            a["avg_tps"] = round(a["completion"] / (a["ms"] / 1000), 1) if a["ms"] > 0 else 0.0
        return agg

    def print_stats(self, model: Optional[str] = None) -> None:
        agg = self.stats(model)
        if not agg:
            console.print("[yellow]No usage recorded yet. Stats accumulate as you chat.[/yellow]")
            return
        table = Table(title="📊 Ollama usage stats", border_style="green")
        table.add_column("Model"); table.add_column("Calls", justify="right")
        table.add_column("Prompt tok", justify="right"); table.add_column("Completion tok", justify="right")
        table.add_column("Avg tok/s", justify="right")
        for name, a in sorted(agg.items()):
            table.add_row(name, str(a["calls"]), str(a["prompt"]), str(a["completion"]), str(a["avg_tps"]))
        console.print(table)

    # -- benchmark ------------------------------------------------------------
    def bench(self, model: str, prompt: str = BENCH_PROMPT,
              max_tokens: int = BENCH_TOKENS) -> Dict[str, Any]:
        host = self.route(model)
        if not host:
            return {"ok": False, "error": f"model '{model}' not found on any host"}
        try:
            res = self.client(host).generate(
                model, prompt, options={"num_predict": max_tokens})
        except OllamaError as e:
            return {"ok": False, "error": str(e)}
        eval_n = res.get("eval_count", 0) or 0
        eval_s = (res.get("eval_duration", 0) or 0) / 1e9
        tps = round(eval_n / eval_s, 1) if eval_s > 0 else 0.0
        self.note_usage(model, host, res)
        return {"ok": True, "model": model, "host": host,
                "tokens": eval_n, "tokens_per_sec": tps,
                "prompt_ms": round((res.get("prompt_eval_duration", 0) or 0) / 1e6, 1)}

    def bench_all(self, prompt: str = BENCH_PROMPT) -> None:
        seen = set()
        rows = []
        for r in self.library():
            name = r.get("name")
            if not name or name in seen or "error" in r:
                continue
            seen.add(name)
            console.print(f"[dim]Benchmarking {name}…[/dim]")
            b = self.bench(name, prompt=prompt)
            rows.append(b)
        table = Table(title=f"⚡ Benchmark ({BENCH_TOKENS} tokens, prompt: {prompt[:40]}…)")
        table.add_column("Model"); table.add_column("Host")
        table.add_column("tok/s", justify="right"); table.add_column("Prompt ms", justify="right")
        for b in rows:
            if b.get("ok"):
                table.add_row(b["model"], b["host"].replace("http://", ""),
                              str(b["tokens_per_sec"]), str(b["prompt_ms"]))
            else:
                table.add_row(b.get("model", "?"), "", f"[red]{b.get('error', '?')}[/red]", "")
        console.print(table)


    # -- Modelfile ------------------------------------------------------------
    @staticmethod
    def build_modelfile(base: str, system: str = "", temperature: float = 0.7,
                        top_p: float = 0.9, num_ctx: int = 4096,
                        stop: Optional[List[str]] = None) -> str:
        lines = [f"FROM {base}"]
        if system.strip():
            lines.append(f'SYSTEM """\n{system.strip()}\n"""')
        lines.append(f"PARAMETER temperature {temperature}")
        lines.append(f"PARAMETER top_p {top_p}")
        lines.append(f"PARAMETER num_ctx {num_ctx}")
        for s in stop or []:
            lines.append(f'PARAMETER stop "{s}"')
        return "\n".join(lines) + "\n"

    def modelfile_wizard(self) -> str:
        console.print(Panel("[bold cyan]📝 Modelfile generator[/bold cyan]\nAnswer a few questions — I'll write a valid Modelfile.", border_style="cyan"))
        base = Prompt.ask("Base model", default="llama3")
        system = Prompt.ask("System prompt (empty = none)", default="")
        temperature = float(Prompt.ask("Temperature", default="0.7"))
        top_p = float(Prompt.ask("Top-p", default="0.9"))
        num_ctx = int(Prompt.ask("Context window", default="4096"))
        mf = self.build_modelfile(base, system, temperature, top_p, num_ctx)
        console.print(Panel(mf, title="Generated Modelfile", border_style="green"))
        return mf

    def modelfile_from_description(self, description: str, base: str = "llama3") -> str:
        """Draft a Modelfile from a natural-language description.

        Tries the local LLM for a polished draft; falls back to a clean
        template with the description as the SYSTEM prompt. The fallback is
        labeled honestly — never presented as model-generated.
        """
        draft = ""
        try:
            from core.brain import think_structured
            res = think_structured(
                "Modelfile drafting",
                f"Write ONLY a valid Ollama Modelfile (no explanation) for this "
                f"assistant. Base model: {base}. Description: {description}",
                model=base)
            if isinstance(res, dict) and res.get("ok"):
                draft = res.get("text", "").strip().strip("`")
        except Exception:
            draft = ""
        if draft and "FROM" in draft.upper():
            console.print("[dim]Drafted with local LLM.[/dim]")
            return draft if draft.endswith("\n") else draft + "\n"
        console.print("[dim]LLM draft unavailable — using template with your "
                      "description as the system prompt.[/dim]")
        return self.build_modelfile(base, system=description)

    def create_from_modelfile(self, name: str, modelfile_text: str,
                              host: Optional[str] = None) -> Dict[str, Any]:
        host = host or self.hosts[0]
        errors: List[str] = []

        def on_line(d: Dict):
            status = d.get("status", "")
            if d.get("error"):
                errors.append(str(d["error"]))
            elif status:
                console.print(f"[dim]{status}[/dim]")

        try:
            self.client(host).create_stream(name, modelfile=modelfile_text, on_line=on_line)
        except OllamaError as e:
            return {"ok": False, "error": str(e)}
        if errors:
            return {"ok": False, "error": "; ".join(errors)}
        return {"ok": True, "name": name, "host": host}

    def import_gguf(self, gguf_path: str, name: str,
                    host: Optional[str] = None) -> Dict[str, Any]:
        """Validate a GGUF file (magic bytes) and import it as *name*."""
        p = Path(gguf_path).expanduser()
        if not p.is_file():
            return {"ok": False, "error": f"file not found: {gguf_path}"}
        try:
            if p.read_bytes()[:4] != b"GGUF":
                return {"ok": False,
                        "error": "not a GGUF file (bad magic bytes) — refusing to import"}
        except Exception as e:
            return {"ok": False, "error": f"cannot read file: {e}"}
        console.print(f"[dim]Importing {_fmt_bytes(p.stat().st_size)} GGUF as '{name}'…[/dim]")
        # FROM with an absolute path is the documented Ollama GGUF import flow.
        mf = f"FROM {p.resolve()}\n"
        return self.create_from_modelfile(name, mf, host=host)

    # -- tracked auto-updates ---------------------------------------------------
    def track(self, model: str) -> None:
        tracked = self.state.setdefault("tracked", [])
        if model not in tracked:
            tracked.append(model)
            self._model_state(model)["tracked"] = True
            self._save()
        console.print(f"[green]📌 Tracking {model} for updates.[/green]")

    def untrack(self, model: str) -> None:
        tracked = self.state.setdefault("tracked", [])
        if model in tracked:
            tracked.remove(model)
        self._model_state(model)["tracked"] = False
        self._save()
        console.print(f"[dim]Untracked {model}.[/dim]")

    def _digest(self, model: str, host: str) -> str:
        try:
            for m in self.client(host).tags():
                if m.get("name") == model:
                    return m.get("digest", "")
        except OllamaError:
            pass
        return ""

    def auto_pull(self) -> List[Dict[str, Any]]:
        """Pull every tracked model; report digest changes (the honest changelog)."""
        tracked = self.state.get("tracked", [])
        if not tracked:
            console.print("[yellow]No tracked models. Use `/ollama track <model>` first.[/yellow]")
            return []
        results = []
        for model in tracked:
            host = self.route(model) or self.hosts[0]
            before = self._digest(model, host)
            statuses: List[str] = []

            def on_line(d: Dict):
                s = d.get("status", "")
                if s and s != statuses[-1:] and len(statuses) < 3:
                    statuses.append(s)

            try:
                self.client(host).pull_stream(model, on_line)
                after = self._digest(model, host)
                changed = bool(before and after and before != after)
                self._model_state(model)["last_pull"] = time.time()
                self._save()
                results.append({"model": model, "host": host, "ok": True,
                                "changed": changed,
                                "before": before[:19], "after": after[:19]})
                if changed:
                    console.print(f"[green]⬆ {model}: updated[/green] "
                                  f"[dim]{before[:12]} → {after[:12]}[/dim]")
                else:
                    console.print(f"[dim]{model}: already current[/dim]")
            except OllamaError as e:
                results.append({"model": model, "host": host, "ok": False, "error": str(e)})
                console.print(f"[red]✗ {model}: {e}[/red]")
        console.print("[dim]Tip: schedule this inside Jade with "
                      "`/schedule add --name \"nightly pull\" --action \"/ollama auto-pull\" "
                      "--cron \"0 4 * * *\"`.[/dim]")
        return results


def note_chat_usage(model: str, host: str, payload: Dict[str, Any]) -> None:
    """One-line hook for the chat path — never raises."""
    try:
        OllamaManager().note_usage(model, host, payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# `/ollama` subcommand dispatcher. Returns True when handled (managed),
# False when the args should fall through to the raw `ollama` binary.
# ---------------------------------------------------------------------------

MANAGED = ("ps", "list", "ls", "prune", "stats", "bench", "warm",
           "hosts", "pull", "track", "untrack", "tracked", "auto-pull",
           "modelfile", "gguf", "keepalive")


def ollama_help() -> None:
    console.print(Panel(
        "[bold cyan]🦙 /ollama[/bold cyan] — managed subcommands\n\n"
        "[green]/ollama ps[/green]              loaded models + keep-alive expiry\n"
        "[green]/ollama list[/green]            library with real disk usage + stale flags\n"
        "[green]/ollama prune[/green]           dry-run delete of stale models ([green]--yes[/green] to delete)\n"
        "[green]/ollama stats [model][/green]   per-model token/timing history\n"
        "[green]/ollama bench [model][/green]   tokens/sec benchmark (default: all models)\n"
        "[green]/ollama warm <model>[/green]    pre-load a model into memory\n"
        "[green]/ollama hosts[/green]           list/add/remove/test/prefer Ollama servers\n"
        "[green]/ollama pull <model>[/green]    pull with progress\n"
        "[green]/ollama track <model>[/green]   watch a model for [green]auto-pull[/green] updates\n"
        "[green]/ollama auto-pull[/green]       update all tracked models (digest changelog)\n"
        "[green]/ollama modelfile[/green]       interactive Modelfile generator\n"
        "[green]/ollama gguf <path> <name>[/green]  import a GGUF file\n\n"
        "[dim]Anything else passes through to the raw `ollama` binary.[/dim]",
        border_style="cyan"))


def handle_ollama_args(argstr: str) -> bool:
    """Route `/ollama ...`. True = handled here; False = raw binary passthrough."""
    try:
        parts = shlex.split(argstr or "")
    except Exception:
        parts = (argstr or "").split()
    if not parts:
        ollama_help()
        return True
    cmd, rest = parts[0].lower(), parts[1:]
    if cmd not in MANAGED:
        return False
    mgr = OllamaManager()
    try:
        if cmd == "ps":
            mgr.print_ps()
        elif cmd in ("list", "ls"):
            mgr.print_library()
        elif cmd == "prune":
            mgr.prune(dry_run="--yes" not in rest)
        elif cmd == "stats":
            mgr.print_stats(rest[0] if rest else None)
        elif cmd == "bench":
            if rest:
                b = mgr.bench(rest[0])
                if b.get("ok"):
                    console.print(f"[green]{b['model']}: {b['tokens_per_sec']} tok/s[/green] "
                                  f"[dim]({b['tokens']} tokens, prompt {b['prompt_ms']}ms)[/dim]")
                else:
                    console.print(f"[red]{b.get('error')}[/red]")
            else:
                mgr.bench_all()
        elif cmd == "warm":
            if not rest:
                console.print("[yellow]Usage: /ollama warm <model>[/yellow]")
            else:
                r = mgr.warm(rest[0])
                console.print(f"[green]🔥 {rest[0]} warmed on {r['host']} ({r['eval_ms']}ms)[/green]"
                              if r.get("ok") else f"[red]{r.get('error')}[/red]")
        elif cmd == "hosts":
            _handle_hosts(mgr, rest)
        elif cmd == "pull":
            if not rest:
                console.print("[yellow]Usage: /ollama pull <model>[/yellow]")
            else:
                _pull_with_progress(mgr, rest[0])
        elif cmd == "track":
            if rest:
                mgr.track(rest[0])
            else:
                console.print("[yellow]Usage: /ollama track <model>[/yellow]")
        elif cmd == "untrack":
            if rest:
                mgr.untrack(rest[0])
        elif cmd == "tracked":
            for t in mgr.state.get("tracked", []):
                console.print(f"📌 {t}")
        elif cmd == "auto-pull":
            mgr.auto_pull()
        elif cmd == "modelfile":
            desc = ""
            if "--from" in rest:
                desc = rest[rest.index("--from") + 1] if rest.index("--from") + 1 < len(rest) else ""
            mf = mgr.modelfile_from_description(desc) if desc else mgr.modelfile_wizard()
            if "--save" in rest:
                i = rest.index("--save")
                out = Path(rest[i + 1]).expanduser() if i + 1 < len(rest) else Path("Modelfile")
                out.write_text(mf)
                console.print(f"[green]Wrote {out}[/green]")
        elif cmd == "gguf":
            if len(rest) < 2:
                console.print("[yellow]Usage: /ollama gguf <path-to.gguf> <name>[/yellow]")
            else:
                r = mgr.import_gguf(rest[0], rest[1])
                console.print(f"[green]✅ Imported {rest[1]}[/green]"
                              if r.get("ok") else f"[red]{r.get('error')}[/red]")
        elif cmd == "keepalive":
            if len(rest) < 2:
                console.print("[yellow]Usage: /ollama keepalive <model> <duration, e.g. 1h>[/yellow]")
            else:
                r = mgr.warm(rest[0], keep_alive=rest[1])
                console.print(f"[green]⏳ {rest[0]} pinned for {rest[1]}[/green]"
                              if r.get("ok") else f"[red]{r.get('error')}[/red]")
    except Exception as e:
        console.print(f"[red]Ollama mgmt error: {e}[/red]")
    return True


def _handle_hosts(mgr: OllamaManager, rest: List[str]) -> None:
    if not rest:
        table = Table(title="🖥️ Ollama hosts (first = preferred)")
        table.add_column("#"); table.add_column("Host"); table.add_column("Status")
        for i, r in enumerate(mgr.test_hosts()):
            status = f"[green]✓ {r['version']}[/green]" if r["ok"] else f"[red]✗ {r.get('error', '')[:40]}[/red]"
            table.add_row(str(i + 1), r["host"], status)
        console.print(table)
        console.print("[dim]/ollama hosts add|remove|test|prefer <url>[/dim]")
        return
    action, url = rest[0].lower(), (rest[1] if len(rest) > 1 else "")
    if action == "add" and url:
        r = mgr.add_host(url)
        console.print(f"[green]✅ Added {url} (v{r['version']})[/green]" if r.get("ok") else f"[red]{r.get('error')}[/red]")
    elif action == "remove" and url:
        r = mgr.remove_host(url)
        console.print("[green]Removed.[/green]" if r.get("ok") else f"[red]{r.get('error')}[/red]")
    elif action == "test" and url:
        try:
            v = mgr.client(url).version()
            console.print(f"[green]✓ {url} reachable (v{v})[/green]")
        except OllamaError as e:
            console.print(f"[red]✗ {e}[/red]")
    elif action == "prefer" and url:
        r = mgr.prefer_host(url)
        console.print("[green]Preferred host updated.[/green]" if r.get("ok") else f"[red]{r.get('error')}[/red]")
    else:
        console.print("[yellow]Usage: /ollama hosts [add|remove|test|prefer <url>][/yellow]")


def _pull_with_progress(mgr: OllamaManager, model: str) -> None:
    host = mgr.hosts[0]
    last = {"s": ""}

    def on_line(d: Dict):
        s = d.get("status", "")
        total, done = d.get("total", 0), d.get("completed", 0)
        if total and done:
            pct = done / total * 100
            msg = f"{s}: {pct:.0f}%"
        else:
            msg = s
        if msg != last["s"]:
            console.print(f"[dim]{msg}[/dim]")
            last["s"] = msg
        if d.get("error"):
            console.print(f"[red]{d['error']}[/red]")

    try:
        mgr.client(host).pull_stream(model, on_line)
        mgr._model_state(model)["last_pull"] = time.time()
        mgr._save()
        console.print(f"[green]✅ Pulled {model}[/green]")
    except OllamaError as e:
        console.print(f"[red]{e}[/red]")
