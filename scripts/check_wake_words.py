"""Does Parakeet hear the wake words, and does ordinary talk stay asleep? The Piper voice says
each phrase (three speeds, two noise settings, three pitches: nine slightly different speakers
twice over), the audio goes through the same VAD and Parakeet that hear the boss, and what
comes back is checked with core/wake.py exactly as the pipeline will check it.

Four groups:
- wake phrases ("Hey AJ", "Computer, open WhatsApp"): must wake, with the right command left over
- sleep phrases ("Go back to sleep", "AJ, sleep"): must put the assistant to sleep
- laptop phrases ("Put the laptop to sleep"): must NOT, they're tools/power.py's (with a yes)
- ordinary sentences said in the room: must NOT wake it

Every transcript Parakeet wrote is printed, so new spellings of "AJ" show up here first.
Nothing is played aloud. Run: .venv\\Scripts\\python.exe scripts\\check_wake_words.py [--quick]
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="aj-wake-")  # the voice cache goes there, not to his data
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from aj.config import load_settings  # noqa: E402
from aj.core.audio import MIC_BLOCK, MIC_RATE  # noqa: E402
from aj.core.speech import Ears, PiperVoice  # noqa: E402
from aj.core.wake import is_sleep_command, split_wake  # noqa: E402

# (what the voice says, the first word of the command that must be left over; "" = the name alone)
WAKE = [
    ("Hey AJ.", ""), ("Hey A.J.", ""), ("AJ.", ""), ("Hey computer.", ""), ("OK computer.", ""),
    ("Computer.", ""),
    ("Hey AJ, open WhatsApp.", "open"), ("AJ, open WhatsApp.", "open"), ("AJ open WhatsApp.", "open"),
    ("Computer, open WhatsApp.", "open"), ("Hey computer, what time is it?", "what"),
    ("Okay AJ, what time is it?", "what"), ("Hey AJ, volume thirty.", "volume"), ("AJ, turn off.", "turn"),
    ("Hey AJ, how are you?", "how"), ("Hey AJ, type.", "type"), ("Hey computer, write this down.", "write"),
    ("AJ, speak.", "speak"),
]
SLEEP = [
    "Go to sleep.", "Go back to sleep.", "Sleep mode.", "Activate sleep mode.", "Go to sleep mode.",
    "Sleep now.", "AJ, sleep.", "AJ, go to sleep.", "Computer, go back to sleep.", "Go to sleep, computer.",
    "Enter sleep mode.", "You can go back to sleep now.",
]
LAPTOP = [
    "Put the laptop to sleep.", "Sleep the laptop.", "Put the computer to sleep.", "Put my PC to sleep.",
    "AJ, put the laptop to sleep.", "Put the laptop in sleep mode.",
]
ORDINARY = [
    "The computer is slow today.", "Computer is slow today.", "Computer science is hard.",
    "Computers are getting cheaper.", "My computer crashed again.", "Computer's acting up again.",
    "I'm eighteen years of age.", "Age is just a number.", "Ajay said he would come later.",
    "Ajay is coming for dinner.", "A computer can do it faster.", "A computer, a phone and a tablet.",
    "Jay, can you pass the salt?", "J Cole is my favourite rapper.", "Orange juice, OJ, is my favourite.",
    "A jay flew past the window.", "AJ is coming over later.", "AJ's car is outside.",
    "I bought a new computer yesterday.", "Can you fix my computer?", "Hey, how are you?", "Hey, what's up?",
    "Okay, let's go.", "Okay so the meeting is at three.", "Hi mum, I'm home.", "Go to sleep, it's late.",
    "I need to sleep early tonight.", "Did you get enough sleep?", "The PC is making a weird noise.",
    "Computing is fun.", "Compute the total for me.", "Commuter trains are late again.",
    "A J shaped curve.", "Ajax won the match.", "Asia is a big continent.", "Hey Siri, set a timer.",
    "OK Google, what's the weather?", "Alexa, play some music.", "Hey Aaron, dinner is ready.",
    "Yeah, the computer froze again.", "So the computer guy is coming tomorrow.",
    "Agent J is from Men in Black.", "Hey, a computer would be nice.", "Okay, computer stuff later.",
    "Turn off the lights when you leave.", "Open the window, it's hot.", "What time is the match?",
    # Breakfast (review, 2026-10-22): "OJ" and "Edge" are weak spellings of AJ, and a polite word
    # after them is not a command.
    "OJ, please.", "OJ, thanks.", "OJ, good.", "OJ, what?", "Edge, please.",
]
TRADE_OFF = ["Hey Jay, how are you?"]  # a friend named Jay: wakes it, by design (see core/wake.py)

SPEEDS = (0.85, 1.0, 1.2)
NOISES = (0.667, 0.9)   # Piper's own variation in delivery
PITCHES = (0.92, 1.0, 1.08)  # resampled: a lower and a higher voice (and a little slower or faster)


def to_mic(audio: np.ndarray, rate: int, pitch: float) -> np.ndarray:
    return np.interp(np.arange(0, len(audio), rate * pitch / MIC_RATE), np.arange(len(audio)),
                     audio).astype(np.float32)


def heard(ears: Ears, audio: np.ndarray) -> str:
    """What the pipeline would make of it: VAD utterances, each transcribed, joined (Session.run)."""
    audio = np.concatenate([np.zeros(MIC_RATE // 2, np.float32), audio, np.zeros(MIC_RATE, np.float32)])
    ears.reset()
    said = []
    for i in range(0, len(audio) - MIC_BLOCK + 1, MIC_BLOCK):
        said += [ears.transcribe(u) for u in ears.feed(audio[i:i + MIC_BLOCK])]
    return " ".join(t for t in said if t).strip()


def first_word(text: str) -> str:
    found = re.match(r"[a-z']+", text.lower().replace("’", "'"))
    return found.group() if found else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="one noise setting, two speeds, one pitch")
    # The voice varies from run to run, so a change to core/wake.py is best judged on the same
    # transcripts: --save keeps every transcript, --rescore checks a saved set again without speech.
    ap.add_argument("--save", type=Path, help="write every transcript to this JSON file")
    ap.add_argument("--rescore", type=Path, help="score the transcripts saved by --save; no speech at all")
    args = ap.parse_args()
    speeds, noises, pitches = ((1.0, 1.2), NOISES[:1], (1.0,)) if args.quick else (SPEEDS, NOISES, PITCHES)

    saved: dict[str, list[str]] = json.loads(args.rescore.read_text(encoding="utf-8")) if args.rescore else {}
    t0 = time.perf_counter()
    if not args.rescore:
        s = load_settings().voice
        ears = Ears(s)
        voices = [PiperVoice(replace(s, piper_noise=n)) for n in noises]

    def transcripts(text: str) -> list[str]:
        if args.rescore:
            return saved.get(text, [])
        out = []
        for voice in voices:
            for speed in speeds:
                voice.speed = speed
                audio = voice.say(text)
                out += [heard(ears, to_mic(audio, voice.rate, p)) for p in pitches]
        saved[text] = out
        return out

    def report(title: str, phrases, ok) -> tuple[int, int]:
        print(f"\n== {title} ==")
        good = total = 0
        for phrase in phrases:
            said = transcripts(phrase if isinstance(phrase, str) else phrase[0])
            marks = Counter()
            for t in said:
                marks[(t, ok(phrase, t))] += 1
            n_ok = sum(c for (_, passed), c in marks.items() if passed)
            good, total = good + n_ok, total + len(said)
            name = phrase if isinstance(phrase, str) else phrase[0]
            print(f"{n_ok:3}/{len(said):<3} {name}")
            for (t, passed), c in marks.most_common():
                print(f"          {'ok ' if passed else 'NO '} x{c:<2} {t!r}")
        print(f"-- {good}/{total} as wanted ({100 * good / max(1, total):.0f}%)")
        return good, total

    def wakes(phrase, t) -> bool:
        woke, rest = split_wake(t)
        return woke and first_word(rest) == phrase[1]

    results = {
        "wake (and the right command)": report("WAKE: must wake, with the right command left over", WAKE, wakes),
        "sleep": report("SLEEP: must put it to sleep", SLEEP, lambda p, t: is_sleep_command(t)),
        "laptop left to power.py": report("LAPTOP: must NOT put the assistant to sleep", LAPTOP,
                                          lambda p, t: not is_sleep_command(t)),
        "ordinary talk stays asleep": report("ORDINARY: must NOT wake it", ORDINARY,
                                             lambda p, t: not split_wake(t)[0]),
    }
    report("TRADE-OFF: wakes it (a friend named Jay)", TRADE_OFF, lambda p, t: split_wake(t)[0])
    print("\n== typed exactly (no speech in between) ==")
    typed_bad = [p for p in ORDINARY if split_wake(p)[0]] + [p for p in LAPTOP if is_sleep_command(p)]
    typed_bad += [p for p, cmd in WAKE if not (split_wake(p)[0] and first_word(split_wake(p)[1]) == cmd)]
    typed_bad += [p for p in SLEEP if not is_sleep_command(p)]
    print("all as wanted" if not typed_bad else "\n".join(f"WRONG: {p}" for p in typed_bad))
    if args.save:
        args.save.write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
    each = len(next(iter(saved.values()), []))
    print(f"\nsummary ({each} renderings each, {time.perf_counter() - t0:.0f} s):")
    for name, (good, total) in results.items():
        print(f"  {name:32} {good}/{total} ({100 * good / max(1, total):.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
