"""Jade reference cloud server — stdlib HTTP app.

Run:  ``python -m server.app``   (or ``docker compose up`` — see server/README.md)

Security posture (reference, not hardened — see server/PRODUCTION.md):
- bearer session tokens (only SHA-256 stored); Authorization header
  values are never logged
- in-memory per-IP rate limiting, stricter on login/pairing-claim
- security headers on every response; JSON bodies capped at 1 MiB
- connector OAuth tokens encrypted at rest (Fernet); refresh handled
  server-side; clients never see refresh tokens
"""

from __future__ import annotations

import json
import logging
import secrets
import socketserver
import time
import urllib.parse
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import __version__
from . import accounts as accounts_mod
from . import chat as chat_mod
from . import oauth_broker as broker
from . import oidc as oidc_mod
from . import pairing as pairing_mod
from .config import ServerConfig
from .db import Database, sha256_hex

log = logging.getLogger("jade-server")
MAX_BODY = 1 << 20  # 1 MiB


# --------------------------------------------------------------------------
# rate limiting (in-memory; swap for redis in production — see PRODUCTION.md)
# --------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque] = {}

    def allowed(self, key: str, limit: int | None = None) -> bool:
        limit = self.per_minute if limit is None else limit
        now = time.time()
        dq = self._hits.setdefault(key, deque())
        while dq and dq[0] < now - 60:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


