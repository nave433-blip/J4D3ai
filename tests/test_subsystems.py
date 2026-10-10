"""Subsystem tests: memory/vector, watcher/monitor, voice/voice.

Heavy optional deps (faiss, numpy, sounddevice, scipy, speech_recognition)
are NOT installed in the test venv, so they are stubbed in sys.modules.
What is tested is our logic: index-id filtering, dim-mismatch archiving,
add() status reporting, watcher ignore/debounce rules, voice temp files.
"""
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, "/home/hatch/workspace/jarvis-dev")


def _install_stubs():
    """Install fake faiss/numpy/sounddevice/scipy/speech_recognition."""
    for m in ("faiss", "numpy", "sounddevice", "scipy",
              "scipy.io", "scipy.io.wavfile", "speech_recognition",
              "memory.vector", "watcher.monitor", "voice.voice"):
        sys.modules.pop(m, None)

    fake_faiss = types.ModuleType("faiss")

    class FakeIndex:
        def __init__(self, dim):
            self.d = dim
            self.ntotal = 0
            self._search_ids = []

        def add(self, arr):
            self.ntotal += 1

        def search(self, arr, k):
            ids = (self._search_ids + [-1] * k)[:k]
            return None, [ids]

    fake_faiss.IndexFlatL2 = FakeIndex

    def _no_file(path):
        raise IOError("no stored index in sandbox")

    fake_faiss.read_index = _no_file
    fake_faiss.write_index = lambda idx, p: None

    fake_np = types.ModuleType("numpy")

    class FakeArr(list):
        def astype(self, _t):
            return self

    fake_np.array = lambda x: FakeArr(x)

    sys.modules["faiss"] = fake_faiss
    sys.modules["numpy"] = fake_np

    # audio stack stubs (voice module import only needs them present)
    sys.modules["sounddevice"] = MagicMock(name="sounddevice")
    scipy = types.ModuleType("scipy")
    scipy_io = types.ModuleType("scipy.io")
    scipy_wav = types.ModuleType("scipy.io.wavfile")
    scipy_wav.write = MagicMock(name="wav.write")
    scipy.io = scipy_io
    scipy_io.wavfile = scipy_wav
    sys.modules["scipy"] = scipy
    sys.modules["scipy.io"] = scipy_io
    sys.modules["scipy.io.wavfile"] = scipy_wav
    sr = types.ModuleType("speech_recognition")
    sr.Recognizer = MagicMock(name="sr.Recognizer")
    sr.AudioFile = MagicMock(name="sr.AudioFile")
    sr.UnknownValueError = type("UnknownValueError", (Exception,), {})
    sr.RequestError = type("RequestError", (Exception,), {})
    sys.modules["speech_recognition"] = sr
    return fake_faiss


