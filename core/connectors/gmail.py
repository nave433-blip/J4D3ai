"""Gmail connector: list, search and read messages (read-only)."""
from __future__ import annotations

import base64
from typing import Any, Dict

from core.connectors._google import GoogleConnector
from core.connectors.oauth import api_get


def _headers(payload: Dict[str, Any]) -> Dict[str, str]:
    out = {}
    for h in payload.get("headers", []):
        out[h.get("name", "").lower()] = h.get("value", "")
    return out


def _snippet_body(msg: Dict[str, Any]) -> str:
    payload = msg.get("payload", {})
    body = payload.get("body", {}).get("data", "")
    if not body:
        for part in payload.get("parts", []):
            if part.get("mimeType", "").startswith("text/plain"):
                body = part.get("body", {}).get("data", "")
                break
    try:
        return base64.urlsafe_b64decode(body + "==").decode("utf-8", "replace")[:6000]
    except Exception:
        return msg.get("snippet", "")


class GmailConnector(GoogleConnector):
    id = "gmail"
    display = "Gmail"
    description = "List, search and read your Gmail messages (read-only)."
    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

    def actions(self) -> Dict[str, Dict[str, Any]]:
        tok = self.fresh_access_token()
        base = "https://gmail.googleapis.com/gmail/v1/users/me"

        def _list(limit: int = 10):
            res = api_get(f"{base}/messages", tok,
                          {"maxResults": str(min(limit, 50))})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            out = []
            for m in res.get("messages", []):
                full = api_get(f"{base}/messages/{m['id']}", tok,
                               {"format": "metadata",
                                "metadataHeaders": "From,Subject,Date"})
                if "error" in full:
                    continue
                h = _headers(full.get("payload", {}))
                out.append({"id": m["id"], "from": h.get("from", ""),
                            "subject": h.get("subject", ""),
                            "date": h.get("date", ""),
                            "snippet": full.get("snippet", "")})
            return {"ok": True, "messages": out}

        def _search(query: str, limit: int = 10):
            res = api_get(f"{base}/messages", tok,
                          {"q": query, "maxResults": str(min(limit, 50))})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            out = []
            for m in res.get("messages", []):
                full = api_get(f"{base}/messages/{m['id']}", tok,
                               {"format": "metadata",
                                "metadataHeaders": "From,Subject,Date"})
                if "error" in full:
                    continue
                h = _headers(full.get("payload", {}))
                out.append({"id": m["id"], "from": h.get("from", ""),
                            "subject": h.get("subject", ""),
                            "date": h.get("date", "")})
            return {"ok": True, "messages": out,
                    "result_size": res.get("resultSizeEstimate", 0)}

        def _read(message_id: str):
            full = api_get(f"{base}/messages/{message_id}", tok, {"format": "full"})
            if "error" in full:
                return {"ok": False, "error": full["error"]}
            h = _headers(full.get("payload", {}))
            return {"ok": True, "from": h.get("from", ""),
                    "subject": h.get("subject", ""), "date": h.get("date", ""),
                    "body": _snippet_body(full)}

        return {
            "list_messages": {"description": "List recent inbox messages.",
                              "run": _list},
            "search": {"description": "Search messages (Gmail query syntax).",
                       "run": _search},
            "read_message": {"description": "Read one message body (message_id).",
                             "run": _read},
        }
