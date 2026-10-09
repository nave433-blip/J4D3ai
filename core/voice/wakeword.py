"""Wake-word ("Hey Jade") detection — pluggable engine interface.

Ships with an open-source default (``openwakeword`` when installed) and an
honest unavailable engine otherwise. A custom-trained "Hey Jade" model
drops in via config: set ``wakeword_model_path`` to your ``.onnx`` model and
``wakeword_threshold`` to taste — see ``core/voice/proprietary/README.md``.

Privacy: wake-word listening is strictly opt-in and entirely local. The
microphone stream never leaves the machine for hotword detection. The first
time it is enabled, the user must acknowledge the privacy notice
(``wakeword_notice_ack`` in config).
"""

from __future__ import annotations

import time
from typing import Optional

from core.voice.base import AudioCapture, VoiceError, WakeWordEngine


WAKEWORD_NOTICE = (
    "Wake-word listening uses your microphone continuously while enabled, "
    "but all detection happens ON THIS MACHINE — audio never leaves your "
    "device for hotword detection. You can disable it at any time with "
    "/voice-wake off or the tray toggle."
)


def notice_acknowledged() -> bool:
    from core.config import load_config
    return bool(load_config().get("wakeword_notice_ack", False))


def acknowledge_notice() -> None:
    from core.config import load_config, save_config
    cfg = load_config()
    cfg["wakeword_notice_ack"] = True
    save_config(cfg)


class UnavailableWakeWordEngine(WakeWordEngine):
    def __init__(self, reason: str):
        self.reason = reason

    def listen_once(self, capture: AudioCapture, timeout: float = 30.0) -> bool:
        raise VoiceError(f"wake-word detection unavailable: {self.reason}")

    @property
    def model_info(self) -> str:
        return f"unavailable: {self.reason}"


class OpenWakeWordEngine(WakeWordEngine):
    """Hotword detection via the optional ``openwakeword`` package.

    Uses a bundled or custom ``.onnx`` model. To train your own "Hey Jade"
    model, see https://github.com/dscripka/openWakeWord#training-new-models
    and drop the resulting file into ``core/voice/proprietary/`` (or set
    ``wakeword_model_path`` in config).
    """

    def __init__(self, model_path: Optional[str] = None,
                 threshold: float = 0.5):
        try:
            from openwakeword.model import Model  # noqa: F401
        except ImportError as e:
            raise VoiceError(
                "openwakeword is not installed (pip install openwakeword)") from e
        from core.config import load_config
        cfg = load_config()
        self.threshold = float(cfg.get("wakeword_threshold", threshold))
        self._model_path = model_path or cfg.get("wakeword_model_path")
        self._model = None

    def _ensure(self):
        if self._model is None:
            from openwakeword.model import Model
            kwargs = {}
            if self._model_path:
                # Custom single-model path.
                kwargs = {"wakeword_models": [self._model_path]}
            self._model = Model(**kwargs)
        return self._model

    @property
    def model_info(self) -> str:
        if self._model_path:
            return f"openwakeword custom model: {self._model_path}"
        return "openwakeword (built-in models; set wakeword_model_path for a custom 'Hey Jade' model)"

    def listen_once(self, capture: AudioCapture, timeout: float = 30.0) -> bool:
        model = self._ensure()
        deadline = time.time() + timeout
        for chunk in capture.stream():
            if chunk.sample_rate != 16000 or chunk.sample_width != 2:
                continue  # openwakeword expects 16kHz 16-bit mono
            scores = model.predict(chunk.pcm)
            for name, score in scores.items():
                if score >= self.threshold:
                    try:
                        model.reset()
                    except Exception:
                        pass
                    return True
            if time.time() >= deadline:
                return False
        return False


def default_wakeword() -> WakeWordEngine:
    """Best available wake-word engine, or an honest unavailable stand-in."""
    try:
        return OpenWakeWordEngine()
    except VoiceError as e:
        return UnavailableWakeWordEngine(str(e))


def run_listener(on_wake, capture: Optional[AudioCapture] = None,
                 engine: Optional[WakeWordEngine] = None,
                 poll_timeout: float = 5.0) -> None:
    """Block forever listening for the hotword; call ``on_wake()`` on each
    detection. Raises VoiceError when detection is unavailable. Ctrl+C stops."""
    from core.voice.audio import default_capture
    capture = capture or default_capture()
    engine = engine or default_wakeword()
    print(f"[jade] Listening for '{engine.hotword}' "
          f"({engine.model_info}). Ctrl+C to stop.")
    try:
        while True:
            try:
                if engine.listen_once(capture, timeout=poll_timeout):
                    on_wake()
            except VoiceError as e:
                print(f"[jade] Wake-word error: {e}")
                break
    except KeyboardInterrupt:
        print("[jade] Wake-word listening stopped.")
