"""Phase 2 of docs/HARDWARE_AWARE_INFERENCE_PLAN.md: where AJ's models run (core/placement.py), the
settle time, moving between turns, recovering a dead server, and the engine and thinker following.
Fake servers and clocks only: no GPU, no llama-server."""
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from aj.config import LlmSettings, PlacementSettings, Settings
from aj.core import pipeline, placement
from aj.core.laptop_state import Snapshot
from aj.core.llm import server_command
from aj.core.placement import CPU, IGPU, RTX, Seen, decide
from aj.core.thinker import Thinker
from aj.engine import Engine, MemoryHook

PLUGGED = Seen(on_ac=True, rtx=True, igpu=True)
BATTERY = Seen(on_ac=False, rtx=False, igpu=True)  # as measured: unplugged, the RTX 4050 is gone


def vulkan0(_server: str) -> str:
    return "Vulkan0"


class DecideTests(unittest.TestCase):
    def test_the_table(self):
        s = PlacementSettings()
        for seen, unit in [
            (PLUGGED, RTX),
            (BATTERY, IGPU),
            (replace(BATTERY, rtx=True), IGPU),           # on battery even with the RTX 4050 there (Hybrid mode)
            (replace(BATTERY, on_ac=None), IGPU),         # Windows can't say: the frugal side
            (replace(BATTERY, igpu=False), CPU),          # discrete-only mode: no 780M
            (replace(PLUGGED, rtx=False), IGPU),          # plugged in but the RTX 4050 is missing
            (replace(PLUGGED, rtx=False, igpu=False), CPU),
            (replace(PLUGGED, heavy_app="DaVinci Resolve"), IGPU),  # out of the way
            (replace(PLUGGED, heavy_app="DaVinci Resolve", igpu=False), RTX),
        ]:
            with self.subTest(seen=seen):
                self.assertEqual(decide(seen, s)[0], unit)
        self.assertEqual(decide(BATTERY, PlacementSettings(battery_unit="cpu"))[0], CPU)
        self.assertEqual(decide(replace(PLUGGED, heavy_app="DaVinci Resolve"), s)[1], "DaVinci Resolve is open")

    def test_which_unit_settings_name(self):
        llm = LlmSettings()
        self.assertEqual(placement.unit_of(llm), RTX)
        self.assertEqual(placement.unit_of(replace(llm, backend="vulkan", device="Vulkan0")), IGPU)
        self.assertEqual(placement.unit_of(replace(llm, backend="vulkan", device="Vulkan1")), RTX)
        self.assertEqual(placement.unit_of(replace(llm, backend="cpu")), CPU)

    def test_heavy_apps_from_the_open_apps(self):
        s = PlacementSettings(gpu_heavy_apps=["DaVinci Resolve", "Cyberpunk"])
        with mock.patch.object(placement, "adapters", return_value=["AMD Radeon(TM) 780M", "NVIDIA GeForce RTX 4050"]), \
                mock.patch.object(placement.power_source, "on_ac_power", return_value=True):
            self.assertEqual(placement.look(s, ["Google Chrome", "DaVinci Resolve"]),
                             Seen(True, True, True, "DaVinci Resolve"))
            self.assertEqual(placement.look(s, ["Google Chrome"]).heavy_app, "")
            self.assertEqual(placement.look(PlacementSettings(), ["DaVinci Resolve"]).heavy_app, "")  # none listed


class SettingsTests(unittest.TestCase):
    def test_fixed_is_the_default_and_changes_nothing(self):
        s = Settings()
        self.assertEqual(s.placement.mode, "fixed")
        self.assertIs(placement.start_settings(s, BATTERY), s.llm)

    def test_auto_starts_on_the_780m_on_battery(self):
        s = Settings()
        s.placement.mode = "auto"
        with mock.patch.object(placement, "vulkan_device", vulkan0):
            llm = placement.start_settings(s, BATTERY)
        self.assertEqual((llm.backend, llm.device, llm.port), ("vulkan", "Vulkan0", s.llm.port))
        self.assertIs(placement.start_settings(s, PLUGGED).backend, "cuda")

    def test_the_780m_unreachable_means_the_cpu(self):
        s = Settings()
        s.placement.mode = "auto"
        with mock.patch.object(placement, "vulkan_device", return_value=None):
            self.assertEqual(placement.start_settings(s, BATTERY).backend, "cpu")

    def test_the_command_line_for_each_unit(self):
        llm = LlmSettings()
        cuda = server_command(llm)
        self.assertEqual(cuda[0], llm.server)
        self.assertNotIn("--device", cuda)
        vk = server_command(replace(llm, backend="vulkan", device="Vulkan0"))
        self.assertEqual(vk[0], llm.vulkan_server)
        self.assertEqual(vk[vk.index("--device") + 1], "Vulkan0")
        cpu = server_command(replace(llm, backend="cpu", device="Vulkan0"))
        self.assertEqual((cpu[0], cpu[cpu.index("-ngl") + 1]), (llm.server, "0"))
        self.assertNotIn("--device", cpu)
        self.assertEqual(vk[vk.index("-ctk") + 1], "q8_0")  # AJ's own KV cache on every unit


