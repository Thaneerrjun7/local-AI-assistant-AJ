"""The Chatterbox voice: how text is handed to it, the pipe to its process, and that the
process ends when AJ is done with it. Uses a fake voice process, not the model."""
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np

from aj.config import VoiceSettings
from aj.core import chatterbox_voice as cv

FAKE = Path(__file__).with_name("fake_voice_server.py")


class TextTests(unittest.TestCase):
    def test_tags_stay_and_voice_marks_are_translated(self):
        self.assertEqual(cv.to_chatterbox("[sigh] Fine. [chuckle]"), "[sigh] Fine. [chuckle]")
        self.assertEqual(cv.to_chatterbox("Wait [pause] there. [slow] Slowly."), "Wait ... there. Slowly.")
        self.assertEqual(cv.to_chatterbox("STAY RIGHT WHERE YOU ARE. Na\u00efve."),
                         "stay right where you are. Naive.")


class ProcessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        for target, value in (("SERVER", FAKE), ("LOG_DIR", Path(tmp.name) / "logs")):
            patcher = mock.patch.object(cv, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def settings(self, clip=""):
        return replace(VoiceSettings(), chatterbox_python=sys.executable, voice_clip=clip)

    def test_speaks_through_the_pipe_and_caches(self):
        voice = cv.ChatterboxVoice(self.settings(), cache_dir=self.cache)
        self.addCleanup(voice.close)
        audio = voice.say("Done.")
        self.assertEqual(voice.rate, 24000)
        self.assertEqual(int(np.sum(np.abs(audio - 0.5) < 1e-6)), 500)  # 100 samples per character
        self.assertAlmostEqual(len(audio) - 500, int(24000 * cv.SENTENCE_GAP), delta=1)
        voice.prerender(["Opening it."])
        self.assertEqual(len(list(self.cache.glob("*.npy"))), 1)  # kept on disk for next time

    def test_closing_ends_the_process(self):
        voice = cv.ChatterboxVoice(self.settings(), cache_dir=self.cache)
        voice.close()
        self.assertIsNotNone(voice.proc.poll())

    def test_a_voice_that_cant_start_says_why(self):
        with self.assertRaisesRegex(RuntimeError, "no graphics card"):
            cv.ChatterboxVoice(replace(self.settings(), hf_home="FAIL"), cache_dir=self.cache)


if __name__ == "__main__":
    unittest.main()
