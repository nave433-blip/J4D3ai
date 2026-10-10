"""Legacy CLI voice entry point — now routed through core/voice.

Recording still uses the local microphone (sounddevice/scipy). Transcription
goes through :func:`core.voice.stt.default_stt` with ``prefer_offline=True``:
fully offline faster-whisper when installed, otherwise the SpeechRecognition
backend (honest about needing the internet), otherwise a clean
"unavailable" error. No audio is ever sent anywhere silently.
"""
import os
import tempfile
import wave

import sounddevice as sd
import scipy.io.wavfile as wav
from core.agent import debug_loop

RECORD_SECONDS = 5
SAMPLE_RATE = 44100


def record_to_wav(path, duration=RECORD_SECONDS):
    """Record from the default microphone and write a WAV file to `path`.

    Raises RuntimeError with a human-readable message if no usable audio
    device is available.
    """
    print(f"Recording for {duration} seconds...")
    try:
        audio = sd.rec(int(duration * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1)
        sd.wait()
    except Exception as e:
        raise RuntimeError(f"no usable audio input device ({e})")
    wav.write(path, SAMPLE_RATE, audio)
    print("Recording finished.")


def transcribe_wav(path):
    """Transcribe a WAV file via the core/voice STT engine stack.

    Offline-first: faster-whisper when installed, else the
    SpeechRecognition backend (needs internet — it says so), else an
    honest VoiceError.
    """
    # Lazy imports: this module must stay importable for tests/CLI help
    # even when the voice subsystem's optional deps are missing.
    from core.voice.base import AudioChunk, VoiceError
    from core.voice.stt import default_stt

    with wave.open(path, "rb") as w:
        chunk = AudioChunk(
            pcm=w.readframes(w.getnframes()),
            sample_rate=w.getframerate(),
            channels=w.getnchannels(),
            sample_width=w.getsampwidth(),
        )
    engine = default_stt(prefer_offline=True)
    print(f"Transcribing ({'offline' if engine.offline else 'online'} engine)...")
    try:
        return engine.transcribe(chunk)
    except VoiceError as e:
        raise RuntimeError(f"JADE could not transcribe the audio: {e}")


def run_voice():
    fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="jade-voice-")
    os.close(fd)
    try:
        try:
            record_to_wav(wav_path)
        except RuntimeError as e:
            print(f"JADE voice error: {e}")
            return
        try:
            cmd = transcribe_wav(wav_path)
        except RuntimeError as e:
            print(f"JADE voice error: {e}")
            return
        print(f"JADE heard: {cmd}")

        # Use the debug_loop to allow tool execution
        debug_loop(cmd)
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
