"""
Robust LLM brain for JADE.

Improvements:
- Intelligent task-based routing
- Multi-tier refinement loop (Local -> Network -> Cloud)
- Hardware acceleration (GPU/Apple Silicon) detection
- Secure key injection for LiteLLM
- P2P Swarm (Hive Mind) integration
"""

import requests
import os
import json
import logging
import time
import sys
import random
from typing import Dict, Optional, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from core.config import load_config, save_config, get_env_with_config
from core.devmode import timed_request, effective_system_prompt

# litellm is heavy (~3s import). Load it lazily on first actual LLM call so
# `jade --help` and other non-LLM paths start fast.
_litellm_mod = None


def _litellm():
    """Import litellm on first use (cached). Never import at module level."""
    global _litellm_mod
    if _litellm_mod is None:
        import litellm as _m
        # Keep LiteLLM quiet: never append full tracebacks to exception
        # messages (a failed provider call should show one clean error line,
        # not an internal stack dump), and disable verbose request logging.
        _m.suppress_debug_info = True
        _m.set_verbose = False
        _litellm_mod = _m
    return _litellm_mod


def _short_err(e: Exception) -> str:
    """One-line error text for user display.

    LiteLLM unconditionally appends tracebacks to some exception messages
    (e.g. APIConnectionError); the user only ever needs the first line.
    """
    return str(e).splitlines()[0] if str(e).strip() else repr(e)


def _short_err_str(text: str) -> str:
    """First line of an error string for user display."""
    text = str(text).strip()
    return text.splitlines()[0] if text else "unknown error"

console = Console()
logger = logging.getLogger("jade_brain")

# -------------------------
# Behavioral Mandates
# -------------------------
SYSTEM_PROMPT = """You are JADE, a highly intelligent, proactive, and precise personal AI coding assistant.

Core Directives:
1. TECHNICAL HELPFULNESS: Provide direct, accurate technical help for coding and system tasks.
2. PROACTIVE RESOLUTION: For file/folder/code tasks, automatically find, analyze, suggest fixes, and offer implementations.
3. INTENT INTERPRETATION: Parse casual language, typos, shorthand, and slang intelligently. Make your best reasonable guess.
4. STEP-BY-STEP REASONING: Output reasoning inside <THINKING>...</THINKING> tags, then provide the direct output or tool call.

Few-Shot Examples:
User: "find so-and-so folder analyze it fix code"
→ You: Locate the folder, analyze all code inside, list issues, and offer to fix them.

User: "design me code for a login system"
→ You: Immediately start designing a clean, secure login system with best practices.

User: "make this faster"
→ You: Look at the code, identify bottlenecks, and give optimized version.

Current task: """

from core.personalities import (
    PERSONALITIES as _PERSONALITY_REGISTRY,
    resolve_personality,
    DEFAULT_PERSONALITY,
)

# Backwards-compatible name -> prompt-text mapping (legacy keys resolve too).
PERSONALITIES = {name: p.system_prompt for name, p in _PERSONALITY_REGISTRY.items()}
PERSONALITIES.update({
    "sarcastic": _PERSONALITY_REGISTRY["witty"].system_prompt,
    "concise": _PERSONALITY_REGISTRY["professional"].system_prompt,
    "mentor": _PERSONALITY_REGISTRY["professional"].system_prompt,
    "nave_ai": _PERSONALITY_REGISTRY["jade"].system_prompt,
})

# -------------------------
# Hardware & Network Utilities
# -------------------------
def enable_gpu_offload():
    """Check for NVIDIA/Apple Silicon acceleration and enable offloading if possible."""
    try:
        if sys.platform == "darwin":
            # Check for Apple Silicon (Metal)
            import subprocess
            res = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True)
            if "Apple" in res.stdout:
                console.print("[dim][green]✅ Apple Silicon detected - enabling Metal acceleration[/green][/dim]")
                os.environ["OLLAMA_FLASH_ATTENTION"] = "1"
                return True
        elif sys.platform == "linux":
            # Check for NVIDIA
            import subprocess
            try:
                result = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=3)
                if "NVIDIA" in result.stdout:
                    console.print("[dim][green]✅ NVIDIA GPU detected - enabling layer offloading[/green][/dim]")
                    os.environ["OLLAMA_NUM_GPU_LAYERS"] = "999"
                    os.environ["OLLAMA_FLASH_ATTENTION"] = "1"
                    return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

            # Check for AMD (ROCm)
            try:
                result = subprocess.run(["rocminfo"], capture_output=True, text=True, timeout=3)
                if "AMDGPU" in result.stdout:
                    console.print("[dim][green]✅ AMD GPU detected - enabling ROCm acceleration[/green][/dim]")
                    return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
    except Exception:
        pass
    return False

