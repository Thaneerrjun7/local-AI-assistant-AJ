"""Phase 2 of the window redesign: the orb moves with AJ's voice and yours, and shows Working
while a tool runs. Here: how loud each audio block is (core/audio.py Level), the needle that
follows it (ui/levels.py), the brain telling when a tool starts and ends, and the engine passing
both on. Nothing opens a device or loads a model."""
import time
import unittest

import numpy as np

from aj.config import Settings
from aj.core import audio
from aj.core import brain as brain_mod
from aj.engine import Engine
from aj.ui import status as looks
from aj.ui.levels import Follower


class LoudnessTests(unittest.TestCase):
    def test_a_speech_scale_from_a_quiet_room_to_loud_speech(self):
        self.assertEqual(audio.loudness(0.0), 0.0)
        self.assertEqual(audio.loudness(10 ** (-50 / 20)), 0.0)   # -50 dBFS
        self.assertAlmostEqual(audio.loudness(10 ** (-31 / 20)), 0.5, places=2)
        self.assertEqual(audio.loudness(10 ** (-12 / 20)), 1.0)   # -12 dBFS
        self.assertEqual(audio.loudness(1.0), 1.0)

    def test_the_loudest_block_in_the_window_and_silence_outside_it(self):
        level = audio.Level()
        level.push(np.full(256, 0.2, np.float32))
        now = time.perf_counter()
        self.assertGreater(level.at(now), 0.8)
        self.assertEqual(level.at(now + 1.0), 0.0)  # nothing in the last 50 ms by then: quiet
        self.assertEqual(audio.Level().at(now), 0.0)

    def test_the_speaker_measures_every_block_it_plays(self):
        player = audio.Player()
        player.play(np.full(480, 0.3, np.float32), "hello")
        out = np.zeros((480, 1), np.float32)
        player._on_output(out, 480, None, None)
        self.assertGreater(player.level.at(time.perf_counter()), 0.8)
        player._on_output(out, 480, None, None)  # the buffer is finished: silence
        self.assertEqual(player.level._history[-1][1], 0.0)

    def test_the_mic_measures_every_block_it_hears(self):
        mic = audio.Mic()
        mic._on_audio(np.full((512, 1), 0.05, np.float32), 512, None, None)
        self.assertEqual(mic.blocks.qsize(), 1)  # still queued for the ears as before
        self.assertGreater(mic.level.at(time.perf_counter()), 0.3)


class FollowerTests(unittest.TestCase):
    def test_rises_fast_falls_slower_and_rests_in_a_silence(self):
        needle = Follower()
        self.assertGreater(needle.step(1.0, 0.033), 0.85)  # one reading: nearly there
        after_one = needle.step(0.0, 0.033)
        self.assertGreater(after_one, 0.6)                  # falls more slowly
        for _ in range(40):
            needle.step(0.0, 0.033)
        self.assertEqual(needle.value, 0.0)                 # exactly still again


class FakeDispatcher:
    def __init__(self, fail=False):
        self.fail = fail

    def run(self, name, args):
        if self.fail:
            raise RuntimeError("broken")
        return "done"


class ToolReportTests(unittest.TestCase):
    def test_the_brain_says_when_a_tool_starts_and_ends(self):
        told = []
        brain = brain_mod.Brain("http://x", Settings(), dispatcher=FakeDispatcher())
        brain.on_tool = told.append
        self.assertEqual(brain._dispatch("get_weather", {}), "done")
        self.assertEqual(told, ["get_weather", ""])

    def test_the_end_is_told_even_when_the_tool_fails(self):
        told = []
        brain = brain_mod.Brain("http://x", Settings(), dispatcher=FakeDispatcher(fail=True))
        brain.on_tool = told.append
        with self.assertRaises(RuntimeError):
            brain._dispatch("calculate", {})
        self.assertEqual(told, ["calculate", ""])

    def test_nobody_listening_is_fine(self):
        brain = brain_mod.Brain("http://x", Settings(), dispatcher=FakeDispatcher())
        self.assertEqual(brain._dispatch("get_weather", {}), "done")

    def test_the_window_names_the_work(self):
        self.assertEqual(looks.working_text("get_weather"), "Working · weather")
        self.assertEqual(looks.working_text("web_search"), "Working · searching the web")
        self.assertEqual(looks.working_text("brand_new_tool"), "Working · brand new tool")


class Recorder:
    def __init__(self):
        self.working = []

    def on_status(self, status):
        pass

    def on_working(self, tool):
        self.working.append(tool)


class FakeLevel:
    def __init__(self, value):
        self.value, self.asked = value, []

    def at(self, when):
        self.asked.append(when)
        return self.value


class EngineTests(unittest.TestCase):
    def test_tool_runs_reach_events_that_listen_for_them(self):
        engine = Engine(Settings())
        engine.events = Recorder()
        engine._tool_running("get_weather")
        engine._tool_running("")
        self.assertEqual(engine.events.working, ["get_weather", ""])

    def test_events_without_on_working_are_left_alone(self):
        engine = Engine(Settings())
        engine.events = object()
        engine._tool_running("get_weather")  # nothing to call, nothing raised

    def test_the_voice_level_is_read_as_heard_after_the_speakers_delay(self):
        engine = Engine(Settings())
        self.assertEqual(engine.voice_level(), 0.0)  # no voice loaded
        engine.player = type("P", (), {"level": FakeLevel(0.7), "output_latency": 0.08})()
        before = time.perf_counter()
        self.assertEqual(engine.voice_level(), 0.7)
        asked = engine.player.level.asked[0]
        self.assertLess(asked, before - 0.07)  # 80 ms ago: what's coming out of the speakers now

    def test_the_mic_level_comes_from_the_sessions_mic(self):
        engine = Engine(Settings())
        self.assertEqual(engine.mic_level(), 0.0)  # no conversation yet
        engine.session = type("S", (), {"source": type("M", (), {"level": FakeLevel(0.4)})()})()
        self.assertEqual(engine.mic_level(), 0.4)


if __name__ == "__main__":
    unittest.main()
