"""Jade voice subsystem — public API.

Stages: wake-word engine -> audio capture -> VAD -> STT -> TTS.
Every stage is a swappable slot; see :mod:`core.voice.base`.
"""

from core.voice.base import (
    AudioCapture, AudioChunk, STTEngine, TTSEngine, VoiceActivityDetector,
    VoiceError, VoiceProfile, WakeWordEngine,
    register_engine, registered_engines,
)
from core.voice import audio, profiles, stt, tts, vad, wakeword, conversation

__all__ = [
    "AudioCapture", "AudioChunk", "STTEngine", "TTSEngine",
    "VoiceActivityDetector", "VoiceError", "VoiceProfile", "WakeWordEngine",
    "register_engine", "registered_engines",
    "audio", "profiles", "stt", "tts", "vad", "wakeword", "conversation",
]
