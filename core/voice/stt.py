"""Speech-to-text engines: voice AND sound-to-text transcription."""

from __future__ import annotations

import io
import wave
from typing import Optional

from core.voice.base import AudioChunk, STTEngine, VoiceError


def chunk_to_wav_bytes(chunk: AudioChunk) -> bytes:
    """Encode an AudioChunk as a WAV file in memory (stdlib only)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(chunk.channels)
        w.setsampwidth(chunk.sample_width)
        w.setframerate(chunk.sample_rate)
        w.writeframes(chunk.pcm)
    return buf.getvalue()


class UnavailableSTT(STTEngine):
    def __init__(self, reason: str):
        self.reason = reason

    def transcribe(self, chunk: AudioChunk) -> str:
        raise VoiceError(f"speech recognition unavailable: {self.reason}")

    @property
    def offline(self) -> bool:
        return False


class GoogleWebSTT(STTEngine):
    """Transcription via the optional ``SpeechRecognition`` package.

    Honest about its limits: uses Google's web speech API, so it needs an
    internet connection (``offline`` is False).
    """

    def __init__(self):
        try:
            import speech_recognition as sr  # noqa: F401
        except ImportError as e:
            raise VoiceError(
                "SpeechRecognition is not installed (pip install SpeechRecognition)") from e

    def transcribe(self, chunk: AudioChunk) -> str:
        import speech_recognition as sr
        recognizer = sr.Recognizer()
        wav_bytes = chunk_to_wav_bytes(chunk)
        with sr.AudioFile(io.BytesIO(wav_bytes)) as source:
            audio_data = recognizer.record(source)
        try:
            return recognizer.recognize_google(audio_data)
        except sr.UnknownValueError as e:
            raise VoiceError("could not understand the audio") from e
        except sr.RequestError as e:
            raise VoiceError(f"recognition service unreachable ({e}); "
                             "this engine needs an internet connection") from e

    @property
    def offline(self) -> bool:
        return False


class FasterWhisperSTT(STTEngine):
    """Fully offline transcription via the optional ``faster-whisper`` package.

    Handles voice AND general sound-to-text: any audio fed in is transcribed
    or, when nothing intelligible is present, reported honestly.
    """

    def __init__(self, model: str = "base"):
        try:
            from faster_whisper import WhisperModel  # noqa: F401
        except ImportError as e:
            raise VoiceError(
                "faster-whisper is not installed (pip install faster-whisper)") from e
        self.model_name = model
        self._model = None

    def _ensure(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_name)
        return self._model

    def transcribe(self, chunk: AudioChunk) -> str:
        import tempfile, os
        wav_bytes = chunk_to_wav_bytes(chunk)
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="jade-stt-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(wav_bytes)
            segments, _ = self._ensure().transcribe(path)
            text = " ".join(s.text.strip() for s in segments).strip()
            if not text:
                raise VoiceError("no intelligible speech in the audio")
            return text
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    @property
    def offline(self) -> bool:
        return True


def default_stt(prefer_offline: bool = False) -> STTEngine:
    """Best available STT engine, or an honest unavailable stand-in."""
    candidates = []
    if prefer_offline:
        candidates.append(("faster-whisper", FasterWhisperSTT))
    candidates.append(("SpeechRecognition (Google web API)", GoogleWebSTT))
    if not prefer_offline:
        candidates.append(("faster-whisper", FasterWhisperSTT))
    errors = []
    for label, cls in candidates:
        try:
            return cls()
        except VoiceError as e:
            errors.append(f"{label}: {e}")
    return UnavailableSTT("; ".join(errors) or "no STT backend installed")