# --------------------------------------------------------------------------
# request handler
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = f"JadeServer/{__version__}"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # quieter than BaseHTTPRequestHandler
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body, content_type="application/json",
              extra_headers: dict | None = None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict):
        self._send(status, json.dumps(obj).encode())

    def _error(self, status: int, message: str):
        self._json(status, {"error": message})

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode())
        except Exception:
            raise ValueError("malformed JSON body")
        return data if isinstance(data, dict) else {}

    def _bearer(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return ""

    @property
    def _app(self) -> "JadeApp":
        return self.server.app  # type: ignore[attr-defined]

    def _user(self, required: bool = True):
        row = accounts_mod.get_user_from_token(self._app.db, self._bearer())
        if required and row is None:
            self._error(HTTPStatus.UNAUTHORIZED, "invalid or expired session token")
            return None
        return row

    def _rate(self, limit: int | None = None, key_suffix: str = "") -> bool:
        ip = self.client_address[0]
        if not self._app.limiter.allowed(f"{ip}:{self.path}{key_suffix}", limit):
            self._error(HTTPStatus.TOO_MANY_REQUESTS, "rate limit exceeded")
            return False
        return True

    # -- routing ------------------------------------------------------------
    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_DELETE(self):
        self._route()

    def _route(self):
        parsed = urllib.parse.urlparse(self.path)
        path, method = parsed.path, self.command
        query = urllib.parse.parse_qs(parsed.query)
        app = self._app
        try:
            if method == "GET" and path == "/healthz":
                return self._json(200, {"ok": True, "version": __version__})

            # -- accounts ---------------------------------------------------
            if method == "POST" and path == "/v1/accounts/signup":
                return self._h_signup()
            if method == "POST" and path == "/v1/accounts/login":
                return self._h_login()
            if method == "POST" and path == "/v1/accounts/logout":
                return self._h_logout()
            if method == "GET" and path == "/v1/accounts/me":
                return self._h_me()
            if method == "POST" and path == "/v1/accounts/change-password":
                return self._h_change_password()

            # -- social sign-in ----------------------------------------------
            if method == "POST" and path.startswith("/v1/auth/oidc/"):
                return self._h_oidc(path.rsplit("/", 1)[-1])
            if method == "POST" and path == "/v1/auth/github":
                return self._h_github_auth()

            # -- connectors ---------------------------------------------------
            if method == "GET" and path == "/v1/connectors":
                return self._h_connectors_list()
            if method == "GET" and path.startswith("/v1/connectors/") and path.endswith("/authorize"):
                cid = path.split("/")[3]
                return self._h_connector_authorize(cid)
            if method == "GET" and path.startswith("/v1/connectors/") and path.endswith("/callback"):
                cid = path.split("/")[3]
                return self._h_connector_callback(cid, query)
            if method == "POST" and path.startswith("/v1/connectors/") and path.endswith("/token"):
                cid = path.split("/")[3]
                return self._h_connector_token(cid)
            if method == "DELETE" and path.startswith("/v1/connectors/"):
                cid = path.split("/")[3]
                return self._h_connector_delete(cid)

            # -- pairing -------------------------------------------------------
            if method == "POST" and path == "/v1/pairing/create":
                return self._h_pairing_create()
            if method == "POST" and path == "/v1/pairing/claim":
                return self._h_pairing_claim()
            if method == "GET" and path.startswith("/v1/pairing/") and path.endswith("/status"):
                pid = path.split("/")[3]
                return self._h_pairing_status(pid, query)
            if method == "POST" and path.startswith("/v1/pairing/") and path.endswith("/accept"):
                pid = path.split("/")[3]
                return self._h_pairing_accept(pid)
            if method == "POST" and path.startswith("/v1/pairing/") and path.endswith("/reject"):
                pid = path.split("/")[3]
                return self._h_pairing_reject(pid)

            # -- chat -----------------------------------------------------------
            if method == "POST" and path == "/v1/chat/completions":
                return self._h_chat()

            return self._error(HTTPStatus.NOT_FOUND, "not found")
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # never leak internals
            log.exception("handler error for %s %s", method, path)
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    # -- account handlers ------------------------------------------------------
    def _h_signup(self):
        if not self._rate(limit=20):
            return
        try:
            body = self._read_json()
            user = accounts_mod.signup(
                self._app.db, body.get("email", ""), body.get("password", ""),
                body.get("display_name", ""))
        except accounts_mod.AccountError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._json(201, {"user": user})

    def _h_login(self):
        if not self._rate(limit=10):
            return
        try:
            body = self._read_json()
            user, token = accounts_mod.login(
                self._app.db, body.get("email", ""), body.get("password", ""),
                self._app.cfg.session_ttl_seconds, body.get("device_label", ""))
        except accounts_mod.AccountError as exc:
            return self._error(HTTPStatus.UNAUTHORIZED, str(exc))
        return self._json(200, {"user": user, "session_token": token,
                                "token_type": "Bearer"})

    def _h_logout(self):
        accounts_mod.logout(self._app.db, self._bearer())
        return self._json(200, {"ok": True})

    def _h_me(self):
        user = self._user()
        if user is None:
            return
        return self._json(200, {"user": {"id": user["id"], "email": user["email"],
                                        "display_name": user["display_name"]}})

    def _h_change_password(self):
        user = self._user()
        if user is None:
            return
        if not self._rate(limit=10):
            return
        try:
            body = self._read_json()
            accounts_mod.change_password(self._app.db, user["id"],
                                         body.get("current_password", ""),
                                         body.get("new_password", ""))
        except accounts_mod.AccountError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._json(200, {"ok": True})

    # -- social sign-in ----------------------------------------------------------
    def _issue_session(self, db, user_id: int, device_label: str):
        token = secrets.token_urlsafe(48)
        import time as _t
        db.create_session(sha256_hex(token), user_id, device_label[:80],
                          int(_t.time()) + self._app.cfg.session_ttl_seconds)
        return token

    def _h_oidc(self, provider: str):
        if not self._rate(limit=20):
            return
        body = self._read_json()
        id_token = body.get("id_token", "")
        cfg = self._app.cfg
        client_ids = {"google": cfg.google_client_id, "apple": cfg.apple_client_id,
                      "microsoft": cfg.microsoft_client_id}
        if provider not in client_ids or not client_ids[provider]:
            return self._error(HTTPStatus.NOT_IMPLEMENTED,
                               f"sign-in with {provider} is not configured on this server")
        try:
            claims = oidc_mod.verify_id_token(provider, id_token, client_ids[provider],
                                              tenant=cfg.microsoft_tenant)
        except oidc_mod.OIDCError as exc:
            return self._error(HTTPStatus.UNAUTHORIZED, f"token verification failed: {exc}")
        user = self._link_or_create(provider, claims)
        token = self._issue_session(self._app.db, user["id"], body.get("device_label", ""))
        return self._json(200, {"user": user, "session_token": token, "token_type": "Bearer"})

    def _h_github_auth(self):
        if not self._rate(limit=20):
            return
        body = self._read_json()
        cfg = self._app.cfg
        if not cfg.github_client_id or not cfg.github_client_secret:
            return self._error(HTTPStatus.NOT_IMPLEMENTED,
                               "sign-in with GitHub is not configured on this server")
        try:
            info = oidc_mod.github_user_from_code(
                body.get("code", ""), cfg.github_client_id, cfg.github_client_secret)
        except oidc_mod.OIDCError as exc:
            return self._error(HTTPStatus.UNAUTHORIZED, f"github auth failed: {exc}")
        user = self._link_or_create("github", info)
        token = self._issue_session(self._app.db, user["id"], body.get("device_label", ""))
        return self._json(200, {"user": user, "session_token": token, "token_type": "Bearer"})

    def _link_or_create(self, provider: str, claims: dict) -> dict:
        """Find the user for a verified social identity, creating/linking as needed."""
        db = self._app.db
        row = db.get_user_by_identity(provider, claims["sub"])
        if row:
            return {"id": row["id"], "email": row["email"], "display_name": row["display_name"]}
        email = claims.get("email", "")
        if email:
            existing = db.get_user_by_email(email)
            if existing:
                # Same verified email -> link the new identity to the account.
                db.link_identity(existing["id"], provider, claims["sub"])
                return {"id": existing["id"], "email": existing["email"],
                        "display_name": existing["display_name"]}
        # New account. Social-only accounts get an unusable random password.
        salt, stored = accounts_mod.hash_password(secrets.token_hex(32))
        user_id = db.create_user(email or f"{provider}-{claims['sub']}@social.local",
                                 salt, stored, claims.get("name", "")[:80])
        db.link_identity(user_id, provider, claims["sub"])
        row = db.get_user(user_id)
        return {"id": row["id"], "email": row["email"], "display_name": row["display_name"]}

    # -- connector handlers -------------------------------------------------------
    def _h_connectors_list(self):
        user = self._user()
        if user is None:
            return
        connected = {r["connector"] for r in self._app.db.list_connector_tokens(user["id"])}
        out = [{"id": cid, "label": meta["label"], "provider": meta["provider"],
                "connected": cid in connected, "scopes": meta["scopes"]}
               for cid, meta in broker.CONNECTORS.items()]
        return self._json(200, {"connectors": out})

    def _h_connector_authorize(self, connector_id: str):
        user = self._user()
        if user is None:
            return
        try:
            state = secrets.token_urlsafe(32)
            url, verifier = broker.build_authorize_url(self._app.cfg, connector_id, state=state)
        except broker.BrokerError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        self._app.db.save_oauth_state(sha256_hex(state), user["id"], connector_id, verifier)
        self._send(302, b"", extra_headers={"Location": url})

    def _h_connector_callback(self, connector_id: str, query: dict):
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code or not state:
            return self._error(HTTPStatus.BAD_REQUEST, "missing code or state")
        if (query.get("error") or [""])[0]:
            return self._error(HTTPStatus.BAD_REQUEST,
                               f"provider refused: {(query.get('error_description') or query['error'])[0]}")
        st = self._app.db.consume_oauth_state(sha256_hex(state))
        if not st or st["connector"] != connector_id:
            return self._error(HTTPStatus.BAD_REQUEST, "invalid or expired state")
        try:
            tokens = broker.exchange_code(self._app.cfg, connector_id, code, st["code_verifier"])
            cipher = broker.TokenCipher(self._app.cfg.token_key)
            broker.store_tokens(self._app.db, cipher, st["user_id"], connector_id, tokens)
        except broker.BrokerError as exc:
            return self._error(HTTPStatus.BAD_GATEWAY, str(exc))
        label = broker.CONNECTORS[connector_id]["label"]
        html = (f"<html><body><h2>{label} connected ✓</h2>"
                "<p>You can close this window and return to Jade.</p></body></html>")
        self._send(200, html, content_type="text/html")

    def _h_connector_token(self, connector_id: str):
        user = self._user()
        if user is None:
            return
        try:
            cipher = broker.TokenCipher(self._app.cfg.token_key)
            access, expires_in = broker.get_access_token(
                self._app.db, cipher, self._app.cfg, user["id"], connector_id)
        except broker.BrokerError as exc:
            return self._error(HTTPStatus.BAD_GATEWAY, str(exc))
        return self._json(200, {"access_token": access, "token_type": "Bearer",
                                "expires_in": expires_in})

    def _h_connector_delete(self, connector_id: str):
        user = self._user()
        if user is None:
            return
        if connector_id not in broker.CONNECTORS:
            return self._error(HTTPStatus.NOT_FOUND, "unknown connector")
        ok = broker.revoke_tokens(self._app.db, user["id"], connector_id)
        return self._json(200, {"ok": True, "was_connected": ok})

    # -- pairing handlers ------------------------------------------------------------
    def _h_pairing_create(self):
        user = self._user()
        if user is None:
            return
        body = self._read_json()
        try:
            res = pairing_mod.create_pairing(self._app.db, user["id"],
                                             body.get("device_name", ""),
                                             self._app.cfg.pairing_code_ttl_seconds)
        except pairing_mod.PairingError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._json(201, res)

    def _h_pairing_claim(self):
        if not self._rate(limit=10):
            return
        body = self._read_json()
        code = body.get("code", "")
        try:
            res = pairing_mod.claim_pairing(self._app.db, code, body.get("device_name", ""))
        except pairing_mod.PairingError:
            pairing_mod.record_attempt(self._app.db, code)
            return self._error(HTTPStatus.BAD_REQUEST, "invalid or expired pairing code")
        return self._json(200, res)

    def _h_pairing_status(self, pairing_id: str, query: dict):
        receipt = (query.get("receipt") or [""])[0]
        if receipt:
            try:
                return self._json(200, pairing_mod.claimer_status(
                    self._app.db, pairing_id, receipt))
            except pairing_mod.PairingError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        user = self._user()
        if user is None:
            return
        try:
            return self._json(200, pairing_mod.owner_status(
                self._app.db, pairing_id, user["id"]))
        except pairing_mod.PairingError as exc:
            return self._error(HTTPStatus.NOT_FOUND, str(exc))

    def _h_pairing_accept(self, pairing_id: str):
        user = self._user()
        if user is None:
            return
        body = self._read_json()
        try:
            res = pairing_mod.accept_pairing(self._app.db, pairing_id, user["id"],
                                             body.get("endpoint_hint", ""))
        except pairing_mod.PairingError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._json(200, res)

    def _h_pairing_reject(self, pairing_id: str):
        user = self._user()
        if user is None:
            return
        try:
            res = pairing_mod.reject_pairing(self._app.db, pairing_id, user["id"])
        except pairing_mod.PairingError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._json(200, res)

    # -- chat --------------------------------------------------------------------------
    def _h_chat(self):
        user = self._user()
        if user is None:
            return
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        if body.get("stream"):
            return self._h_chat_stream(body)
        try:
            status, resp = chat_mod.chat_completions(self._app.cfg, body)
        except chat_mod.ChatError as exc:
            return self._error(exc.status, str(exc))
        return self._json(status, resp)

    def _h_chat_stream(self, body: dict):
        try:
            gen = chat_mod.chat_completions_stream(self._app.cfg, body)
            first = next(gen)  # fail fast before headers if unconfigured
        except chat_mod.ChatError as exc:
            return self._error(exc.status, str(exc))
        except StopIteration:
            return self._error(HTTPStatus.BAD_GATEWAY, "empty upstream stream")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(first if first.endswith(b"\n") else first + b"\n")
            for chunk in gen:
                self.wfile.write(chunk if chunk.endswith(b"\n") else chunk + b"\n")
            self.wfile.write(b"data: [DONE]\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


# --------------------------------------------------------------------------
# app wiring
# --------------------------------------------------------------------------

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class JadeApp:
    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        problems = cfg.validate()
        if problems:
            raise SystemExit("configuration error:\n- " + "\n- ".join(problems))
        data_dir = Path(cfg.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(data_dir / "jade-server.db")
        self.db.delete_expired_sessions()
        self.db.purge_expired_pairings()
        self.limiter = RateLimiter(cfg.rate_limit_per_minute)
        self.httpd: ThreadedHTTPServer | None = None

    def serve(self):
        self.httpd = ThreadedHTTPServer((self.cfg.host, self.cfg.port), _Handler)
        self.httpd.app = self  # type: ignore[attr-defined]
        # Redacted config only — never log secrets.
        log.info("jade-server %s listening on %s:%d", __version__,
                 self.cfg.host, self.httpd.server_port)
        log.info("config: %s", self.cfg.redacted())
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass

    def shutdown(self):
        if self.httpd:
            self.httpd.shutdown()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Defense in depth: scrub anything that looks like a credential from
    # log records, in case a future handler ever logs too much.
    import re as _re
    _secret_re = _re.compile(
        r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]+|"
        r"(\"(?:password|client_secret|refresh_token|access_token|id_token|code)\"\s*:\s*\")([^\"]+)(\")"
    )

    class _Scrub(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                record.msg = _secret_re.sub(
                    lambda m: (m.group(1) or "") + "***" + (m.group(4) or "")
                    if m.group(1) else m.group(2) + "***" + m.group(4),
                    str(record.msg),
                )
            except Exception:
                pass
            return True

    logging.getLogger().addFilter(_Scrub())
    cfg = ServerConfig()
    JadeApp(cfg).serve()


if __name__ == "__main__":
    main()
