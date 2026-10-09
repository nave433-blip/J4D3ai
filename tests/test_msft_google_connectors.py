"""Tests for the Microsoft + Google connector expansion.

All network access is mocked — these tests verify request construction,
response parsing, paging, backoff/refresh logic and honest failure modes.
Nothing here touches the real Graph/Google APIs.
"""
import json
import sys

import pytest

from core.connectors import (
    get_connector, list_connectors,
    GoogleDriveConnector, GmailConnector, GoogleCalendarConnector,
    GoogleTasksConnector, GoogleContactsConnector, GoogleDocsConnector,
    YouTubeConnector, OutlookConnector, OneDriveConnector, TeamsConnector,
    OneNoteConnector, AzureConnector,
)
from core.connectors import graph as gmod
from core.connectors import oauth as omod


# ---------------------------------------------------------------- registry
def test_registry_has_all_new_connectors():
    ids = {c.id for c in list_connectors()}
    for expected in ("onedrive", "teams", "onenote", "azure",
                     "gtasks", "gcontacts", "gdocs", "youtube",
                     "outlook", "gmail", "gdrive", "gcal"):
        assert expected in ids, f"missing connector {expected}"


def test_registry_ids_unique_and_actions_dicts():
    ids = [c.id for c in list_connectors()]
    assert len(ids) == len(set(ids))
    for c in list_connectors():
        assert isinstance(c.actions(), dict)


def test_new_connectors_not_connected_without_tokens():
    for cid in ("onedrive", "teams", "onenote", "azure",
                "gtasks", "gcontacts", "gdocs", "youtube"):
        c = get_connector(cid)
        st = c.status()
        assert st["connected"] is False
        assert st["needs"], f"{cid} should explain what it needs"


