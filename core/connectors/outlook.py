"""Outlook / Microsoft 365 connector via Microsoft Graph.

Bring-your-own Azure app registration: set ``ms_client_id`` in Jade's
settings (``jade settings set ms_client_id <id>``) — any "personal
Microsoft account" multi-tenant app works. Shares the Microsoft token file
with the other Microsoft connectors (see core.connectors.graph).

Delegated scopes: Mail.Read, Mail.Send, Calendars.ReadWrite,
Contacts.Read, Tasks.ReadWrite.
"""
from __future__ import annotations

from typing import Any, Dict, List

from core.connectors.base import Connector
from core.connectors.graph import GraphConnectorBase


def _summarize_messages(messages: List[Dict[str, Any]]):
    out = []
    for m in messages:
        frm = (m.get("from") or {}).get("emailAddress", {})
        out.append({"id": m.get("id"), "from": frm.get("address", ""),
                    "subject": m.get("subject", ""),
                    "received": m.get("receivedDateTime", ""),
                    "preview": m.get("bodyPreview", "")})
    return out


def _summarize_events(events: List[Dict[str, Any]]):
    out = []
    for e in events:
        out.append({"id": e.get("id"),
                    "subject": e.get("subject", "(no title)"),
                    "start": (e.get("start") or {}).get("dateTime", ""),
                    "end": (e.get("end") or {}).get("dateTime", ""),
                    "location": (e.get("location") or {}).get("displayName", "")})
    return out


