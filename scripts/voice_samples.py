"""Render the same few lines in several voices, so the user can pick one by ear (2026-11-18:
"show me how the Kokoro 82M British male Lewis, Daniel and Fable sound, and how Piper sounds").

    .venv\\Scripts\\python.exe scripts\\voice_samples.py                 # the default set
    .venv\\Scripts\\python.exe scripts\\voice_samples.py --probe         # every Piper speaker: pitch, so men only

Each line goes through the same sentence chunker AJ speaks with, so the samples sound the way he
would. WAVs land in data\\voice_samples (not in git), one per voice, plus how long the first piece
took to render, which is how soon he would start talking.
"""
import argparse
import sys
import time
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aj.config import VoiceSettings  # noqa: E402
from aj.core.chunker import SentenceChunker  # noqa: E402
from aj.core.speech import PiperVoice, Voice  # noqa: E402

OUT = ROOT / "data" / "voice_samples"
LINES = [
    "Good evening, sir. Ready when you are.",
    "It's 31 degrees in Kuala Lumpur, sir, with rain likely after 4 PM.",
    "If I may, sir, staying up all night before an exam is a poor trade.",
    "Opening File Explorer. Sir, will there be anything else?",
    "Very good, sir. I'll be in the tray if you need me.",
]
STEADY = {"piper_noise": 0.45, "piper_noise_w": 0.6}  # less random delivery from piece to piece


def kokoro(sid: int) -> Voice:
    return Voice(replace(VoiceSettings(), speaker_id=sid, tts_provider="cpu"))


def piper(name: str, speaker: int = 0, steady: bool = True) -> PiperVoice:
    s = replace(VoiceSettings(), piper_voice=f"vits-piper-en_GB-{name}", piper_speaker=speaker)
    return PiperVoice(replace(s, **STEADY) if steady else s)


SETS = {
    "kokoro_lewis": lambda: kokoro(27),
    "kokoro_daniel": lambda: kokoro(24),
    "kokoro_fable": lambda: kokoro(25),
    "piper_now_northern": lambda: piper("northern_english_male-medium", steady=False),
}


def chunks(line: str) -> list[str]:
    c, out = SentenceChunker(), []
    for word in line.split(" "):
        out += c.feed(word + " ")
    return out + c.flush()


def render(voice, lines: list[str]) -> tuple[np.ndarray, float]:
    """All the lines, with a second between them, and the slowest first-piece render (ms)."""
    parts, first = [], 0.0
    for line in lines:
        for i, chunk in enumerate(chunks(line)):
            t0 = time.perf_counter()
            parts.append(voice.say(chunk))
            if i == 0:
                first = max(first, (time.perf_counter() - t0) * 1000)
        parts.append(np.zeros(int(voice.rate * 1.0), dtype=np.float32))
    return np.concatenate(parts), first


def save(path: Path, audio: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def pitch(audio: np.ndarray, rate: int) -> float:
    """Median pitch of the voiced parts, in Hz (men about 85-160, women about 165-255)."""
    frame, found = int(rate * 0.04), []
    lo, hi = int(rate / 300), int(rate / 70)
    for i in range(0, len(audio) - frame, frame):
        x = audio[i:i + frame] - audio[i:i + frame].mean()
        if np.sqrt(np.mean(x ** 2)) < 0.02:
            continue
        ac = np.correlate(x, x, "full")[frame - 1:]
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] > 0.4 * ac[0]:
            found.append(rate / lag)
    return float(np.median(found)) if found else 0.0


def probe() -> None:
    voices = [("alan-medium", 0), ("miro-high", 0), ("dii-high", 0), ("semaine-medium", 1), ("semaine-medium", 2),
              *(("southern_english_male-medium", n) for n in range(8)), ("northern_english_male-medium", 0)]
    for name, speaker in voices:
        v = piper(name, speaker)
        audio, ms = render(v, LINES[:1])
        print(f"{name:32} speaker {speaker}: pitch {pitch(audio, v.rate):5.0f} Hz, first piece {ms:4.0f} ms", flush=True)
        save(OUT / "probe" / f"{name}_{speaker}.wav", audio, v.rate)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--piper", action="append", default=[], metavar="NAME:SPEAKER",
                    help="add a Piper voice, e.g. alan-medium:0 or semaine-medium:2")
    args = ap.parse_args()
    if args.probe:
        probe()
        return 0
    sets = dict(SETS)
    for spec in args.piper:
        name, _, speaker = spec.partition(":")
        sets[f"piper_{name.split('-')[0]}_{speaker or 0}"] = lambda n=name, s=int(speaker or 0): piper(n, s)
    for label, make in sets.items():
        voice = make()
        audio, ms = render(voice, LINES)
        save(OUT / f"{label}.wav", audio, voice.rate)
        print(f"{label:28} {len(audio) / voice.rate:5.1f} s of speech, first piece in {ms:4.0f} ms "
              f"(slowest of {len(LINES)} lines)", flush=True)
    print(f"Saved in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