class SteadyTests(unittest.TestCase):
    def test_a_change_counts_once_it_has_lasted(self):
        now = [0.0]
        steady = placement.Steady(RTX, 30, clock=lambda: now[0])
        self.assertIsNone(steady.due(RTX))
        self.assertIsNone(steady.due(IGPU))
        now[0] = 29.9
        self.assertIsNone(steady.due(IGPU))
        now[0] = 30.0
        self.assertEqual(steady.due(IGPU), IGPU)

    def test_a_loose_cable_starts_the_wait_again(self):
        now = [0.0]
        steady = placement.Steady(RTX, 30, clock=lambda: now[0])
        steady.due(IGPU)
        now[0] = 20.0
        steady.due(RTX)       # plugged back in
        now[0] = 25.0
        steady.due(IGPU)      # out again
        now[0] = 40.0
        self.assertIsNone(steady.due(IGPU))  # only 15 s this time
        now[0] = 55.0
        self.assertEqual(steady.due(IGPU), IGPU)


class FakeProc:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class FakeServer:
    made: list["FakeServer"] = []
    fail = False

    def __init__(self, llm: LlmSettings):
        self.settings, self.base = llm, f"http://127.0.0.1:{llm.port}"
        self.proc: FakeProc | None = None
        self.stopped = False
        FakeServer.made.append(self)

    def start(self):
        if FakeServer.fail:
            raise RuntimeError("no device")
        self.proc = FakeProc()
        return 1.0

    def stop(self):
        self.stopped, self.proc = True, None


class PlacerTests(unittest.TestCase):
    def setUp(self):
        FakeServer.made, FakeServer.fail = [], False
        self.settings = Settings()
        self.settings.placement.mode = "auto"
        self.now = [0.0]
        self.seen = PLUGGED
        self.idle = [True]
        self.warmed, self.adopted = [], []
        self.server = FakeServer(self.settings.llm)
        self.server.start()
        self.placer = placement.Placer(
            self.settings, self.server, open_apps=lambda: [], idle=lambda: self.idle[0],
            warm_up=self.warmed.append, adopt=lambda s, u, why: self.adopted.append((s, u, why)),
            stop=threading.Event(), look_now=lambda: self.seen, make=FakeServer, clock=lambda: self.now[0],
            find_device=vulkan0)

    def test_unplugged_for_the_settle_time_it_moves_to_the_780m(self):
        self.seen = BATTERY
        self.assertFalse(self.placer.step())
        self.now[0] = 29.0
        self.assertFalse(self.placer.step())
        self.now[0] = 31.0
        self.assertTrue(self.placer.step())
        new = FakeServer.made[-1]
        self.assertEqual((new.settings.backend, new.settings.device, new.settings.port), ("vulkan", "Vulkan0", 8079))
        self.assertEqual(self.warmed, [new.base])             # the persona read before it takes over
        self.assertEqual(self.adopted, [(new, IGPU, "on battery")])
        self.assertTrue(self.server.stopped)                  # the old one, after
        self.assertEqual(self.placer.unit, IGPU)
        self.seen = PLUGGED                                   # and back, on the first port again
        self.now[0] = 100.0
        self.placer.step()
        self.now[0] = 131.0
        self.assertTrue(self.placer.step())
        self.assertEqual((FakeServer.made[-1].settings.backend, FakeServer.made[-1].settings.port), ("cuda", 8077))

    def test_a_new_server_that_fails_leaves_him_where_he_is(self):
        self.seen, FakeServer.fail = BATTERY, True
        self.placer.step()
        self.now[0] = 31.0
        self.assertFalse(self.placer.step())
        self.assertEqual((self.placer.unit, self.adopted, self.server.stopped), (RTX, [], False))
        FakeServer.fail = False
        self.now[0] = 60.0
        self.assertFalse(self.placer.step())  # waits a minute before trying again
        self.now[0] = 92.0
        self.assertTrue(self.placer.step())

    def test_it_moves_only_between_turns(self):
        self.seen, self.idle[0] = BATTERY, False
        self.placer.step()
        self.now[0] = 31.0
        moved = threading.Thread(target=self.placer.step)
        moved.start()
        time.sleep(0.5)
        self.assertEqual(self.adopted, [])  # a turn is being answered
        self.idle[0] = True
        moved.join(2)
        self.assertEqual(len(self.adopted), 1)

    def test_a_dead_server_is_replaced_at_once(self):
        self.seen = BATTERY
        self.server.proc.code = 1  # the RTX 4050 went away under it
        self.idle[0] = False       # a turn is waiting for it
        self.assertTrue(self.placer.recover())
        self.assertEqual(self.adopted[0][1], IGPU)
        self.assertIn("had stopped", self.adopted[0][2])
        self.assertFalse(self.placer.recover())  # the new one is alive: nothing to do

    def test_asleep_with_the_model_let_go_nothing_moves(self):
        self.server.stop()  # Settings.sleep.free_gpu
        self.seen = BATTERY
        self.now[0] = 100.0
        self.assertFalse(self.placer.step())
        self.assertFalse(self.placer.recover())


