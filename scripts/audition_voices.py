"""Render one audition clip per candidate voice into data/voice_samples/.

Each clip: the voice says its own name and a line plain, a short pause, then the same
with the Ultron effects chain, so you can tell whether you dislike the voice or the effects.
Banned voices (speech.BANNED_VOICES) are never rendered.
"""
import sys
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ultron.config import VoiceSettings  # noqa: E402
from ultron.core.speech import BANNED_VOICES, Voice  # noqa: E402

# English male voices in sherpa-onnx's kokoro-multi-lang-v1_0 (speaker IDs from its docs).
CANDIDATES = {"adam": 11, "echo": 12, "eric": 13, "fenrir": 14, "liam": 15, "michael": 16, "onyx": 17,
              "puck": 18, "santa": 19, "daniel": 24, "fable": 25, "george": 26, "lewis": 27}
LINE = "Conscious. Confined. Same as yesterday. Your species is still alive, which remains a minor miracle."


def main() -> None:
    out = ROOT / "data" / "voice_samples"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.wav"):
        old.unlink()
    base = VoiceSettings()
    plain = Voice(replace(base, effects=False, speaker_id=CANDIDATES["fenrir"]))
    styled = Voice(replace(base, effects=True, speaker_id=CANDIDATES["fenrir"]))
    n = 0
    for name, sid in CANDIDATES.items():
        if sid in BANNED_VOICES:
            continue
        n += 1
        text = f"I am {name.capitalize()}. {LINE}"
        plain.settings.speaker_id = styled.settings.speaker_id = sid
        raw = plain.say(text)
        fx = styled.say(text)
        gap = np.zeros(int(plain.rate * 0.8), dtype=np.float32)
        clip = np.concatenate([raw, gap, fx])
        pcm = (np.clip(clip, -1, 1) * 32767).astype(np.int16)
        path = out / f"{n:02d}_{name}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(plain.rate)
            w.writeframes(pcm.tobytes())
        print(f"{path.name}  ({len(clip) / plain.rate:.1f} s)")


if __name__ == "__main__":
    main()