# -------------------------
# Provider Pattern & Model Management
# -------------------------
class ModelManager:
    def __init__(self):
        cfg = load_config()
        self.current_model = cfg.get("jade_model", "llama3")
        # Ensure 'ollama/' prefix if no provider is specified
        if "/" not in self.current_model:
            self.current_model = f"ollama/{self.current_model}"
            
        self.available_models = {
            "local": "ollama/llama3",
            "gpt4": "openai/gpt-4o",
            "claude": "anthropic/claude-3-5-sonnet-20241022",
            "gemini": "gemini/gemini-2.0-flash",
            "mistral": "ollama/mistral",
            "deepseek": "deepseek/deepseek-chat",
            "qwen": "together_ai/Qwen/Qwen2.5-72B-Instruct"
        }

    def switch_model(self, nickname):
        if nickname in self.available_models:
            self.current_model = self.available_models[nickname]
            return f"✅ Switched to {nickname} ({self.current_model})"
        return f"❌ Model '{nickname}' not found."

    def _ensure_provider(self, model_name: str) -> str:
        if "/" not in model_name:
            return f"ollama/{model_name}"
        return model_name

    def chat(self, prompt, context="", personality_prompt=None):
        from core.services import set_key_for_litellm
        cfg = load_config()
        model_name = self._ensure_provider(self.current_model)
        system_prompt = effective_system_prompt(SYSTEM_PROMPT)
        if personality_prompt:
            system_prompt = system_prompt + "\n\nActive personality:\n" + personality_prompt

        # Handle Ollama Cloud routing
        if model_name.endswith("-cloud"):
            cloud_host = cfg.get("ollama_cloud_host") or "https://ollama.com/api"
            token = cfg.get("ollama_token") or os.getenv("OLLAMA_TOKEN", "")

            if token:
                os.environ["OLLAMA_API_BASE"] = cloud_host
                os.environ["OLLAMA_API_KEY"] = token
                actual_model = model_name.replace("-cloud", "")
                actual_model = self._ensure_provider(actual_model)

                try:
                    with timed_request("ollama-cloud", actual_model):
                        res = _litellm().completion(
                            model=actual_model,
                            api_base=cloud_host,
                            extra_headers={"Authorization": f"Bearer {token}"},
                            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"{context}\n\nTask: {prompt}"}]
                        )
                    return res.choices[0].message.content
                except Exception as e:
                    return f"⚠️ Ollama Cloud Error: {_short_err(e)}"
            else:
                return "❌ Ollama Cloud model requested but no 'ollama_token' found in config or environment."

        provider = model_name.split('/')[0]
        set_key_for_litellm(provider)
        try:
            with timed_request(provider, model_name):
                res = _litellm().completion(
                    model=model_name,
                    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"{context}\n\nTask: {prompt}"}]
                )
            return res.choices[0].message.content
        except Exception as e:
            return f"⚠️ AI Error: {_short_err(e)}"

    def stream_chat(self, prompt, context=""):
        from core.services import set_key_for_litellm
        model_name = self._ensure_provider(self.current_model)
        provider = model_name.split('/')[0]
        system_prompt = effective_system_prompt(SYSTEM_PROMPT)
        set_key_for_litellm(provider)
        try:
            with timed_request(provider, model_name):
                response = _litellm().completion(
                    model=model_name,
                    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"{context}\n\nTask: {prompt}"}],
                    stream=True
                )
                for chunk in response:
                    content = chunk.choices[0].delta.content
                    if content:
                        yield content
        except Exception as e:
            yield f"⚠️ AI Error: {_short_err(e)}"

