"""YouTube connector (READ-ONLY) via YouTube Data API v3.

Scope: youtube.readonly. Quota reality: every Cloud project gets 10,000
quota units/day by default and ``search.list`` costs 100 units per call —
so ~100 searches/day on the free quota. Reads via channels/playlists cost
1–3 units. The connector surfaces quota errors honestly instead of
pretending they didn't happen.
"""
from __future__ import annotations

from typing import Any, Dict

from core.connectors._google import GoogleConnector
from core.connectors.oauth import api_get

BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeConnector(GoogleConnector):
    id = "youtube"
    display = "YouTube"
    description = "Search YouTube, list channel uploads, video details (read-only)."
    scopes = ["https://www.googleapis.com/auth/youtube.readonly"]

    def actions(self) -> Dict[str, Dict[str, Any]]:
        tok = self.fresh_access_token()

        def _search(query: str, limit: int = 10):
            res = api_get(f"{BASE}/search", tok,
                          {"part": "snippet", "q": query, "type": "video",
                           "maxResults": str(min(limit, 50))})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            out = []
            for it in res.get("items", []):
                vid = (it.get("id") or {}).get("videoId", "")
                sn = it.get("snippet", {})
                out.append({"video_id": vid, "title": sn.get("title", ""),
                            "channel": sn.get("channelTitle", ""),
                            "published": sn.get("publishedAt", "")})
            return {"ok": True, "videos": out,
                    "note": "search.list costs 100 quota units per call."}

        def _channel_uploads(channel_id: str, limit: int = 20):
            ch = api_get(f"{BASE}/channels", tok,
                         {"part": "contentDetails", "id": channel_id})
            if "error" in ch:
                return {"ok": False, "error": ch["error"]}
            items = ch.get("items", [])
            if not items:
                return {"ok": False, "error": "Channel not found."}
            uploads = ((items[0].get("contentDetails") or {})
                       .get("relatedPlaylists") or {}).get("uploads", "")
            if not uploads:
                return {"ok": False, "error": "No uploads playlist found."}
            pl = api_get(f"{BASE}/playlistItems", tok,
                         {"part": "snippet,contentDetails",
                          "playlistId": uploads,
                          "maxResults": str(min(limit, 50))})
            if "error" in pl:
                return {"ok": False, "error": pl["error"]}
            return {"ok": True, "videos": [
                {"video_id": (it.get("contentDetails") or {}).get("videoId", ""),
                 "title": (it.get("snippet") or {}).get("title", ""),
                 "published": (it.get("snippet") or {}).get("publishedAt", "")}
                for it in pl.get("items", [])]}

        def _video_details(video_id: str):
            res = api_get(f"{BASE}/videos", tok,
                          {"part": "snippet,statistics,contentDetails",
                           "id": video_id})
            if "error" in res:
                return {"ok": False, "error": res["error"]}
            items = res.get("items", [])
            if not items:
                return {"ok": False, "error": "Video not found."}
            v = items[0]
            sn, st = v.get("snippet", {}), v.get("statistics", {})
            return {"ok": True, "title": sn.get("title", ""),
                    "channel": sn.get("channelTitle", ""),
                    "published": sn.get("publishedAt", ""),
                    "duration": (v.get("contentDetails") or {}).get("duration", ""),
                    "views": st.get("viewCount", ""),
                    "likes": st.get("likeCount", ""),
                    "description": (sn.get("description") or "")[:2000]}

        return {
            "search": {"description": "Search videos (query). Costs 100 quota units.",
                       "run": _search},
            "channel_uploads": {"description": "List a channel's uploads (channel_id).",
                                "run": _channel_uploads},
            "video_details": {"description": "Get video details (video_id).",
                              "run": _video_details},
        }
