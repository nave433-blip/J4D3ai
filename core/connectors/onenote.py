"""OneNote connector via Microsoft Graph.

Delegated scopes: Notes.ReadWrite. Bring-your-own Azure app registration
(see core.connectors.graph); shares the Microsoft token file with the
other Microsoft connectors.
"""
from __future__ import annotations

from typing import Any, Dict

from core.connectors.base import Connector
from core.connectors.graph import GraphConnectorBase


class OneNoteConnector(GraphConnectorBase, Connector):
    id = "onenote"
    display = "OneNote"
    description = ("List OneNote notebooks/sections/pages and create pages "
                   "from simple HTML via Microsoft Graph.")
    auth_kind = "oauth2"
    scopes = ["Notes.ReadWrite"]

    def actions(self) -> Dict[str, Dict[str, Any]]:
        client = self.graph()

        def _list_notebooks():
            res = client.get("/me/onenote/notebooks",
                             {"$select": "id,displayName,createdDateTime"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "notebooks": [
                {"id": n.get("id"), "name": n.get("displayName", ""),
                 "created": n.get("createdDateTime", "")}
                for n in res.get("value", [])]}

        def _list_sections(notebook_id: str = ""):
            path = (f"/me/onenote/notebooks/{notebook_id}/sections"
                    if notebook_id else "/me/onenote/sections")
            res = client.get(path, {"$select": "id,displayName,parentNotebook"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "sections": [
                {"id": s.get("id"), "name": s.get("displayName", "")}
                for s in res.get("value", [])]}

        def _list_pages(section_id: str = "", limit: int = 20):
            path = (f"/me/onenote/sections/{section_id}/pages"
                    if section_id else "/me/onenote/pages")
            res = client.get(path, {"$top": str(min(limit, 50)),
                                    "$select": "id,title,createdDateTime",
                                    "$orderby": "createdDateTime desc"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "pages": [
                {"id": p.get("id"), "title": p.get("title", ""),
                 "created": p.get("createdDateTime", "")}
                for p in res.get("value", [])]}

        def _read_page(page_id: str):
            # HTML content endpoint — return as text.
            res = client.request("GET", f"/me/onenote/pages/{page_id}/content",
                                 raw=True)
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True,
                    "html": res["bytes"].decode("utf-8", "replace")[:12000]}

        def _create_page(title: str, html_body: str, section_id: str = ""):
            from core.connectors import graph as gmod
            import urllib.request
            html = (f"<!DOCTYPE html><html><head><title>{title}</title></head>"
                    f"<body>{html_body}</body></html>")
            path = (f"/me/onenote/sections/{section_id}/pages"
                    if section_id else "/me/onenote/pages")
            req = urllib.request.Request(
                gmod.GRAPH + path, data=html.encode("utf-8"), method="POST",
                headers={"Authorization": f"Bearer {client._auth()}",
                         "Content-Type": "text/html"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    import json as _json
                    info = _json.loads(r.read().decode())
            except Exception as e:
                return {"ok": False, "error": f"Page creation failed: {e}"}
            return {"ok": True, "id": info.get("id"),
                    "title": info.get("title", "")}

        return {
            "list_notebooks": {"description": "List OneNote notebooks.",
                               "run": _list_notebooks},
            "list_sections": {"description": "List sections (notebook_id='', all).",
                              "run": _list_sections},
            "list_pages": {"description": "List recent pages (section_id='', all).",
                           "run": _list_pages},
            "read_page": {"description": "Read a page's HTML (page_id).",
                          "run": _read_page},
            "create_page": {"description": "Create a page from HTML (title, html_body, [section_id]).",
                            "run": _create_page},
        }