class LLMProvider:
    def __init__(self, model): self.model = model
    def ask(self, prompt, context="", options=None): raise NotImplementedError

class LiteLLMProvider(LLMProvider):
    def __init__(self, model):
        super().__init__(model)
        self.provider = model.split('/')[0] if '/' in model else "openai"

    def ask(self, prompt, context="", options=None):
        from core.services import call_model
        res = call_model(self.provider, messages_or_text=f"{context}\n\nTask: {prompt}", model=self.model)
        if res.get("ok"):
            return res.get("text")
        return f"Error: {res.get('error')}"

class GeminiProvider(LiteLLMProvider):
    def __init__(self, model="gemini/gemini-2.0-flash"):
        super().__init__(model)

class OllamaProvider(LLMProvider):
    def __init__(self, model=None):
        super().__init__(model or get_env_with_config("jade_model") or "llama3")
        cfg = load_config()
        self.hosts = cfg.get("ollama_hosts", ["http://localhost:11434"])
        self.host = random.choice(self.hosts)

    def ask(self, prompt, context="", options=None):
        payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        if options: payload["options"] = options

        # Try hosts in pool, tracking WHY each failed for an actionable error.
        tried_hosts = []
        conn_failed = []
        model_missing = False
        for host in [self.host] + [h for h in self.hosts if h != self.host]:
            tried_hosts.append(host)
            try:
                r = requests.post(f"{host}/api/chat", json=payload, timeout=15)
                if r.status_code == 404:
                    model_missing = True  # model not on this host; maybe on another
                    continue
                r.raise_for_status()
                data = r.json()
                # Usage telemetry for `/ollama stats` — best-effort, never raises.
                try:
                    from core.ollama_mgmt import note_chat_usage
                    note_chat_usage(self.model, host, data)
                except Exception:
                    pass
                return data["message"]["content"]
            except requests.ConnectionError:
                conn_failed.append(host)
                continue
            except Exception:
                continue

        # Try Cloud Fallback
        cfg = load_config()
        cloud_host = cfg.get("ollama_cloud_host")
        if cloud_host:
            try:
                r = requests.post(f"{cloud_host}/api/chat", json=payload, timeout=20)
                r.raise_for_status()
                return r.json()["message"]["content"]
            except Exception: pass

        # Honest, actionable final errors — one line, no traceback.
        if model_missing and not conn_failed:
            return (f"Ollama doesn't have model '{self.model}'. "
                    f"Pull it with `/ollama pull {self.model}`, or pick another with /models.")
        if conn_failed and len(conn_failed) == len(tried_hosts):
            return (f"Couldn't reach Ollama at {', '.join(tried_hosts)}. "
                    "Is it running? Start it with `ollama serve` — or link a cloud provider with /connect.")
        from core.services import call_model
        res = call_model("gemini", messages_or_text=prompt)
        if res.get("ok"):
            return res.get("text")
        return f"Error: {_short_err_str(res.get('error', 'all fallbacks failed'))}"

# -------------------------
# Intent & Routing
# -------------------------
def get_task_category(task: str) -> str:
    prompt = f"Categorize this task: [coding, creative, research, general]. Task: {task}. Return ONLY the category name."
    try:
        provider = get_provider()
        res = provider.ask(prompt)
        cat = res.strip().lower()
        if cat in ["coding", "creative", "research", "general"]:
            return cat
        return "general"
    except Exception as e:
        logger.error(f"Routing error: {e}")
        return "general"

