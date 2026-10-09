"""Hive mind: multi-model consensus, P2P shared result cache, capability registry.

Three pieces, all in Jade's own style:

1. ``hive_ask(question)`` — asks every *configured* AI provider the same
   question in parallel and synthesizes one consensus answer with
   per-model attributions. Providers that fail are skipped gracefully and
   reported as skipped — never faked.

2. P2P shared result cache — peers can share cached LLM answers keyed by a
   normalized prompt hash, so the swarm doesn't pay twice for the same
   question. Cache entries carry model name + timestamp only. Participation
   is opt-in (``hive_cache_sharing`` in config, default off). Stale entries
   expire via ``hive_cache_ttl`` (seconds, default 24h).

3. Capability registry — peers advertise which providers/models they have
   (names only) via the P2P ``status`` action. API keys, tokens, and any
   other credential material are NEVER transmitted.

Security: the shared cache carries prompts (hashed for lookup; full text
only for entries this node chose to share), answers, model names, and
timestamps. Private vector memories never leave the machine.
"""
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.config import CONFIG_DIR, load_config

console = Console()

CACHE_FILE = CONFIG_DIR / "hive_cache.json"
DEFAULT_TTL = 86400  # 24 hours
MAX_CACHE_ENTRIES = 500


# ---------------------------------------------------------------------------
# Prompt normalization / hashing
# ---------------------------------------------------------------------------

def normalize_prompt(question: str) -> str:
    """Collapse whitespace + lowercase so trivially different prompts hash equal."""
    return " ".join(question.strip().lower().split())


