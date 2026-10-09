"""Microsoft Teams connector via Microsoft Graph.

Delegated scopes: Team.ReadBasic.All, Channel.ReadBasic.All,
ChannelMessage.Read.All (needs admin consent on work/school tenants),
ChannelMessage.Send.

Admin-consent note: on organizational tenants, reading channel messages
(``ChannelMessage.Read.All``) requires an admin to grant consent in the
Azure portal. ``list_teams``/``list_channels`` work with plain user
consent. If an action returns 403, that is the likely cause — the
connector reports the Graph error honestly instead of guessing.
"""
from __future__ import annotations

from typing import Any, Dict

from core.connectors.base import Connector
from core.connectors.graph import GraphConnectorBase


class TeamsConnector(GraphConnectorBase, Connector):
    id = "teams"
    display = "Microsoft Teams"
    description = ("List joined teams/channels, read recent channel messages, "
                   "send channel messages via Microsoft Graph.")
    auth_kind = "oauth2"
    scopes = ["Team.ReadBasic.All", "Channel.ReadBasic.All",
              "ChannelMessage.Read.All", "ChannelMessage.Send"]

    def actions(self) -> Dict[str, Dict[str, Any]]:
        client = self.graph()

        def _list_teams():
            res = client.get_paged("/me/joinedTeams",
                                   {"$select": "id,displayName,description"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "teams": [
                {"id": t.get("id"), "name": t.get("displayName", ""),
                 "description": t.get("description", "")}
                for t in res["items"]]}

        def _list_channels(team_id: str):
            res = client.get(f"/teams/{team_id}/channels",
                             {"$select": "id,displayName,description"})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "channels": [
                {"id": c.get("id"), "name": c.get("displayName", ""),
                 "description": c.get("description", "")}
                for c in res.get("value", [])]}

        def _read_messages(team_id: str, channel_id: str, limit: int = 20):
            res = client.get(
                f"/teams/{team_id}/channels/{channel_id}/messages",
                {"$top": str(min(limit, 50)),
                 "$select": "id,from,body,createdDateTime",
                 "$orderby": "createdDateTime desc"})
            if "error" in res:
                err = res["error"]
                if "403" in err:
                    err += (" This usually means ChannelMessage.Read.All still "
                            "needs admin consent on your tenant.")
                return {"ok": False, "error": err}
            out = []
            for m in res.get("value", []):
                frm = ((m.get("from") or {}).get("user") or {}).get("displayName", "")
                out.append({"id": m.get("id"), "from": frm,
                            "created": m.get("createdDateTime", ""),
                            "body": (m.get("body") or {}).get("content", "")[:4000]})
            return {"ok": True, "messages": out}

        def _send_message(team_id: str, channel_id: str, text: str):
            res = client.request(
                "POST", f"/teams/{team_id}/channels/{channel_id}/messages",
                payload={"body": {"contentType": "text", "content": text}})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            return {"ok": True, "id": res.get("id")}

        return {
            "list_teams": {"description": "List teams you have joined.",
                           "run": _list_teams},
            "list_channels": {"description": "List channels in a team (team_id).",
                              "run": _list_channels},
            "read_messages": {"description": "Read recent channel messages (team_id, channel_id). May need admin consent.",
                              "run": _read_messages},
            "send_message": {"description": "Post a message to a channel (team_id, channel_id, text).",
                             "run": _send_message},
        }
