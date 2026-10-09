"""
Service manager: API keys, provider validation, model listing, routing, adapters.

Adds support for 'nemotron' and 'qwen', robust Ollama repair helper, and normalized error types.
"""

from typing import Dict, Optional, List, Any
import requests
import json
import os
import sys
import time
import webbrowser
import keyring
import shutil
import subprocess
from core.config import load_config, save_config
from rich.console import Console

console = Console()

API_KEYS_KEY = "api_keys"
KEYRING_SERVICE_NAME = "jade_cli"
# Pre-rename service name: read for compatibility, never written.
_KEYRING_SERVICE_NAME_LEGACY = "jarvis_cli"
DEFAULT_MODELS_KEY = "default_models"
# Providers with real support in this codebase: either connection validation
# in validate_provider_connection() or a key mapping in set_key_for_litellm().
# Image/video/speech services and invented names were removed — they were
# advertised but had no working integration.
KNOWN_PROVIDERS = [
    "openai", "anthropic", "gemini", "ollama", "mistral", "deepseek", "groq",
    "together", "cohere", "perplexity", "github",
    "gpt4all", "llama_cpp", "vllm", "sglang", "nemotron", "qwen", "local",
]

# Environment variable fallbacks for API keys. Providers with a widely used
# alternate variable name list it second.
ENV_KEY_VARS = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "cohere": ("COHERE_API_KEY",),
    "perplexity": ("PERPLEXITY_API_KEY",),
    "qwen": ("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
    "github": ("GITHUB_TOKEN", "GH_TOKEN"),
}

# Key/model utilities
def _load_keys() -> Dict[str, str]:
    cfg = load_config()
    return cfg.get(API_KEYS_KEY, {})

def _save_keys(keys: Dict[str, str]):
    cfg = load_config()
    cfg[API_KEYS_KEY] = keys
    save_config(cfg)

def set_api_key(provider: str, key: str):
    try:
        keyring.set_password(KEYRING_SERVICE_NAME, provider.lower(), key)
    except Exception as e:
        console.print(f"[red]Failed to save key in keyring: {e}[/red]")
        # Fallback to config if keyring fails
        keys = _load_keys()
        keys[provider.lower()] = key
        _save_keys(keys)
    return {"ok": True, "provider": provider.lower()}

def get_api_key(provider: str) -> Optional[str]:
    # First try keyring
    try:
        key = keyring.get_password(KEYRING_SERVICE_NAME, provider.lower())
        if not key:
            key = keyring.get_password(_KEYRING_SERVICE_NAME_LEGACY, provider.lower())
        if key: return key
    except Exception:
        pass

    # Fallback to config file
    key = _load_keys().get(provider.lower())
    if key:
        return key

    # Fallback to environment variables
    for var in ENV_KEY_VARS.get(provider.lower(), ()):
        val = os.getenv(var)
        if val:
            return val
    return None

def set_key_for_litellm(provider: str):
    """Fetch key from secure storage and set in os.environ for litellm."""
    key = get_api_key(provider)
    if key:
        env_map = {
            "groq": "GROQ_API_KEY",
            "openai": "OPENAI_API_KEY",
            "together": "TOGETHER_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "mistral": "MISTRAL_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY"
        }
        os.environ[env_map.get(provider.lower(), f"{provider.upper()}_API_KEY")] = key
    return key

def unset_api_key(provider: str):
    for service in (KEYRING_SERVICE_NAME, _KEYRING_SERVICE_NAME_LEGACY):
        try:
            keyring.delete_password(service, provider.lower())
        except Exception:
            pass

    keys = _load_keys()
    if provider.lower() in keys:
        del keys[provider.lower()]
        _save_keys(keys)
    return {"ok": True, "provider": provider.lower()}

def list_available_keys() -> List[str]:
    """Return a list of providers that have an API key configured."""
    available = []
    
    # Check keyring
    for p in KNOWN_PROVIDERS:
        try:
            if keyring.get_password(KEYRING_SERVICE_NAME, p.lower()):
                available.append(p)
        except Exception:
            pass
            
    # Check config
    cfg_keys = _load_keys()
    for p, val in cfg_keys.items():
        if val and p not in available:
            available.append(p)
            
    # Check environment (common ones)
    for p, var in ENV_KEY_VARS.items():
        if p not in available and any(os.getenv(v) for v in var):
            available.append(p)

    return sorted(list(set(available)))