@pytest.fixture()
def vmod(tmp_path, monkeypatch):
    """Fresh memory.vector bound to a sandbox HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _install_stubs()
    import memory.vector as v
    v._index = None
    v._store = []
    yield v
    for m in ("faiss", "numpy", "memory.vector", "watcher.monitor",
              "voice.voice", "sounddevice", "scipy", "scipy.io",
              "scipy.io.wavfile", "speech_recognition"):
        sys.modules.pop(m, None)


class TestVectorMemory:
    def test_search_ignores_negative_ids(self, vmod):
        """FAISS returns -1 for empty slots; _store[-1] must never leak."""
        with patch.object(vmod, "_get_embedding", return_value=[0.1] * 4):
            assert vmod.add("first") is True
            assert vmod.add("second") is True
            vmod._index._search_ids = [-1, 0, 99]
            results = vmod.search("q", k=3)
        # -1 (empty slot) and 99 (out of range) are dropped; only id 0 kept
        assert results == ["first"]

    def test_add_returns_false_when_embedding_fails(self, vmod):
        with patch.object(vmod, "_get_embedding", return_value=None):
            assert vmod.add("nope") is False
        assert vmod._store == []

    def test_add_returns_true_and_persists(self, vmod, tmp_path):
        with patch.object(vmod, "_get_embedding", return_value=[0.1] * 4):
            assert vmod.add("hello", metadata="t") is True
        assert vmod._store == ["[t] hello"]
        assert (tmp_path / ".jade" / "memory" / "memory_store.pkl").exists()

    def test_dim_change_archives_old_index(self, vmod, tmp_path):
        memdir = tmp_path / ".jade" / "memory"
        with patch.object(vmod, "_get_embedding", return_value=[0.1] * 4):
            assert vmod.add("v1") is True
        assert vmod._index.d == 4
        # the write_index stub is a no-op; simulate the real on-disk index
        (memdir / "memory.index").write_bytes(b"fake-index")
        # embedding model changed -> 8-dim vectors
        with patch.object(vmod, "_get_embedding", return_value=[0.1] * 8):
            assert vmod.add("v2") is True
        assert vmod._index.d == 8
        assert vmod._store == ["v2"]  # stale entries not mixed in
        backups = list(memdir.glob("*.bak-*"))
        assert backups, "old index should be archived, not deleted"

    def test_clear_wipes(self, vmod):
        with patch.object(vmod, "_get_embedding", return_value=[0.1] * 4):
            vmod.add("x")
        assert vmod.clear() == "Memory successfully cleared."
        assert vmod._store == []


class TestWatcher:
    @pytest.fixture()
    def wmod(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _install_stubs()
        import watcher.monitor as w
        yield w
        for m in ("faiss", "numpy", "memory.vector", "watcher.monitor"):
            sys.modules.pop(m, None)

    def test_ignored_dirs(self, wmod):
        assert wmod._ignored(".git/hooks/pre-commit") is True
        assert wmod._ignored("proj/__pycache__/a.pyc") is True
        assert wmod._ignored("proj/venv/lib/x.py") is True
        assert wmod._ignored("src/main.py") is False

    def test_non_python_ignored(self, wmod):
        h = wmod.Handler()
        ev = SimpleNamespace(is_directory=False, src_path="/tmp/a.txt")
        with patch.object(wmod, "think") as t:
            h.on_modified(ev)
        t.assert_not_called()

    def test_debounce(self, wmod, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n")
        h = wmod.Handler()
        ev = SimpleNamespace(is_directory=False, src_path=str(target))
        with patch.object(wmod, "think", return_value="analysis") as t, \
             patch.object(wmod, "add", return_value=True):
            h.on_modified(ev)
            h.on_modified(ev)  # within DEBOUNCE_SECONDS -> skipped
        assert t.call_count == 1

    def test_memory_failure_warns(self, wmod, tmp_path, capsys):
        target = tmp_path / "b.py"
        target.write_text("y = 2\n")
        h = wmod.Handler()
        ev = SimpleNamespace(is_directory=False, src_path=str(target))
        with patch.object(wmod, "think", return_value="analysis"), \
             patch.object(wmod, "add", return_value=False):
            h.on_modified(ev)
        assert "not saved" in capsys.readouterr().out


class TestVoice:
    @pytest.fixture()
    def voicemod(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        _install_stubs()
        import voice.voice as v
        yield v
        for m in ("voice.voice", "sounddevice", "scipy", "scipy.io",
                  "scipy.io.wavfile", "speech_recognition"):
            sys.modules.pop(m, None)

    def test_run_voice_uses_tempfile_and_cleans_up(self, voicemod, tmp_path):
        with patch.object(voicemod, "record_to_wav") as rec, \
             patch.object(voicemod, "transcribe_wav", return_value="hi jade"), \
             patch.object(voicemod, "debug_loop") as loop:
            voicemod.run_voice()
        path = rec.call_args[0][0]
        assert path != str(tmp_path / "cmd.wav")
        assert "jade-voice-" in os.path.basename(path)
        assert not os.path.exists(path), "temp wav must be removed"
        assert (tmp_path / "cmd.wav").exists() is False
        loop.assert_called_once_with("hi jade")

    def test_run_voice_handles_no_microphone(self, voicemod, capsys):
        with patch.object(voicemod, "record_to_wav",
                          side_effect=RuntimeError("no usable audio input device (boom)")), \
             patch.object(voicemod, "debug_loop") as loop:
            voicemod.run_voice()  # must not raise
        assert "no usable audio input device" in capsys.readouterr().out
        loop.assert_not_called()

    def test_record_to_wav_wraps_device_errors(self, voicemod):
        with patch.object(voicemod.sd, "rec", side_effect=OSError("no device")):
            with pytest.raises(RuntimeError, match="no usable audio input"):
                voicemod.record_to_wav("/tmp/x.wav")

    def test_transcribe_wav_routes_through_core_voice(self, voicemod, tmp_path,
                                                      monkeypatch):
        """transcribe_wav must use the core/voice STT stack, not a direct
        Google web API call."""
        import wave as _wave

        wav_path = str(tmp_path / "in.wav")
        with _wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 1600)

        import core.voice.stt as stt_mod
        import core.voice.base as base

        seen = {}

        class FakeSTT:
            offline = True

            def transcribe(self, chunk):
                seen["chunk"] = chunk
                assert isinstance(chunk, base.AudioChunk)
                assert chunk.sample_rate == 16000
                return "hello jade"

        monkeypatch.setattr(stt_mod, "default_stt",
                            lambda prefer_offline=False: FakeSTT())
        assert voicemod.transcribe_wav(wav_path) == "hello jade"

    def test_transcribe_wav_surfaces_engine_failure_honestly(
            self, voicemod, tmp_path, monkeypatch):
        import wave as _wave

        wav_path = str(tmp_path / "in.wav")
        with _wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 160)

        import core.voice.stt as stt_mod
        import core.voice.base as base

        class FailingSTT:
            offline = False

            def transcribe(self, chunk):
                raise base.VoiceError("nothing installed")

        monkeypatch.setattr(stt_mod, "default_stt",
                            lambda prefer_offline=False: FailingSTT())
        with pytest.raises(RuntimeError, match="nothing installed"):
            voicemod.transcribe_wav(wav_path)
