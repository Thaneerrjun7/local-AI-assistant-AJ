"""Does the voice say every word clearly? It speaks each test sentence (exactly as a reply would
be spoken: the same clean-up and voice settings), then Parakeet, the speech-to-text that hears
the boss, writes down what it heard. Words that come back different are words said unclearly.

Nothing is played aloud. Run: .venv\\Scripts\\python.exe scripts\\check_pronunciation.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="aj-pron-")
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from aj.config import load_settings  # noqa: E402
from aj.core.chunker import clean_for_speech  # noqa: E402
from aj.core.speech import Ears, PiperVoice, Voice  # noqa: E402

SENTENCES = [
    # his everyday replies
    "Opening WhatsApp.", "Switching to Google Chrome.", "Locked.", "Closed Spotify.",
    "Battery at 80%, plugged in.", "Volume at 30%.", "Brightness at 50%.",
    "It's 10:28 PM on Monday, October 19th, 2026.",
    "Reminder set for 7:30 AM tomorrow: call mom.", "Timer set for 5 minutes.",
    "Here today: 27°C now, high 31°C, low 25°C, thunderstorms this afternoon, 80% chance of rain.",
    "C: 120 GB free of 475 GB.", "Shutting down in 30 seconds. Say cancel the shutdown to stop it.",
    "I can't send messages yet.", "Sure. What would you like me to open?",
    # numbers and units
    "That's 1,234 files in 3.5 seconds.", "The CPU is at 12%, and RAM is 71% used.",
    "It weighs about 2.5 kg and costs $799.", "The download is 390 MB, about 4 minutes.",
    "It runs at 2.4 GHz with 16 GB of RAM.", "The sun is about 5,500 degrees Celsius at the surface.",
    # short forms and symbols
    "The USB drive, the PDF and the AI model are ready.", "Use Wi-Fi or Bluetooth, e.g. for headphones.",
    "Tom & Jerry vs. the world, etc.", "Open google.com or github.com.",
    # names and apps
    "Opening File Explorer.", "Opening YouTube.", "Microsoft Edge, Discord and Steam are open.",
    # explanations
    "A CPU fetches each instruction from memory, works out what it means, and carries it out.",
    "Vaccines show your immune system a harmless copy of a germ, so it learns what to look for.",
    "The sky looks blue because air scatters blue light more than red light.",
    # the conversation
    "You said you're going to school later, but you didn't say which school.",
    "Hmm. I don't know that yet. Want me to look it up?",
    "Good question. I think the answer is yes.",
    # "good" sounded like "guv" and "RAM" like "rum" to the boss (2026-10-22): see chunker._SAY_AS
    "Good.", "Good, that's done.", "Good. Opening it now.", "Sounds good.", "Not good.", "Very good.",
    "Good morning.", "That's a good question.", "Good good good.", "That looks good to me.",
    "Your RAM is fine.", "RAM usage is 68 percent.", "Close a few tabs to free up RAM.",
    "I checked the RAM now. Current RAM usage: 68 percent.",
]

_SMALL = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
          "sixteen seventeen eighteen nineteen").split()
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
_SAME = {"ok": "okay", "mum": "mom", "colour": "color", "wifi": "wi fi", "e g": "eg", "gigabytes": "gb",
         "megabytes": "mb", "kilograms": "kg", "gigahertz": "ghz", "percent": "%"}


def canon(text: str) -> list[str]:
    """Words to compare: lowercase, no punctuation, number words as digits."""
    text = text.lower().replace("%", " percent ").replace("°c", " degrees celsius ").replace("&", " and ")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    words = re.findall(r"[a-z]+|\d+(?:\.\d+)?", text)
    out, number = [], None
    for w in words:
        value = _SMALL.index(w) if w in _SMALL else _TENS.get(w)
        if value is not None:
            number = (number or 0) + value if number is None or number % 10 == 0 and value < 10 or number == 0 \
                else None
            if number is None:
                number = value
            continue
        if w in ("hundred", "thousand") and number is not None:
            number *= 100 if w == "hundred" else 1000
            continue
        if number is not None:
            out.append(str(number))
            number = None
        out.append(_SAME.get(w, w))
    if number is not None:
        out.append(str(number))
    return out


def errors(want: list[str], got: list[str]) -> int:
    """Word edit distance."""
    row = list(range(len(got) + 1))
    for i, w in enumerate(want, 1):
        prev, row[0] = row[0], i
        for j, g in enumerate(got, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (w != g))
    return row[-1]


def main() -> None:
    s = load_settings().voice
    voice = PiperVoice(s) if s.engine == "piper" else Voice(s)
    ears = Ears(s)
    bad, total_words, total_errors = [], 0, 0
    for sentence in SENTENCES:
        spoken = clean_for_speech(sentence)
        audio = voice.say(spoken)
        heard = ears.transcribe(np.interp(np.arange(0, len(audio), voice.rate / 16000), np.arange(len(audio)),
                                          audio).astype(np.float32))
        want, got = canon(spoken), canon(heard)
        e = errors(want, got)
        total_words, total_errors = total_words + len(want), total_errors + e
        if e:
            bad.append((e / max(1, len(want)), sentence, heard))
    print(f"voice: {s.engine} {s.piper_voice if s.engine == 'piper' else s.speaker_id}")
    print(f"{len(SENTENCES)} sentences, {total_words} words, {total_errors} heard differently "
          f"({100 * (1 - total_errors / total_words):.1f}% clear)")
    for rate, sentence, heard in sorted(bad, reverse=True):
        print(f"  {rate:4.0%}  said: {sentence}\n        heard: {heard}")


if __name__ == "__main__":
    main()
