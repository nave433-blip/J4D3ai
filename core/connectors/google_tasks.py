"""Google Tasks connector: task lists and tasks CRUD."""
from __future__ import annotations

from typing import Any, Dict

from core.connectors._google import GoogleConnector
from core.connectors.oauth import api_get, api_request

BASE = "https://tasks.googleapis.com/tasks/v1"


class GoogleTasksConnector(GoogleConnector):
    id = "gtasks"
    display = "Google Tasks"
    description = "Manage Google Tasks lists and tasks (CRUD)."
    scopes = ["https://www.googleapis.com/auth/tasks"]

    def actions(self) -> Dict[str, Dict[str, Any]]:
        tok = (self.get_token() or {}).get("access_token", "")

        def _lists():
            res = api_get(f"{BASE}/users/@me/lists", tok,
                          {"maxResults": "100"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "lists": [
                {"id": l.get("id"), "title": l.get("title", "")}
                for l in res.get("items", [])]}

        def _list_tasks(tasklist: str = "@default", limit: int = 20,
                        show_completed: bool = False):
            res = api_get(f"{BASE}/lists/{tasklist}/tasks", tok,
                          {"maxResults": str(min(limit, 100)),
                           "showCompleted": str(show_completed).lower()})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "tasks": [
                {"id": t.get("id"), "title": t.get("title", ""),
                 "status": t.get("status", ""), "due": t.get("due", ""),
                 "notes": (t.get("notes") or "")[:500]}
                for t in res.get("items", [])]}

        def _add(tasklist: str, title: str, notes: str = "", due: str = ""):
            payload: Dict[str, Any] = {"title": title}
            if notes:
                payload["notes"] = notes
            if due:
                payload["due"] = due  # RFC 3339, e.g. 2026-10-01T00:00:00Z
            res = api_request("POST", f"{BASE}/lists/{tasklist}/tasks", tok,
                              payload=payload)
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "id": res.get("id"), "title": res.get("title")}

        def _complete(tasklist: str, task_id: str):
            res = api_request("PATCH", f"{BASE}/lists/{tasklist}/tasks/{task_id}",
                              tok, payload={"status": "completed"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "completed": task_id}

        def _delete(tasklist: str, task_id: str):
            res = api_request("DELETE",
                              f"{BASE}/lists/{tasklist}/tasks/{task_id}", tok)
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "deleted": task_id}

        return {
            "list_tasklists": {"description": "List your task lists.",
                               "run": _lists},
            "list_tasks": {"description": "List tasks (tasklist='@default', limit, show_completed).",
                           "run": _list_tasks},
            "add_task": {"description": "Add a task (tasklist, title, [notes], [due RFC3339]).",
                         "run": _add},
            "complete_task": {"description": "Mark a task completed (tasklist, task_id).",
                              "run": _complete},
            "delete_task": {"description": "Delete a task (tasklist, task_id).",
                            "run": _delete},
        }