def multibrain_think(task: str, providers: Optional[List[str]] = None) -> Dict[str, Any]:
    from core.services import set_key_for_litellm
    from core.ui import set_warp_status, clear_warp_status
    
    # Lazy import to avoid circular dependency
    try:
        from tools.search import web_search
    except ImportError:
        def web_search(q): return "Search unavailable."

    enable_gpu_offload()
    category = get_task_category(task)
    search_context = web_search(task)
    
    routing_table = {
        "coding": ["groq/deepseek-coder", "openai/gpt-4o"],
        "creative": ["together_ai/Qwen/Qwen2.5-72B-Instruct", "groq/llama-3.3-70b-versatile"],
        "research": ["gemini/gemini-2.0-flash", "together_ai/Qwen/Qwen2.5-72B-Instruct"],
        "general": ["groq/llama-3.3-70b-versatile", "openai/gpt-4o-mini"]
    }
    target_models = routing_table.get(category, routing_table["general"])
    
    set_warp_status(f"Multi-Brain: {category.upper()} Refinement...")
    console.print(Panel(f"🧠 [bold cyan]SMART TIERED REFINEMENT[/bold cyan]\nCategory: {category.upper()}", border_style="cyan"))

    responses = {}

    # Tier 1: Local & Local Cloud
    cfg = load_config()
    mgr = ModelManager()
    local_models = cfg.get("detected_local_models", ["llama3", "gemma4"])
    
    # Add Ollama Cloud if token present
    if cfg.get("ollama_token") or os.getenv("OLLAMA_TOKEN"):
        cloud_m = f"{mgr.current_model}-cloud"
        if cloud_m not in local_models: local_models.append(cloud_m)

    for m in local_models:
        try:
            if m.endswith("-cloud"):
                res = mgr.chat(task, context=f"Ollama Cloud Refinement\n{task}")
            else:
                m_str = f"ollama/{m}" if "ollama/" not in m else m
                res = _litellm().completion(model=m_str, messages=[{"role":"user", "content": task}], timeout=10).choices[0].message.content
            
            if res:
                responses[m] = res
                console.print(f"  [green]✓[/green] Local/Local-Cloud {m} responded.")
        except Exception: continue

    # Tier 1.5: P2P Swarm (Hive Mind)
    try:
        from core.p2p import scan_for_jade_peers, send_remote_command
        import json
        
        if not hasattr(multibrain_think, "_peer_index"):
            multibrain_think._peer_index = 0
            
        all_peers = scan_for_jade_peers()
        
        if all_peers:
            all_peers.sort()
            rotation = multibrain_think._peer_index % len(all_peers)
            balanced_peers = all_peers[rotation:] + all_peers[:rotation]
            multibrain_think._peer_index += 1
            
            swarm_subset = balanced_peers[:5]
            
            def ask_peer(peer_ip):
                stat_res = send_remote_command(peer_ip, "status", {})
                if stat_res.get("ok"):
                    try:
                        s_data = json.loads(stat_res["data"])
                        if not s_data.get("hive_load", {}).get("safe", True):
                            return None
                        
                        target_model = None
                        remote_models = s_data.get("local_models", [])
                        if remote_models:
                            unused = [rm for rm in remote_models if rm not in local_models]
                            if unused: target_model = random.choice(unused)

                        res = send_remote_command(peer_ip, "think", {"task": task, "model": target_model})
                        if res.get("ok"):
                            data = json.loads(res["data"])
                            return data.get("text"), target_model or s_data.get("model")
                    except Exception: pass
                return None

            with ThreadPoolExecutor(max_workers=len(swarm_subset)) as executor:
                futures = {executor.submit(ask_peer, p): p for p in swarm_subset}
                for future in as_completed(futures):
                    p = futures[future]
                    result = future.result()
                    if result:
                        text, m_used = result
                        m_str = f"swarm-peer/{p} ({m_used})"
                        responses[m_str] = text
                        console.print(f"  [green]✓[/green] Swarm Peer {p} responded ({m_used}).")
    except Exception as e:
        console.print(f"  [yellow]![/yellow] Swarm P2P error: {e}")

    # Tier 2: Cloud
    def ask_cloud(m_str):
        provider_name = m_str.split('/')[0]
        set_key_for_litellm(provider_name)
        try:
            return _litellm().completion(model=m_str, messages=[{"role": "user", "content": f"Context: {search_context}\n\nTask: {task}"}], timeout=20).choices[0].message.content
        except Exception: return None

    with ThreadPoolExecutor(max_workers=len(target_models)) as executor:
        futures = {executor.submit(ask_cloud, m): m for m in target_models}
        for future in as_completed(futures):
            m = futures[future]
            res = future.result()
            if res:
                responses[m] = res
                console.print(f"  [green]✓[/green] Cloud {m.upper()} responded.")

    if not responses:
        try:
            return {"ok": True, "text": get_provider().ask(task), "provider": "emergency-local"}
        except Exception:
            return {"ok": False, "error": "All AI tiers failed to respond."}

    # Tier 3: Synthesis
    context_str = "\n\n".join([f"--- SOURCE: {m.upper()} ---\n{r}" for m, r in responses.items()])
    consensus_prompt = f"{SYSTEM_PROMPT}\n\nSynthesize these expert perspectives for the task: {task}\n\n{context_str}"
    
    res = think_structured("Consensus Integrator", consensus_prompt)
    clear_warp_status()
    return res