# ---------------------------------------------------------------- graph client
class _Resp:
    def __init__(self, status, body, headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_http_factory(script):
    """script: list of (status, headers, body-bytes); records calls."""
    calls = []

    def _fake(method, url, access_token=None, payload=None, params=None,
              content_type="application/json", timeout=30):
        calls.append({"method": method, "url": url, "payload": payload,
                      "params": params})
        status, headers, body = script.pop(0)
        return status, headers, body

    _fake.calls = calls
    return _fake


def test_safe_compare():
    assert gmod.safe_compare("abc", "abc") is True
    assert gmod.safe_compare("abc", "abd") is False


def test_device_code_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(gmod, "_TOKEN_FILE", str(tmp_path / "t.json"))
    dc = {"device_code": "DC", "user_code": "UC",
          "verification_uri": "https://microsoft.com/devicelogin",
          "interval": 0, "expires_in": 900}
    pending = (400, {}, json.dumps({"error": "authorization_pending"}).encode())
    token = {"access_token": "AT", "refresh_token": "RT",
             "expires_in": 3600, "scope": "Mail.Read"}
    fake = _fake_http_factory([
        (200, {}, json.dumps(dc).encode()),
        pending,
        (200, {}, json.dumps(token).encode()),
    ])
    monkeypatch.setattr(gmod, "_http", fake)
    monkeypatch.setattr(gmod.time, "sleep", lambda s: None)
    out = gmod.device_code_flow("cid", ["Mail.Read"])
    assert out["access_token"] == "AT"
    assert "obtained_at" in out
    assert fake.calls[0]["url"] == gmod.MS_DEVICE_CODE_URL
    assert fake.calls[2]["payload"]["grant_type"].startswith(
        "urn:ietf:params:oauth:grant-type:device_code")


def test_refresh_token(monkeypatch):
    new = {"access_token": "AT2", "expires_in": 3600}
    fake = _fake_http_factory([(200, {}, json.dumps(new).encode())])
    monkeypatch.setattr(gmod, "_http", fake)
    out = gmod.refresh_ms_token({"refresh_token": "RT", "scope": "s"}, "cid")
    assert out["access_token"] == "AT2"
    assert out["refresh_token"] == "RT"  # preserved when omitted


def test_graph_paging_and_429_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr(gmod.time, "sleep", lambda s: sleeps.append(s))
    page1 = {"value": [{"id": "1"}],
             "@odata.nextLink": "https://graph.microsoft.com/v1.0/x?$skip=1"}
    page2 = {"value": [{"id": "2"}]}
    fake = _fake_http_factory([
        (429, {"Retry-After": "1"}, b"throttled"),
        (200, {}, json.dumps(page1).encode()),
        (200, {}, json.dumps(page2).encode()),
    ])
    monkeypatch.setattr(gmod, "_http", fake)
    client = gmod.GraphClient({"access_token": "AT", "expires_in": 99999,
                               "obtained_at": gmod.time.time()}, "cid")
    out = client.get_paged("/me/messages")
    assert out["ok"] is True
    assert [i["id"] for i in out["items"]] == ["1", "2"]
    assert sleeps and sleeps[0] == 1  # honored Retry-After


def test_graph_401_triggers_refresh(monkeypatch):
    new_tok = {"access_token": "FRESH", "refresh_token": "RT",
               "expires_in": 3600, "obtained_at": gmod.time.time()}
    fake = _fake_http_factory([
        (401, {}, b"unauthorized"),
        (200, {}, json.dumps({"value": []}).encode()),
    ])
    monkeypatch.setattr(gmod, "_http", fake)
    monkeypatch.setattr(gmod, "refresh_ms_token", lambda t, c: new_tok)
    saved = {}
    client = gmod.GraphClient({"access_token": "OLD", "expires_in": 99999,
                               "obtained_at": gmod.time.time()}, "cid",
                              on_token=saved.update)
    out = client.get("/me")
    assert out == {"value": []}
    assert saved["access_token"] == "FRESH"


def test_token_file_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(gmod, "_TOKEN_FILE", str(tmp_path / "tok.json"))
    gmod.save_ms_token({"access_token": "X"})
    assert gmod.load_ms_token()["access_token"] == "X"
    gmod.clear_ms_token()
    assert gmod.load_ms_token() is None


# ---------------------------------------------------------------- fake graph
class FakeGraph:
    def __init__(self, routes):
        self.routes = routes  # (method, path-prefix) -> response
        self.calls = []

    def _route(self, method, path, **kw):
        self.calls.append({"method": method, "path": path, **kw})
        for (m, prefix), resp in self.routes.items():
            if m == method and path.startswith(prefix):
                return resp() if callable(resp) else resp
        return {"error": f"no mock route for {method} {path}"}

    def get(self, path, params=None):
        return self._route("GET", path, params=params)

    def request(self, method, path, params=None, payload=None, raw=False):
        return self._route(method, path, params=params, payload=payload,
                           raw=raw)

    def get_paged(self, path, params=None, limit=200):
        res = self.get(path, params)
        if "error" in res:
            return {"error": res["error"], "items": []}
        return {"ok": True, "items": res.get("value", [])}

    def _auth(self):
        return "FAKE"


def _ms_connector(cid, monkeypatch, routes):
    c = get_connector(cid)
    monkeypatch.setattr(gmod, "load_ms_token",
                        lambda: {"access_token": "AT",
                                 "scope": "openid profile email offline_access " +
                                          " ".join(c.scopes)})
    fake = FakeGraph(routes)
    monkeypatch.setattr(c, "graph", lambda: fake)
    return c, fake


# ---------------------------------------------------------------- outlook
def test_outlook_send_mail_constructs_payload(monkeypatch):
    c, fake = _ms_connector("outlook", monkeypatch, {
        ("POST", "/me/sendMail"): {"ok": True},
    })
    res = c.run_action("send_mail", to="a@x.com, b@x.com",
                       subject="Hi", body="hello")
    assert res["ok"] is True
    call = fake.calls[0]
    assert call["path"] == "/me/sendMail"
    recips = call["payload"]["message"]["toRecipients"]
    assert [r["emailAddress"]["address"] for r in recips] == ["a@x.com", "b@x.com"]


def test_outlook_todo_crud(monkeypatch):
    c, fake = _ms_connector("outlook", monkeypatch, {
        ("GET", "/me/todo/lists"): {"value": [{"id": "L1", "displayName": "Tasks"}]},
        ("POST", "/me/todo/lists/L1/tasks"): {"id": "T1", "title": "Buy milk"},
        ("PATCH", "/me/todo/lists/L1/tasks/T1"): {"ok": True},
    })
    assert c.run_action("todo_lists")["lists"][0]["id"] == "L1"
    added = c.run_action("todo_add", list_id="L1", title="Buy milk")
    assert added["id"] == "T1"
    done = c.run_action("todo_complete", list_id="L1", task_id="T1")
    assert done["completed"] == "T1"


def test_outlook_calendar_create(monkeypatch):
    c, fake = _ms_connector("outlook", monkeypatch, {
        ("POST", "/me/events"): {"id": "E1", "webLink": "https://x"},
    })
    res = c.run_action("create_event", subject="Standup",
                       start="2026-10-01T09:00:00", end="2026-10-01T09:30:00")
    assert res["id"] == "E1"
    payload = fake.calls[0]["payload"]
    assert payload["start"]["dateTime"] == "2026-10-01T09:00:00"


# ---------------------------------------------------------------- teams / onenote / onedrive
def test_teams_list_and_send(monkeypatch):
    c, fake = _ms_connector("teams", monkeypatch, {
        ("GET", "/me/joinedTeams"): {"value": [{"id": "T1", "displayName": "Eng"}]},
        ("GET", "/teams/T1/channels"): {"value": [{"id": "C1", "displayName": "gen"}]},
        ("POST", "/teams/T1/channels/C1/messages"): {"id": "M1"},
    })
    assert c.run_action("list_teams")["teams"][0]["name"] == "Eng"
    sent = c.run_action("send_message", team_id="T1", channel_id="C1",
                        text="hello team")
    assert sent["id"] == "M1"
    assert fake.calls[-1]["payload"]["body"]["content"] == "hello team"


def test_teams_read_403_mentions_admin_consent(monkeypatch):
    c, fake = _ms_connector("teams", monkeypatch, {
        ("GET", "/teams/T1/channels/C1/messages"):
            {"error": "Graph HTTP 403: Access denied"},
    })
    res = c.run_action("read_messages", team_id="T1", channel_id="C1")
    assert res["ok"] is False
    assert "admin consent" in res["error"]


def test_onenote_lists_and_create(monkeypatch):
    c, fake = _ms_connector("onenote", monkeypatch, {
        ("GET", "/me/onenote/notebooks"):
            {"value": [{"id": "N1", "displayName": "Work"}]},
    })
    res = c.run_action("list_notebooks")
    assert res["notebooks"][0]["name"] == "Work"
    assert "create_page" in c.actions()


def test_onedrive_path_confinement():
    from core.connectors.onedrive import _is_path_confined
    assert _is_path_confined("/etc/passwd") is None
    assert _is_path_confined("../../etc/passwd") is None
    ok = _is_path_confined("some/sub/dir/file.txt")
    assert ok is not None and str(ok).endswith("some/sub/dir/file.txt")


def test_onedrive_delta_parses_token(monkeypatch):
    c, fake = _ms_connector("onedrive", monkeypatch, {
        ("GET", "/me/drive/root/delta"): {
            "value": [{"name": "a.txt"}],
            "@odata.deltaLink": "https://x/delta?token=ABC123"},
    })
    res = c.run_action("delta")
    assert res["ok"] is True
    assert res["delta_token"] == "ABC123"


# ---------------------------------------------------------------- azure
def test_azure_is_read_only():
    c = get_connector("azure")
    for name in c.actions():
        assert name.startswith("list_"), f"non-read action exposed: {name}"


def test_azure_needs_message_lists_missing(monkeypatch):
    c = get_connector("azure")
    monkeypatch.setattr(c, "_tenant_id", lambda: "")
    monkeypatch.setattr(c, "_client_id", lambda: "")
    monkeypatch.setattr(c, "_client_secret", lambda: "")
    msg = c._needs_message()
    assert "azure_tenant_id" in msg and "azure_client_id" in msg


# ---------------------------------------------------------------- google
def _google_connector(cid, monkeypatch):
    c = get_connector(cid)
    monkeypatch.setattr(c, "get_token", lambda: {"access_token": "AT"})
    return c


def test_google_tasks_crud(monkeypatch):
    import core.connectors.google_tasks as gt
    c = _google_connector("gtasks", monkeypatch)
    monkeypatch.setattr(gt, "api_get",
                        lambda url, tok, params=None: {"items": [
                            {"id": "L1", "title": "My Tasks"}]})
    calls = []
    monkeypatch.setattr(
        gt, "api_request",
        lambda m, url, tok, payload=None, params=None: (
            calls.append({"method": m, "url": url, "payload": payload}),
            {"id": "T9", "title": payload["title"]})[1])
    assert c.run_action("list_tasklists")["lists"][0]["id"] == "L1"
    added = c.run_action("add_task", tasklist="@default", title="Call mom")
    assert added["id"] == "T9"
    assert calls[0]["method"] == "POST"
    assert calls[0]["payload"]["title"] == "Call mom"


def test_google_contacts_search_and_create(monkeypatch):
    import core.connectors.google_contacts as gc
    c = _google_connector("gcontacts", monkeypatch)
    monkeypatch.setattr(
        gc, "api_get",
        lambda url, tok, params=None: {"results": [
            {"person": {"resourceName": "people/1",
                        "names": [{"displayName": "Ada"}],
                        "emailAddresses": [{"value": "ada@x.com"}]}}]})
    captured = {}
    monkeypatch.setattr(
        gc, "api_request",
        lambda m, url, tok, payload=None, params=None: (
            captured.update(payload),
            {"resourceName": "people/2",
             "names": [{"displayName": "Bob"}]})[1])
    res = c.run_action("search_contacts", query="ada")
    assert res["contacts"][0]["name"] == "Ada"
    created = c.run_action("create_contact", display_name="Bob", email="bob@x.com")
    assert created["contact"]["name"] == "Bob"
    assert captured["emailAddresses"][0]["value"] == "bob@x.com"


def test_google_docs_text_extraction():
    import core.connectors.google_docs as gd
    doc = {"title": "T", "body": {"content": [
        {"paragraph": {"elements": [
            {"textRun": {"content": "Hello "}},
            {"textRun": {"content": "world"}}]}},
        {"table": {}},  # non-paragraph elements are skipped, not crashed on
    ]}}
    assert gd._doc_text(doc) == "Hello world"


def test_google_docs_append_finds_end_index(monkeypatch):
    import core.connectors.google_docs as gd
    c = _google_connector("gdocs", monkeypatch)
    monkeypatch.setattr(
        gd, "api_get",
        lambda url, tok, params=None: {"body": {"content": [{"endIndex": 42}]}})
    captured = {}
    monkeypatch.setattr(
        gd, "api_request",
        lambda m, url, tok, payload=None, params=None: (
            captured.update(payload), {"ok": True})[1])
    res = c.run_action("append_doc", document_id="D1", text="more")
    assert res["appended_chars"] == 4
    loc = captured["requests"][0]["insertText"]["location"]
    assert loc["index"] == 41  # endIndex - 1


def test_youtube_search_quota_note(monkeypatch):
    import core.connectors.youtube as yt
    c = _google_connector("youtube", monkeypatch)
    monkeypatch.setattr(
        yt, "api_get",
        lambda url, tok, params=None: {"items": [
            {"id": {"videoId": "V1"},
             "snippet": {"title": "T", "channelTitle": "C",
                         "publishedAt": "2026-01-01"}}]})
    res = c.run_action("search", query="python")
    assert res["videos"][0]["video_id"] == "V1"
    assert "quota" in res["note"]


# ---------------------------------------------------------------- oauth helper
def test_api_request_posts_json(monkeypatch):
    seen = {}

    class _R(_Resp):
        pass

    def _urlopen(req, timeout=30):
        seen["method"] = req.get_method()
        seen["data"] = json.loads(req.data.decode())
        seen["auth"] = req.get_header("Authorization")
        return _R(200, json.dumps({"ok": True}).encode())

    monkeypatch.setattr(omod.urllib.request, "urlopen", _urlopen)
    out = omod.api_request("POST", "https://x.test/things", "TOK",
                           payload={"a": 1})
    assert out == {"ok": True}
    assert seen["method"] == "POST"
    assert seen["data"] == {"a": 1}
    assert seen["auth"] == "Bearer TOK"


# ---------------------------------------------------------------- windows integration
def test_windows_integration_not_windows():
    import core.windows_integration as wi
    if sys.platform == "win32":
        pytest.skip("Windows-only assertions inverted on Windows")
    for fn, args in ((wi.toast, ("a", "b")), (wi.startup_status, ()),
                     (wi.windows_system_summary, ())):
        out = fn(*args)
        assert out["ok"] is False and "Windows only" in out["error"]
    assert wi.set_startup(True)["ok"] is False
    assert wi.default_protocol_handler("http")["ok"] is False


def test_windows_integration_toast_ps_escaping():
    import core.windows_integration as wi
    script = wi._toast_ps_script("it's", "a 'test'", "app")
    assert "it''s" in script and "a ''test''" in script


def test_windows_integration_driver_refusal():
    import core.windows_integration as wi
    out = wi.driver_info()
    assert out["ok"] is False
    assert "cannot install" in out["error"] or "not possible" in out["error"]
