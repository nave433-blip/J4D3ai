"""Continuous voice conversation with barge-in support.

Modes:
  - ``push_to_talk``: record one utterance per turn (caller drives the loop).
  - ``always_listening``: loop record -> transcribe -> think -> speak until
    the user says a stop phrase or interrupts.

Barge-in: while Jade is speaking, the VAD watches the microphone; when
speech is detected, the in-progress utterance is stopped (``speak_handle``
is killable) and the user's interruption becomes the next turn.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from core.voice.base import AudioCapture, STTEngine, TTSEngine, VoiceError, VoiceProfile
from core.voice.vad import EnergyVAD, record_until_silence

STOP_PHRASES = {"stop", "quit", "exit", "goodbye", "good night", "that's enough"}


class Conversation:
    def __init__(self, capture: AudioCapture, stt: STTEngine, tts: TTSEngine,
                 vad: Optional[EnergyVAD] = None,
                 profile: Optional[VoiceProfile] = None,
                 think: Optional[Callable[[str], str]] = None,
                 on_event: Optional[Callable[[str, str], None]] = None,
                 barge_in_grace: float = 0.4):
        self.capture = capture
        self.stt = stt
        self.tts = tts
        self.vad = vad or EnergyVAD()
        self.profile = profile
        self.think = think or (lambda text: text)
        self.on_event = on_event or (lambda kind, msg: None)
        # Seconds after speech starts during which VAD triggers are ignored.
        # The first moments of playback are almost always the speaker's own
        # output leaking into the mic, not a real interruption.
        self.barge_in_grace = max(0.0, barge_in_grace)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, kind: str, msg: str) -> None:
        try:
            self.on_event(kind, msg)
        except Exception:
            pass

    def listen_once(self, device: Optional[int] = None) -> str:
        """Record one utterance (until silence) and transcribe it."""
        chunk = record_until_silence(self.capture, self.vad, device=device)
        if not chunk.pcm:
            raise VoiceError("no speech detected")
        text = self.stt.transcribe(chunk)
        self._emit("heard", text)
        return text

    def _speak_with_barge_in(self, text: str) -> Optional[str]:
        """Speak ``text``; if the user interrupts, stop and return what they
        said. Returns None when speech completed without interruption."""
        handle = self.tts.speak_handle(text, self.profile)
        started = time.monotonic()
        try:
            # Poll the mic while speaking: speech during playback = barge-in.
            # VAD hits inside the grace window are ignored — that's the
            # speaker's own output leaking into the microphone.
            for chunk in self.capture.stream():
                if handle.done or self._stop.is_set():
                    break
                if self.vad.is_speech(chunk):
                    if time.monotonic() - started < self.barge_in_grace:
                        continue
                    handle.stop()
                    self._emit("barge_in", "user interrupted")
                    try:
                        return self.listen_once()
                    except VoiceError:
                        return ""
            handle.wait()
            return None
        finally:
            try:
                handle.stop()
            except Exception:
                pass

    def run(self, mode: str = "push_to_talk",
            device: Optional[int] = None) -> None:
        """Run the conversation loop. ``mode`` is ``push_to_talk`` or
        ``always_listening``."""
        if mode not in ("push_to_talk", "always_listening"):
            raise VoiceError(f"unknown conversation mode: {mode}")
        self._emit("start", f"mode={mode}")
        pending: Optional[str] = None
        while not self._stop.is_set():
            try:
                if pending is None:
                    if mode == "push_to_talk":
                        # One utterance per run() call in push-to-talk.
                        utter = self.listen_once(device=device)
                        if utter.strip().lower() in STOP_PHRASES:
                            break
                        reply = self.think(utter)
                        self._emit("reply", reply)
                        self._speak_with_barge_in(reply)
                        break
                    utter = self.listen_once(device=device)
                else:
                    utter = pending
                    pending = None
                if utter.strip().lower() in STOP_PHRASES:
                    self._emit("stop", "stop phrase heard")
                    break
                reply = self.think(utter)
                self._emit("reply", reply)
                interrupted = self._speak_with_barge_in(reply)
                if interrupted:
                    pending = interrupted
            except VoiceError as e:
                self._emit("error", str(e))
                if mode == "push_to_talk":
                    break
                time.sleep(1.0)
        self._emit("end", "conversation ended")