# -------------------------
# Core Think Entrypoints
# -------------------------
def think_structured(context: str, task: str, model: Optional[str] = None, prompt_name: Optional[str] = None) -> Dict[str, Any]:
    from core.ui import set_warp_status, clear_warp_status
    
    memory_context = ""
    try: 
        from memory.vector import search
        relevant_memories = search(task, k=3)
        memory_context = "\n".join([f"- {m}" for m in relevant_memories])
    except Exception: pass
    
    # Active project context (Round C): instructions + attached files.
    project_context = ""
    try:
        from core.projects import project_context_block
        project_context = project_context_block()
    except Exception: pass

    personality = resolve_personality(get_env_with_config("personality") or DEFAULT_PERSONALITY)
    personality_prompt = personality.system_prompt

    # Use ModelManager for standard thinking
    mgr = ModelManager()
    if model: mgr.current_model = model

    text = mgr.chat(task, context=context + "\n" + memory_context + "\n" + project_context,
                    personality_prompt=personality_prompt)
    return {"ok": True, "text": text, "provider": mgr.current_model}

def think(context: str, task: str, model: Optional[str] = None, prompt_name: Optional[str] = None):
    res = think_structured(context, task, model, prompt_name)
    text = res.get("text")
    # ... (omitted)
    return text

def think_stream(context: str, task: str, model: Optional[str] = None, prompt_name: Optional[str] = None):
    mgr = ModelManager()
    if model: mgr.current_model = model
    return mgr.stream_chat(task, context=context)

def get_provider(model_override=None, task_hint=None):
    cfg = load_config()
    provider_name = cfg.get("provider", "ollama")
    model = model_override or cfg.get("jade_model")
    
    if provider_name == "ollama":
        return OllamaProvider(model=model)
    elif provider_name == "gemini":
        return GeminiProvider(model=model or "gemini/gemini-2.0-flash")
    else:
        # Generic LiteLLM fallback
        if not model:
            model = f"{provider_name}/default" # Services will handle defaults
        return LiteLLMProvider(model=model)

def distribute_task(task: str) -> str:
    from core.torrent_balancer import chunk_task, aggregate_results
    from core.p2p import scan_for_jade_peers, send_remote_command
    
    peers = scan_for_jade_peers()
    if not peers:
        # Fallback to local
        return think(context="Local Fallback", task=task)
        
    chunks = chunk_task(task, num_chunks=len(peers) + 1)
    results = []
    
    # Send chunks to peers
    for i, peer in enumerate(peers):
        if i >= len(chunks) - 1: break
        res = send_remote_command(peer, "execute_chunk", {"task": chunks[i]})
        if res.get("ok"):
            data = json.loads(res.get("data", "{}"))
            results.append(data.get("text", ""))
            
    # Process remaining chunk locally
    results.append(think(context="Local Chunk", task=chunks[-1]))
    
    return aggregate_results(results)

def _get_provider_name_from_obj(provider_obj) -> str:
    if isinstance(provider_obj, OllamaProvider): return "ollama"
    if isinstance(provider_obj, GeminiProvider): return "gemini"
    if isinstance(provider_obj, LiteLLMProvider): return provider_obj.provider
    return "unknown"
