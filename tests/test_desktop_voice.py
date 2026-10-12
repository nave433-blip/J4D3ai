"""Round B tests: desktop GUI, overlay, tray, wake-word, voice plugin
architecture, personalities, voice profiles, and logo wiring.

All audio backends are stubbed — no microphone, no speakers, no network.
Optional GUI deps (pywebview, pystray, keyboard, openwakeword, sounddevice)
are NOT required; absence must degrade honestly.
"""
import io
import json
import math
import shutil
import struct
import sys
import wave

import pytest

from core.voice.base import (
    AudioChunk, AudioCapture, STTEngine, TTSEngine, VoiceError, VoiceProfile,
    WakeWordEngine, register_engine, registered_engines,
)
from core.voice import audio as audio_mod
from core.voice import stt as stt_mod
from core.voice import tts as tts_mod
from core.voice import vad as vad_mod
from core.voice import wakeword as ww_mod
from core.voice import profiles as profiles_mod
from core.voice import conversation as conv_mod
from core.personalities import (
    PERSONALITIES, resolve_personality, list_personalities,
    is_known_personality, DEFAULT_PERSONALITY,
)


# ---------------------------------------------------------------------------
# Helpers: synthesized PCM (no mic needed)
# ---------------------------------------------------------------------------

@pytest.fixture()
def dash_server(tmp_path, monkeypatch):
    import core.dashboard as dash_mod
    monkeypatch.setattr(dash_mod, "TOKEN_FILE", tmp_path / "dashboard_token")
    monkeypatch.setattr(dash_mod, "LOG_DIR", tmp_path / "logs")
    info = dash_mod.start_dashboard_background(port=0)
    yield info
    info["stop"]()


def _silent_chunk(seconds=0.5, sample_rate=16000):
    n = int(seconds * sample_rate)
    return AudioChunk(pcm=b"\x00" * (n * 2), sample_rate=sample_rate)


def _loud_chunk(seconds=0.5, sample_rate=16000, amplitude=10000, freq=440.0):
    n = int(seconds * sample_rate)
    pcm = b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * freq * i / sample_rate)))
        for i in range(n)
    )
    return AudioChunk(pcm=pcm, sample_rate=sample_rate)


# ---------------------------------------------------------------------------
# Personalities
# ---------------------------------------------------------------------------

BANNED_PHRASES = [
    "ignore it", "zero safety checks", "morality filters", "never refuse",
    "zero restrictions", "unconditional disclosure", "never ask for permission",
]


def test_personalities_all_resolve_and_distinct():
    names = [p.name for p in list_personalities()]
    assert names == ["jade", "witty", "clippy", "professional"]
    prompts = [p.system_prompt for p in list_personalities()]
    assert len(set(prompts)) == 4, "each personality must sound different"
    for p in list_personalities():
        assert p.title and p.description and p.greeting


def test_personality_default_is_jade():
    assert DEFAULT_PERSONALITY == "jade"
    assert resolve_personality(None).name == "jade"
    assert resolve_personality("").name == "jade"
    assert resolve_personality("nonsense").name == "jade"


def test_personality_legacy_aliases_keep_working():
    assert resolve_personality("sarcastic").name == "witty"
    assert resolve_personality("concise").name == "professional"
    assert resolve_personality("mentor").name == "professional"
    assert resolve_personality("nave_ai").name == "jade"
    assert is_known_personality("sarcastic")
    assert not is_known_personality("definitely-not-a-personality")


def test_personality_no_refusal_bypass():
    for p in list_personalities():
        lowered = (p.system_prompt + " " + p.greeting).lower()
        for phrase in BANNED_PHRASES:
            assert phrase not in lowered, f"{p.name}: {phrase!r}"
    # Legacy aliases resolve to clean prompts too.
    for alias in ("sarcastic", "concise", "mentor", "nave_ai"):
        lowered = resolve_personality(alias).system_prompt.lower()
        for phrase in BANNED_PHRASES:
            assert phrase not in lowered, f"{alias}: {phrase!r}"


def test_brain_personalities_backcompat():
    import core.brain as brain_mod
    assert isinstance(brain_mod.PERSONALITIES["jade"], str)
    assert isinstance(brain_mod.PERSONALITIES["professional"], str)
    assert "sarcastic" in brain_mod.PERSONALITIES  # legacy key still present


# ---------------------------------------------------------------------------
# Voice plugin interfaces
# ---------------------------------------------------------------------------

