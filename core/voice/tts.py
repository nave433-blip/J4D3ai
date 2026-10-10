"""Text-to-speech engines.

``PlatformTTS`` is the default and needs no extra packages: it shells out to
the OS-native speech synthesizer (macOS ``say``, Linux ``espeak-ng`` /
``espeak`` / ``spd-say``, Windows SAPI via PowerShell). ``speak_handle()``
runs the synthesizer as a killable subprocess, which is what enables
barge-in (the user interrupts, she yields).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from typing import Dict, List, Optional

from core.voice.base import TTSEngine, VoiceError, VoiceProfile


class _SpeakHandle:
    """Background speech handle with stop()/wait() for barge-in."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc

    def stop(self) -> None:
        try:
            self._proc.kill()
        except Exception:
            pass

    def wait(self, timeout: Optional[float] = None) -> None:
        try:
            self._proc.wait(timeout=timeout)
        except Exception:
            pass

    @property
    def done(self) -> bool:
        return self._proc.poll() is not None


class _ThreadSpeakHandle:
    """Handle for engines that speak on a thread (stop is best-effort)."""

    def __init__(self, thread: threading.Thread, stop_flag: threading.Event):
        self._thread = thread
        self._stop_flag = stop_flag

    def stop(self) -> None:
        self._stop_flag.set()

    def wait(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout=timeout)

    @property
    def done(self) -> bool:
        return not self._thread.is_alive()


class UnavailableTTS(TTSEngine):
    def __init__(self, reason: str):
        self.reason = reason

    def speak(self, text: str, profile: Optional[VoiceProfile] = None) -> None:
        raise VoiceError(f"text-to-speech unavailable: {self.reason}")

    def speak_handle(self, text: str, profile: Optional[VoiceProfile] = None):
        raise VoiceError(f"text-to-speech unavailable: {self.reason}")

    def synthesize_to_file(self, text: str, path: str,
                           profile: Optional[VoiceProfile] = None) -> str:
        raise VoiceError(f"text-to-speech unavailable: {self.reason}")

    def list_voices(self) -> List[Dict[str, str]]:
        raise VoiceError(f"text-to-speech unavailable: {self.reason}")


def _platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


class PlatformTTS(TTSEngine):
    """OS-native speech synthesis via stdlib subprocess. No extra packages.

    Voice selection is best-effort per platform: ``profile.voice_id`` is
    passed through when the platform supports it, ``profile.rate`` adjusts
    words-per-minute where supported.
    """

    def _macos_cmd(self, text: str, profile: Optional[VoiceProfile]):
        cmd = ["say"]
        if profile and profile.voice_id:
            cmd += ["-v", profile.voice_id]
        if profile and profile.rate:
            cmd += ["--rate", str(max(80, min(500, 175 + profile.rate)))]
        cmd.append(text)
        return cmd

    def _linux_cmd(self, text: str, profile: Optional[VoiceProfile]):
        binary = (shutil.which("espeak-ng") or shutil.which("espeak")
                  or shutil.which("spd-say"))
        if binary is None:
            raise VoiceError("no Linux speech synthesizer found "
                             "(install espeak-ng or speech-dispatcher)")
        if binary.endswith("spd-say"):
            cmd = ["spd-say"]
            if profile and profile.rate:
                cmd += ["--rate", str(max(-100, min(100, profile.rate)))]
            cmd += ["--wait", text]
            return cmd
        cmd = [binary]
        if profile and profile.voice_id:
            cmd += ["-v", profile.voice_id]
        if profile and profile.rate:
            cmd += ["-s", str(max(80, min(500, 175 + profile.rate)))]
        cmd.append(text)
        return cmd

    def _windows_cmd(self, text: str, profile: Optional[VoiceProfile]):
        # SAPI via PowerShell; single-quoted text is escaped for safety.
        safe = text.replace("'", "''")
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        )
        if profile and profile.voice_id:
            ps += f"$s.SelectVoice('{profile.voice_id}'); "
        if profile and profile.rate:
            ps += f"$s.Rate = {[max(-10, min(10, profile.rate // 20))][0]}; "
        ps += f"$s.Speak('{safe}');"
        return ["powershell", "-NoProfile", "-Command", ps]

    def _cmd(self, text: str, profile: Optional[VoiceProfile]):
        plat = _platform()
        if plat == "macos":
            if shutil.which("say") is None:
                raise VoiceError("macOS 'say' command not found")
            return self._macos_cmd(text, profile)
        if plat == "windows":
            return self._windows_cmd(text, profile)
        return self._linux_cmd(text, profile)

    def speak(self, text: str, profile: Optional[VoiceProfile] = None) -> None:
        handle = self.speak_handle(text, profile)
        handle.wait()

    def speak_handle(self, text: str, profile: Optional[VoiceProfile] = None):
        if not text or not text.strip():
            raise VoiceError("nothing to speak")
        cmd = self._cmd(text, profile)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL)
        except FileNotFoundError as e:
            raise VoiceError(f"speech synthesizer not found ({e})") from e
        except OSError as e:
            raise VoiceError(f"could not start speech synthesizer ({e})") from e
        return _SpeakHandle(proc)

    def synthesize_to_file(self, text: str, path: str,
                           profile: Optional[VoiceProfile] = None) -> str:
        """Render speech to an audio file. Returns the MIME type written.

        macOS ``say -o`` writes AIFF; espeak-ng/espeak ``-w`` and Windows
        SAPI ``SetOutputToWaveFile`` write WAV. Raises VoiceError when the
        platform cannot render to a file.
        """
        if not text or not text.strip():
            raise VoiceError("nothing to speak")
        plat = _platform()
        try:
            if plat == "macos":
                if shutil.which("say") is None:
                    raise VoiceError("macOS 'say' command not found")
                cmd = ["say", "-o", path]
                if profile and profile.voice_id:
                    cmd += ["-v", profile.voice_id]
                if profile and profile.rate:
                    cmd += ["--rate", str(max(80, min(500, 175 + profile.rate)))]
                cmd.append(text)
                mime = "audio/aiff"
            elif plat == "windows":
                safe_text = text.replace("'", "''")
                safe_path = path.replace("'", "''")
                ps = (
                    "Add-Type -AssemblyName System.Speech; "
                    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                )
                if profile and profile.voice_id:
                    ps += f"$s.SelectVoice('{profile.voice_id}'); "
                if profile and profile.rate:
                    ps += f"$s.Rate = {[max(-10, min(10, profile.rate // 20))][0]}; "
                ps += (f"$s.SetOutputToWaveFile('{safe_path}'); "
                       f"$s.Speak('{safe_text}'); $s.Dispose();")
                cmd = ["powershell", "-NoProfile", "-Command", ps]
                mime = "audio/wav"
            else:
                binary = (shutil.which("espeak-ng") or shutil.which("espeak"))
                if binary is None:
                    raise VoiceError(
                        "file rendering needs espeak-ng or espeak "
                        "(spd-say cannot write audio files)")
                cmd = [binary, "-w", path]
                if profile and profile.voice_id:
                    cmd += ["-v", profile.voice_id]
                if profile and profile.rate:
                    cmd += ["-s", str(max(80, min(500, 175 + profile.rate)))]
                cmd.append(text)
                mime = "audio/wav"
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  stdin=subprocess.DEVNULL, timeout=120)
            if proc.returncode != 0:
                raise VoiceError(f"speech synthesizer exited with code {proc.returncode}")
        except FileNotFoundError as e:
            raise VoiceError(f"speech synthesizer not found ({e})") from e
        except OSError as e:
            raise VoiceError(f"could not render speech ({e})") from e
        return mime

    def list_voices(self) -> List[Dict[str, str]]:
        plat = _platform()
        try:
            if plat == "macos":
                out = subprocess.run(["say", "-v", "?"], capture_output=True,
                                     text=True, timeout=10).stdout
                voices = []
                for line in out.splitlines():
                    parts = line.split("#")
                    name = parts[0].strip()
                    if name:
                        voices.append({"id": name.split()[0], "name": name})
                return voices
            if plat == "linux":
                binary = shutil.which("espeak-ng") or shutil.which("espeak")
                if binary:
                    out = subprocess.run([binary, "--voices"], capture_output=True,
                                         text=True, timeout=10).stdout
                    voices = []
                    for line in out.splitlines()[1:]:
                        cols = line.split()
                        if len(cols) >= 4:
                            voices.append({"id": cols[3], "name": " ".join(cols[4:]) or cols[3]})
                    return voices
            if plat == "windows":
                ps = ("Add-Type -AssemblyName System.Speech; "
                      "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | "
                      "ForEach-Object { $_.VoiceInfo.Name }")
                out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                     capture_output=True, text=True, timeout=15).stdout
                return [{"id": n.strip(), "name": n.strip()}
                        for n in out.splitlines() if n.strip()]
        except Exception as e:
            raise VoiceError(f"could not list system voices ({e})") from e
        return []


