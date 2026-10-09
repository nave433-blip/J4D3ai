"""Environment-based configuration for the Jade reference server.

All settings come from the environment (see ``.env.example``). Nothing
secret is ever logged — use ``redacted()`` when printing config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


@dataclass
class ServerConfig:
    host: str = field(default_factory=lambda: _env("JADE_SERVER_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("JADE_SERVER_PORT", 8080))
    data_dir: str = field(default_factory=lambda: _env("JADE_DATA_DIR", "./server-data"))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", ""))

    # Fernet key (base64, 32 bytes) protecting stored connector tokens.
    # Generate: openssl rand -base64 32
    token_key: str = field(default_factory=lambda: _env("JADE_TOKEN_KEY", ""))

    session_ttl_seconds: int = field(default_factory=lambda: _env_int("JADE_SESSION_TTL", 30 * 24 * 3600))
    pairing_code_ttl_seconds: int = field(default_factory=lambda: _env_int("JADE_PAIRING_TTL", 600))
    public_url: str = field(default_factory=lambda: _env("JADE_PUBLIC_URL", "http://localhost:8080"))

    rate_limit_per_minute: int = field(default_factory=lambda: _env_int("JADE_RATE_LIMIT", 120))

    # --- OAuth app credentials (server holds ONE set per provider so end
    # users never register their own OAuth apps) ---
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET", ""))
    microsoft_client_id: str = field(default_factory=lambda: _env("MICROSOFT_CLIENT_ID", ""))
    microsoft_client_secret: str = field(default_factory=lambda: _env("MICROSOFT_CLIENT_SECRET", ""))
    microsoft_tenant: str = field(default_factory=lambda: _env("MICROSOFT_TENANT", "common"))
    github_client_id: str = field(default_factory=lambda: _env("GITHUB_CLIENT_ID", ""))
    github_client_secret: str = field(default_factory=lambda: _env("GITHUB_CLIENT_SECRET", ""))
    apple_client_id: str = field(default_factory=lambda: _env("APPLE_CLIENT_ID", ""))
    # Apple uses a signed client_secret JWT (team id + key id + .p8).
    # The reference server documents the shape; operators supply it.
    apple_team_id: str = field(default_factory=lambda: _env("APPLE_TEAM_ID", ""))
    apple_key_id: str = field(default_factory=lambda: _env("APPLE_KEY_ID", ""))
    apple_private_key_pem: str = field(default_factory=lambda: _env("APPLE_PRIVATE_KEY_PEM", ""))

    # --- Hosted chat completions (OpenAI-compatible endpoint) ---
    chat_provider: str = field(default_factory=lambda: _env("JADE_CHAT_PROVIDER", ""))
    chat_base_url: str = field(default_factory=lambda: _env("JADE_CHAT_BASE_URL", ""))
    chat_api_key: str = field(default_factory=lambda: _env("JADE_CHAT_API_KEY", ""))
    chat_model: str = field(default_factory=lambda: _env("JADE_CHAT_MODEL", ""))

    def validate(self) -> list[str]:
        """Return a list of configuration problems (empty = ok to boot)."""
        problems: list[str] = []
        if self.database_url.startswith("postgresql://"):
            problems.append(
                "DATABASE_URL points at postgres, but the reference implementation "
                "ships the sqlite backend only — see server/PRODUCTION.md "
                "('database' checklist item) for the swap."
            )
        return problems

    def redacted(self) -> dict:
        """Config safe to print/log: secrets replaced."""
        secret_names = {
            "token_key", "google_client_secret", "microsoft_client_secret",
            "github_client_secret", "apple_private_key_pem", "chat_api_key",
        }
        out = {}
        for k, v in self.__dict__.items():
            if k in secret_names:
                out[k] = "***set***" if v else ""
            else:
                out[k] = v
        return out
