"""Error-UX tests: the most common failure paths must print one actionable
line in normal mode — never a traceback, never a cryptic dump."""
import socket

import pytest
import requests

import core.services as services_mod
from core.services import call_model, _needs_api_key
import core.brain as brain_mod


# --- missing API key ----------------------------------------------------------

def test_needs_api_key_local_providers():
    assert not _needs_api_key("ollama")
    assert not _needs_api_key("vllm")
    assert _needs_api_key("openai")
    assert _needs_api_key("gemini")


def test_call_model_missing_key_actionable(monkeypatch):
    monkeypatch.setattr(services_mod, "get_api_key", lambda p: None)
    for var in ("OPENAI_API_KEY",):
        monkeypatch.delenv(var, raising=False)
    res = call_model("openai", "hello")
    assert res["ok"] is False
    assert res["error_type"] == "missing_key"
    assert "/connect" in res["error"]
    assert "\n" not in res["error"]  # one line


def test_call_model_local_provider_no_key_needed(monkeypatch):
    # ollama must not hit the missing-key path (it would try HTTP instead).
    monkeypatch.setattr(services_mod, "get_api_key", lambda p: None)
    called = {}
    def _fake_complete(*a, **k):
        called["yes"] = True
        raise RuntimeError("nope")
    monkeypatch.setattr(services_mod, "_complete_via_litellm", _fake_complete)
    res = call_model("ollama", "hello")
    assert called.get("yes")  # went through to the (mocked) completion
    assert res["error_type"] == "call_failed"


# --- Ollama down / model missing ------------------------------------------------

def _ollama_provider():
    p = brain_mod.OllamaProvider.__new__(brain_mod.OllamaProvider)
    p.model = "llama3"
    p.hosts = ["http://127.0.0.1:1"]
    p.host = p.hosts[0]
    return p


def test_ollama_down_actionable(monkeypatch):
    def _boom(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "post", _boom)
    monkeypatch.setattr(brain_mod, "load_config", lambda: {})
    out = _ollama_provider().ask("hello")
    assert "Couldn't reach Ollama" in out
    assert "ollama serve" in out
    assert "/connect" in out
    assert "\n" not in out


def test_ollama_model_missing_actionable(monkeypatch):
    class _R:
        status_code = 404
        def raise_for_status(self): pass
        def json(self): return {"error": "not found"}
    monkeypatch.setattr(requests, "post", lambda *a, **k: _R())
    monkeypatch.setattr(brain_mod, "load_config", lambda: {})
    out = _ollama_provider().ask("hello")
    assert "doesn't have model 'llama3'" in out
    assert "/ollama pull llama3" in out
    assert "\n" not in out


def test_ollama_success_still_works(monkeypatch):
    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": "hi there"}}
    monkeypatch.setattr(requests, "post", lambda *a, **k: _R())
    monkeypatch.setattr(brain_mod, "load_config", lambda: {})
    assert _ollama_provider().ask("hello") == "hi there"


# --- dashboard bind failure ------------------------------------------------------

def test_dashboard_port_in_use_clean_error(tmp_path, monkeypatch):
    import core.dashboard as dash_mod
    monkeypatch.setattr(dash_mod, "TOKEN_FILE", tmp_path / "tok")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        info = dash_mod.start_dashboard_background(port=port)
        assert info["port"] is None
        assert "error" in info and str(port) in info["error"]
        info["stop"]()
    finally:
        s.close()
