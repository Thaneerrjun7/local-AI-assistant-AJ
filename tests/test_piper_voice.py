"""The Piper voice (core/speech.PiperVoice): loads on the CPU and speaks through Voice's pipeline.
Skipped when the voice files aren't downloaded (scripts/fetch_speech.ps1)."""
import unittest
from pathlib import Path

from aj.config import VoiceSettings
from aj.core.speech import PiperVoice

SETTINGS = VoiceSettings(engine="piper")
HERE = (Path(SETTINGS.piper_dir) / SETTINGS.piper_voice).is_dir()


@unittest.skipUnless(HERE, "Piper voice not downloaded")
class PiperVoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.voice = PiperVoice(SETTINGS)

    def test_it_runs_on_the_cpu(self):
        self.assertFalse(self.voice.uses_gpu)
        self.assertEqual(self.voice.rate, 22050)

    def test_it_speaks_with_pauses_and_caches_short_phrases(self):
        plain = self.voice.say("Done.")
        self.assertGreater(len(plain), self.voice.rate * 0.2)
        self.assertIs(self.voice._words("Done.", self.voice.speed), self.voice._words("Done.", self.voice.speed))
        paused = self.voice.say("Well... fine.")
        self.assertGreater(len(paused), len(self.voice.say("Well fine.")))  # "..." is a real pause

    def test_a_missing_voice_says_so(self):
        with self.assertRaises(FileNotFoundError):
            PiperVoice(VoiceSettings(engine="piper", piper_voice="no-such-voice"))


if __name__ == "__main__":
    unittest.main()
