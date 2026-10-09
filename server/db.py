"""Storage for the Jade reference server.

Reference backend: SQLite (file in the data dir, WAL mode). All access
goes through the small ``Database`` interface below using parameterized
queries only.

Production swap: implement this same method set against Postgres
(see server/PRODUCTION.md, "database" checklist item). The interface is
deliberately narrow so the swap is mechanical.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS identities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider   TEXT NOT NULL,
    subject    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(provider, subject)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_label TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_tokens (
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    connector        TEXT NOT NULL,
    access_token_enc TEXT NOT NULL,
    refresh_token_enc TEXT NOT NULL DEFAULT '',
    expires_at       INTEGER NOT NULL DEFAULT 0,
    scopes           TEXT NOT NULL DEFAULT '',
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (user_id, connector)
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash    TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    connector     TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pairing (
    id               TEXT PRIMARY KEY,
    code_hash        TEXT NOT NULL UNIQUE,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    owner_device     TEXT NOT NULL DEFAULT '',
    claim_device     TEXT NOT NULL DEFAULT '',
    claim_receipt    TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    accept_token_hash TEXT NOT NULL DEFAULT '',
    endpoint_hint    TEXT NOT NULL DEFAULT '',
    created_at       INTEGER NOT NULL,
    expires_at       INTEGER NOT NULL
);
"""


def _now() -> int:
    return int(time.time())


class Database:
    """SQLite-backed store. Thread-safe via check_same_thread=False + a lock."""

    def __init__(self, path: str | Path):
        import threading
        self._lock = threading.RLock()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.executescript(SCHEMA)

    # -- low-level helpers -------------------------------------------------
    def _exec(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _one(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- users --------------------------------------------------------------
    def create_user(self, email: str, salt: str, pw_hash: str, display_name: str = "") -> int:
        cur = self._exec(
            "INSERT INTO users (email, password_salt, password_hash, display_name, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (email.lower().strip(), salt, pw_hash, display_name, _now()),
        )
        return cur.lastrowid

    def get_user_by_email(self, email: str):
        return self._one("SELECT * FROM users WHERE email = ?", (email.lower().strip(),))

    def get_user(self, user_id: int):
        return self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    def set_password(self, user_id: int, salt: str, pw_hash: str) -> None:
        self._exec("UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
                   (salt, pw_hash, user_id))

    # -- linked OIDC identities ---------------------------------------------
    def link_identity(self, user_id: int, provider: str, subject: str) -> None:
        self._exec(
            "INSERT OR IGNORE INTO identities (user_id, provider, subject, created_at)"
            " VALUES (?, ?, ?, ?)",
            (user_id, provider, subject, _now()),
        )

    def get_user_by_identity(self, provider: str, subject: str):
        return self._one(
            "SELECT u.* FROM users u JOIN identities i ON i.user_id = u.id"
            " WHERE i.provider = ? AND i.subject = ?",
            (provider, subject),
        )

    # -- sessions ------------------------------------------------------------
    def create_session(self, token_hash: str, user_id: int, device_label: str, expires_at: int) -> None:
        self._exec(
            "INSERT INTO sessions (token_hash, user_id, device_label, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (token_hash, user_id, device_label, _now(), expires_at),
        )

    def get_session(self, token_hash: str):
        return self._one("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_session(self, token_hash: str) -> None:
        self._exec("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_expired_sessions(self) -> int:
        cur = self._exec("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
        return cur.rowcount

    # -- connector tokens ------------------------------------------------------
    def store_connector_tokens(self, user_id: int, connector: str, access_enc: str,
                               refresh_enc: str, expires_at: int, scopes: str) -> None:
        self._exec(
            "INSERT INTO connector_tokens (user_id, connector, access_token_enc,"
            " refresh_token_enc, expires_at, scopes, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, connector) DO UPDATE SET"
            " access_token_enc=excluded.access_token_enc,"
            " refresh_token_enc=excluded.refresh_token_enc,"
            " expires_at=excluded.expires_at, scopes=excluded.scopes,"
            " updated_at=excluded.updated_at",
            (user_id, connector, access_enc, refresh_enc, expires_at, scopes, _now()),
        )

    def get_connector_tokens(self, user_id: int, connector: str):
        return self._one(
            "SELECT * FROM connector_tokens WHERE user_id = ? AND connector = ?",
            (user_id, connector),
        )

    def list_connector_tokens(self, user_id: int):
        return self._all("SELECT connector, expires_at, scopes, updated_at FROM connector_tokens"
                         " WHERE user_id = ?", (user_id,))

    def delete_connector_tokens(self, user_id: int, connector: str) -> None:
        self._exec("DELETE FROM connector_tokens WHERE user_id = ? AND connector = ?",
                   (user_id, connector))

    # -- OAuth state (PKCE verifiers for the authorize round-trip) --------------
    def save_oauth_state(self, state_hash: str, user_id: int, connector: str,
                         code_verifier: str, ttl: int = 600) -> None:
        self._exec(
            "INSERT INTO oauth_states (state_hash, user_id, connector, code_verifier,"
            " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (state_hash, user_id, connector, code_verifier, _now(), _now() + ttl),
        )

    def consume_oauth_state(self, state_hash: str):
        """Fetch-and-delete; returns the row or None (single use)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM oauth_states WHERE state_hash = ?", (state_hash,)).fetchone()
            if row:
                self._conn.execute("DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,))
                self._conn.commit()
            self._conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (_now(),))
            self._conn.commit()
            return row

    # -- pairing ---------------------------------------------------------------
    def create_pairing(self, pairing_id: str, code_hash: str, user_id: int,
                       owner_device: str, expires_at: int) -> None:
        self._exec(
            "INSERT INTO pairing (id, code_hash, user_id, owner_device, status,"
            " created_at, expires_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (pairing_id, code_hash, user_id, owner_device, _now(), expires_at),
        )

    def get_pairing_by_code_hash(self, code_hash: str):
        return self._one("SELECT * FROM pairing WHERE code_hash = ?", (code_hash,))

    def get_pairing(self, pairing_id: str):
        return self._one("SELECT * FROM pairing WHERE id = ?", (pairing_id,))

    def update_pairing(self, pairing_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE pairing SET {cols} WHERE id = ?", (*fields.values(), pairing_id))

    def delete_pairing(self, pairing_id: str) -> None:
        self._exec("DELETE FROM pairing WHERE id = ?", (pairing_id,))

    def purge_expired_pairings(self) -> int:
        cur = self._exec("DELETE FROM pairing WHERE expires_at < ?", (_now(),))
        return cur.rowcount


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_hex(12)}"
