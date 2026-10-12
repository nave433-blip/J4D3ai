"""Track B tests: every AI provider validates against a real endpoint.

All network is mocked — no test touches the live internet. Each test asserts
the exact URL, auth scheme, and headers the validator sends, plus actionable
error behavior.
"""
import pytest
import requests
from unittest.mock import patch, MagicMock

import core.services as services
from core.services import (
    validate_provider_connection,
    validate_key_provider,
    validate_generic_host,
    PROVIDER_VALIDATION,
)


def _resp(status=200, payload=None):
    m = MagicMock()
    m.status_code = status
    m.text = "mock-body"
    m.json.return_value = payload if payload is not None else {"data": [{"id": "m1"}, {"id": "m2"}]}
    return m


@pytest.fixture()
def net(monkeypatch):
    """Mock requests.get/post inside core.services."""
    fake = MagicMock()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    fake.get.return_value = _resp(200)
    fake.post.return_value = _resp(200, {"choices": [{"message": {"content": "ok"}}]})
    return fake


# ---------------------------------------------------------------- endpoints

def test_openai_hits_models_endpoint_with_bearer(net):
    r = validate_provider_connection("openai", extra={"key": "sk-test"})
    assert r["ok"] is True
    url = net.get.call_args[0][0]
    assert url == "https://api.openai.com/v1/models"
    assert net.get.call_args[1]["headers"]["Authorization"] == "Bearer sk-test"
    assert r["models_count"] == 2


def test_gemini_uses_documented_models_endpoint(net):
    r = validate_provider_connection("gemini", extra={"key": "gkey"})
    assert r["ok"] is True
    url = net.get.call_args[0][0]
    assert url.startswith("https://generativelanguage.googleapis.com/v1beta/models")
    assert "key=gkey" in url


def test_anthropic_uses_x_api_key_and_version_header(net):
    r = validate_provider_connection("anthropic", extra={"key": "sk-ant-x"})
    assert r["ok"] is True, "anthropic must do a real network check, not 'key present'"
    url = net.get.call_args[0][0]
    headers = net.get.call_args[1]["headers"]
    assert url == "https://api.anthropic.com/v1/models"
    assert headers["x-api-key"] == "sk-ant-x"
    assert headers["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize("provider,url", [
    ("cohere", "https://api.cohere.com/v1/models"),
    ("mistral", "https://api.mistral.ai/v1/models"),
    ("deepseek", "https://api.deepseek.com/models"),
    ("groq", "https://api.groq.com/openai/v1/models"),
    ("together", "https://api.together.xyz/v1/models"),
    ("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1/models"),
])
def test_bearer_providers_hit_documented_endpoints(net, provider, url):
    r = validate_provider_connection(provider, extra={"key": "k"})
    assert r["ok"] is True, f"{provider} must validate against a real endpoint"
    assert net.get.call_args[0][0] == url
    assert net.get.call_args[1]["headers"]["Authorization"] == "Bearer k"


def test_github_validates_token_against_user_endpoint(net):
    net.get.return_value = _resp(200, {"login": "octocat"})
    r = validate_provider_connection("github", extra={"key": "ghp_x"})
    assert r["ok"] is True
    assert net.get.call_args[0][0] == "https://api.github.com/user"
    assert "octocat" in r["note"]


def test_perplexity_uses_minimal_chat_completion(net):
    r = validate_provider_connection("perplexity", extra={"key": "pplx-x"})
    assert r["ok"] is True
    assert net.post.call_args[0][0] == "https://api.perplexity.ai/chat/completions"
    body = net.post.call_args[1]["json"]
    assert body["max_tokens"] == 1  # cheapest documented check
    assert "1-token" in r["note"]


def test_qwen_is_key_based_not_host_only(net):
    """Qwen must validate as an API-key provider (AuthManager lists it so)."""
    r = validate_provider_connection("qwen", extra={"key": "sk-q"})
    assert r["ok"] is True
    assert net.get.call_args[0][0].startswith("https://dashscope.aliyuncs.com/")


# ---------------------------------------------------------------- errors

def test_401_yields_actionable_hint_with_key_url(net):
    net.get.return_value = _resp(401)
    r = validate_provider_connection("groq", extra={"key": "bad"})
    assert r["ok"] is False
    assert r["error_type"] == "unauthorized"
    assert "https://console.groq.com/keys" in r["hint"]
    assert r["key_url"] == "https://console.groq.com/keys"


def test_unreachable_gives_network_hint(net):
    import requests as rq
    net.get.side_effect = rq.exceptions.ConnectionError("dns failed")
    r = validate_provider_connection("mistral", extra={"key": "k"})
    assert r["ok"] is False
    assert r["error_type"] == "unreachable"
    assert "network" in r["hint"].lower()


def test_missing_key_points_at_key_url(monkeypatch):
    monkeypatch.setattr(services, "get_api_key", lambda p: None)
    r = validate_provider_connection("together")
    assert r["ok"] is False
    assert r["error_type"] == "unauthorized"
    assert "https://api.together.xyz/settings/api-keys" in r["hint"]


def test_generic_host_4xx_is_not_success(net):
    net.get.return_value = _resp(404)
    r = validate_generic_host("http://localhost:9/nope")
    assert r["ok"] is False
    assert r["error_type"] == "http_error"
    assert "404" in r["error"]


def test_generic_host_200_ok(net):
    r = validate_generic_host("http://localhost:11434")
    assert r["ok"] is True
    assert r["status"] == 200


# ---------------------------------------------------------------- metadata

def test_free_labels_only_where_verified():
    free = {p: m["free"] for p, m in PROVIDER_VALIDATION.items()}
    assert free["gemini"] and "no credit card" in free["gemini"].lower()
    assert free["groq"] and "free" in free["groq"].lower()
    assert free["mistral"] is not None
    assert free["cohere"] is not None
    assert free["together"] is not None
    assert free["github"] is not None
    # Paid APIs must not carry a guessed free-tier claim.
    for p in ("openai", "anthropic", "deepseek", "qwen", "perplexity"):
        assert free[p] is None, f"{p} must not claim an unverified free tier"


def test_key_urls_are_real_https_links():
    for p, m in PROVIDER_VALIDATION.items():
        url = m["key_url"]
        assert url.startswith("https://"), f"{p}: {url}"
        assert " " not in url
    # Qwen's console moved to the Bailian/Model Studio console (verified).
    assert PROVIDER_VALIDATION["qwen"]["key_url"] == \
        "https://bailian.console.aliyun.com/?apiKey=1#/api-key"


def test_auth_manager_urls_match_validated_metadata():
    from core.auth import AuthManager
    for p, m in PROVIDER_VALIDATION.items():
        assert AuthManager.PROVIDERS[p]["url"] == m["key_url"], \
            f"{p}: AuthManager URL diverged from validated metadata"


def test_unknown_provider_not_supported():
    r = validate_key_provider("nope", "k")
    assert r["ok"] is False
    assert r["error_type"] == "other"
