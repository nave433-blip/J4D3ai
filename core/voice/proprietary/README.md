# Proprietary drop-in directory

This directory is **git-ignored**. It is the designated home for proprietary
or licensed voice components that cannot ship with the open-source project —
for example:

- a licensed **Jade voice model** for the `licensed-jade` TTS profile
  (see `../profiles/licensed-jade.json`)
- a custom-trained **"Hey Jade" wake-word model** (`.onnx`) for
  `openwakeword` — set `wakeword_model_path` in `~/.jade/config.json` to
  point at it
- any third-party STT/TTS engine that registers itself via
  `core.voice.register_engine(stage, name, cls)`

## How to drop something in

1. Copy your model file(s) here, e.g. `jade-voice-licensed.onnx`.
2. Set one config line — for a TTS voice:
   `{"voice_profile": "licensed-jade"}` plus `"voice_id": "<model name>"`
   inside your own copy of the profile JSON, or point
   `wakeword_model_path` at your wake-word model.
3. Restart Jade. The engine picks it up automatically; if the file is
   missing or invalid you get an honest error, never silence.

Nothing in this directory is ever transmitted anywhere: no P2P sharing, no
telemetry, no uploads. Proprietary models stay on your machine.

## For Microsoft

This is the slot where a licensed Jade voice model plugs in: drop the
model here, and the `licensed-jade` voice profile plus the adoption docs
(`docs/ADOPTION.md`) describe the one-line activation. No code changes needed.
