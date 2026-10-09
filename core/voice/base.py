"""Plugin interfaces for Jade's voice subsystem.

Every stage of the voice pipeline is a swappable slot:

    wake-word engine -> audio capture -> VAD -> STT -> (brain) -> TTS

Each abstract class documents the contract a drop-in implementation must
honor. Engines that need optional third-party packages must degrade to an
honest "unavailable" error naming the missing package — never fake success.

Proprietary drop-ins (licensed wake-word models, licensed TTS voices, ...)
belong in ``core/voice/proprietary/`` (git-ignored) and register themselves
via :func:`core.voice.register_engine`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class AudioChunk:
    """A slice of raw PCM audio."""
    pcm: bytes
    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2  # bytes per sample (16-bit)


@dataclass(frozen=True)
class VoiceProfile:
    """A named TTS voice profile. Profiles are data (JSON files under
    ``core/voice/profiles/``) so new ones — including a future licensed
    Jade voice — drop in without code changes."""
    name: str
    description: str
    engine: str  # which TTS engine this profile targets, e.g. "platform"
    voice_id: str = ""  # engine-specific voice identifier (may be "")
    rate: int = 0  # words per minute adjustment, 0 = engine default
    pitch: int = 0  # semitone adjustment, 0 = engine default (best effort)
    note: str = ""  # shown to the user, e.g. "drop licensed model here"


class VoiceError(RuntimeError):
    """Raised when a voice stage is unavailable or fails honestly."""


class AudioCapture(ABC):
    """Captures microphone audio."""

    @abstractmethod
    def list_devices(self) -> List[Dict[str, str]]:
        """Return [{"index": ..., "name": ...}] of input devices."""

    @abstractmethod
    def record(self, seconds: float, device: Optional[int] = None) -> AudioChunk:
        """Record ``seconds`` of audio and return it."""

    @abstractmethod
    def stream(self, device: Optional[int] = None):
        """Yield AudioChunk frames indefinitely (generator)."""


class WakeWordEngine(ABC):
    """Detects a hotword such as 'Hey Jade' in an audio stream."""

    hotword: str = "Hey Jade"

    @abstractmethod
    def listen_once(self, capture: AudioCapture, timeout: float = 30.0) -> bool:
        """Block until the hotword is heard or ``timeout`` elapses.
        Returns True when the hotword was detected."""

    @property
    @abstractmethod
    def model_info(self) -> str:
        """Human-readable description of the loaded model."""


class VoiceActivityDetector(ABC):
    """Decides whether a chunk of audio contains speech."""

    @abstractmethod
    def is_speech(self, chunk: AudioChunk) -> bool:
        """Return True if the chunk likely contains speech."""


class STTEngine(ABC):
    """Speech-to-text (and sound-to-text) transcription."""

    @abstractmethod
    def transcribe(self, chunk: AudioChunk) -> str:
        """Transcribe an audio chunk to text. Raises VoiceError on failure."""

    @property
    @abstractmethod
    def offline(self) -> bool:
        """True if transcription works without an internet connection."""


class TTSEngine(ABC):
    """Text-to-speech synthesis."""

    @abstractmethod
    def speak(self, text: str, profile: Optional[VoiceProfile] = None) -> None:
        """Speak ``text`` synchronously (blocks until done)."""

    @abstractmethod
    def speak_handle(self, text: str, profile: Optional[VoiceProfile] = None):
        """Start speaking in the background; return a handle with ``stop()``
        and ``wait()`` methods (enables barge-in)."""

    @abstractmethod
    def list_voices(self) -> List[Dict[str, str]]:
        """Return available system voices as [{"id":..., "name":...}]."""


# ---------------------------------------------------------------------------
# Engine registry — proprietary drop-ins register here.
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, Dict[str, type]] = {
    "capture": {}, "wakeword": {}, "vad": {}, "stt": {}, "tts": {},
}


def register_engine(stage: str, name: str, cls: type) -> None:
    """Register a drop-in engine class for ``stage`` under ``name``."""
    if stage not in _REGISTRY:
        raise ValueError(f"unknown voice stage: {stage}")
    _REGISTRY[stage][name] = cls


def registered_engines(stage: str) -> Dict[str, type]:
    return dict(_REGISTRY.get(stage, {}))