def _load_default_models() -> Dict[str, str]:
    cfg = load_config()
    return cfg.get(DEFAULT_MODELS_KEY, {})

def _save_default_models(d: Dict[str, str]):
    cfg = load_config()
    cfg[DEFAULT_MODELS_KEY] = d
    save_config(cfg)

def set_default_model(provider: str, model_name: str):
    d = _load_default_models()
    d[provider.lower()] = model_name
    _save_default_models(d)
    return {"ok": True, "provider": provider.lower(), "model": model_name}

def get_default_model(provider: str) -> Optional[str]:
    return _load_default_models().get(provider.lower())

def get_connected_providers() -> List[str]:
    """Return a list of all providers that have an API key or host configured."""
    connected = []
    cfg = load_config()
    for p in KNOWN_PROVIDERS:
        if p == "ollama":
            if cfg.get("ollama_host") or cfg.get("ollama_hosts"): connected.append(p)
            continue
        
        # Check for host-based providers
        if f"{p}_host" in cfg and cfg.get(f"{p}_host"):
            connected.append(p)
            continue
            
        # Check for key-based providers
        if get_api_key(p):
            connected.append(p)
            
    return connected

# --------------------
# Validation helpers (enhanced)
# --------------------
def validate_gemini(key: str, timeout: float = 5.0) -> Dict:
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return {"ok": True, "provider": "gemini", "note": "Gemini key validated"}
        if r.status_code == 400:
            return {"ok": False, "error": "Gemini key invalid", "error_type": "unauthorized"}
        return {"ok": False, "error": f"Gemini error {r.status_code}", "error_type": "other"}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_type": "unreachable"}

def validate_openai(key: str, timeout: float = 5.0) -> Dict:
    url = "https://api.openai.com/v1/models"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 200:
            return {"ok": True, "provider": "openai", "models_count": len(r.json().get("data", []))}
        if r.status_code == 401:
            return {"ok": False, "error": "Unauthorized: invalid OpenAI key", "error_type": "unauthorized"}
        return {"ok": False, "error": f"OpenAI returned HTTP {r.status_code}", "error_type": "other"}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_type": "unreachable"}

def validate_ollama(host: str, timeout: float = 2.0) -> Dict:
    """
    Probes common Ollama endpoints and returns a helpful status.
    """
    host = host.rstrip("/")
    endpoints = ["/api/tags", "/api/models", "/api/health", "/api/completions", ""]
    last_exc = None
    for p in endpoints:
        url = host + p if p else host
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return {"ok": True, "provider": "ollama", "endpoint": url}
            if r.status_code == 401:
                return {"ok": False, "error": "Ollama requires authentication (401)", "error_type": "auth", "endpoint": url}
            if r.status_code == 404:
                continue
            if 400 <= r.status_code < 500:
                return {"ok": False, "error": f"Ollama returned HTTP {r.status_code}", "error_type": "auth", "endpoint": url}
        except requests.exceptions.RequestException as e:
            last_exc = e
            continue
    return {"ok": False, "error": f"Could not reach Ollama at {host}. Error: {last_exc}", "error_type": "unreachable"}

