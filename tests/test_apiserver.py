"""Tests for core/apiserver.py — OpenAI-compatible chat-completions server.

The server runs on an ephemeral loopback port in a thread; the token comes
from the JADE_API_TOKEN env var so no token file is touched.
``core.apiserver.think_structured`` is monkeypatched — no real model calls.
"""
import http.client
import json
import os

import pytest

import core.apiserver as api_mod
from core.apiserver import start_api_background


@pytest.fixture()
def api_env(monkeypatch):
    monkeypatch.setenv("JADE_API_TOKEN", "test-token-123")
    yield


@pytest.fixture()
def server(api_env):
    info = start_api_background(port=0)
    assert info["server"] is not None, info.get("error")
    yield info
    info["stop"]()


def _req(server, method, path, body=None, token="test-token-123",
         raw_body=None):
    conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=10)
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if raw_body is not None:
        payload = raw_body
        headers["Content-Type"] = "application/json"
    elif body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    else:
        payload = None
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        parsed = json.loads(data) if data else None
    except json.JSONDecodeError:
        parsed = None
    return resp.status, parsed


def _stub_think(monkeypatch, result):
    def fake(context, task, model=None):
        fake.seen = {"context": context, "task": task, "model": model}
        return result
    monkeypatch.setattr(api_mod, "think_structured", fake)
    return fake


def test_models_list_shape(server):
    status, body = _req(server, "GET", "/v1/models")
    assert status == 200
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    for entry in body["data"]:
        assert set(entry) >= {"id", "object", "owned_by"}
        assert entry["object"] == "model"


def test_chat_completions_happy_path(server, monkeypatch):
    fake = _stub_think(monkeypatch, {"ok": True, "text": "stubbed reply",
                                    "provider": "stub"})
    status, body = _req(server, "POST", "/v1/chat/completions", {
        "model": "jade-default",
        "messages": [{"role": "user", "content": "hello"},
                     {"role": "assistant", "content": "hi"},
                     {"role": "user", "content": "again"}],
    })
    assert status == 200
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "jade-default"
    assert body["choices"][0]["message"] == {"role": "assistant",
                                            "content": "stubbed reply"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 0, "completion_tokens": 0,
                            "total_tokens": 0}
    # Prompt was assembled with ROLE: prefixes and reached the brain.
    assert "USER: hello" in fake.seen["task"]
    assert "ASSISTANT: hi" in fake.seen["task"]


def test_chat_missing_auth(server):
    status, _ = _req(server, "POST", "/v1/chat/completions",
                     {"model": "x", "messages": [{"role": "user",
                                                 "content": "hi"}]},
                     token=None)
    assert status == 401


def test_chat_wrong_auth(server):
    status, body = _req(server, "GET", "/v1/models", token="wrong-token")
    assert status == 401
    assert "error" in body


