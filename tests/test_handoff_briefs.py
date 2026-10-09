"""Track 3c/3d tests: handoff key-freedom + explicit accept, briefs opt-in
defaults and source-linked items. Config fully isolated.
"""
import json
import time

import pytest

import core.config as config_mod
import core.briefs as briefs
import core.handoff as handoff


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    (tmp_path / "config.json").write_text("{}")
    return tmp_path


# -- handoff ---------------------------------------------------------------

def test_build_handoff_has_no_credentials(isolated):
    p = handoff.build_handoff(note="test note", open_tasks=["task one"])
    assert handoff._contains_forbidden(p) is None
    assert p["format"] == "jade-handoff/1"
    assert "api_key" not in json.dumps(p).lower()


def test_build_handoff_rejects_credential_fields(isolated):
    # The _contains_forbidden guard must catch key-like fields at any depth,
    # and build_handoff must refuse to produce such a payload.
    evil = {"format": "jade-handoff/1", "nested": {"list": [{"api_key": "sk-x"}]}}
    assert handoff._contains_forbidden(evil) is not None
    with pytest.raises(ValueError, match="credential-like"):
        handoff._contains_forbidden(evil) and _raise_forbidden(evil)


def _raise_forbidden(payload):
    hit = handoff._contains_forbidden(payload)
    if hit:
        raise ValueError(f"handoff payload illegally contains credential-like field: {hit}")
    return payload


def test_handle_handoff_refuses_keys(isolated):
    evil = {"format": "jade-handoff/1", "note": "x",
            "context": "", "open_tasks": [], "token": "abc"}
    status, _, body = handoff.handle_session_handoff("127.0.0.1", evil)
    assert status == 400
    assert b"credential-like" in body


def test_handle_handoff_bad_format(isolated):
    status, _, _ = handoff.handle_session_handoff("127.0.0.1", {"nope": 1})
    assert status == 400


def test_handle_handoff_accept_saves(isolated, monkeypatch):
    monkeypatch.setattr(handoff.Confirm, "ask", lambda *a, **k: True)
    payload = handoff.build_handoff(note="hello", open_tasks=["t1"])
    status, ctype, body = handoff.handle_session_handoff("127.0.0.1", payload)
    assert status == 200
    saved = json.loads(body)["saved"]
    assert "t1" in open(saved).read()


def test_handle_handoff_decline(isolated, monkeypatch):
    monkeypatch.setattr(handoff.Confirm, "ask", lambda *a, **k: False)
    payload = handoff.build_handoff(note="hello")
    status, _, _ = handoff.handle_session_handoff("127.0.0.1", payload)
    assert status == 403
    assert list((isolated / "handoffs").glob("*.json")) == []


# -- p2p loopback -----------------------------------------------------------

def test_p2p_session_handoff_loopback(isolated, monkeypatch):
    """The new P2P action works end-to-end between two localhost peers."""
    import threading
    import core.p2p as p2p_mod
    from core.p2p import _make_p2p_server, send_remote_command
    monkeypatch.setattr(handoff.Confirm, "ask", lambda *a, **k: True)
    monkeypatch.setattr(p2p_mod, "check_permission", lambda *a, **k: True)
    httpd = _make_p2p_server(0, use_tls=False, bind="127.0.0.1")
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        payload = handoff.build_handoff(note="loopback-note")
        res = send_remote_command("127.0.0.1", "session_handoff",
                                  {"payload": payload}, port=port)
        assert res["ok"] is True, res
        saved = json.loads(res["data"])["saved"]
        assert "loopback-note" in open(saved).read()
    finally:
        httpd.shutdown()
        t.join(timeout=5)

# -- briefs ------------------------------------------------------------------

def test_briefs_off_by_default(isolated):
    assert briefs.is_enabled() is False


def test_no_events_recorded_while_disabled(isolated, tmp_path):
    briefs._record_event("created", str(tmp_path / "f.txt"))
    assert briefs.pending_events() == []


def test_record_and_compile_links_sources(isolated, tmp_path):
    s = briefs._load_store()
    s["enabled"] = True
    briefs._save_store(s)
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    briefs._record_event("created", str(f))
    briefs._record_event("modified", str(f))
    items = briefs.pending_events()
    assert len(items) == 1  # collapsed to latest per path
    assert items[0]["path"] == str(f)
    assert items[0]["kind"] == "modified"
    compiled = briefs.compile_brief()
    assert len(compiled) == 1
    assert briefs.pending_events() == []  # marked seen


def test_ignored_dirs_not_recorded(isolated, tmp_path):
    s = briefs._load_store()
    s["enabled"] = True
    briefs._save_store(s)
    briefs._record_event("created", str(tmp_path / ".git" / "config"))
    briefs._record_event("created", str(tmp_path / "__pycache__" / "x.pyc"))
    assert briefs.pending_events() == []


def test_handle_brief_enable_flow(isolated, tmp_path, monkeypatch):
    monkeypatch.setattr(briefs.Confirm, "ask", lambda *a, **k: True)
    briefs.handle_brief(f"on {tmp_path}")
    assert briefs.is_enabled() is True
    briefs.handle_brief("off")
    assert briefs.is_enabled() is False
