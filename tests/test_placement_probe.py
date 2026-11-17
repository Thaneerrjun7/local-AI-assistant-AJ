"""Phase 0 of docs/HARDWARE_AWARE_INFERENCE_PLAN.md: the readings and verdicts of scripts/hwprobe.py,
scripts/check_devices.py and scripts/bench_baseline.py, from canned inputs (no hardware asked)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bench_baseline as bb  # noqa: E402
import bench_llm  # noqa: E402
import bench_placement as bp  # noqa: E402
import check_devices as cd  # noqa: E402
import hwprobe  # noqa: E402

# What this laptop printed, 2026-10-26.
VULKAN = """Available devices:
  Vulkan0: AMD Radeon(TM) 780M (8033 MiB, 7631 MiB free)
  Vulkan1: NVIDIA GeForce RTX 4050 Laptop GPU (5920 MiB, 5152 MiB free)
"""
RTX = {"InstanceId": "PCI\\VEN_10DE&DEV_28E1&SUBSYS_3C9217AA&REV_A1\\4&2483AB1A&0&0009",
       "FriendlyName": "NVIDIA GeForce RTX 4050 Laptop GPU", "Class": "Display", "Status": "OK", "Present": True}
RADEON = {"InstanceId": "PCI\\VEN_1002&DEV_15BF\\4&1", "FriendlyName": "AMD Radeon(TM) 780M", "Class": "Display",
          "Status": "OK", "Present": True}
NPU = {"InstanceId": "PCI\\VEN_1022&DEV_1502&SUBSYS_00001022\\4&1", "FriendlyName": "AMD IPU Device", "Class": "System",
       "Status": "OK", "Present": True}


class ReadingTests(unittest.TestCase):
    def test_power_data_says_which_d_state(self):
        def raw(state):
            return (12).to_bytes(4, "little") + state.to_bytes(4, "little") + bytes(4)
        self.assertEqual(hwprobe.decode_power_data(raw(1)), "D0")
        self.assertEqual(hwprobe.decode_power_data(raw(4)), "D3")
        self.assertIsNone(hwprobe.decode_power_data(raw(0)))
        self.assertIsNone(hwprobe.decode_power_data(b"\x00"))

    def test_battery_draw_is_power_leaving_the_battery(self):
        self.assertEqual(hwprobe.battery_from(1, 0, 56240), hwprobe.Battery(True, 0.0, 56240))  # plugged in, full
        self.assertEqual(hwprobe.battery_from(2, -12345, 50000).watts, 12.35)  # on battery
        self.assertEqual(hwprobe.battery_from(5, 30000, 40000).watts, 0.0)     # charging

    def test_nvidia_smi_and_vulkan_output(self):
        smi = hwprobe.parse_nvidia("NVIDIA GeForce RTX 4050 Laptop GPU, 617.42, 6141, 3180\n", "1234, 3050\n")
        self.assertEqual(smi, {"name": "NVIDIA GeForce RTX 4050 Laptop GPU", "driver": "617.42", "total_mb": 6141,
                               "used_mb": 3180, "used_by_pid_mb": {1234: 3050}})
        self.assertEqual(hwprobe.parse_nvidia("unavailable (no nvidia-smi)", ""), {})
        self.assertEqual(hwprobe.parse_vulkan(VULKAN),
                         [{"id": "Vulkan0", "name": "AMD Radeon(TM) 780M", "total_mb": 8033, "free_mb": 7631},
                          {"id": "Vulkan1", "name": "NVIDIA GeForce RTX 4050 Laptop GPU", "total_mb": 5920,
                           "free_mb": 5152}])

    def test_percentiles(self):
        self.assertEqual(hwprobe.pct([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(hwprobe.pct([5], 95), 5)
        self.assertIsNone(hwprobe.pct([], 50))


class DeviceCheckTests(unittest.TestCase):
    def check(self, devices, vulkan=VULKAN, nvidia=None):
        nvidia = {"total_mb": 6141} if nvidia is None else nvidia
        return cd.check(devices, [], hwprobe.parse_vulkan(vulkan), nvidia, "M3CN50WW", "AMD Ryzen 7 7840HS", 16)

    def test_this_laptop(self):
        units = self.check([RTX, RADEON])
        self.assertEqual({k: u["usable"] for k, u in units.items()},
                         {"rtx4050": True, "radeon780m": True, "cpu": True, "npu": False})
        self.assertIn("not present", units["npu"]["reason"])
        self.assertIn("M3CN50WW", units["npu"]["reason"])
        self.assertTrue(units["radeon780m"]["hybrid_mode"])

    def test_an_npu_that_shows_up_still_needs_its_smoke_test(self):
        units = self.check([RTX, RADEON, NPU])
        self.assertIsNone(units["npu"]["usable"])
        self.assertIn("smoke test", units["npu"]["reason"])

    def test_why_the_780m_isnt_usable(self):
        self.assertIn("Hybrid", self.check([RTX])["radeon780m"]["reason"])           # discrete-only mode
        self.assertIn("-Vulkan", self.check([RTX, RADEON], vulkan="")["radeon780m"]["reason"])  # no Vulkan build

    def test_no_answer_from_nvidia_smi(self):
        self.assertFalse(self.check([RTX, RADEON], nvidia={})["rtx4050"]["usable"])


class BaselineTests(unittest.TestCase):
    CONSOLE = ["AJ is on (3.7 s to load).", "you    > What time is it?", "aj     > It's 10:41 PM.",
               "         heard 390 | asked 391 | first token 420 | first chunk 425 | voice ready 600 | SOUND 640 ms after you stopped",
               "you    > Tell me a joke.", "aj     > ...",
               "         heard 380 | asked 381 | first token - | first chunk - | voice ready - | SOUND - ms after you stopped",
               "         heard 400 | asked 401 | first token 500 | first chunk 510 | voice ready 700 | SOUND 900 ms after you stopped"]

    def test_turn_timings_come_from_ajs_console(self):
        self.assertEqual(bb.load_seconds(self.CONSOLE), 3.7)
        self.assertEqual(bb.turn_times(self.CONSOLE), [{"first_token_ms": 420, "first_sound_ms": 640},
                                                       {"first_token_ms": None, "first_sound_ms": None},
                                                       {"first_token_ms": 500, "first_sound_ms": 900}])
        lat = bb.latency(bb.turn_times(self.CONSOLE))
        self.assertEqual((lat["turns"], lat["counted"]), (3, 2))  # the first is the warm-up
        self.assertEqual(lat["first_sound_ms"], {"p50": 900, "p95": 900})

    def test_summary(self):
        samples = [{"t": 0.0, "on_ac": False, "watts": 10.0, "gpu": "D3", "free_ram_mb": 4000,
                    "working_set_mb": 900.0, "cpu_seconds": 1.0,
                    "by_process_mb": {"python.exe": 600.0, "llama-server.exe": 300.0}},
                   {"t": 10.0, "on_ac": False, "watts": 14.0, "gpu": "D0", "free_ram_mb": 3800,
                    "working_set_mb": 1100.0, "cpu_seconds": 2.6,
                    "by_process_mb": {"python.exe": 700.0, "llama-server.exe": 400.0}}]
        out = bb.summarize(samples, threads=16)
        self.assertEqual(out["watts"]["mean"], 12.0)
        self.assertEqual(out["rtx4050_awake_share"], 0.5)
        self.assertEqual(out["rtx4050_present_share"], 1.0)
        gone = bb.summarize([dict(s, gpu="absent") for s in samples], threads=16)
        self.assertEqual(gone["rtx4050_present_share"], 0.0)
        self.assertNotIn("rtx4050_awake_share", gone)
        self.assertEqual(out["working_set_mb"], {"mean": 1000, "max": 1100, "min": 900})
        self.assertEqual(out["by_process_mb"], {"llama-server.exe": {"mean": 350, "max": 400},
                                                "python.exe": {"mean": 650, "max": 700}})
        self.assertEqual(out["cpu"], {"percent_of_one_core": 16.0, "percent_of_whole_cpu": 1.0})
        plugged = bb.summarize([dict(s, on_ac=True, watts=0.0) for s in samples], threads=16)
        self.assertNotIn("watts", plugged)  # no draw to speak of while charging

    def test_the_battery_verdict(self):
        def run(watts, awake):
            return {"measured": {"watts": {"mean": watts}, "rtx4050_awake_share": awake}}
        self.assertTrue(bb.verdict({"battery-off": run(8.0, 0.0), "battery-idle": run(14.5, 1.0)}).startswith("Confirmed"))
        self.assertTrue(bb.verdict({"battery-off": run(8.0, 0.0), "battery-idle": run(8.4, 0.0)}).startswith("Rejected"))
        self.assertTrue(bb.verdict({"battery-off": run(8.0, 0.0), "battery-idle": run(10.0, 0.1)}).startswith("Partly"))
        self.assertTrue(bb.verdict({}).startswith("Not answered"))
        gone = {"measured": {"watts": {"mean": 18.0}, "rtx4050_present_share": 0.0, "cpu": {"percent_of_whole_cpu": 50.2}}}
        said = bb.verdict({"battery-off": run(10.7, 0.0), "battery-idle": gone})
        self.assertTrue(said.startswith("Different from the guess"), said)
        self.assertIn("7.3 W more", said)
        gone["prompt_evals"] = [{"ms": 577670.22, "tokens": 7634, "tokens_per_s": 13.22}]
        self.assertIn("7634 tokens), took 577.7 s on the CPU",
                      bb.verdict({"battery-off": run(10.7, 0.0), "battery-idle": gone}))
        silent = {"measured": {"seconds": 1798.4}, "latency": {"turns": 0}}
        self.assertIn("answered nothing in 30 minutes",
                      bb.verdict({"battery-off": run(10.7, 0.0), "battery-idle": gone, "battery-talk": silent}))

    def test_llama_servers_own_prompt_timing(self):
        log = ("9.42.825.405 I slot print_timing: id  0 | task 0 | prompt eval time =  577670.22 ms /  7634 tokens "
               "(   75.67 ms per token,    13.22 tokens per second)\n"
               "9.42.825.419 I slot print_timing: id  0 | task 0 |        eval time =       0.00 ms /     1 tokens\n")
        self.assertEqual(bb.prompt_evals(log), [{"ms": 577670.22, "tokens": 7634, "tokens_per_s": 13.22}])

    def test_every_condition_has_its_power_source(self):
        self.assertTrue(all(c.on_ac for n, c in bb.CONDITIONS.items() if n.startswith(("ac-", "listener"))))
        self.assertFalse(any(c.on_ac for n, c in bb.CONDITIONS.items() if n.startswith("battery-")))


class PhaseOneTests(unittest.TestCase):
    """Phase 1: scripts/bench_llm.py's new readings and scripts/bench_placement.py's set."""

    def test_the_set(self):
        ac = {r.name: r for r in bp.RUNS["ac"]}
        self.assertEqual({r.unit for r in ac.values()}, {"RTX 4050", "Radeon 780M"})
        battery = bp.RUNS["battery"]
        self.assertTrue(battery and all(r.unit == "Radeon 780M" for r in battery))  # the RTX 4050 is off unplugged
        self.assertNotIn("json", next(r for r in battery if r.model == bp.QWEN).sections)
        argv = ac["qwen-780m-ac"].argv()
        self.assertEqual(argv[argv.index("--device") + 1], "Vulkan0")
        self.assertEqual(argv[argv.index("--extra") + 1:], ["-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0"])
        self.assertNotIn("--device", ac["qwen-rtx4050-ac"].argv())

    def test_which_runs_use_the_rtx4050(self):
        self.assertTrue(bench_llm.uses_rtx4050("cuda", ""))
        self.assertTrue(bench_llm.uses_rtx4050("vulkan", "Vulkan1"))
        self.assertFalse(bench_llm.uses_rtx4050("vulkan", "Vulkan0"))
        self.assertFalse(bench_llm.uses_rtx4050("cpu", ""))

    def test_watts_per_section(self):
        samples = [{"section": "idle", "on_ac": False, "watts": 12.0}, {"section": "idle", "on_ac": False, "watts": 14.0},
                   {"section": "tools", "on_ac": False, "watts": 30.0}]
        self.assertEqual(bench_llm.watts_by_section(samples), {"idle": 13.0, "tools": 30.0})
        self.assertIsNone(bench_llm.watts_by_section([dict(samples[0], on_ac=True)]))

    def test_prompt_timing_from_the_server(self):
        resp = {"timings": {"prompt_n": 7634, "prompt_ms": 41234.5, "prompt_per_second": 185.13}}
        self.assertEqual(bench_llm.prefill_timing(resp), {"tokens": 7634, "seconds": 41.23, "tokens_per_s": 185.1})
        self.assertEqual(bench_llm.prefill_timing({}), {"tokens": None, "seconds": None, "tokens_per_s": None})

    def test_a_report_row(self):
        result = {"name": "qwen-780m-battery", "unit": "Radeon 780M", "load_seconds": 6.2,
                  "prefill": {"seconds": 41.2, "tokens_per_s": 185.1},
                  "latency": {"cached": {"first_sentence": {"median_ms": 640, "p95_ms": 910}, "decode_tps": 21.4}},
                  "tools": {"score_pct": 91.7}, "memory_after_load": {"working_set_mb": 420},
                  "system_ram_mb": {"model": 3100}, "vram_model_mb": None,
                  "power_w": {"load": 20.0, "idle": 14.1, "latency": 29.5, "tools": 31.2}}
        self.assertEqual(bp.row(result), "| qwen-780m-battery | Radeon 780M | 6.2 s | 41.2 s (185.1 tok/s) | 640 / 910 ms | "
                                         "21.4 | 91.7% | - | - | 420 / 3100 MB | - | 14.1 W idle, up to 31.2 W working |")

    def test_what_it_shows(self):
        results = {"qwen-rtx4050-ac": {"prefill": {"seconds": 2.9}, "tools": {"score_pct": 90.0},
                                       "json": {"grammar": {"recall_pct": 77.8}}, "system_ram_mb": {"model": 403}},
                   "qwen-780m-ac": {"prefill": {"seconds": 23.3}, "tools": {"score_pct": 88.3},
                                    "json": {"grammar": {"recall_pct": 75.6}}, "system_ram_mb": {"model": 3834}},
                   "qwen-780m-battery": {"prefill": {"seconds": 24.4}, "power_w": {"idle": 14.47}},
                   "thinker-rtx4050-ac": {"hard": {"rows": [{"ok": True}] * 12}},
                   "thinker-780m-ac": {"hard": {"rows": [{"ok": True}] * 10 + [{"ok": False}] * 2}}}
        baseline = {"battery-talk": {"prompt_evals": [{"ms": 577670.22, "tokens": 7634, "tokens_per_s": 13.22}]},
                    "battery-off": {"measured": {"watts": {"mean": 10.74}}},
                    "battery-idle": {"measured": {"watts": {"mean": 24.17}}}}
        said = "\n".join(bp.findings(results, baseline))
        self.assertIn("2.9 s on the RTX 4050; 23.3 s on the 780M plugged in; 24.4 s on the 780M on battery; "
                      "577.7 s on the CPU on battery (Phase 0)", said)
        self.assertIn("14.47 W with Qwen's server alone on the 780M, against 10.74 W for the laptop without AJ and 24.17 W", said)
        self.assertIn("tools 90.0% (RTX 4050) vs 88.3% (780M)", said)
        self.assertIn("12/12 on the RTX 4050, 10/12 on the 780M", said)
        self.assertIn("3834 MB of system RAM there, against 403 MB", said)
        self.assertEqual(bp.findings({}, {}), [])


if __name__ == "__main__":
    unittest.main()