def prompt_hash(question: str) -> str:
    return hashlib.sha256(normalize_prompt(question).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Local result cache
# ---------------------------------------------------------------------------

def _load_cache() -> Dict:
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


def _save_cache(data: Dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
        os.chmod(CACHE_FILE, 0o600)
    except Exception:
        pass


def _is_fresh(entry: Dict, ttl: int, now: Optional[float] = None) -> bool:
    now = now if now is not None else time.time()
    try:
        return (now - float(entry.get("timestamp", 0))) < ttl
    except (TypeError, ValueError):
        return False


def cache_lookup_local(question: str, ttl: Optional[int] = None) -> List[Dict]:
    """Return fresh local cache entries for a question (may be empty)."""
    cfg = load_config()
    ttl = DEFAULT_TTL if ttl is None else ttl
    ttl = int(cfg.get("hive_cache_ttl", ttl))
    h = prompt_hash(question)
    entries = [e for e in _load_cache().get(h, []) if _is_fresh(e, ttl)]
    return sorted(entries, key=lambda e: e.get("timestamp", 0), reverse=True)


def cache_lookup_local_hash(prompt_hash_value: str, ttl: Optional[int] = None) -> List[Dict]:
    """Fresh entries by hash — used by the P2P handler (no question text needed)."""
    cfg = load_config()
    ttl = DEFAULT_TTL if ttl is None else ttl
    ttl = int(cfg.get("hive_cache_ttl", ttl))
    return [e for e in _load_cache().get(prompt_hash_value, []) if _is_fresh(e, ttl)]


def _prune(data: Dict) -> Dict:
    """Drop oldest entries past MAX_CACHE_ENTRIES. Returns the pruned dict."""
    all_entries = [(hh, e) for hh, es in data.items() for e in es]
    if len(all_entries) > MAX_CACHE_ENTRIES:
        all_entries.sort(key=lambda t: t[1].get("timestamp", 0))
        drop = set()
        for hh, e in all_entries[:len(all_entries) - MAX_CACHE_ENTRIES]:
            drop.add((hh, e.get("timestamp")))
        for hh in list(data.keys()):
            data[hh] = [e for e in data[hh] if (hh, e.get("timestamp")) not in drop]
            if not data[hh]:
                del data[hh]
    return data


def cache_store_local(question: str, model: str, answer: str,
                      origin: str = "local") -> None:
    """Store an answer locally. Prunes oldest entries past MAX_CACHE_ENTRIES."""
    data = _load_cache()
    h = prompt_hash(question)
    entry = {
        "model": model,
        "answer": answer,
        "timestamp": time.time(),
        "origin": origin,
    }
    entries = [e for e in data.get(h, []) if e.get("origin") != origin or e.get("model") != model]
    entries.append(entry)
    data[h] = entries
    _save_cache(_prune(data))


def cache_store_hash(prompt_hash_value: str, model: str, answer: str,
                     timestamp: float, origin: str) -> None:
    """Store an entry received from a peer (keyed by hash — the question text
    is not available on this side). Shapes/lengths are capped defensively."""
    data = _load_cache()
    h = str(prompt_hash_value)[:64]
    data.setdefault(h, []).append({
        "model": str(model)[:120],
        "answer": str(answer)[:20000],
        "timestamp": float(timestamp),
        "origin": str(origin)[:64],
    })
    _save_cache(_prune(data))


def sharing_enabled() -> bool:
    """P2P cache participation is strictly opt-in."""
    try:
        return bool(load_config().get("hive_cache_sharing", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# P2P cache + capability registry
# ---------------------------------------------------------------------------

def peer_capabilities(peer_ip: str, peer_port: int = 11435,
                      use_tls=None, verify_tls: bool = False) -> Dict:
    """Fetch a peer's advertised capabilities (names only — no key material)."""
    from core.p2p import send_remote_command
    res = send_remote_command(peer_ip, "status", {}, port=peer_port,
                             use_tls=use_tls, verify_tls=verify_tls)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "unreachable")}
    try:
        data = json.loads(res["data"])
    except Exception:
        return {"ok": False, "error": "bad status payload"}
    caps = data.get("capabilities", {})
    return {
        "ok": True,
        "name": data.get("name"),
        "version": data.get("version"),
        "providers": caps.get("providers", []),
        "models": caps.get("models", []),
        "features": caps.get("features", []),
    }


def local_capabilities() -> Dict:
    """This node's advertised capabilities — provider/model NAMES only."""
    from core.connect import AuthManager, is_configured
    from core.p2p import P2P_FEATURES, CURRENT_VERSION
    providers = [p for p in AuthManager.PROVIDERS.keys() if is_configured(p)]
    cfg = load_config()
    models = [m for m in cfg.get("detected_local_models", []) if isinstance(m, str)]
    model = cfg.get("jade_model")
    if model and model not in models:
        models.append(model)
    return {
        "providers": sorted(providers),
        "models": sorted(set(models)),
        "features": list(P2P_FEATURES.get(CURRENT_VERSION, P2P_FEATURES.get("0.2.6", []))),
        # Local services — separate from the action feature map so this never
        # affects is_p2p_compatible() action gating. Names/states only.
        "local_services": _local_services(),
    }


def _local_services() -> Dict[str, Any]:
    """Honest advertisement of optional local services (scheduler/dashboard)."""
    services: Dict[str, Any] = {}
    try:
        from core.scheduler import scheduler_status
        st = scheduler_status()
        services["scheduler"] = {
            "available": True,
            "running": bool(st.get("running")),
            "jobs": int(st.get("jobs", 0)),
        }
    except Exception as e:
        services["scheduler"] = {"available": False, "error": str(e)}
    # The dashboard is stdlib-only (http.server) — always available,
    # loopback-bound by default. It is not running until /dashboard starts it.
    services["dashboard"] = {"available": True, "default_bind": "loopback"}
    return services


def cache_lookup_peers(question: str, use_tls=None,
                       verify_tls: bool = False) -> List[Dict]:
    """Ask opted-in peers for cached answers. Returns fresh entries."""
    if not sharing_enabled():
        return []
    from core.p2p import scan_for_jade_peer_endpoints, send_remote_command
    h = prompt_hash(question)
    found: List[Dict] = []
    try:
        peers = scan_for_jade_peer_endpoints(use_tls=use_tls, verify_tls=verify_tls)
    except Exception:
        return []
    for ip, port in peers:
        try:
            res = send_remote_command(ip, "hive_cache_get", {"prompt_hash": h},
                                      port=port, use_tls=use_tls,
                                      verify_tls=verify_tls)
            if res.get("ok"):
                payload = json.loads(res["data"])
                for e in payload.get("entries", []):
                    if isinstance(e, dict) and e.get("answer"):
                        e = dict(e)
                        e.setdefault("origin", f"peer:{ip}")
                        found.append(e)
        except Exception:
            continue
    cfg = load_config()
    ttl = int(cfg.get("hive_cache_ttl", DEFAULT_TTL))
    return [e for e in found if _is_fresh(e, ttl)]


def cache_share_peers(question: str, model: str, answer: str,
                      use_tls=None, verify_tls: bool = False) -> int:
    """Push a fresh answer to opted-in peers. Returns number of peers that stored it."""
    if not sharing_enabled():
        return 0
    from core.p2p import scan_for_jade_peer_endpoints, send_remote_command
    entry = {
        "prompt_hash": prompt_hash(question),
        "model": model,
        "answer": answer,
        "timestamp": time.time(),
    }
    stored = 0
    try:
        peers = scan_for_jade_peer_endpoints(use_tls=use_tls, verify_tls=verify_tls)
    except Exception:
        return 0
    for ip, port in peers:
        try:
            res = send_remote_command(ip, "hive_cache_put", {"entry": entry},
                                      port=port, use_tls=use_tls,
                                      verify_tls=verify_tls)
            if res.get("ok"):
                stored += 1
        except Exception:
            continue
    return stored


# ---------------------------------------------------------------------------
# Multi-model consensus
# ---------------------------------------------------------------------------

# Litellm model ids used when the user hasn't set an explicit default model
# for the provider. Only providers whose litellm mapping is well-established
# are listed — anything else must come from get_default_model().
_HIVE_FALLBACK_MODELS = {
    "openai": "openai/gpt-4o-mini",
    "anthropic": "anthropic/claude-3-5-sonnet-20241022",
    "gemini": "gemini/gemini-2.0-flash",
    "groq": "groq/llama-3.3-70b-versatile",
    "deepseek": "deepseek/deepseek-chat",
    "mistral": "mistral/mistral-large-latest",
    "together": "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "perplexity": "perplexity/llama-3.1-sonar-large-128k-online",
    "cohere": "cohere/command-r-plus",
    "qwen": "together_ai/Qwen/Qwen2.5-72B-Instruct",
}


def resolve_hive_model(provider: str) -> Optional[str]:
    """Litellm model id for a provider, or None if it can't be asked.

    Prefers the user's configured default model; falls back to a known
    litellm mapping. Providers without either are skipped, never guessed.
    """
    provider = provider.lower()
    try:
        from core.services import get_default_model
        custom = get_default_model(provider)
        if custom:
            return custom
    except Exception:
        pass
    if provider == "ollama":
        cfg = load_config()
        model = cfg.get("jade_model", "llama3") or "llama3"
        return model if "/" in model else f"ollama/{model}"
    return _HIVE_FALLBACK_MODELS.get(provider)


def hive_candidates(providers: Optional[List[str]] = None) -> List[Dict]:
    """Configured providers that can actually be asked.

    Each item: {provider, model} or {provider, skipped, reason}.
    """
    from core.connect import AuthManager, is_configured
    names = providers or [p for p in AuthManager.PROVIDERS.keys() if is_configured(p)]
    out = []
    for name in names:
        model = resolve_hive_model(name)
        if model:
            out.append({"provider": name, "model": model})
        else:
            out.append({"provider": name, "skipped": True,
                        "reason": "no model mapping — set one with /models"})
    return out


def _ask_one(provider: str, model: str, question: str, timeout: float) -> Dict:
    """Ask a single provider. Never raises — failures become status dicts."""
    from core.brain import think_structured
    import concurrent.futures
    start = time.time()
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(think_structured, "Hive Mind", question, model)
            res = fut.result(timeout=timeout)
        text = (res.get("text") or "").strip()
        if not res.get("ok") or not text:
            return {"provider": provider, "model": model, "ok": False,
                    "error": res.get("error") or "empty response",
                    "latency_ms": int((time.time() - start) * 1000)}
        return {"provider": provider, "model": model, "ok": True, "text": text,
                "latency_ms": int((time.time() - start) * 1000)}
    except Exception as e:
        return {"provider": provider, "model": model, "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "latency_ms": int((time.time() - start) * 1000)}


def synthesize_consensus(question: str, answers: List[Dict]) -> Dict:
    """Combine per-model answers into one attributed consensus.

    Falls back to a plain concatenation with headers if the synthesizer
    itself fails — the individual answers are never discarded silently.
    """
    from core.brain import think_structured
    sections = "\n\n".join(
        f"--- {a['provider'].upper()} ({a['model']}) ---\n{a['text']}"
        for a in answers
    )
    prompt = (
        "You are the Hive Mind consensus integrator. Below are answers to the "
        f"same question from {len(answers)} different AI models.\n\n"
        f"QUESTION: {question}\n\n{sections}\n\n"
        "Synthesize ONE best answer: keep what the models agree on, reconcile "
        "disagreements explicitly, and drop anything only one model claims "
        "without support. Write the final answer directly — no preamble about "
        "being an AI."
    )
    try:
        res = think_structured("Hive Consensus", prompt)
        text = (res.get("text") or "").strip()
        if res.get("ok") and text:
            return {"ok": True, "text": text, "method": "synthesized"}
    except Exception:
        pass
    fallback = (
        "_Consensus synthesis unavailable — showing individual model answers:_\n\n"
        + sections
    )
    return {"ok": True, "text": fallback, "method": "concatenated"}


def hive_ask(question: str, providers: Optional[List[str]] = None,
             timeout: float = 90.0, use_cache: bool = True,
             max_models: int = 5) -> Dict:
    """Ask the hive a question. Returns a rich result dict.

    Order: shared cache → parallel provider asks → consensus synthesis.
    """
    question = question.strip()
    if not question:
        return {"ok": False, "error": "empty question"}

    if use_cache:
        cached = cache_lookup_local(question)
        if not cached and sharing_enabled():
            cached = cache_lookup_peers(question)
        if cached:
            best = cached[0]
            age = int(time.time() - best.get("timestamp", time.time()))
            return {"ok": True, "text": best["answer"], "method": "cache",
                    "attributions": [{
                        "provider": "cache", "model": best.get("model", "?"),
                        "origin": best.get("origin", "local"), "age_s": age,
                        "ok": True,
                    }]}

    candidates = [c for c in hive_candidates(providers) if not c.get("skipped")]
    skipped = [c for c in hive_candidates(providers) if c.get("skipped")]
    candidates = candidates[:max_models]
    if not candidates:
        return {"ok": False, "error": "no configured providers",
                "skipped": skipped,
                "next_steps": "Run /connect to link at least one AI provider."}

    attributions: List[Dict] = []
    answers: List[Dict] = []
    with ThreadPoolExecutor(max_workers=max(1, len(candidates))) as ex:
        futs = {ex.submit(_ask_one, c["provider"], c["model"], question, timeout): c
                for c in candidates}
        for fut in as_completed(futs):
            r = fut.result()
            attributions.append(r)
            if r["ok"]:
                answers.append(r)

    if not answers:
        return {"ok": False, "error": "all providers failed",
                "attributions": attributions, "skipped": skipped}

    consensus = synthesize_consensus(question, answers)
    cache_store_local(question, "hive-consensus", consensus["text"])
    shared = cache_share_peers(question, "hive-consensus", consensus["text"]) \
        if sharing_enabled() else 0
    return {"ok": True, "text": consensus["text"],
            "method": consensus["method"],
            "attributions": attributions, "skipped": skipped,
            "shared_with_peers": shared}


def display_hive_result(res: Dict) -> None:
    """Render a hive_ask result: consensus + attribution table."""
    if not res.get("ok"):
        from core.ui import ui_error
        console.print(ui_error(
            "Hive mind failed",
            res.get("error", "unknown error"),
            why="",
            next_steps=res.get("next_steps", "Check /connections and try again."),
        ))
        return
    method = res.get("method", "?")
    title = {"cache": "🍯 Hive cache hit", "synthesized": "🐝 Hive consensus",
             "concatenated": "🐝 Hive answers"}.get(method, "🐝 Hive mind")
    console.print(Panel(res["text"], title=f"[bold yellow]{title}[/bold yellow]",
                        border_style="yellow"))
    table = Table(show_header=True, header_style="bold dim", box=None)
    table.add_column("Provider"); table.add_column("Model")
    table.add_column("Status"); table.add_column("Latency")
    for a in res.get("attributions", []):
        status = "[green]✓[/green]" if a.get("ok") else f"[red]✗ {a.get('error', '')[:60]}[/red]"
        lat = f"{a.get('latency_ms', '?')}ms" if a.get("ok") else \
            (f"{a.get('age_s', '?')}s ago" if a.get("origin") else "—")
        table.add_row(a.get("provider", "?"), a.get("model", "?"), status, lat)
    for s in res.get("skipped", []):
        table.add_row(s.get("provider", "?"), "—",
                      f"[dim]skipped: {s.get('reason', '')[:60]}[/dim]", "—")
    console.print(table)
    if res.get("shared_with_peers"):
        console.print(f"[dim]Shared with {res['shared_with_peers']} peer(s).[/dim]")