class FollowingTests(unittest.TestCase):
    def test_the_thinker_loads_where_the_model_is(self):
        llm = LlmSettings()
        thinker = Thinker(llm)
        self.assertEqual((thinker.server, thinker._unit[0]), (llm.server, "--fit"))
        thinker.stop = mock.Mock()
        thinker.place("vulkan", "Vulkan0")
        thinker.stop.assert_called_once()
        self.assertEqual((thinker.server, thinker._unit), (llm.vulkan_server, ["-ngl", "99", "--device", "Vulkan0"]))
        thinker.place("vulkan", "Vulkan0")
        thinker.stop.assert_called_once()  # already there
        self.assertEqual(Thinker(replace(llm, backend="cpu"))._unit, ["-ngl", "0"])

    def test_the_status_says_where_his_model_runs(self):
        self.assertNotIn("your own model", Snapshot(battery=80, plugged=True).summary())
        self.assertTrue(Snapshot(battery=80, plugged=False, ai_unit="the Radeon 780M (on battery)").summary()
                        .endswith("your own model running on the Radeon 780M (on battery)"))

    def test_the_gpu_pings_only_on_the_rtx4050(self):
        for wakes, pinged in ((None, True), (lambda: True, True), (lambda: False, False)):
            fake = SimpleNamespace(ears=SimpleNamespace(hearing_speech=True), _last_wake=0.0, _waker=None,
                                   wakes_gpu=wakes, brain=SimpleNamespace(base="http://x"))
            with mock.patch.object(pipeline, "wake_gpu"):
                pipeline.Session._keep_gpu_awake_while_you_talk(fake)
            self.assertEqual(fake._waker is not None, pinged)

    def test_the_engine_points_everything_at_the_new_server(self):
        engine = Engine(Settings(), hear=False, speak=False)
        engine.brain = SimpleNamespace(base=engine.server.base, memory=MemoryHook(engine.server.base))
        engine.thinker = mock.Mock()
        new = FakeServer(replace(engine.settings.llm, backend="vulkan", device="Vulkan0", port=8079))
        engine._adopt(new, IGPU, "on battery")
        self.assertIs(engine.server, new)
        self.assertEqual((engine.brain.base, engine.brain.memory.base, engine.dictation.llm[0]), (new.base,) * 3)
        engine.thinker.place.assert_called_once_with("vulkan", "Vulkan0")
        self.assertEqual(engine.state.snapshot().ai_unit, "the Radeon 780M (on battery)")
        self.assertFalse(engine._wakes_gpu())
        engine.brain.memory.stop()


class UnplugCheckTests(unittest.TestCase):
    def test_the_timeline_summary(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import check_unplug

        def at(t, ac, rtx, servers, model):
            return {"t": t, "on_ac": ac, "rtx_present": rtx, "rtx_state": None, "servers": servers, "model": model}
        timeline = [at(0, True, True, 1, "the RTX 4050"), at(40, False, True, 1, "the RTX 4050"),
                    at(45, False, False, 0, "the RTX 4050"), at(48, False, False, 1, "the RTX 4050"),
                    at(80, False, False, 1, "the Radeon 780M"), at(150, True, False, 1, "the Radeon 780M"),
                    at(155, True, True, 2, "the Radeon 780M"), at(190, True, True, 1, "the RTX 4050")]
        self.assertEqual(check_unplug.summarize(timeline),
                         {"unplugged_at": 40, "rtx_gone_after_s": 5, "on_780m_after_s": 40, "servers_at_once_max": 2,
                          "no_server_seconds": 1, "plugged_back_at": 150, "rtx_back_after_s": 5, "on_rtx_after_s": 40})
        self.assertEqual(check_unplug.summarize(timeline[:1]), {"unplugged_at": None})


if __name__ == "__main__":
    unittest.main()
