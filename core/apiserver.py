"""OpenAI-compatible chat-completions server for Jade — stdlib only.

Exposes Jade's brain behind the same REST shape as the OpenAI API so any
OpenAI-compatible client (SDKs, editor plugins, scripts) can talk to it
without modification:

Endpoints
---------
- ``GET /v1/models``
    Lists the configured providers as models::

        {"object": "list",
         "data": [{"id": "ollama/llama3", "object": "model",
                   "owned_by": "ollama"}, ...]}

    Model ids come from each configured provider's litellm id
    (``core.hive.resolve_hive_model``), falling back to ``jade-<provider>``.
    If nothing is configured the data list is empty — that is honest,
    not an error.

- ``POST /v1/chat/completions``
    OpenAI-style body::

        {"model": "ollama/llama3",
         "messages": [{"role": "user", "content": "hello"}]}

    Messages are concatenated into a prompt (each prefixed ``ROLE: ``) and
    routed through ``core.brain.think_structured``. The requested ``model``
    id is mapped to a configured provider when recognizable; otherwise the
    default model is used. ``stream: true`` is rejected with HTTP 400
    (streaming is not supported); malformed JSON is HTTP 400; a brain
    failure is HTTP 502.

    Success shape::

        {"id": "chatcmpl-...", "object": "chat.completion", "created": 1234,
         "model": "<echoed request>",
         "choices": [{"index": 0,
                      "message": {"role": "assistant", "content": "..."},
                      "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    Token usage is reported as zeros: Jade does not track token
    accounting, and it would be dishonest to invent numbers.

Auth
----
Every request needs ``Authorization: Bearer <token>``. The token comes from
the ``JADE_API_TOKEN`` environment variable, or is generated per run and
stored at ``~/.jade/api_token`` (0600) — mirroring ``core.dashboard``.
Tokens are never logged.

Binding
-------
Loopback-only (127.0.0.1) by default. ``bind_lan=True`` binds 0.0.0.0 but
prints a loud warning and requires explicit opt-in, like the dashboard.

Quick start
-----------
    $ JADE_API_TOKEN=secret python -m core.apiserver   # (see __main__ below)

    $ curl -s http://127.0.0.1:18789/v1/models \\
        -H "Authorization: Bearer secret" | python -m json.tool

    $ curl -s http://127.0.0.1:18789/v1/chat/completions \\
        -H "Authorization: Bearer secret" \\
        -H "Content-Type: application/json" \\
        -d '{"model": "jade-default",
             "messages": [{"role": "user", "content": "Say hi in one line."}]}' \\
        | python -m json.tool
"""

from __future__ import annotations

import hmac
import http.server
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from core.config import CONFIG_DIR

TOKEN_ENV = "JADE_API_TOKEN"
TOKEN_FILE = CONFIG_DIR / "api_token"

# Cap request bodies and the assembled prompt so a client can't ask the
# server to chew unbounded input.
_MAX_BODY = 1 << 20          # 1 MiB
_MAX_PROMPT_CHARS = 32_000


def _get_token() -> str:
    """Bearer token: env var first, else a persisted per-run token (0600)."""
    env = os.environ.get(TOKEN_ENV)
    if env and env.strip():
        return env.strip()
    if TOKEN_FILE.exists():
        try:
            return TOKEN_FILE.read_text().strip()
        except OSError:
            pass
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(token)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_FILE)
    return token


def _authed(handler: http.server.BaseHTTPRequestHandler, token: str) -> bool:
    auth = handler.headers.get("Authorization") or ""
    supplied = ""
    if auth.startswith("Bearer "):
        supplied = auth[len("Bearer "):].strip()
    return bool(supplied) and bool(token) and hmac.compare_digest(supplied, token)


# ---------------------------------------------------------------------------
# Lazy brain binding — module-global so tests can monkeypatch
# ``core.apiserver.think_structured``. Imported lazily to keep importing this
# module cheap (core.brain pulls in heavy provider deps).
# ---------------------------------------------------------------------------

def _lazy_think_structured(context: str, task: str,
                           model: Optional[str] = None) -> Dict[str, Any]:
    from core.brain import think_structured as _real
    return _real(context, task, model=model)


think_structured = _lazy_think_structured  # noqa: E305 — intentional indirection


# ---------------------------------------------------------------------------
# Model inventory
# ---------------------------------------------------------------------------

def _model_inventory() -> List[Dict[str, str]]:
    """Configured providers as OpenAI-style model entries.

    Each entry gets the provider's litellm model id (hive mapping or the
    user's configured default); providers without a resolvable id get
    ``jade-<provider>``. Failures produce an empty list, never a lie.
    """
    try:
        from core.auth import AuthManager
        from core.connect import is_configured
        from core.hive import resolve_hive_model
    except Exception:
        return []
    entries: List[Dict[str, str]] = []
    try:
        providers = sorted(AuthManager.PROVIDERS.keys())
    except Exception:
        return []
    for provider in providers:
        try:
            if not is_configured(provider):
                continue
        except Exception:
            continue
        try:
            model_id = resolve_hive_model(provider)
        except Exception:
            model_id = None
        if not model_id:
            model_id = f"jade-{provider}"
        entries.append({"id": model_id, "object": "model",
                        "owned_by": provider})
    return entries


