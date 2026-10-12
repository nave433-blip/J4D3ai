"""Google Docs / Sheets / Slides connector.

Scopes: documents, spreadsheets, presentations (full per-app access).
``google_drive.py`` covers Drive file listing/search — this connector
covers the editors' APIs and does not duplicate it.

Honest limits: Docs structural-element parsing extracts paragraph text;
tables/footnotes come through as inline text. Sheets appends rows;
formatting/charts are out of scope.
"""
from __future__ import annotations

from typing import Any, Dict, List

from core.connectors._google import GoogleConnector
from core.connectors.oauth import api_get, api_request

DOCS = "https://docs.googleapis.com/v1/documents"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"


def _doc_text(doc: Dict[str, Any]) -> str:
    """Extract plain text from a Docs API document body."""
    chunks: List[str] = []
    for el in (doc.get("body") or {}).get("content", []):
        para = el.get("paragraph")
        if not para:
            continue
        for pel in para.get("elements", []):
            tr = pel.get("textRun")
            if tr:
                chunks.append(tr.get("content", ""))
    return "".join(chunks)


class GoogleDocsConnector(GoogleConnector):
    id = "gdocs"
    display = "Google Docs / Sheets / Slides"
    description = ("Read/create/append Google Docs, read/append Sheets, "
                   "list Slides decks.")
    scopes = ["https://www.googleapis.com/auth/documents",
              "https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/presentations"]

    def actions(self) -> Dict[str, Dict[str, Any]]:
        tok = self.fresh_access_token()

        # ---- Docs ------------------------------------------------------
        def _read_doc(document_id: str, limit: int = 12000):
            res = api_get(f"{DOCS}/{document_id}", tok)
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True,
                    "title": (res.get("title") or ""),
                    "text": _doc_text(res)[:limit]}

        def _create_doc(title: str):
            res = api_request("POST", DOCS, tok, payload={"title": title})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "document_id": res.get("documentId"),
                    "title": res.get("title")}

        def _append_doc(document_id: str, text: str):
            # Insert at end of body: index = endIndex - 1.
            doc = api_get(f"{DOCS}/{document_id}", tok,
                          {"fields": "body.content.endIndex"})
            if "error" in doc:
                return {"ok": False, "error": doc["error"]}
            try:
                idx = doc["body"]["content"][-1]["endIndex"] - 1
            except (KeyError, IndexError, TypeError):
                return {"ok": False, "error": "Could not find document end."}
            res = api_request(
                "POST", f"{DOCS}/{document_id}:batchUpdate", tok,
                payload={"requests": [{"insertText": {
                    "location": {"index": idx}, "text": text}}]})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "appended_chars": len(text)}

        # ---- Sheets ----------------------------------------------------
        def _read_sheet(spreadsheet_id: str, range: str = "A1:Z100"):
            res = api_get(f"{SHEETS}/{spreadsheet_id}/values/{range}", tok)
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "range": res.get("range", ""),
                    "values": res.get("values", [])}

        def _append_sheet(spreadsheet_id: str, range: str, rows: List[List[str]]):
            res = api_request(
                "POST", f"{SHEETS}/{spreadsheet_id}/values/{range}:append", tok,
                params={"valueInputOption": "USER_ENTERED"},
                payload={"values": rows})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            upd = res.get("updates", {})
            return {"ok": True,
                    "updated_cells": upd.get("updatedCells", 0),
                    "updated_range": upd.get("updatedRange", "")}

        # ---- Slides ----------------------------------------------------
        def _list_slides(presentation_id: str):
            res = api_get(
                f"https://slides.googleapis.com/v1/presentations/{presentation_id}",
                tok, {"fields": "title,slides(objectId,pageElements.shape.text.textElements.textRun.content)"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            slides = []
            for s in res.get("slides", []):
                texts = []
                for pe in s.get("pageElements", []):
                    shape = pe.get("shape") or {}
                    for te in (shape.get("text") or {}).get("textElements", []):
                        tr = te.get("textRun")
                        if tr:
                            texts.append(tr.get("content", ""))
                slides.append({"id": s.get("objectId"),
                               "text": "".join(texts)[:2000]})
            return {"ok": True, "title": res.get("title", ""),
                    "slides": slides}

        return {
            "read_doc": {"description": "Read a Google Doc as text (document_id).",
                         "run": _read_doc},
            "create_doc": {"description": "Create a blank doc (title).",
                           "run": _create_doc},
            "append_doc": {"description": "Append text to a doc (document_id, text).",
                           "run": _append_doc},
            "read_sheet": {"description": "Read sheet rows (spreadsheet_id, range='A1:Z100').",
                           "run": _read_sheet},
            "append_sheet": {"description": "Append rows (spreadsheet_id, range, rows).",
                             "run": _append_sheet},
            "list_slides": {"description": "List slides + text (presentation_id).",
                            "run": _list_slides},
        }
