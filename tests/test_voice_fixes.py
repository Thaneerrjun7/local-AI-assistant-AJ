"""The fixes of 2026-11-18, after the user's first try of the new character: "sir" kept inside its
sentence, his own mic claims dropped, the voice-control phrases, more room for the model and
less history when it's still too much."""
import io
import unittest
import urllib.error
from unittest import mock

from aj.config import LlmSettings, Settings
from aj.core import brain as brain_mod
from aj.core.chunker import SentenceChunker, join_address
from aj.core.delivery import action_claims
from aj.core.llm import server_command
from aj.core.pipeline import voice_control
from aj.core.wake import split_wake


def chunked(text: str, size: int = 0) -> list[str]:
    c, out = SentenceChunker(), []
    pieces = [text[i:i + size] for i in range(0, len(text), size)] if size else [w + " " for w in text.split(" ")]
    for piece in pieces:
        out += c.feed(piece)
    return out + c.flush()


class SirTests(unittest.TestCase):
    """ "His sir looks like it's someone else speaking completely": each piece is voiced alone."""

    def test_never_a_cut_just_before_sir(self):
        for size in (0, 1, 3, 7):
            parts = chunked("Because I'm a British AI, sir, and a bit of a posh machine. No accent at all.", size)
            self.assertTrue(all(not p.lower().startswith(("sir", "boss")) for p in parts), parts)
            self.assertIn("Because I'm a British AI, sir,", parts)

    def test_a_lone_sir_joins_the_sentence_before(self):
        for size in (0, 1, 2, 5):
            self.assertEqual(chunked("Why did the coffee file a police report? It got mugged! Sir.", size),
                             ["Why did the coffee file a police report?", "It got mugged, sir."])
        self.assertEqual(join_address("Done. Boss."), "Done, boss.")

    def test_sir_starting_a_real_sentence_stays(self):
        self.assertEqual(chunked("Hello, sir. Sir, will there be anything else?"),
                         ["Hello, sir.", "Sir, will there be anything else?"])
        self.assertEqual(chunked("Sirius is a star. Sir Isaac Newton was clever."),
                         ["Sirius is a star.", "Sir Isaac Newton was clever."])


class MicClaimTests(unittest.TestCase):
    def test_saying_his_mic_is_off_is_never_true(self):
        for said in ["Mic is off, sir.", "Yes, sir. The mic is off.", "I've turned off the microphone.",
                     "Silent now. Mic is off, sir."]:
            self.assertIn(None, action_claims(said.split(". ")[-1]), said)

    def test_telling_him_how_is_not_a_claim(self):
        self.assertNotIn(None, action_claims("Say turn off the mic and it's done."))
        self.assertNotIn(None, action_claims("Click the mic button to turn it off."))


class VoiceControlTests(unittest.TestCase):
    def test_mic_off(self):
        for said in ["off your mic.", "Turn off the mic.", "turn the mic off please", "mute", "Mute.", "mute me",
                     "mute myself", "stop listening", "AJ, mute", "mic off", "can you turn off your microphone"]:
            self.assertEqual(voice_control(split_wake(said)[1]), "mic off", said)

    def test_hush(self):
        for said in ["mute yourself", "Stop talking.", "be quiet", "shut up", "hush"]:
            self.assertEqual(voice_control(said), "hush", said)

    def test_everything_else_goes_on_as_before(self):
        for said in ["mute the volume", "mute the laptop", "unmute", "turn off the lights", "the mic is broken",
                     "quiet down the music", "turn on the mic", "what's my mic called"]:
            self.assertIsNone(voice_control(said), said)


class RoomTests(unittest.TestCase):
    def test_one_kv_pool_shared_by_the_two_slots(self):
        cmd = server_command(LlmSettings())
        self.assertIn("-kvu", cmd)
        self.assertEqual(cmd[cmd.index("-np") + 1], "2")

    def test_too_long_drops_the_oldest_history_and_tries_again(self):
        b = brain_mod.Brain("http://x", Settings())
        b._tools = []
        b.history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(12)]
        sent = []

        def model(base, messages, *a, **k):
            sent.append(len(messages))
            if len(sent) == 1:
                raise urllib.error.HTTPError("http://x", 400, "too long", {}, io.BytesIO(b""))
            return iter([("text", "Fine, sir.")])

        with mock.patch.object(brain_mod, "stream_events", side_effect=model), \
                mock.patch("aj.tools.intents.match", return_value=None), mock.patch("builtins.print"):
            said = b.reply("how are you", None, mock.Mock(first_token=0, first_chunk=0))
        self.assertEqual(said, "Fine, sir.")
        self.assertEqual(len(sent), 2)
        self.assertLess(sent[1], sent[0])  # the second try had less history in it

    def test_nothing_left_to_drop_raises(self):
        b = brain_mod.Brain("http://x", Settings())
        b._tools = []
        error = urllib.error.HTTPError("http://x", 400, "too long", {}, io.BytesIO(b""))
        with mock.patch.object(brain_mod, "stream_events", side_effect=error), \
                mock.patch("aj.tools.intents.match", return_value=None):
            with self.assertRaises(urllib.error.HTTPError):
                b.reply("how are you", None, mock.Mock(first_token=0, first_chunk=0))


if __name__ == "__main__":
    unittest.main()