class Pyttsx3TTS(TTSEngine):
    """Cross-platform TTS via the optional ``pyttsx3`` package (offline)."""

    def __init__(self):
        try:
            import pyttsx3  # noqa: F401
        except ImportError as e:
            raise VoiceError("pyttsx3 is not installed (pip install pyttsx3)") from e

    def _apply_profile(self, engine, profile: Optional[VoiceProfile]):
        if profile is None:
            return
        try:
            if profile.voice_id:
                for v in engine.getProperty("voices"):
                    if profile.voice_id in (v.id, v.name):
                        engine.setProperty("voice", v.id)
                        break
            if profile.rate:
                engine.setProperty("rate", 175 + profile.rate)
        except Exception:
            pass

    def speak(self, text: str, profile: Optional[VoiceProfile] = None) -> None:
        import pyttsx3
        engine = pyttsx3.init()
        try:
            self._apply_profile(engine, profile)
            engine.say(text)
            engine.runAndWait()
        finally:
            try:
                engine.stop()
            except Exception:
                pass

    def speak_handle(self, text: str, profile: Optional[VoiceProfile] = None):
        import pyttsx3
        stop_flag = threading.Event()

        def _run():
            engine = pyttsx3.init()
            try:
                self._apply_profile(engine, profile)
                engine.say(text)
                # runAndWait blocks; poll the stop flag between utterances.
                engine.runAndWait()
            finally:
                try:
                    engine.stop()
                except Exception:
                    pass

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return _ThreadSpeakHandle(thread, stop_flag)

    def list_voices(self) -> List[Dict[str, str]]:
        import pyttsx3
        engine = pyttsx3.init()
        return [{"id": v.id, "name": v.name} for v in engine.getProperty("voices")]


def default_tts() -> TTSEngine:
    """Best available TTS engine, or an honest unavailable stand-in."""
    for label, make in (("platform", PlatformTTS), ("pyttsx3", Pyttsx3TTS)):
        try:
            engine = make()
            # PlatformTTS construction never fails; verify a synthesizer exists.
            if label == "platform":
                try:
                    engine._cmd("test", None)
                except VoiceError as e:
                    raise e
            return engine
        except VoiceError:
            continue
    return UnavailableTTS("no speech synthesizer found on this system")
