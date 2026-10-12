"""Google Calendar connector: list upcoming events (read-only)."""
from __future__ import annotations

import datetime
from typing import Any, Dict

from core.connectors._google import GoogleConnector
from core.connectors.oauth import api_get


class GoogleCalendarConnector(GoogleConnector):
    id = "gcal"
    display = "Google Calendar"
    description = "List your upcoming Google Calendar events (read-only)."
    scopes = ["https://www.googleapis.com/auth/calendar.readonly"]

    def actions(self) -> Dict[str, Dict[str, Any]]:
        tok = self.fresh_access_token()

        def _upcoming(days: int = 7, limit: int = 20):
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            end = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(days=days)).isoformat()
            res = api_get("https://www.googleapis.com/calendar/v3/calendars/primary/events",
                          tok, {"timeMin": now, "timeMax": end,
                                "maxResults": str(min(limit, 100)),
                                "singleEvents": "true", "orderBy": "startTime"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            out = []
            for e in res.get("items", []):
                start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "")
                out.append({"summary": e.get("summary", "(no title)"),
                            "start": start,
                            "location": e.get("location", ""),
                            "link": e.get("htmlLink", "")})
            return {"ok": True, "events": out}

        return {
            "upcoming": {"description": "List upcoming events (days, limit).",
                         "run": _upcoming},
        }
