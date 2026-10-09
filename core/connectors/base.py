"""Generic connector framework for Jade.

A connector links an external app account (Google Drive, Gmail, Outlook,
Calendar, …) so the chat layer can call simple, confirmed actions on it.

Rules:
- Tokens live in the OS keyring under ``jade_connector_<id>`` — never in
  plaintext config, never transmitted over P2P.
- :meth:`Connector.status` must be honest: ``connected: False`` with a clear
  ``needs`` message until a real token exists. No faked connections.
- Actions return ``{"ok": ..., }`` dicts and raise nothing; the chat layer
  confirms with the user before invoking anything side-effecting.

To add a connector: subclass :class:`Connector`, implement
``connect()`` (usually via :mod:`core.connectors.oauth`), declare
:meth:`actions`, and register it in :mod:`core.connectors`.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import keyring

KEYRING_PREFIX = "jade_connector_"


class Connector:
    id: str = "base"
    display: str = "Base connector"
    description: str = ""
    auth_kind: str = "oauth2"  # "oauth2" | "token" | "none"
    scopes: List[str] = []

    # -- token storage ---------------------------------------------------
    def keyring_service(self) -> str:
        return f"{KEYRING_PREFIX}{self.id}"

    def get_token(self) -> Optional[Dict[str, Any]]:
        try:
            raw = keyring.get_password(self.keyring_service(), "token")
        except Exception:
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def save_token(self, token: Dict[str, Any]) -> None:
        keyring.set_password(self.keyring_service(), "token", json.dumps(token))

    def clear_token(self) -> None:
        try:
            keyring.delete_password(self.keyring_service(), "token")
        except Exception:
            pass

    # -- lifecycle -------------------------------------------------------
    def is_connected(self) -> bool:
        tok = self.get_token()
        return bool(tok and (tok.get("access_token") or tok.get("token")))

    def connect(self) -> Dict[str, Any]:
        """Interactive connect flow. Subclasses implement the real flow."""
        return {"ok": False, "error": "Not implemented."}

    def disconnect(self, _confirmed: bool = False) -> Dict[str, Any]:
        if not self.is_connected():
            return {"ok": True, "message": "Already disconnected."}
        if not _confirmed:
            from core.approvals import confirm
            if not confirm(f"Disconnect {self.display}? Stored tokens will be deleted."):
                return {"ok": False, "error": "cancelled"}
        self.clear_token()
        return {"ok": True, "message": f"{self.display} disconnected."}

    def status(self) -> Dict[str, Any]:
        tok = self.get_token()
        return {
            "id": self.id,
            "display": self.display,
            "connected": self.is_connected(),
            "account": (tok or {}).get("account", ""),
            "scopes": self.scopes,
            "needs": "" if self.is_connected() else self._needs_message(),
        }

    def _needs_message(self) -> str:
        return f"Run `/connector connect {self.id}` to link your account."

    # -- actions ---------------------------------------------------------
    def actions(self) -> Dict[str, Dict[str, Any]]:
        """name -> {"description": str, "run": callable(**kwargs) -> dict}."""
        return {}

    def run_action(self, name: str, **kwargs) -> Dict[str, Any]:
        acts = self.actions()
        if name not in acts:
            return {"ok": False, "error": f"Unknown action '{name}'."}
        if not self.is_connected():
            return {"ok": False, "error": f"{self.display} is not connected. {self._needs_message()}"}
        try:
            return acts[name]["run"](**kwargs)
        except Exception as e:
            return {"ok": False, "error": f"{self.display} action '{name}' failed: {e}"}