class OutlookConnector(GraphConnectorBase, Connector):
    id = "outlook"
    display = "Outlook / Microsoft 365"
    description = ("Outlook mail (read + send), calendar (CRUD), contacts "
                   "and To Do tasks via Microsoft Graph.")
    auth_kind = "oauth2"
    scopes = ["Mail.Read", "Mail.Send", "Calendars.ReadWrite",
              "Contacts.Read", "Tasks.ReadWrite"]

    def actions(self) -> Dict[str, Dict[str, Any]]:
        client = self.graph()

        # ---- mail ------------------------------------------------------
        def _list_messages(limit: int = 10):
            res = client.get("/me/messages",
                             {"$top": str(min(limit, 50)),
                              "$select": "id,subject,from,receivedDateTime,bodyPreview",
                              "$orderby": "receivedDateTime desc"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "messages": _summarize_messages(res.get("value", []))}

        def _search_messages(query: str, limit: int = 10):
            res = client.get("/me/messages",
                             {"$top": str(min(limit, 50)), "$search": f'"{query}"',
                              "$select": "id,subject,from,receivedDateTime,bodyPreview"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "messages": _summarize_messages(res.get("value", []))}

        def _read_message(message_id: str):
            res = client.get(f"/me/messages/{message_id}",
                             {"$select": "subject,from,receivedDateTime,body"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            body = (res.get("body") or {}).get("content", "")
            frm = (res.get("from") or {}).get("emailAddress", {})
            return {"ok": True, "from": frm.get("address", ""),
                    "subject": res.get("subject", ""),
                    "received": res.get("receivedDateTime", ""),
                    "body": body[:12000]}

        def _send_mail(to: str, subject: str, body: str):
            recipients = [{"emailAddress": {"address": a.strip()}}
                          for a in to.split(",") if a.strip()]
            if not recipients:
                return {"ok": False, "error": "No recipients given."}
            payload = {"message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": recipients}}
            res = client.request("POST", "/me/sendMail", payload=payload)
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "sent_to": [r["emailAddress"]["address"]
                                            for r in recipients]}

        # ---- calendar --------------------------------------------------
        def _upcoming_events(days: int = 7, limit: int = 20):
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            end = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(days=days)).isoformat()
            res = client.get("/me/calendarview",
                             {"startDateTime": now, "endDateTime": end,
                              "$top": str(min(limit, 50)),
                              "$select": "id,subject,start,end,location",
                              "$orderby": "start/dateTime"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "events": _summarize_events(res.get("value", []))}

        def _create_event(subject: str, start: str, end: str,
                          location: str = "", body: str = "",
                          timezone: str = "UTC"):
            payload = {"subject": subject,
                       "start": {"dateTime": start, "timeZone": timezone},
                       "end": {"dateTime": end, "timeZone": timezone}}
            if location:
                payload["location"] = {"displayName": location}
            if body:
                payload["body"] = {"contentType": "Text", "content": body}
            res = client.request("POST", "/me/events", payload=payload)
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "id": res.get("id"),
                    "link": res.get("webLink", "")}

        def _update_event(event_id: str, subject: str = "", start: str = "",
                          end: str = "", location: str = ""):
            payload: Dict[str, Any] = {}
            if subject:
                payload["subject"] = subject
            if start:
                payload["start"] = {"dateTime": start, "timeZone": "UTC"}
            if end:
                payload["end"] = {"dateTime": end, "timeZone": "UTC"}
            if location:
                payload["location"] = {"displayName": location}
            if not payload:
                return {"ok": False, "error": "Nothing to update."}
            res = client.request("PATCH", f"/me/events/{event_id}",
                                 payload=payload)
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "id": res.get("id")}

        def _delete_event(event_id: str):
            res = client.request("DELETE", f"/me/events/{event_id}")
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "deleted": event_id}

        # ---- contacts --------------------------------------------------
        def _list_contacts(limit: int = 20):
            res = client.get("/me/contacts",
                             {"$top": str(min(limit, 50)),
                              "$select": "displayName,emailAddresses,mobilePhone",
                              "$orderby": "displayName"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            out = []
            for c in res.get("value", []):
                emails = [e.get("address", "") for e in c.get("emailAddresses", [])]
                out.append({"name": c.get("displayName", ""),
                            "emails": emails,
                            "mobile": (c.get("mobilePhone") or "")})
            return {"ok": True, "contacts": out}

        def _search_contacts(query: str, limit: int = 10):
            filt = f"contains(displayName,'{query.replace(chr(39), chr(39)*2)}')"
            res = client.get("/me/contacts",
                             {"$filter": filt, "$top": str(min(limit, 50)),
                              "$select": "displayName,emailAddresses,mobilePhone"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            out = [{"name": c.get("displayName", ""),
                    "emails": [e.get("address", "") for e in c.get("emailAddresses", [])]}
                   for c in res.get("value", [])]
            return {"ok": True, "contacts": out}

        # ---- To Do -----------------------------------------------------
        def _todo_lists():
            res = client.get("/me/todo/lists", {"$select": "id,displayName"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "lists": [
                {"id": l.get("id"), "name": l.get("displayName", "")}
                for l in res.get("value", [])]}

        def _todo_tasks(list_id: str, limit: int = 20):
            res = client.get(f"/me/todo/lists/{list_id}/tasks",
                             {"$top": str(min(limit, 50)),
                              "$select": "id,title,status,dueDateTime"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "tasks": [
                {"id": t.get("id"), "title": t.get("title", ""),
                 "status": t.get("status", ""),
                 "due": ((t.get("dueDateTime") or {}).get("dateTime", ""))}
                for t in res.get("value", [])]}

        def _todo_add(list_id: str, title: str):
            res = client.request("POST", f"/me/todo/lists/{list_id}/tasks",
                                 payload={"title": title})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "id": res.get("id"), "title": res.get("title")}

        def _todo_complete(list_id: str, task_id: str):
            res = client.request("PATCH",
                                 f"/me/todo/lists/{list_id}/tasks/{task_id}",
                                 payload={"status": "completed"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "completed": task_id}

        def _todo_delete(list_id: str, task_id: str):
            res = client.request("DELETE",
                                 f"/me/todo/lists/{list_id}/tasks/{task_id}")
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "deleted": task_id}

        return {
            "list_messages": {"description": "List recent Outlook messages.",
                              "run": _list_messages},
            "search_messages": {"description": "Search Outlook messages.",
                                "run": _search_messages},
            "read_message": {"description": "Read one message (message_id).",
                             "run": _read_message},
            "send_mail": {"description": "Send mail (to, subject, body).",
                          "run": _send_mail},
            "upcoming_events": {"description": "List upcoming calendar events (days, limit).",
                                "run": _upcoming_events},
            "create_event": {"description": "Create event (subject, start, end, [location], [body], [timezone]). ISO datetimes.",
                             "run": _create_event},
            "update_event": {"description": "Update event fields (event_id, ...).",
                             "run": _update_event},
            "delete_event": {"description": "Delete an event (event_id).",
                             "run": _delete_event},
            "list_contacts": {"description": "List contacts (limit).",
                              "run": _list_contacts},
            "search_contacts": {"description": "Search contacts by name (query).",
                                "run": _search_contacts},
            "todo_lists": {"description": "List To Do task lists.",
                           "run": _todo_lists},
            "todo_tasks": {"description": "List tasks in a list (list_id).",
                           "run": _todo_tasks},
            "todo_add": {"description": "Add a task (list_id, title).",
                         "run": _todo_add},
            "todo_complete": {"description": "Mark a task completed (list_id, task_id).",
                              "run": _todo_complete},
            "todo_delete": {"description": "Delete a task (list_id, task_id).",
                            "run": _todo_delete},
        }
