"""OpenAI-compatible ``/v1/chat/completions`` for the hosted server.

This is the HOSTED counterpart to the on-device API server (Round A):
point any OpenAI-compatible client at ``$JADE_PUBLIC_URL/v1`` with a
Jade session token as the bearer key and requests are relayed to the
provider configured via ``JADE_CHAT_*``.

``http_post`` / streaming are injectable for tests; no live network in
the test suite.
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid


class ChatError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _provider_request(cfg, body: dict, *, http_post=None) -> dict:
    if not cfg.chat_base_url or not cfg.chat_api_key:
        raise ChatError(
            "no chat provider configured — set JADE_CHAT_BASE_URL and "
            "JADE_CHAT_API_KEY (see .env.example)", status=501,
        )
    model = body.get("model") or cfg.chat_model
    if not model:
        raise ChatError("no model specified and JADE_CHAT_MODEL is unset", status=400)
    upstream = {
        "model": model,
        "messages": body.get("messages", []),
    }
    for passthrough in ("temperature", "max_tokens", "top_p", "stop"):
        if passthrough in body:
            upstream[passthrough] = body[passthrough]

    def _do_post():
        req = urllib.request.Request(
            cfg.chat_base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(upstream).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {cfg.chat_api_key}",
                     "User-Agent": "jade-server/0.1"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())

    post = http_post or _do_post
    try:
        data = post()
    except ChatError:
        raise
    except Exception as exc:
        raise ChatError(f"upstream provider error: {exc}") from exc
    if not isinstance(data, dict) or "choices" not in data:
        raise ChatError("upstream provider returned an unexpected response")
    # Normalize the id/created fields so clients always see a coherent shape.
    data.setdefault("id", f"chatcmpl-{uuid.uuid4().hex[:12]}")
    data.setdefault("created", int(time.time()))
    data.setdefault("model", model)
    return data


def chat_completions(cfg, body: dict, *, http_post=None) -> tuple[int, dict]:
    """Non-streaming completion. Returns (status, response_body)."""
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise ChatError("request body must include a 'messages' array", status=400)
    if body.get("stream"):
        raise ChatError("use the SSE stream variant for stream=true", status=400)
    return 200, _provider_request(cfg, body, http_post=http_post)


def chat_completions_stream(cfg, body: dict, *, http_post_stream=None):
    """Yield SSE ``data:`` chunks for ``stream=true`` requests.

    The default implementation relays the upstream SSE byte stream.
    ``http_post_stream`` may be injected in tests to yield fake chunks.
    """
    if not cfg.chat_base_url or not cfg.chat_api_key:
        raise ChatError("no chat provider configured", status=501)
    model = body.get("model") or cfg.chat_model
    upstream = {"model": model, "messages": body.get("messages", []), "stream": True}
    for passthrough in ("temperature", "max_tokens", "top_p", "stop"):
        if passthrough in body:
            upstream[passthrough] = body[passthrough]

    def _default_stream():
        req = urllib.request.Request(
            cfg.chat_base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(upstream).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {cfg.chat_api_key}",
                     "User-Agent": "jade-server/0.1"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            for line in resp:
                yield line

    stream = http_post_stream or _default_stream
    try:
        for chunk in stream():
            if isinstance(chunk, str):
                chunk = chunk.encode()
            yield chunk if chunk.startswith(b"data:") or chunk.strip() == b"" else b"data: " + chunk
    except ChatError:
        raise
    except Exception as exc:
        raise ChatError(f"upstream stream error: {exc}") from exc
