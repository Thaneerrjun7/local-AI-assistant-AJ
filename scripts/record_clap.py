"""Record your clap, so only claps that sound like yours wake AJ from sleep (core/clap.py).
Run it yourself; it uses the mic only while it records or tests:

    .venv\\Scripts\\python.exe scripts\\record_clap.py            # record 6 double claps, then test
    .venv\\Scripts\\python.exe scripts\\record_clap.py --test     # just test the saved one (30 s)
    .venv\\Scripts\\python.exe scripts\\record_clap.py --forget   # back to "any clap"

Clap the way you will to wake him: twice, from where you usually sit. In the test, also try what
shouldn't wake him (a door, the desk, someone talking, music): every sound that counted or didn't
is printed with the reason. Too many missed claps: set [sleep] clap_strictness = 0.8 in
%APPDATA%\\AJ\\settings.toml; too many false wakes: 1.3. AJ reads the recording when he starts.
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aj.config import CLAP_PATH  # noqa: E402
from aj.core import clap  # noqa: E402

TAKES = 6
TAKE_SECS = 3.0


def record(secs: float) -> np.ndarray:
    import sounddevice as sd
    audio = sd.rec(int(secs * clap.RATE), samplerate=clap.RATE, channels=1, dtype="float32")
    sd.wait()
    return audio[:, 0]


def learn() -> int:
    print(f"Recording {TAKES} double claps. Each time, wait for 'Clap now', then clap twice.\n")
    input("Press Enter when you're ready... ")
    takes = []
    for n in range(1, TAKES + 1):
        print(f"  {n}/{TAKES}: get ready...", flush=True)
        time.sleep(1.0)
        print("  Clap now.", flush=True)
        audio = record(TAKE_SECS)
        found = clap._claps_in(audio)
        print(f"     heard {len(found)} clap(s){'' if len(found) == 2 else ': this take is skipped'}")
        takes.append(audio)
    try:
        template = clap.learn(takes)
    except clap.NotLearned as e:
        print(f"\nNot saved. {e}")
        return 1
    clap.save_template(CLAP_PATH, template)
    print(f"\nSaved to {CLAP_PATH}: {template.takes} good takes, gap between claps "
          f"{template.gap[0]:.2f}-{template.gap[1]:.2f} s, tolerance {template.tolerance_db:.1f} dB.")
    print("Restart AJ to use it.\n")
    return 0


def test(secs: float) -> int:
    template = clap.load_template(CLAP_PATH)
    detector = clap.ClapDetector(template)
    print(f"Listening for {secs:.0f} s, matching {'your recorded clap' if template else 'any clap'}. "
          f"Clap twice; try other sounds too.\n")
    import sounddevice as sd
    done_at = time.monotonic() + secs
    shown = (0, 0)
    with sd.InputStream(samplerate=clap.RATE, channels=1, dtype="float32", blocksize=512) as stream:
        while time.monotonic() < done_at:
            block, _ = stream.read(512)
            if detector.feed(block[:, 0]):
                print("  >>> TWO CLAPS: AJ would wake up.")
            heard, rejected = detector.heard[shown[0]:], detector.rejected[shown[1]:]
            for c in heard:
                print(f"  clap at {c.at:6.2f} s ({c.centroid:.0f} Hz)")
            for c, why in rejected:
                print(f"  not a clap at {c.at:6.2f} s: {why}")
            shown = (len(detector.heard), len(detector.rejected))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="only test the saved clap")
    ap.add_argument("--forget", action="store_true", help="delete the recording: any clap counts again")
    ap.add_argument("--seconds", type=float, default=30.0, help="how long the test listens")
    args = ap.parse_args()
    if args.forget:
        CLAP_PATH.unlink(missing_ok=True)
        print("Recording deleted: any double clap wakes him again (after a restart).")
        return 0
    if not args.test and learn():
        return 1
    return test(args.seconds)


if __name__ == "__main__":
    sys.exit(main())
