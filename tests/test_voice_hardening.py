"""Round: voice hardening — PowerShell quoting, engine fallback, barge-in grace.

No microphone, no speakers, no network. Windows-only code paths are tested
by calling the command builders directly (never executed).
"""
import math
import struct
import sys

import pytest

from core.voice.base import (
    AudioChunk, AudioCapture, STTEngine, TTSEngine, VoiceError, VoiceProfile,
)
from core.voice import tts as tts_mod
from core.voice import vad as vad_mod
from core.voice import conversation as conv_mod


def _loud_chunk(seconds=0.2, sample_rate=16000, amplitude=10000):
    n = int(seconds * sample_rate)
    pcm = b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 440 * i / sample_rate)))
        for i in range(n)
    )
    return AudioChunk(pcm=pcm, sample_rate=sample_rate)


class _ScriptCapture(AudioCapture):
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def list_devices(self):
        return [{"index": "0", "name": "stub mic"}]

    def record(self, seconds, device=None):
        return self._chunks[0]

    def stream(self, device=None):
        yield from self._chunks


class _ScriptSTT(STTEngine):
    def __init__(self, texts):
        self._texts = list(texts)

    def transcribe(self, chunk):
        return self._texts.pop(0)

    @property
    def offline(self):
        return True


class _StubHandle:
    def __init__(self):
        self.stopped = False
        self._done = False

    def stop(self):
        self.stopped = True
        self._done = True

    def wait(self, timeout=None):
        self._done = True

    @property
    def done(self):
        return self._done


class _ScriptTTS(TTSEngine):
    def __init__(self):
        self.handles = []

    def speak(self, text, profile=None):
        pass

    def speak_handle(self, text, profile=None):
        h = _StubHandle()
        self.handles.append(h)
        return h

    def list_voices(self):
        return []


class _AlwaysSpeechVAD(vad_mod.EnergyVAD):
    def is_speech(self, chunk):
        return True


# ---------------------------------------------------------------------------
# PowerShell quoting
# ---------------------------------------------------------------------------

def test_ps_quote_doubles_single_quotes():
    assert tts_mod._ps_quote("plain") == "'plain'"
    assert tts_mod._ps_quote("a'b") == "'a''b'"
    assert tts_mod._ps_quote("'; Evil; '") == "'''; Evil; '''"


def _speak_arg(ps: str) -> str:
    """Extract the $s.Speak('...') argument and un-escape it (round-trip)."""
    start = ps.index("$s.Speak('") + len("$s.Speak('")
    end = ps.rindex("');")
    return ps[start:end].replace("''", "'")


def test_windows_cmd_escapes_hostile_voice_id():
    tts = tts_mod.PlatformTTS()
    evil = "a'); Write-Host PWNED; ('b"
    cmd = tts._windows_cmd("hello", VoiceProfile(
        name="x", description="", engine="platform", voice_id=evil))
    ps = cmd[3]
    # The voice id must round-trip through quoting: PowerShell sees one
    # string literal, not a breakout.
    assert f"$s.SelectVoice('{evil.replace(chr(39), chr(39)*2)}');" in ps
    assert ps.count("SelectVoice(") == 1


def test_windows_cmd_escapes_hostile_text():
    tts = tts_mod.PlatformTTS()
    evil = "a'); Write-Host PWNED; ('b"
    cmd = tts._windows_cmd(evil, None)
    ps = cmd[3]
    assert _speak_arg(ps) == evil


def test_sapi_rate_clamped():
    assert tts_mod._sapi_rate(None) == ""
    assert tts_mod._sapi_rate(VoiceProfile("n", "d", "platform", rate=0)) == ""
    assert tts_mod._sapi_rate(VoiceProfile("n", "d", "platform", rate=400)) == "10"
    assert tts_mod._sapi_rate(VoiceProfile("n", "d", "platform", rate=-400)) == "-10"


# ---------------------------------------------------------------------------
# default_tts fallback
# ---------------------------------------------------------------------------

def test_default_tts_unavailable_carries_platform_reason(monkeypatch):
    def _boom(self, text, profile=None):
        raise VoiceError("no Linux speech synthesizer found (test)")

    monkeypatch.setattr(tts_mod.PlatformTTS, "_cmd", _boom)
    monkeypatch.setitem(sys.modules, "pyttsx3", None)
    import builtins
    real_import = builtins.__import__

    def _no_pyttsx3(name, *a, **k):
        if name == "pyttsx3":
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_pyttsx3)
    engine = tts_mod.default_tts()
    assert isinstance(engine, tts_mod.UnavailableTTS)
    assert "no Linux speech synthesizer" in engine.reason


def test_default_tts_prefers_platform_when_available(monkeypatch):
    monkeypatch.setattr(tts_mod.PlatformTTS, "_cmd",
                        lambda self, text, profile=None: ["true"])
    engine = tts_mod.default_tts()
    assert isinstance(engine, tts_mod.PlatformTTS)


# ---------------------------------------------------------------------------
# Barge-in grace window
# ---------------------------------------------------------------------------

def _convo(chunks, grace):
    return conv_mod.Conversation(
        capture=_ScriptCapture(chunks),
        stt=_ScriptSTT(["never mind"]),
        tts=_ScriptTTS(),
        vad=_AlwaysSpeechVAD(),
        barge_in_grace=grace,
    )


def test_barge_in_grace_suppresses_immediate_trigger():
    events = []
    convo = _convo([_loud_chunk()], grace=3600)
    convo.on_event = lambda k, m: events.append((k, m))
    result = convo._speak_with_barge_in("hello there")
    assert result is None, "grace window must suppress the instant trigger"
    assert not any(k == "barge_in" for k, _ in events)


def test_barge_in_fires_after_grace(monkeypatch):
    events = []
    convo = _convo([_loud_chunk()], grace=0.4)
    convo.on_event = lambda k, m: events.append((k, m))
    # First monotonic() call (speech start) returns t0; later calls return
    # t0 + 10s so the trigger lands well past the grace window.
    import time as time_mod
    t0 = time_mod.monotonic()
    calls = iter([t0, t0 + 10.0, t0 + 10.0, t0 + 10.0])
    monkeypatch.setattr(time_mod, "monotonic", lambda: next(calls, t0 + 10.0))
    result = convo._speak_with_barge_in("hello there")
    assert result == "never mind"
    assert any(k == "barge_in" for k, _ in events)


def test_barge_in_grace_clamped_at_zero():
    convo = _convo([], grace=-5)
    assert convo.barge_in_grace == 0.0
