"""Render test phrases with a plain Kokoro voice into data/test_audio/*.wav.

They stand in for the user's voice in end-to-end runs:
  python -m aj --wav data/test_audio/*.wav
"""
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from aj.config import VoiceSettings  # noqa: E402
from aj.core.speech import PiperVoice  # noqa: E402

PHRASES = {
    "01_how_are_you": "Hey Computer, how are you doing?",
    "02_joke": "Tell me a joke.",
    "03_humans": "What do you think of humans?",
    "04_time": "What time is it?",
    "05_tip": "Calculate an eighteen percent tip on eighty seven dollars sixty, split four ways.",
    "06_weather": "What's the weather in Toronto?",
    "07_sky": "Why is the sky blue?",
    "08_rain": "Does it rain today?",  # a follow-up to 06: should be answered, not the report again
    "09_there": "Computer, are you there?",
}


def main() -> None:
    out = ROOT / "data" / "test_audio"
    out.mkdir(parents=True, exist_ok=True)
    voice = PiperVoice(VoiceSettings())  # the voice he speaks with: standing in for the boss
    for name, text in PHRASES.items():
        samples = voice.say(text)
        pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
        with wave.open(str(out / f"{name}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(voice.rate)
            w.writeframes(pcm.tobytes())
        print(f"{name}.wav  {len(samples) / voice.rate:.1f} s  {text!r}")


if __name__ == "__main__":
    main()