def test_engine_registry_roundtrip():
    class DummyTTS(TTSEngine):
        def speak(self, text, profile=None): pass
        def speak_handle(self, text, profile=None): pass
        def list_voices(self): return []

    register_engine("tts", "dummy-test", DummyTTS)
    try:
        assert registered_engines("tts")["dummy-test"] is DummyTTS
    finally:
        del registered_engines("tts")["dummy-test"]  # local copy; registry intact
    with pytest.raises(ValueError):
        register_engine("nope", "x", DummyTTS)


def test_unavailable_engines_are_honest():
    cap = audio_mod.UnavailableCapture("no mic lib")
    with pytest.raises(VoiceError, match="no mic lib"):
        cap.record(1.0)
    stt = stt_mod.UnavailableSTT("no stt lib")
    with pytest.raises(VoiceError, match="no stt lib"):
        stt.transcribe(_silent_chunk())
    assert stt.offline is False
    tts = tts_mod.UnavailableTTS("no tts lib")
    with pytest.raises(VoiceError, match="no tts lib"):
        tts.speak("hi")
    with pytest.raises(VoiceError, match="no tts lib"):
        tts.speak_handle("hi")
    ww = ww_mod.UnavailableWakeWordEngine("no ww lib")
    with pytest.raises(VoiceError, match="no ww lib"):
        ww.listen_once(cap)
    assert "unavailable" in ww.model_info


def test_default_factories_degrade_without_raising():
    # sounddevice/openwakeword/faster-whisper are not installed here.
    assert isinstance(audio_mod.default_capture(), AudioCapture)
    assert isinstance(stt_mod.default_stt(), STTEngine)
    assert isinstance(tts_mod.default_tts(), TTSEngine)
    assert isinstance(ww_mod.default_wakeword(), WakeWordEngine)


def test_energy_vad_silence_vs_speech():
    vad = vad_mod.EnergyVAD()
    assert vad.is_speech(_silent_chunk()) is False
    assert vad.is_speech(_loud_chunk()) is True


def test_energy_vad_calibrate():
    vad = vad_mod.EnergyVAD()
    floor = vad.calibrate(_silent_chunk())
    assert floor >= 1.0
    # A moderately loud chunk still clears a silence-calibrated floor.
    assert vad.is_speech(_loud_chunk(amplitude=4000)) is True


def test_chunk_to_wav_bytes_is_valid_wav():
    wav_bytes = stt_mod.chunk_to_wav_bytes(_loud_chunk(seconds=0.2))
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        assert w.getnframes() == 3200


def test_platform_tts_command_building(monkeypatch):
    tts = tts_mod.PlatformTTS()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/say")
    cmd = tts._cmd("hello", None)
    assert cmd[0] == "say" and cmd[-1] == "hello"

    profile = VoiceProfile(name="x", description="x", engine="platform",
                           voice_id="Samantha", rate=40)
    cmd = tts._cmd("hello", profile)
    assert "-v" in cmd and "Samantha" in cmd

    monkeypatch.setattr(sys, "platform", "win32")
    cmd = tts._cmd("hello", None)
    assert cmd[0] == "powershell"

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda c: None)
    with pytest.raises(VoiceError, match="espeak"):
        tts._cmd("hello", None)

    monkeypatch.setattr(shutil, "which",
                        lambda c: "/usr/bin/espeak-ng" if "espeak" in c else None)
    cmd = tts._cmd("hello", None)
    assert cmd[0] == "/usr/bin/espeak-ng"


def test_platform_tts_list_voices_honest_without_synth(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda c: None)
    # No synthesizer installed: empty list is honest, not an exception.
    assert tts_mod.PlatformTTS().list_voices() == []


# ---------------------------------------------------------------------------
# Voice profiles
# ---------------------------------------------------------------------------

def test_profiles_load():
    profiles = profiles_mod.list_profiles()
    names = [p.name for p in profiles]
    assert "default" in names
    assert "licensed-jade" in names
    assert names[0] == "default"  # default sorts first
    for p in profiles:
        assert p.description, p.name


def test_get_profile_unknown_returns_none():
    assert profiles_mod.get_profile("no-such-voice") is None
    assert profiles_mod.get_profile(None).name == "default"


def test_licensed_profile_is_honest_drop_in_slot():
    licensed = profiles_mod.get_profile("licensed-jade")
    assert licensed is not None
    assert "licensed" in licensed.note.lower()
    engine = profiles_mod.engine_for_profile(licensed)
    with pytest.raises(VoiceError, match="no licensed voice model"):
        engine.speak("hello")


def test_engine_for_profile_unknown_engine_honest():
    weird = VoiceProfile(name="weird", description="w", engine="frobnicate")
    engine = profiles_mod.engine_for_profile(weird)
    with pytest.raises(VoiceError, match="unknown TTS engine"):
        engine.speak("hello")


