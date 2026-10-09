import os
import tempfile

import sounddevice as sd
import scipy.io.wavfile as wav
import speech_recognition as sr
from core.agent import debug_loop

# NOTE: transcription uses Google's web speech API (recognize_google) and
# therefore requires an internet connection. It is not offline/local speech
# recognition.

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
    """Transcribe a WAV file via the speech-recognition backend."""
    recognizer = sr.Recognizer()
    with sr.AudioFile(path) as source:
        audio_data = recognizer.record(source)
    print("Transcribing...")
    return recognizer.recognize_google(audio_data)


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
        except sr.UnknownValueError:
            print("JADE could not understand the audio.")
            return
        except sr.RequestError as e:
            print(f"JADE voice error: {e} (speech recognition needs an internet connection)")
            return
        print(f"JADE heard: {cmd}")

        # Use the debug_loop to allow tool execution
        debug_loop(cmd)
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
