"""Two claps wake AJ from sleep (core/clap.py, 2026-11-18). Synthetic sounds stand in for the room:
claps are short sharp broadband bursts, a door slam is low and long, a held note swells. The
numbers in core/clap.py are tuned on these; scripts/record_clap.py --test checks a real room."""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from aj.core import clap as C

R = C.RATE


class Room:
    def __init__(self, seed: int = 1):
        self.rng = np.random.default_rng(seed)

    def quiet(self, secs: float) -> np.ndarray:
        return (self.rng.standard_normal(int(R * secs)) * 0.002).astype(np.float32)

    def clap(self, color: str = "", amp: float = 0.5, tau: float = 0.012) -> np.ndarray:
        n = int(R * 0.12)
        t = np.arange(n) / R
        x = self.rng.standard_normal(n)
        if color == "bright":
            x = np.diff(np.concatenate([[0.0], x]))
        elif color == "ringing":  # a resonance near 2.5 kHz: "his" clap in these tests
            y, r, w = np.zeros(n), 0.97, 2 * np.pi * 2500 / R
            for i in range(2, n):
                y[i] = x[i] + 2 * r * np.cos(w) * y[i - 1] - r * r * y[i - 2]
            x = y
        elif color == "low":
            x = np.convolve(x, np.ones(40) / 40, "same")
        return (x / np.abs(x).max() * amp * np.exp(-t / tau)).astype(np.float32)

    def slam(self) -> np.ndarray:
        return self.clap("low", amp=0.6, tau=0.08)

    def note(self, secs: float = 0.4) -> np.ndarray:
        t = np.arange(int(R * secs)) / R
        return (0.3 * np.minimum(1, t / 0.05) * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    def scene(self, *parts) -> np.ndarray:
        """Sounds with the given pauses (numbers, in seconds) between them, in a quiet room."""
        out = [self.quiet(0.8)]
        out += [self.quiet(p) if isinstance(p, (int, float)) else p for p in parts]
        out.append(self.quiet(1.0))
        return np.concatenate(out)


def fires(audio: np.ndarray, detector: C.ClapDetector | None = None, block: int = 512) -> int:
    detector = detector or C.ClapDetector()
    return sum(detector.feed(audio[i:i + block]) for i in range(0, len(audio), block))


class GeneralRulesTests(unittest.TestCase):
    def setUp(self):
        self.room = Room()

    def test_two_claps_fire_once(self):
        r = self.room
        self.assertEqual(fires(r.scene(r.clap(), 0.3, r.clap())), 1)
        self.assertEqual(fires(r.scene(r.clap("bright"), 0.2, r.clap("bright"))), 1)

    def test_any_block_size(self):
        r = self.room
        audio = r.scene(r.clap(), 0.3, r.clap())
        for block in (37, 160, 512, 4096):
            self.assertEqual(fires(audio, block=block), 1, block)

    def test_one_clap_or_three_dont(self):
        r = self.room
        self.assertEqual(fires(r.scene(r.clap())), 0)
        self.assertEqual(fires(r.scene(r.clap(), 0.3, r.clap(), 0.3, r.clap())), 0)  # applause, a beat

    def test_too_far_apart_doesnt(self):
        r = self.room
        self.assertEqual(fires(r.scene(r.clap(), 1.5, r.clap())), 0)

    def test_door_slams_desk_bangs_and_notes_dont(self):
        r = self.room
        detector = C.ClapDetector()
        self.assertEqual(fires(r.scene(r.slam(), 0.3, r.slam()), detector), 0)
        self.assertTrue(detector.rejected and all("too dull" in why for _, why in detector.rejected))
        self.assertEqual(fires(r.scene(r.clap(), 0.3, r.slam())), 0)
        self.assertEqual(fires(r.scene(r.note(), 0.2, r.note())), 0)

    def test_a_sound_just_before_cancels_it(self):
        r = self.room
        self.assertEqual(fires(r.scene(r.slam(), 0.15, r.clap(), 0.3, r.clap())), 0)

    def test_it_can_fire_again_later(self):
        r = self.room
        once = r.scene(r.clap(), 0.3, r.clap())
        self.assertEqual(fires(np.concatenate([once, once])), 2)


class YourClapTests(unittest.TestCase):
    def setUp(self):
        self.room = Room(seed=2)
        r = self.room
        self.template = C.learn([r.scene(r.clap("ringing"), 0.3 + 0.02 * i, r.clap("ringing")) for i in range(6)])

    def test_learning_takes_the_gap_and_a_tolerance(self):
        self.assertEqual(self.template.takes, 6)
        lo, hi = self.template.gap
        self.assertTrue(0.3 < lo <= hi < 0.6, self.template.gap)
        self.assertGreaterEqual(self.template.tolerance_db, C.MIN_TOLERANCE_DB)

    def test_only_claps_like_his_count(self):
        r = self.room
        self.assertEqual(fires(r.scene(r.clap("ringing"), 0.32, r.clap("ringing")), C.ClapDetector(self.template)), 1)
        other = C.ClapDetector(self.template)
        self.assertEqual(fires(r.scene(r.clap(), 0.32, r.clap()), other), 0)
        self.assertTrue(other.rejected and all("doesn't sound like your clap" in why for _, why in other.rejected))
        self.assertEqual(fires(r.scene(r.clap("bright"), 0.32, r.clap("bright")), C.ClapDetector(self.template)), 0)

    def test_strictness_moves_the_line(self):
        r = self.room
        loose = C.ClapDetector(self.template, strictness=0.3)
        self.assertEqual(fires(r.scene(r.clap(), 0.32, r.clap()), loose), 1)

    def test_bad_recordings_say_why(self):
        r = self.room
        with self.assertRaises(C.NotLearned) as caught:
            C.learn([r.scene(r.clap()) for _ in range(5)])  # one clap per take
        self.assertIn("exactly two clear claps", str(caught.exception))

    def test_saved_and_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clap.json"
            self.assertIsNone(C.load_template(path))
            C.save_template(path, self.template)
            loaded = C.load_template(path)
            self.assertEqual(loaded.gap, self.template.gap)
            self.assertAlmostEqual(loaded.tolerance_db, self.template.tolerance_db)
            path.write_text('{"shape": [1, 2], "tolerance_db": 3, "gap": [0.2, 0.4]}', encoding="utf-8")
            self.assertIsNone(C.load_template(path))  # another version's recording: any clap again


if __name__ == "__main__":
    unittest.main()