def _map_requested_model(requested: Optional[str]) -> Optional[str]:
    """Map a client-supplied model id to a configured model id.

    Returns the inventory id when the request matches one we know (exact
    match, or a ``provider/...`` id whose provider is configured); otherwise
    None, meaning "use the default model".
    """
    if not requested:
        return None
    inventory = _model_inventory()
    ids = {e["id"]: e["owned_by"] for e in inventory}
    if requested in ids:
        return requested
    # ``provider/<anything>`` with a configured provider is recognizable.
    provider = requested.split("/", 1)[0].lower()
    if any(p == provider for p in ids.values()):
        return requested
    return None


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    token: str = ""
    server_version = "JadeAPI/1.0"

    def log_message(self, *a):  # keep quiet; never log tokens
        pass

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, code: int,
               err_type: str = "invalid_request_error") -> None:
        self._json({"error": {"message": message, "type": err_type}}, code)

    def _require_auth(self) -> bool:
        if not _authed(self, self.token):
            self._error("invalid or missing bearer token", 401,
                        "authentication_error")
            return False
        return True

    def _read_json(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            length = 0
        if length <= 0:
            return {}, None
        raw = self.rfile.read(min(length, _MAX_BODY))
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, "request body is not valid JSON"
        if not isinstance(data, dict):
            return None, "request body must be a JSON object"
        return data, None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self._require_auth():
            return
        if parsed.path == "/v1/models":
            self._json({"object": "list", "data": _model_inventory()})
        else:
            self._error("not found", 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self._require_auth():
            return
        if parsed.path != "/v1/chat/completions":
            self._error("not found", 404)
            return
        data, err = self._read_json()
        if err:
            self._error(err, 400)
            return
        if data.get("stream"):
            self._error("streaming not supported", 400)
            return
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            self._error("'messages' must be a non-empty array", 400)
            return
        parts: List[str] = []
        for m in messages:
            if not isinstance(m, dict):
                self._error("each message must be an object", 400)
                return
            role = str(m.get("role", "user"))
            content = m.get("content", "")
            if isinstance(content, list):  # content parts — take text pieces
                content = " ".join(
                    str(p.get("text", "")) for p in content
                    if isinstance(p, dict) and p.get("type") == "text")
            content = str(content)
            parts.append(f"{role.upper()}: {content}")
        prompt = "\n\n".join(parts)[:_MAX_PROMPT_CHARS]

        requested_model = data.get("model")
        mapped_model = _map_requested_model(
            requested_model if isinstance(requested_model, str) else None)

        try:
            result = think_structured("API server", prompt, model=mapped_model)
        except Exception as e:
            self._error(f"brain error: {type(e).__name__}: {e}", 502,
                        "api_error")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err_msg = (result or {}).get("error", "unknown brain failure") \
                if isinstance(result, dict) else "unknown brain failure"
            self._error(f"brain error: {err_msg}", 502, "api_error")
            return
        text = str(result.get("text", ""))
        # Token usage is not tracked by Jade — zeros are honest placeholders.
        self._json({
            "id": "chatcmpl-" + secrets.token_hex(12),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": requested_model if requested_model else "jade-default",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0},
        })


class APIServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _bind_server(host: str, port: int, token: str):
    """Bind the API server, or return (None, error_message)."""
    handler = type("AuthedAPIHandler", (_Handler,), {"token": token})
    try:
        return APIServer((host, port), handler), None
    except OSError as e:
        return None, (f"Couldn't bind the API server to "
                      f"{host}:{port or 'a free port'} ({e}).")


def run_api_server(port: int = 0, bind_lan: bool = False):
    """Start the API server in the foreground.

    Returns ``(server, thread, url, token)``; the thread is started and the
    server is already serving. Call ``server.shutdown()`` /
    ``server.server_close()`` to stop it.
    """
    token = _get_token()
    host = "0.0.0.0" if bind_lan else "127.0.0.1"
    if bind_lan:
        print("⚠️  WARNING: Jade API server bound to ALL interfaces (LAN). "
              "Anyone on your network with the bearer token can use your AI.")
    server, err = _bind_server(host, port, token)
    if server is None:
        raise OSError(err)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="jade-apiserver")
    thread.start()
    url = f"http://127.0.0.1:{actual_port}"
    print(f"🤖 Jade API server: {url}  (token: {TOKEN_ENV} env var "
          f"or ~/.jade/api_token)")
    return server, thread, url, token


def start_api_background(port: int = 0, bind_lan: bool = False) -> Dict[str, Any]:
    """Start the API server on a background thread.

    Returns ``{"server", "url", "port", "token", "stop", "error"?}`` —
    mirrors ``core.dashboard.start_dashboard_background``. The token is
    returned so callers/tests can authorize without reading the token file.
    """
    token = _get_token()
    host = "0.0.0.0" if bind_lan else "127.0.0.1"
    if bind_lan:
        print("⚠️  WARNING: Jade API server bound to ALL interfaces (LAN). "
              "Anyone on your network with the bearer token can use your AI.")
    server, err = _bind_server(host, port, token)
    if server is None:
        return {"server": None, "url": None, "port": None, "token": token,
                "stop": lambda: None, "error": err}
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="jade-apiserver")
    thread.start()

    def _stop():
        server.shutdown()
        server.server_close()

    return {"server": server, "thread": thread,
            "url": f"http://127.0.0.1:{actual_port}", "port": actual_port,
            "token": token, "stop": _stop}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Jade OpenAI-compatible API server")
    parser.add_argument("--port", type=int, default=18789)
    parser.add_argument("--bind-lan", action="store_true",
                        help="bind 0.0.0.0 (warning: exposes the API to your LAN)")
    args = parser.parse_args()
    try:
        srv, _thr, _url, _tok = run_api_server(port=args.port,
                                               bind_lan=args.bind_lan)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            srv.shutdown()
            srv.server_close()
    except OSError as e:
        print(e)