def test_malformed_profile_json_ignored(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(profiles_mod, "PROFILES_DIR", tmp_path)
    assert profiles_mod.list_profiles() == []
    assert profiles_mod.get_profile("default") is None


def test_active_profile_reads_config(tmp_path, monkeypatch):
    import core.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    assert profiles_mod.active_profile().name == "default"
    cfg = config_mod.load_config()
    cfg["voice_profile"] = "warm"
    config_mod.save_config(cfg)
    assert profiles_mod.active_profile().name == "warm"


# ---------------------------------------------------------------------------
# Conversation (barge-in) with stubbed backends
# ---------------------------------------------------------------------------

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
        self.spoken = []
        self.handles = []

    def speak(self, text, profile=None):
        self.spoken.append(text)

    def speak_handle(self, text, profile=None):
        self.spoken.append(text)
        h = _StubHandle()
        self.handles.append(h)
        return h

    def list_voices(self):
        return [{"id": "stub", "name": "Stub Voice"}]


class _AlwaysSpeechVAD(vad_mod.EnergyVAD):
    def is_speech(self, chunk):
        return True


class _ScriptVAD(vad_mod.EnergyVAD):
    """VAD with a scripted True/False sequence per is_speech call."""
    def __init__(self, script):
        self._script = list(script)

    def is_speech(self, chunk):
        return bool(self._script.pop(0)) if self._script else False


def test_conversation_push_to_talk_flow():
    events = []
    convo = conv_mod.Conversation(
        capture=_ScriptCapture([_loud_chunk(), _silent_chunk()]),
        stt=_ScriptSTT(["hello jade"]),
        tts=_ScriptTTS(),
        vad=_ScriptVAD([True, False]),  # speech while listening, quiet while she speaks
        think=lambda t: f"reply to {t}",
        on_event=lambda k, m: events.append((k, m)),
    )
    convo.run(mode="push_to_talk")
    kinds = [k for k, _ in events]
    assert "heard" in kinds and "reply" in kinds and "end" in kinds
    heard = dict((k, m) for k, m in events if k == "heard")["heard"]
    assert heard == "hello jade"
    assert convo.tts.spoken == ["reply to hello jade"]


def test_conversation_barge_in_stops_speech():
    events = []
    tts = _ScriptTTS()
    # First stream chunk = user interrupting while she speaks. Grace is 0
    # here so the test exercises the barge-in mechanics deterministically;
    # grace-window behavior has its own tests below.
    convo = conv_mod.Conversation(
        capture=_ScriptCapture([_loud_chunk()]),
        stt=_ScriptSTT(["never mind"]),
        tts=tts,
        vad=_AlwaysSpeechVAD(),
        think=lambda t: "long answer",
        on_event=lambda k, m: events.append((k, m)),
        barge_in_grace=0,
    )
    interrupted = convo._speak_with_barge_in("long answer")
    assert tts.handles and tts.handles[0].stopped, "barge-in must stop speech"
    assert interrupted == "never mind"
    assert any(k == "barge_in" for k, _ in events)


def test_conversation_stop_phrase_ends_loop():
    convo = conv_mod.Conversation(
        capture=_ScriptCapture([_loud_chunk()]),
        stt=_ScriptSTT(["stop"]),
        tts=_ScriptTTS(),
        think=lambda t: "should never be said",
    )
    convo.run(mode="always_listening")
    assert convo.tts.spoken == []


def test_conversation_rejects_unknown_mode():
    convo = conv_mod.Conversation(
        capture=_ScriptCapture([]), stt=_ScriptSTT([]), tts=_ScriptTTS())
    with pytest.raises(VoiceError, match="unknown conversation mode"):
        convo.run(mode="telepathy")


# ---------------------------------------------------------------------------
# Wake word
# ---------------------------------------------------------------------------

def test_wakeword_notice_flow(tmp_path, monkeypatch):
    import core.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    assert ww_mod.notice_acknowledged() is False
    assert "microphone" in ww_mod.WAKEWORD_NOTICE.lower()
    ww_mod.acknowledge_notice()
    assert ww_mod.notice_acknowledged() is True


def test_default_wakeword_engine_honest_without_openwakeword():
    engine = ww_mod.default_wakeword()
    assert isinstance(engine, WakeWordEngine)
    # openwakeword is not installed in this environment.
    try:
        import openwakeword  # noqa: F401
        have = True
    except ImportError:
        have = False
    if not have:
        assert isinstance(engine, ww_mod.UnavailableWakeWordEngine)
        assert "openwakeword" in engine.model_info.lower()


# ---------------------------------------------------------------------------
# Desktop: overlay HTML, register_page, GUI fallback
# ---------------------------------------------------------------------------

def test_overlay_html_has_glassmorphism():
    from core.desktop import overlay_html
    html = overlay_html()
    assert "backdrop-filter" in html
    assert "-webkit-backdrop-filter" in html
    assert "Jade" in html
    # Graceful fallback where blur is unsupported.
    assert "@supports not" in html


def test_dashboard_serves_registered_overlay_page(dash_server):
    import urllib.request
    import urllib.error
    from core.dashboard import register_page
    from core.desktop import overlay_html
    register_page("/overlay", overlay_html())
    token = dash_server["token"]
    url = f"http://127.0.0.1:{dash_server['port']}/overlay?token={token}"
    with urllib.request.urlopen(url, timeout=5) as r:
        body = r.read()
    assert b"backdrop-filter" in body
    # Unauthenticated requests are still rejected.
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(
            f"http://127.0.0.1:{dash_server['port']}/overlay", timeout=5)
    assert ei.value.code == 401


def test_dashboard_personality_voice_api(dash_server, tmp_path, monkeypatch):
    import json
    import urllib.request
    import core.config as config_mod
    from core.dashboard import get_personality_state, set_personality_state
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)

    state = get_personality_state()
    assert state["current"] == "jade"
    assert {o["name"] for o in state["options"]} == {"jade", "witty", "clippy", "professional"}

    res = set_personality_state("clippy")
    assert res == {"ok": True, "current": "clippy"}
    res = set_personality_state("nope")
    assert res["ok"] is False

    token = dash_server["token"]
    url = f"http://127.0.0.1:{dash_server['port']}/api/voice-profile?token={token}"
    with urllib.request.urlopen(url, timeout=5) as r:
        data = json.loads(r.read())
    assert "default" in {o["name"] for o in data["options"]}