def test_chat_stream_rejected(server, monkeypatch):
    _stub_think(monkeypatch, {"ok": True, "text": "nope", "provider": "stub"})
    status, body = _req(server, "POST", "/v1/chat/completions", {
        "model": "jade-default", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert status == 400
    assert body["error"]["message"] == "streaming not supported"


def test_chat_malformed_json(server):
    status, body = _req(server, "POST", "/v1/chat/completions",
                        raw_body=b"{this is not json")
    assert status == 400
    assert "error" in body


def test_chat_empty_messages_rejected(server):
    status, body = _req(server, "POST", "/v1/chat/completions",
                        {"model": "jade-default", "messages": []})
    assert status == 400
    assert "error" in body


def test_chat_think_failure_is_502(server, monkeypatch):
    _stub_think(monkeypatch, {"ok": False, "error": "provider exploded"})
    status, body = _req(server, "POST", "/v1/chat/completions", {
        "model": "jade-default",
        "messages": [{"role": "user", "content": "hi"}]})
    assert status == 502
    assert "provider exploded" in body["error"]["message"]


def test_token_from_env_not_file(server):
    # Fixture sets JADE_API_TOKEN; the request already proved it works.
    assert server["token"] == "test-token-123"


# ---------------------------------------------------------------------------
# /v1/audio/* — OpenAI-compatible speech endpoints (no network, no real TTS)


class _FakeTTS:
    def __init__(self, voices=None, fail=None):
        self._voices = voices if voices is not None else [
            {"id": "v1", "name": "Voice One"}]
        self._fail = fail
        self.seen = {}

    def synthesize_to_file(self, text, path, profile=None):
        self.seen = {"text": text, "profile": profile}
        if self._fail:
            raise self._fail
        with open(path, "wb") as f:
            f.write(b"FAKEAUDIO:" + text.encode("utf-8"))
        return "audio/wav"

    def list_voices(self):
        if self._fail:
            raise self._fail
        return self._voices


def _stub_tts(monkeypatch, engine):
    import core.voice.tts as tts_mod
    monkeypatch.setattr(tts_mod, "default_tts", lambda: engine)
    return engine


def _req_raw(server, method, path, body=None, token="test-token-123"):
    conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=10)
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    payload = json.dumps(body).encode() if body is not None else None
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    ctype = resp.getheader("Content-Type")
    conn.close()
    return resp.status, ctype, data


def test_audio_speech_happy_path(server, monkeypatch):
    engine = _stub_tts(monkeypatch, _FakeTTS())
    status, ctype, data = _req_raw(server, "POST", "/v1/audio/speech",
                                   {"model": "tts-1", "input": "hello there",
                                    "voice": "alloy"})
    assert status == 200
    assert ctype == "audio/wav"
    assert data.startswith(b"FAKEAUDIO:hello there")
    assert engine.seen["text"] == "hello there"
    assert engine.seen["profile"].voice_id == "alloy"


def test_audio_speech_requires_input(server, monkeypatch):
    _stub_tts(monkeypatch, _FakeTTS())
    for bad in ({}, {"input": ""}, {"input": "   "}, {"input": 123}):
        status, _, data = _req_raw(server, "POST", "/v1/audio/speech", bad)
        assert status == 400, bad
        assert b"input" in data


def test_audio_speech_rejects_oversize_input(server, monkeypatch):
    _stub_tts(monkeypatch, _FakeTTS())
    status, _, _ = _req_raw(server, "POST", "/v1/audio/speech",
                            {"input": "x" * 5000})
    assert status == 400


def test_audio_speech_rejects_unsupported_format(server, monkeypatch):
    _stub_tts(monkeypatch, _FakeTTS())
    status, _, data = _req_raw(server, "POST", "/v1/audio/speech",
                               {"input": "hi", "response_format": "mp3"})
    assert status == 400
    assert b"mp3" in data


def test_audio_speech_engine_failure_is_502(server, monkeypatch):
    from core.voice.base import VoiceError
    _stub_tts(monkeypatch, _FakeTTS(fail=VoiceError("no synth here")))
    status, _, data = _req_raw(server, "POST", "/v1/audio/speech",
                               {"input": "hi"})
    assert status == 502
    assert b"no synth here" in data


def test_audio_speech_requires_auth(server, monkeypatch):
    _stub_tts(monkeypatch, _FakeTTS())
    status, _, _ = _req_raw(server, "POST", "/v1/audio/speech",
                            {"input": "hi"}, token=None)
    assert status == 401


def test_audio_voices_list(server, monkeypatch):
    _stub_tts(monkeypatch, _FakeTTS())
    status, body = _req(server, "GET", "/v1/audio/voices")
    assert status == 200
    assert body["object"] == "list"
    assert body["data"] == [{"id": "v1", "name": "Voice One"}]


def test_audio_voices_unavailable_is_502(server, monkeypatch):
    from core.voice.base import VoiceError
    _stub_tts(monkeypatch, _FakeTTS(fail=VoiceError("nothing installed")))
    status, body = _req(server, "GET", "/v1/audio/voices")
    assert status == 502
    assert "nothing installed" in body["error"]["message"]
