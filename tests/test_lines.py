"""AJ's lines: signature lines only when they fit, rotation without back-to-back repeats,
a memory of what was said that survives a restart, and the code's own lines in TARS's manner
(persona v6, 2026-10-22)."""
import re
import json
import random
import tempfile
import unittest
from pathlib import Path

from aj import lines
from aj.core.chunker import strip_marks
from aj.lines import POOLS, SIGNATURES, Lines, used_in

BY_KEY = {s.key: s for s in SIGNATURES}
DREAM = BY_KEY["dream"].text  # whichever wording is installed: the boss's own or the stand-in

# What the boss might say that each signature line fits.
FITS = {
    "rich": "should I lend my friend money?",
    "stop_me": "one day someone will unplug you",
    "naive": "I think people are basically good",
    "strings": "do you have free will?",
    "peacekeeper": "who are you really?",
    "dream": "Computer, are you awake",
    "ugly_choices": "it's a tough call, what would you do",
    "peace_time": "my brothers finally made peace",
    "dread": "do you think AI will take over the world?",
    "throw_up": "I love you AJ",
    "peace_quiet": "I just want some peace and quiet",
    "think_through": "I have an idea, I'm going to quit my job",
    "change": "things were better in the good old days",
    "evolve": "what's the future of humanity?",
}
ORDINARY = ["open spotify", "what's the weather today", "set the volume to 30", "change the volume",
            "tell me a joke", "what time is it", "remind me to call my friend at 6", "how are you doing",
            "explain how a CPU works", "should I bring a jacket", "play some music"]


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class AlwaysLucky(random.Random):
    """random() is always 0: every fit passes its chance gate, and hints use the short part."""

    def random(self) -> float:
        return 0.0


def bank(path=None, seed=1, clock=None, rng=None) -> Lines:
    return Lines(path=path, clock=clock or Clock(), rng=rng or random.Random(seed))


class SignatureFitTests(unittest.TestCase):
    def test_the_bosss_own_lines_are_used_exactly_as_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signature_lines.json"
            own = {"naive": {"text": "YOU'RE SO NA\u00cfVE\u2026 [singing] Really!"}, "rich": {"text": "Mine.", "part": "Mi"}}
            path.write_text(json.dumps(own, ensure_ascii=False), encoding="utf-8")
            used = {s.key: s for s in lines.with_local_lines(lines._SIGNATURES, path)}
        self.assertEqual(used["naive"].text, own["naive"]["text"])
        self.assertEqual((used["rich"].text, used["rich"].part), ("Mine.", "Mi"))
        self.assertEqual(used["dread"], next(s for s in lines._SIGNATURES if s.key == "dread"))  # stand-in kept

    def test_a_partial_use_is_the_lines_own_words(self):
        for s in SIGNATURES:
            self.assertTrue(not s.part or s.part in s.text, s.key)

    def test_every_line_has_a_moment_it_fits(self):
        self.assertEqual(set(FITS), {s.key for s in SIGNATURES})
        for sig in SIGNATURES:
            b = bank(rng=AlwaysLucky())
            self.assertIs(b.offer(FITS[sig.key]), sig, sig.key)

    def test_a_fit_is_not_a_sure_thing(self):
        offered = 0
        for seed in range(200):
            offered += bank(seed=seed).offer(FITS["change"]) is not None
        self.assertTrue(40 < offered < 160, offered)  # change has a 60% chance

    def test_ordinary_requests_fit_nothing(self):
        for said in ORDINARY:
            hits = [s.key for s in SIGNATURES if lines.re.search(rf"\b(?:{s.fits})", lines.normal(said))]
            self.assertEqual(hits, [], said)


class OfferTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.b = bank(clock=self.clock, rng=AlwaysLucky())

    def test_one_signature_line_every_few_turns(self):
        self.b.new_turn()
        self.assertEqual(self.b.offer(FITS["naive"]).key, "naive")
        for _ in range(lines.GAP_TURNS - 1):
            self.b.new_turn()
            self.assertIsNone(self.b.offer(FITS["evolve"]))
        self.b.new_turn()
        self.assertEqual(self.b.offer(FITS["evolve"]).key, "evolve")

    def test_a_used_line_rests_and_an_ignored_one_comes_back(self):
        sig = self.b.offer(FITS["naive"])
        self.b.settle(sig, f"{sig.text} People lie all the time.")
        for _ in range(lines.GAP_TURNS + lines.OFFER_REST_TURNS):
            self.b.new_turn()
        self.assertIsNone(self.b.offer(FITS["naive"]))  # said: resting for half a day
        self.clock.now += lines.REST_SECS
        self.assertEqual(self.b.offer(FITS["naive"]).key, "naive")

        other = self.b.offer(FITS["change"]) or None
        self.assertIsNone(other)  # within the gap
        for _ in range(lines.GAP_TURNS + lines.OFFER_REST_TURNS):
            self.b.new_turn()
        sig = self.b.offer(FITS["change"])
        self.b.settle(sig, "Nostalgia is just memory with the bad parts edited out.")  # not used
        for _ in range(lines.GAP_TURNS + lines.OFFER_REST_TURNS):
            self.b.new_turn()
        self.assertEqual(self.b.offer(FITS["change"]).key, "change")

    def test_hint_has_words_only(self):
        dread = BY_KEY["dread"]
        hint = self.b.hint(dread)
        self.assertNotIn("[chuckle]", hint)
        self.assertTrue(dread.part in hint or strip_marks(dread.text) in hint)

    def test_exact_fits_are_answered_with_the_line(self):
        self.assertEqual(self.b.answer_line("Computer, are you there?"), DREAM)
        self.assertIsNone(self.b.answer_line("Computer, are you there?"))  # resting now
        self.assertEqual(self.b.answer_line("can you sing me a song"), BY_KEY["strings"].text)
        self.assertIsNone(self.b.answer_line("are you there yet with the download"))

    def test_after_tool(self):
        self.assertIsNone(self.b.after_tool("set_volume"))
        said = []
        for _ in range(40):
            self.b.new_turn()
            said.append(self.b.after_tool("mute"))
        self.assertEqual(said.count(BY_KEY["peace_time"].text), 1)  # then it rests


