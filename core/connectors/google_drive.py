"""Google Drive connector: list, search and read files (read-only)."""
from __future__ import annotations

from typing import Any, Dict

from core.connectors._google import GoogleConnector
from core.connectors.oauth import api_get


class GoogleDriveConnector(GoogleConnector):
    id = "gdrive"
    display = "Google Drive"
    description = "List, search and read your Google Drive files (read-only)."
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]

    def actions(self) -> Dict[str, Dict[str, Any]]:
        tok = self.fresh_access_token()

        def _list(folder: str = "root", limit: int = 20):
            q = f"'{folder}' in parents and trashed=false"
            res = api_get("https://www.googleapis.com/drive/v3/files", tok,
                          {"q": q, "pageSize": str(min(limit, 100)),
                           "fields": "files(id,name,mimeType,size,modifiedTime)"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "files": res.get("files", [])}

        def _search(query: str, limit: int = 20):
            res = api_get("https://www.googleapis.com/drive/v3/files", tok,
                          {"q": f"name contains '{query}' and trashed=false",
                           "pageSize": str(min(limit, 100)),
                           "fields": "files(id,name,mimeType,size,modifiedTime)"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "files": res.get("files", [])}

        def _read(file_id: str):
            meta = api_get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                           tok, {"fields": "id,name,mimeType,size"})
            if "error" in meta:
                return {"ok": False, "error": meta["error"]}
            mime = meta.get("mimeType", "")
            if mime == "application/vnd.google-apps.document":
                url = (f"https://www.googleapis.com/drive/v3/files/{file_id}/export")
                res = api_get(url, tok, {"mimeType": "text/plain"}, raw=True)
            elif mime.startswith("text/") or mime in (
                    "application/json", "application/pdf"):
                url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
                res = api_get(url, tok, {"alt": "media"}, raw=True)
            else:
                return {"ok": True, "name": meta.get("name"),
                        "note": f"Binary file ({mime}); metadata only."}
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "name": meta.get("name"),
                    "text": res.get("text", "")[:12000]}

        return {
            "list_files": {"description": "List files in a Drive folder.",
                           "run": _list},
            "search_files": {"description": "Search Drive files by name.",
                             "run": _search},
            "read_file": {"description": "Read a file's text content (file_id).",
                          "run": _read},
        }
