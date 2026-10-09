"""OneDrive connector via Microsoft Graph.

Delegated scopes: Files.ReadWrite. Bring-your-own Azure app registration
(see core.connectors.graph); shares the Microsoft token file with the
other Microsoft connectors.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from core.connectors.base import Connector
from core.connectors.graph import GraphConnectorBase

# Downloads are confined to the Jade workspace, mirroring core/p2p.py.
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent


def _is_path_confined(raw_path: str) -> "Path | None":
    try:
        full = Path(os.path.expanduser(raw_path)).resolve()
    except Exception:
        return None
    try:
        full.relative_to(WORKSPACE_ROOT)
    except ValueError:
        return None
    return full


class OneDriveConnector(GraphConnectorBase, Connector):
    id = "onedrive"
    display = "OneDrive"
    description = "List, upload and download OneDrive files via Microsoft Graph."
    auth_kind = "oauth2"
    scopes = ["Files.ReadWrite"]

    def actions(self) -> Dict[str, Dict[str, Any]]:
        client = self.graph()

        def _list(folder: str = "/", limit: int = 20):
            path = folder.strip("/") or "root"
            seg = "root" if path == "root" else f"root:/{path}"
            res = client.get(f"/me/drive/{seg}/children",
                             {"$top": str(min(limit, 100)),
                              "$select": "name,size,lastModifiedDateTime,folder,file"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            out = [{"name": i.get("name"), "size": i.get("size"),
                    "folder": "folder" in i,
                    "modified": i.get("lastModifiedDateTime")}
                   for i in res.get("value", [])]
            return {"ok": True, "files": out}

        def _delta(token: str = ""):
            # Simple delta listing: pass the token from a previous call to
            # get only changes. First call (token="") returns everything.
            url = (f"/me/drive/root/delta?token={token}" if token
                   else "/me/drive/root/delta")
            res = client.get(url)
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            items = [{"name": i.get("name"), "deleted": "deleted" in i}
                     for i in res.get("value", [])]
            return {"ok": True, "items": items,
                    "delta_token": (res.get("@odata.deltaLink") or "").split("token=")[-1]}

        def _upload(local_path: str, remote_path: str):
            # Simple upload (<= 4 MiB). Larger files need an upload session;
            # we say so instead of silently failing.
            src = Path(os.path.expanduser(local_path))
            if not src.is_file():
                return {"ok": False, "error": f"Local file not found: {local_path}"}
            size = src.stat().st_size
            if size > 4 * 1024 * 1024:
                return {"ok": False,
                        "error": "Files over 4 MiB need a resumable upload session, "
                                 "which this connector does not implement yet."}
            remote = remote_path.strip("/")
            from core.connectors import graph as gmod
            import urllib.request
            url = f"{gmod.GRAPH}/me/drive/root:/{remote}:/content"
            req = urllib.request.Request(
                url, data=src.read_bytes(), method="PUT",
                headers={"Authorization": f"Bearer {client._auth()}",
                         "Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    import json as _json
                    info = _json.loads(r.read().decode())
            except Exception as e:
                return {"ok": False, "error": f"Upload failed: {e}"}
            return {"ok": True, "name": info.get("name"), "size": info.get("size")}

        def _download(remote_path: str, local_path: str):
            dest = _is_path_confined(local_path)
            if dest is None:
                return {"ok": False,
                        "error": "Downloads are confined to the Jade workspace."}
            remote = remote_path.strip("/")
            res = client.request("GET", f"/me/drive/root:/{remote}:/content",
                                 raw=True)
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(res["bytes"])
            return {"ok": True, "saved": str(dest), "bytes": len(res["bytes"])}

        return {
            "list_files": {"description": "List OneDrive folder contents (folder, limit).",
                           "run": _list},
            "delta": {"description": "List changed files since delta token (token='').",
                      "run": _delta},
            "upload": {"description": "Upload a local file <= 4 MiB (local_path, remote_path).",
                       "run": _upload},
            "download": {"description": "Download a file into the workspace (remote_path, local_path).",
                         "run": _download},
        }