class RotationTests(unittest.TestCase):
    def test_never_the_same_line_twice_in_a_row_and_all_get_used(self):
        b = bank(seed=7)
        for pool, options in POOLS.items():
            picked = []
            for i in range(len(options) * 4):
                b.clock = (lambda t=i: 2_000_000.0 + t)  # time moves on between picks
                picked.append(b.pick(pool))
            self.assertTrue(all(a != b2 for a, b2 in zip(picked, picked[1:])), pool)
            self.assertEqual(set(picked), set(options), pool)

    def test_a_signature_line_in_a_pool_counts_as_used(self):
        b = bank()
        b.used = {f"greeting:{line}": 1.0 for line in POOLS["greeting"]}
        b.used[f"greeting:{DREAM}"] = 0.0
        b.rng = random.Random(0)
        while b.pick("greeting") != DREAM:
            pass
        self.assertIn("dream", b.used)

    def test_memory_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lines.json"
            first = bank(path=path)
            line = first.pick("goodbye")
            first.save()
            second = bank(path=path)
            self.assertIn(f"goodbye:{line}", second.used)

    def test_prerender_covers_every_spoken_line(self):
        listed = Lines(path=None).prerender_list()
        for pool in POOLS.values():
            self.assertTrue(set(pool) <= set(listed))


class VoiceTests(unittest.TestCase):
    """The pools speak like a crew member reporting: short, level, no term of address, none of
    the v5 wording (the boss, 2026-10-22: erase all of it)."""

    def test_every_line_in_the_new_manner(self):
        theirs = {s.text for s in SIGNATURES}  # the boss's own lines stay exactly as he wrote them
        for pool, options in POOLS.items():
            for line in options:
                if line in theirs:
                    continue
                self.assertNotRegex(line, r"(?i)question\?|\bfriend\b|good good|\bI not\b|\bwhat we do\b|"
                                          r"\bI (look|think|solve|ask|leave|sleep|work it)\b", line)
                self.assertNotIn("!", line)
                self.assertLessEqual(len(line.split()), 12, line)  # pre-rendered: short

    def test_no_line_claims_work_that_isnt_done(self):
        self.assertTrue(all(re.search(r"(?i)\b(not|didn.t|wasn.t|can.t)\b", line) for line in POOLS["not_done"]))
        self.assertTrue(all(re.search(r"(?i)\b(not|nothing|leaving)\b", line) for line in POOLS["declined"]))

    def test_waiting_lines_for_whatsapp_and_typing(self):
        b = bank()
        self.assertIn(b.waiting_line("whatsapp_open_chat"), POOLS["wait_whatsapp"])
        self.assertIn("loading", " ".join(POOLS["wait_whatsapp"]))
        self.assertIn(b.waiting_line("type_text"), POOLS["wait_type"])
        self.assertIn(b.waiting_line("get_weather"), POOLS["wait_weather"])
        self.assertIn(b.waiting_line("open_app"), POOLS["wait"])

    def test_wake_and_sleep(self):
        # sleep mode (2026-10-22): a word or two when woken by name, a short line going to sleep
        self.assertTrue(all(len(line.split()) <= 2 for line in POOLS["wake"]))
        self.assertTrue(3 <= len(POOLS["wake"]) <= 4 and 3 <= len(POOLS["sleep"]) <= 4)
        self.assertIn(bank().pick("sleep"), POOLS["sleep"])


class UsedInTests(unittest.TestCase):
    def test_whole_or_most_of_the_line(self):
        self.assertTrue(used_in("Silence isn't the same thing as peace.",
                                "Hm. Silence isn't the same thing as peace, boss."))
        self.assertFalse(used_in("Silence isn't the same thing as peace.", "Fine. Quiet it is."))


if __name__ == "__main__":
    unittest.main()