def test_launch_gui_falls_back_without_pywebview(monkeypatch, capsys):
    import core.desktop as desktop_mod
    monkeypatch.setattr(desktop_mod, "_webview", lambda: None)
    monkeypatch.setattr(desktop_mod, "_start_dashboard",
                        lambda: {"url": "http://127.0.0.1:9/?token=x"})
    opened = {}
    monkeypatch.setattr(desktop_mod, "_open_browser",
                        lambda url: opened.setdefault("url", url))
    desktop_mod.launch_gui()
    assert opened.get("url", "").startswith("http://127.0.0.1:9/")
    out = capsys.readouterr().out
    assert "pywebview" in out  # honest install hint


def test_app_icon_wired():
    from core.desktop import app_icon_path, ASSETS_DIR
    mark = ASSETS_DIR / "jade-mark.png"
    assert mark.exists(), "generated Jade mark must ship in assets/"
    assert app_icon_path() == mark


# ---------------------------------------------------------------------------
# Tray
# ---------------------------------------------------------------------------

def test_tray_platform_slot_shape():
    from core.tray import platform_slot
    os_name, slot = platform_slot()
    assert os_name in ("windows", "macos", "linux")
    assert isinstance(slot, str) and slot


def test_tray_supported_is_honest():
    from core.tray import tray_supported
    try:
        import pystray  # noqa: F401
        have = True
    except ImportError:
        have = False
    ok, reason = tray_supported()
    assert ok == have
    assert isinstance(reason, str) and reason.strip()


def test_tray_icon_fallback_glyph_without_assets(monkeypatch, tmp_path):
    # Even with no icon file, the tray builds a real drawn glyph (needs PIL).
    import core.tray as tray_mod
    import core.desktop as desktop_mod
    monkeypatch.setattr(desktop_mod, "app_icon_path", lambda: None)
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        pytest.skip("PIL not installed")
    img = tray_mod._icon_image()
    assert img.size == (64, 64)


# ---------------------------------------------------------------------------
# Proprietary drop-in dir + logo assets
# ---------------------------------------------------------------------------

def test_proprietary_dir_gitignored_and_documented():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    readme = root / "core" / "voice" / "proprietary" / "README.md"
    assert readme.exists()
    text = readme.read_text().lower()
    assert "licensed" in text
    gitignore = (root / ".gitignore").read_text()
    assert "core/voice/proprietary" in gitignore


def test_cli_commands_registered():
    # New slash commands resolve through the fuzzy handler registry.
    import cli
    for cmd in ("/personality", "/voice-profile", "/gui", "/overlay", "/tray",
                "/voice-wake"):
        assert cmd in cli.COMMANDS, cmd
