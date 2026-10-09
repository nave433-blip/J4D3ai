"""Voice profile loading and engine resolution.

Profiles are JSON data files under ``core/voice/profiles/`` — adding a new
voice is adding a file, no code changes. The ``licensed-jade`` profile is
a deliberate drop-in slot: it resolves to an honest unavailable engine until
a licensed voice model is placed in ``core/voice/proprietary/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from core.voice.base import TTSEngine, VoiceError, VoiceProfile
from core.voice.tts import UnavailableTTS

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"


def _load_file(path: Path) -> Optional[VoiceProfile]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return VoiceProfile(
            name=str(data.get("name") or path.stem),
            description=str(data.get("description", "")),
            engine=str(data.get("engine", "platform")),
            voice_id=str(data.get("voice_id", "")),
            rate=int(data.get("rate", 0)),
            pitch=int(data.get("pitch", 0)),
            note=str(data.get("note", "")),
        )
    except (TypeError, ValueError):
        return None


def list_profiles() -> List[VoiceProfile]:
    profiles = []
    if PROFILES_DIR.is_dir():
        for path in sorted(PROFILES_DIR.glob("*.json")):
            p = _load_file(path)
            if p is not None:
                profiles.append(p)
    # "default" first, then alphabetical.
    profiles.sort(key=lambda p: (p.name != "default", p.name))
    return profiles


def get_profile(name: Optional[str]) -> Optional[VoiceProfile]:
    if not name:
        name = "default"
    want = name.strip().lower()
    for p in list_profiles():
        if p.name.lower() == want:
            return p
    return None


def engine_for_profile(profile: VoiceProfile) -> TTSEngine:
    """Resolve the TTS engine for a profile, honestly.

    Unknown/proprietary engines become an UnavailableTTS with a clear
    message instead of a fake success.
    """
    from core.voice import tts as tts_mod

    engine_name = (profile.engine or "platform").strip().lower()
    if engine_name == "platform":
        return tts_mod.default_tts()
    if engine_name == "pyttsx3":
        try:
            return tts_mod.Pyttsx3TTS()
        except VoiceError as e:
            return UnavailableTTS(str(e))
    if engine_name == "licensed":
        prop_dir = Path(__file__).resolve().parent / "proprietary"
        hint = (f"no licensed voice model installed for profile "
                f"'{profile.name}'. See {prop_dir / 'README.md'}")
        return UnavailableTTS(hint)
    return UnavailableTTS(f"unknown TTS engine '{profile.engine}' "
                          f"for profile '{profile.name}'")


def active_profile() -> VoiceProfile:
    """The profile selected in config (``voice_profile``), defaulting safely."""
    from core.config import load_config
    cfg = load_config()
    profile = get_profile(cfg.get("voice_profile"))
    if profile is None:
        profile = get_profile("default")
    # get_profile("default") always exists (shipped JSON); guard anyway.
    if profile is None:
        return VoiceProfile(name="default", description="fallback",
                            engine="platform")
    return profile


def speak_text(text: str, profile: Optional[VoiceProfile] = None) -> None:
    """Speak ``text`` with the active (or given) profile. Honest on failure."""
    profile = profile or active_profile()
    engine = engine_for_profile(profile)
    engine.speak(text, profile)