# --------------------
# Provider validation metadata (audited 2026-09-25).
#
# Every key provider below is validated against a real, documented endpoint —
# no fake "key present" checks. "free" labels describe legitimate free/public
# tiers verified against provider documentation; providers without a verified
# free tier have free=None (paid API) rather than a guessed claim.
# --------------------
PROVIDER_VALIDATION = {
    "openai": {
        "auth": "bearer",
        "validate_url": "https://api.openai.com/v1/models",
        "key_url": "https://platform.openai.com/api-keys",
        "docs": "https://platform.openai.com/docs/api-reference/models/list",
        "free": None,
    },
    "gemini": {
        "auth": "query_param",
        "validate_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "key_url": "https://aistudio.google.com/app/apikey",
        "docs": "https://ai.google.dev/gemini-api/docs/models",
        "free": "Free tier via Google AI Studio (no credit card required)",
    },
    "anthropic": {
        "auth": "x-api-key",
        "validate_url": "https://api.anthropic.com/v1/models",
        "extra_headers": {"anthropic-version": "2023-06-01"},
        "key_url": "https://console.anthropic.com/settings/keys",
        "docs": "https://docs.anthropic.com/en/api/models-list",
        "free": None,
    },
    "cohere": {
        "auth": "bearer",
        "validate_url": "https://api.cohere.com/v1/models",
        "key_url": "https://dashboard.cohere.com/api-keys",
        "docs": "https://docs.cohere.com/reference/list-models",
        "free": "Free developer trial keys (rate-limited)",
    },
    "mistral": {
        "auth": "bearer",
        "validate_url": "https://api.mistral.ai/v1/models",
        "key_url": "https://console.mistral.ai/api-keys/",
        "docs": "https://docs.mistral.ai/api/",
        "free": "Free experimentation tier on La Plateforme",
    },
    "deepseek": {
        "auth": "bearer",
        "validate_url": "https://api.deepseek.com/models",
        "key_url": "https://platform.deepseek.com/api_keys",
        "docs": "https://api-docs.deepseek.com/api/list-models",
        "free": None,
    },
    "groq": {
        "auth": "bearer",
        "validate_url": "https://api.groq.com/openai/v1/models",
        "key_url": "https://console.groq.com/keys",
        "docs": "https://console.groq.com/docs/models",
        "free": "Always-free tier (no credit card, rate-limited)",
    },
    "together": {
        "auth": "bearer",
        "validate_url": "https://api.together.xyz/v1/models",
        "key_url": "https://api.together.xyz/settings/api-keys",
        "docs": "https://docs.together.ai/reference/get-models",
        "free": "$5 in free credits on new signups",
    },
    "qwen": {
        "auth": "bearer",
        "validate_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1#/api-key",
        "docs": "https://www.alibabacloud.com/help/en/model-studio/",
        "free": None,
        "note": "International endpoint; China-region accounts may need their regional endpoint.",
    },
    "github": {
        "auth": "bearer",
        "validate_url": "https://api.github.com/user",
        "key_url": "https://github.com/settings/tokens",
        "docs": "https://docs.github.com/en/rest/users/users#get-the-authenticated-user",
        "free": "Free — personal access tokens cost nothing",
    },
    # Perplexity publishes no free key-validation endpoint; validation uses a
    # minimal 1-token chat completion (documented endpoint, negligible cost).
    "perplexity": {
        "auth": "bearer",
        "validate_url": "https://api.perplexity.ai/chat/completions",
        "validate_method": "POST",
        "key_url": "https://www.perplexity.ai/settings/api",
        "docs": "https://docs.perplexity.ai/api-reference/chat-completions",
        "free": None,
    },
}

# Providers whose validation is key-based (not host-based).
KEY_PROVIDERS = tuple(PROVIDER_VALIDATION.keys())


def _validation_hint(provider: str, error_type: Optional[str],
                     status_code: Optional[int] = None) -> str:
    """One actionable next step for a failed validation."""
    meta = PROVIDER_VALIDATION.get(provider, {})
    key_url = meta.get("key_url") or "the provider dashboard"
    if error_type == "unauthorized":
        code = f" (HTTP {status_code})" if status_code else ""
        return (f"Key rejected{code}. Create a fresh key at {key_url} "
                f"and run `/connect {provider}` again.")
    if error_type == "unreachable":
        return ("Could not reach the provider API. Check your network/VPN "
                "connection and try again in a moment.")
    if error_type == "http_error":
        docs = meta.get("docs") or key_url
        return (f"Server reachable but returned HTTP {status_code}. The "
                f"validation endpoint may have moved — check {docs}")
    return ""


def _enrich_validation(provider: str, result: Dict) -> Dict:
    """Attach key_url, free-tier label, and an actionable hint to a result.

    Never changes ok/error — only adds context fields.
    """
    meta = PROVIDER_VALIDATION.get(provider, {})
    result = dict(result)
    if meta.get("key_url"):
        result.setdefault("key_url", meta["key_url"])
    if meta.get("free"):
        result.setdefault("free", meta["free"])
    if not result.get("ok") and not result.get("hint"):
        hint = _validation_hint(provider, result.get("error_type"),
                                result.get("status_code"))
        if hint:
            result["hint"] = hint
    return result


