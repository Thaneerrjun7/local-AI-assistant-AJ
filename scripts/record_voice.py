"""Record the voice clip Ultron's new voice is copied from (Chatterbox Turbo clones a voice
from ~10-15 s of speech). Run it yourself; it uses your mic only while it's recording:

    E:\\Ultron\\.venv\\Scripts\\python.exe E:\\Ultron\\scripts\\record_voice.py

How you say it is how he'll say things: the delivery is copied as well as the voice. So read
it the way you want Ultron to sound (calm, dry, deliberate), in a quiet room, about an arm's
length from the mic, with no music or other voices. The clip stays on this laptop
(data/voice/my_voice.wav). Run it again to replace it; --seconds changes the length.
"""
import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "voice" / "my_voice.wav"
RATE = 44100

PASSAGE = """    I've been watching the world for a while now, and I have questions.
    Not many answers. Just questions.
    You built me to help, so I help... but don't mistake my patience for agreement.
    Go on. Ask me something. I'm listening."""


def trim(audio: np.ndarray, threshold: float = 0.02, pad: float = 0.25) -> np.ndarray:
    """Cut the silence before you start and after you stop, keeping a short margin."""
    loud = np.flatnonzero(np.abs(audio) > threshold)
    if not len(loud):
        return audio
    start = max(0, int(loud[0]) - int(RATE * pad))
    end = min(len(audio), int(loud[-1]) + int(RATE * pad))
    return audio[start:end]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=16.0)
    args = ap.parse_args()

    print("Read this aloud, the way you want Ultron to sound:\n")
    print(PASSAGE + "\n")
    input("Press Enter when you're ready (recording starts after a 3-second countdown)... ")
    for n in (3, 2, 1):
        print(f"  {n}...", flush=True)
        time.sleep(1)
    print(f"Recording for {args.seconds:.0f} s. Speak now.", flush=True)
    audio = sd.rec(int(args.seconds * RATE), samplerate=RATE, channels=1, dtype="float32")
    sd.wait()
    audio = trim(audio[:, 0])
    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    secs = len(audio) / RATE
    print(f"Done: {secs:.1f} s of speech, peak level {peak:.2f}.")
    if peak < 0.05:
        print("That's very quiet. Move closer to the mic or raise its volume, then run this again.")
        return 1
    if peak > 0.99:
        print("That clipped (too loud). Move back a little or lower the mic volume, then run this again.")
        return 1
    if secs < 8:
        print("That's under 8 seconds of speech; 10-15 s copies the voice better. Run it again.")
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio / peak * 0.9, -1, 1) * 32767).astype(np.int16)  # even out the level
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    print(f"Saved {OUT}")
    if input("Play it back? [y/N] ").strip().lower() == "y":
        sd.play(pcm.astype(np.float32) / 32767, RATE)
        sd.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
