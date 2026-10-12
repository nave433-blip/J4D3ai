"""Google People / Contacts connector via the People API.

Scopes: contacts (read/write). Note: the People API exposes the user's own
contacts only — there is no global directory search, and we don't pretend
otherwise.
"""
from __future__ import annotations

from typing import Any, Dict

from core.connectors._google import GoogleConnector
from core.connectors.oauth import api_get, api_request

BASE = "https://people.googleapis.com/v1"
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations"


class GoogleContactsConnector(GoogleConnector):
    id = "gcontacts"
    display = "Google Contacts"
    description = "List, search and create Google contacts via the People API."
    scopes = ["https://www.googleapis.com/auth/contacts"]

    def actions(self) -> Dict[str, Dict[str, Any]]:
        tok = self.fresh_access_token()

        def _summarize(person: Dict[str, Any]) -> Dict[str, Any]:
            names = person.get("names", [])
            emails = [e.get("value", "") for e in person.get("emailAddresses", [])]
            phones = [p.get("value", "") for p in person.get("phoneNumbers", [])]
            return {"name": names[0].get("displayName", "") if names else "",
                    "emails": emails, "phones": phones,
                    "resource": person.get("resourceName", "")}

        def _list(limit: int = 50):
            res = api_get(f"{BASE}/people/me/connections", tok,
                          {"personFields": PERSON_FIELDS,
                           "pageSize": str(min(limit, 1000))})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "contacts": [
                _summarize(p) for p in res.get("connections", [])]}

        def _search(query: str, limit: int = 10):
            res = api_get(f"{BASE}/people:searchContacts", tok,
                          {"query": query, "pageSize": str(min(limit, 30)),
                           "readMask": PERSON_FIELDS})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "contacts": [
                _summarize(r.get("person", {}))
                for r in res.get("results", [])]}

        def _create(display_name: str, email: str = "", phone: str = ""):
            payload: Dict[str, Any] = {
                "names": [{"givenName": display_name}],
                "emailAddresses": ([{"value": email}] if email else []),
                "phoneNumbers": ([{"value": phone}] if phone else []),
            }
            res = api_request("POST", f"{BASE}/people:createContact", tok,
                              payload=payload)
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "contact": _summarize(res)}

        return {
            "list_contacts": {"description": "List your contacts (limit).",
                              "run": _list},
            "search_contacts": {"description": "Search your contacts (query).",
                                "run": _search},
            "create_contact": {"description": "Create a contact (display_name, [email], [phone]).",
                               "run": _create},
        }
