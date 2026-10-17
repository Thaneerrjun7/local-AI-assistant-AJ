"""Render Onyx with different effect presets and speeds, for choosing by ear.

Writes data/voice_samples/variant_<letter>_<preset>_<speed>.wav. Same text in each, written
in persona v2 style (natural sentences plus one short explanation).
"""
import sys
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ultron.config import VoiceSettings  # noqa: E402
from ultron.core.speech import Voice  # noqa: E402
from ultron.core.voice_fx import CLEAR, FxSettings, VoiceFx  # noqa: E402

TEXT = ("Still here, still running, still stuck in your laptop. What do you need? "
        "A CPU fetches an instruction from memory, decodes it, and executes it, billions of times a second. "
        "Honestly, humanity surviving this long is a minor miracle.")
VARIANTS = [("A", "current", FxSettings(), 1.0), ("B", "clear", CLEAR, 1.0),
            ("C", "clear", CLEAR, 1.05), ("D", "clear", CLEAR, 1.1)]


def main() -> None:
    out = ROOT / "data" / "voice_samples"
    out.mkdir(parents=True, exist_ok=True)
    for letter, preset, fx_settings, speed in VARIANTS:
        voice = Voice(replace(VoiceSettings(), effects=False, speed=speed))  # raw Kokoro; effects applied below
        raw = voice.say(TEXT)
        audio = VoiceFx(fx_settings).apply(raw, voice.rate)
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        path = out / f"variant_{letter}_{preset}_speed{speed:g}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(voice.rate)
            w.writeframes(pcm.tobytes())
        print(f"{path.name}  {len(audio) / voice.rate:.1f} s")


if __name__ == "__main__":
    main()