def validate_key_provider(provider: str, key: str, timeout: float = 8.0) -> Dict:
    """Validate a key provider against its real, documented endpoint.

    Returns {"ok", "provider", "note"?/"models_count"?, "error"?, "error_type"?}.
    """
    provider = provider.lower()
    meta = PROVIDER_VALIDATION.get(provider)
    if meta is None:
        return {"ok": False, "provider": provider,
                "error": f"Provider '{provider}' not supported for validation.",
                "error_type": "other"}
    url = meta["validate_url"]
    headers = dict(meta.get("extra_headers") or {})
    if meta.get("auth") == "x-api-key":
        headers["x-api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    try:
        if meta.get("validate_method") == "POST":
            # Perplexity: minimal 1-token completion on the cheapest model.
            r = requests.post(
                url, headers={**headers, "Content-Type": "application/json"},
                json={"model": "sonar", "max_tokens": 1,
                      "messages": [{"role": "user", "content": "Reply with: ok"}]},
                timeout=timeout)
        else:
            r = requests.get(url, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        return {"ok": False, "provider": provider,
                "error": f"Could not reach {provider}: {e}",
                "error_type": "unreachable"}
    if r.status_code == 200:
        try:
            data = r.json()
        except Exception:
            data = None
        result: Dict[str, Any] = {"ok": True, "provider": provider}
        if provider == "github" and isinstance(data, dict) and data.get("login"):
            result["note"] = f"Token valid (GitHub user: {data['login']})"
        elif isinstance(data, dict) and isinstance(data.get("data"), list):
            count = len(data["data"])
            result["models_count"] = count
            result["note"] = f"{provider} key validated ({count} models visible)"
        else:
            result["note"] = f"{provider} key validated"
        if provider == "perplexity":
            result["note"] += " (validation used a minimal 1-token completion)"
        return result
    if r.status_code in (401, 403):
        return {"ok": False, "provider": provider,
                "error": f"{provider} key rejected (HTTP {r.status_code})",
                "error_type": "unauthorized", "status_code": r.status_code}
    return {"ok": False, "provider": provider,
            "error": f"{provider} returned HTTP {r.status_code}: {r.text[:200]}",
            "error_type": "http_error", "status_code": r.status_code}


def validate_generic_host(host: str, timeout: float = 2.0) -> Dict:
    """Probe a host-based provider. Only 2xx counts as reachable — a 4xx/5xx
    means the server answered but the endpoint is wrong, which is actionable."""
    try:
        r = requests.get(host, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": str(e), "error_type": "unreachable",
                "hint": "Host unreachable — check the URL and that the server is running."}
    if 200 <= r.status_code < 300:
        return {"ok": True, "status": r.status_code}
    return {"ok": False, "error": f"Host reachable but returned HTTP {r.status_code}",
            "error_type": "http_error", "status_code": r.status_code,
            "hint": f"Server answered with HTTP {r.status_code} — check the base URL/path."}

def _missing_key_result(provider: str) -> Dict:
    meta = PROVIDER_VALIDATION.get(provider, {})
    key_url = meta.get("key_url") or "the provider dashboard"
    result: Dict[str, Any] = {
        "ok": False, "provider": provider,
        "error": f"{provider} key not configured",
        "error_type": "unauthorized",
        "hint": f"Get a key at {key_url} then run `/connect {provider}`.",
    }
    return _enrich_validation(provider, result)


def validate_provider_connection(provider: str, extra: Optional[Dict[str, Any]] = None) -> Dict:
    provider = provider.lower()
    extra = extra or {}
    if provider in ("openai", "gemini"):
        # These two keep their dedicated validators (real endpoint checks).
        key = extra.get("key") or get_api_key(provider)
        if not key:
            return _missing_key_result(provider)
        res = validate_openai(key) if provider == "openai" else validate_gemini(key)
        return _enrich_validation(provider, res)
    if provider in KEY_PROVIDERS:
        key = extra.get("key") or get_api_key(provider)
        if not key:
            if provider == "qwen":
                # Graceful fallback: qwen was historically host-configured.
                cfg = load_config()
                host = extra.get("host") or cfg.get("qwen_host")
                if host:
                    res = validate_generic_host(host)
                    res["note"] = "No API key set; validated saved host only."
                    return res
            return _missing_key_result(provider)
        return _enrich_validation(provider, validate_key_provider(provider, key))
    if provider == "ollama":
        cfg = load_config()
        host = extra.get("host") or cfg.get("ollama_host")
        if not host: return {"ok": False, "error": "Ollama host not configured", "error_type": "unreachable"}
        return validate_ollama(host)
    if provider in ("gpt4all", "llama_cpp", "vllm", "sglang", "nemotron", "local"):
        cfg = load_config()
        host = extra.get("host") or cfg.get(f"{provider}_host")
        if not host: return {"ok": False, "error": f"No host configured for {provider}", "error_type": "unreachable"}
        return validate_generic_host(host)
    return {"ok": False, "error": f"Provider '{provider}' not supported for validation.", "error_type": "other"}

# --------------------
# Model listing
# --------------------
def list_models_for_provider(provider: str, extra: Optional[Dict[str, Any]] = None) -> Dict:
    provider = provider.lower()
    extra = extra or {}
    if provider == "openai":
        key = extra.get("key") or get_api_key("openai")
        if not key: return {"ok": False, "error": "OpenAI key required", "error_type": "unauthorized"}
        try:
            r = requests.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=5)
            if r.status_code == 200:
                models = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
                return {"ok": True, "models": models}
            return {"ok": False, "error": f"OpenAI error {r.status_code}", "error_type": "other"}
        except Exception as e:
            return {"ok": False, "error": str(e), "error_type": "unreachable"}
    if provider == "ollama":
        cfg = load_config()
        host = extra.get("host") or cfg.get("ollama_host")
        if not host: return {"ok": False, "error": "Ollama host not configured", "error_type": "unreachable"}
        for p in ("/api/tags", "/api/models"):
            try:
                r = requests.get(host.rstrip("/") + p, timeout=4)
                if r.status_code == 200:
                    data = r.json()
                    models = [m.get("name") for m in data.get("models", []) if m.get("name")]
                    return {"ok": True, "models": models}
            except Exception: continue
        return {"ok": False, "error": "Could not fetch models from Ollama", "error_type": "unreachable"}
    
    fallback = {
        "openai": ["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo"],
        "anthropic": ["claude-3-5-sonnet-20241022", "claude-3-opus-20240229"],
        "gemini": ["gemini-1.5-pro", "gemini-2.0-flash"],
    }
    return {"ok": True, "models": fallback.get(provider, [])}

# --------------------
# Call entrypoints
# --------------------
def _complete_via_litellm(model_str: str, messages: List, temperature: float, timeout: float):
    """Thin wrapper around litellm.completion (separate for testability)."""
    import litellm
    return litellm.completion(
        model=model_str,
        messages=messages,
        temperature=temperature,
        timeout=timeout
    )

def _needs_api_key(provider: str) -> bool:
    """True unless the provider is local/host-only (Ollama, vLLM, ...)."""
    try:
        from core.auth import AuthManager  # lazy: core.auth imports core.services
        info = AuthManager.PROVIDERS.get(provider.lower(), {})
        return not info.get("host_only", False)
    except Exception:
        return provider.lower() != "ollama"


def call_model(provider: str, messages_or_text: Any, model: Optional[str] = None, temperature: float = 0.2, timeout: float = 30.0) -> Dict:
    provider = provider.lower()
    set_key_for_litellm(provider)

    # Fail fast with an actionable line instead of a cryptic litellm auth error.
    if _needs_api_key(provider) and not get_api_key(provider):
        return {"ok": False, "error_type": "missing_key",
                "error": f"No API key found for '{provider}'. Run /connect to link it (takes ~30 seconds)."}
    
    if isinstance(messages_or_text, str):
        messages = [{"role": "user", "content": messages_or_text}]
    else:
        messages = messages_or_text

    try:
        # Construct model string if provider prefix missing
        model_str = model
        if model and '/' not in model and provider not in ["openai", "anthropic", "gemini"]:
             model_str = f"{provider}/{model}"
        elif not model:
            # Fallback to sensible defaults
            defaults = {
                "openai": "gpt-4o-mini",
                "anthropic": "claude-3-5-sonnet-20241022",
                "gemini": "gemini-1.5-flash",
                "groq": "groq/llama-3.3-70b-versatile",
                "together": "together_ai/Qwen/Qwen2.5-72B-Instruct",
                "deepseek": "deepseek/deepseek-chat"
            }
            model_str = defaults.get(provider, f"ollama/llama3")

        response = _complete_via_litellm(model_str, messages, temperature, timeout)
        return {"ok": True, "text": response.choices[0].message.content, "provider": provider, "model": model_str}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_type": "call_failed"}

def install_ollama_model(model_name: str, host: str = "http://localhost:11434"):
    """Downloads an Ollama model if not present."""
    console.print(f"[dim]Checking for model: {model_name}...[/dim]")
    try:
        response = requests.post(f"{host.rstrip('/')}/api/pull", json={"name": model_name}, timeout=300, stream=True)
        if response.status_code == 200:
            console.print(f"[green]✅ Model '{model_name}' downloaded/updated successfully.[/green]")
            return True
        else:
            console.print(f"[red]❌ Failed to download model '{model_name}': {response.status_code}[/red]")
            return False
    except Exception as e:
        console.print(f"[red]❌ Error downloading model: {e}[/red]")
        return False

def check_cloud_dependencies():
    """Checks for essential cloud LLM dependencies."""
    required = ["openai", "anthropic", "google.generativeai"]
    missing = []
    for pkg in required:
        try:
            import importlib
            importlib.import_module(pkg.split('.')[0])
        except ImportError:
            missing.append(pkg)
    
    if missing:
        console.print(f"[yellow]⚠️ Missing cloud dependencies: {', '.join(missing)}. Some cloud models may not work.[/yellow]")
        return False
    return True

def ensure_ollama() -> Dict:
    cfg = load_config()
    host = cfg.get("ollama_host", "http://localhost:11434")
    v = validate_ollama(host, timeout=1.0)
    if v.get("ok"):
        return {"ok": True, "message": f"Ollama is running at {host}", "host": host}
    
    # Attempt repair
    console.print(f"[yellow]Ollama not reachable at {host}. Attempting auto-repair...[/yellow]")
    rep = repair_ollama(host)
    if rep.get("fixed"):
        return {"ok": True, "message": "Ollama repaired.", "host": cfg.get("ollama_host")}
    
    return {"ok": False, "error": "Ollama is not responding. Please ensure it is installed and running.", "error_type": "missing"}

def repair_ollama(host: Optional[str] = None, open_app_if_mac: bool = True,
                prompt_for_host: bool = True) -> Dict:
    """Probe candidate Ollama hosts and persist the first working one.

    Returns {"fixed": bool, "host": str|None, "attempts": [{"host","ok","error_type"}]}.
    """
    cfg = load_config()
    candidates = [host] if host else []
    candidates += [cfg.get("ollama_host"), "http://localhost:11434", "http://127.0.0.1:11434"]
    # Dedupe while preserving order: explicit host argument is always tried first
    candidates = list(dict.fromkeys(filter(None, candidates)))
    attempts = []

    def _try_all():
        for h in candidates:
            res = validate_ollama(h)
            attempts.append({"host": h, "ok": res.get("ok", False),
                             "error_type": res.get("error_type")})
            if res.get("ok"):
                cfg["ollama_host"] = h
                save_config(cfg)
                return {"fixed": True, "host": h, "attempts": attempts}
        return None

    hit = _try_all()
    if hit:
        return hit

    # Auth failure on a host: persist it so the user can intervene
    auth_attempts = [a for a in attempts if a.get("error_type") == "auth"]
    if auth_attempts:
        cfg["ollama_host"] = auth_attempts[0]["host"]
        save_config(cfg)
        return {"fixed": False, "host": auth_attempts[0]["host"],
                "attempts": attempts,
                "error": "Ollama host requires authentication; host saved for manual setup."}

    # OS specific app launching
    if open_app_if_mac:
        if sys.platform == "darwin":
            os.system("open -a Ollama &")
            time.sleep(5)
        elif sys.platform == "linux":
            # Try launching ollama via systemd or desktop entry
            os.system("systemctl --user start ollama &")
            os.system("ollama serve &")
            time.sleep(5)

        hit = _try_all()
        if hit:
            return hit

    return {"fixed": False, "host": None, "attempts": attempts}
