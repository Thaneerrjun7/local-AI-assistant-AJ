"""System tools: fast-path phrasing, argument checks, and the real Core Audio read path.

Nothing here leaves the laptop changed: the one volume write is undone in a finally block,
media keys are mocked (a real one would pause your music) and no window is moved.
Run: .venv\\Scripts\\python.exe -m unittest discover -s tests -t .
"""
import base64
import struct
import subprocess
import tempfile
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from unittest import mock

try:
    import ultron.tools.intents as intents
    import ultron.tools.system as system
    from ultron.core.power import PowerState
    from ultron.tools.registry import ToolError
except ModuleNotFoundError as e:  # the tool registry is written in a parallel change
    if e.name not in ("ultron.tools.registry", "ultron.tools.intents"):
        raise
    system = None


def setUpModule():
    if system is None:
        raise unittest.SkipTest("ultron.tools.registry isn't there yet")


UP, DOWN = {"change": "up"}, {"change": "down"}

SAID = {
    # volume
    "volume 30": ("set_volume", {"level": 30}),
    "Set volume to 30 percent.": ("set_volume", {"level": 30}),
    "set the volume to 30%": ("set_volume", {"level": 30}),
    "Hey Ultron, volume at fifty-five": ("set_volume", {"level": 55}),
    "turn the volume up to 80": ("set_volume", {"level": 80}),
    "max volume": ("set_volume", {"level": 100}),
    "Turn it up": ("set_volume", UP),
    "turn it down a bit, please": ("set_volume", DOWN),
    "can you turn up the volume": ("set_volume", UP),
    "volume down": ("set_volume", DOWN),
    "louder": ("set_volume", UP),
    "a little quieter": ("set_volume", DOWN),
    "lower the volume": ("set_volume", DOWN),
    "mute": ("mute", {"on": True}),
    "Unmute.": ("mute", {"on": False}),
    "mute the sound": ("mute", {"on": True}),
    "turn the sound back on": ("mute", {"on": False}),
    # brightness
    "brightness 50": ("set_brightness", {"level": 50}),
    "set the screen brightness to 70 percent": ("set_brightness", {"level": 70}),
    "brighter": ("set_brightness", UP),
    "Dimmer.": ("set_brightness", DOWN),
    "make the screen a bit brighter": ("set_brightness", UP),
    "turn the brightness down": ("set_brightness", DOWN),
    "dim the screen": ("set_brightness", DOWN),
    # media
    "pause": ("media", {"action": "play_pause"}),
    "play": ("media", {"action": "play_pause"}),
    "resume the music": ("media", {"action": "play_pause"}),
    "next song": ("media", {"action": "next"}),
    "skip this song": ("media", {"action": "next"}),
    "previous": ("media", {"action": "previous"}),
    "go back a track": ("media", {"action": "previous"}),
    "stop the music": ("media", {"action": "stop"}),
    # windows
    "snap the window to the left": ("snap_window", {"position": "left"}),
    "move this window to the right half of the screen": ("snap_window", {"position": "right"}),
    "maximize": ("snap_window", {"position": "maximize"}),
    "minimise this window": ("snap_window", {"position": "minimize"}),
    "restore the window": ("snap_window", {"position": "restore"}),
    # screenshot, time, battery
    "take a screenshot": ("screenshot", {}),
    "Take a screen shot.": ("screenshot", {}),
    "screenshot": ("screenshot", {}),
    "what time is it": ("current_time", {}),
    "What's the time?": ("current_time", {}),
    "what time is it right now": ("current_time", {}),
    "what's the date": ("current_time", {}),
    "What's today's date?": ("current_time", {}),
    "what day is it": ("current_time", {}),
    "how much battery": ("battery_status", {}),
    "How much battery do I have left?": ("battery_status", {}),
    "what's my battery at": ("battery_status", {}),
    "am I plugged in": ("battery_status", {}),
}

NOT_MINE = ["play despacito", "what does mute mean", "turn off the lights", "volume", "brightness",
            "open spotify", "set a timer for 30 minutes", "time to go", "next", "stop",
            "skip to the end", "whats the time in london", "turn up the heat", "dim sum near me",
            "take a picture of my cat", "what is the volume of a cube", "volume banana"]


class IntentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = intents._rules
        cls.accepted = intents.collect([system])

    @classmethod
    def tearDownClass(cls):
        intents._rules = cls.saved

    def test_every_rule_compiles_and_names_a_real_tool(self):
        self.assertEqual(self.accepted, len(system.INTENTS))

    def test_phrasings_reach_the_right_tool(self):
        for said, expected in SAID.items():
            with self.subTest(said=said):
                self.assertEqual(intents.match(said), expected)

    def test_other_requests_are_left_to_the_model(self):
        for said in NOT_MINE:
            with self.subTest(said=said):
                self.assertIsNone(intents.match(said))

    def test_spoken_numbers(self):
        for text, number in {"7": 7, "zero": 0, "seventeen": 17, "fifty": 50, "thirty five": 35,
                             "a hundred": 100, "max": 100, "half": 50}.items():
            self.assertEqual(system._number(text), number, text)


class ArgumentTests(unittest.TestCase):
    def test_bad_levels_and_directions_are_refused(self):
        for level, change in [(None, None), (150, None), (-5, None), (True, None), ("loud", None),
                              (None, "sideways"), (None, 3)]:
            with self.subTest(level=level, change=change), self.assertRaises(ToolError):
                system._validated(level, change, "Volume")

    def test_good_arguments(self):
        self.assertEqual(system._validated("30%", None, "Volume"), (30, 0))
        self.assertEqual(system._validated(None, "Up", "Volume"), (None, 10))
        self.assertEqual(system._validated(None, "down", "Volume"), (None, -10))
        self.assertEqual(system._validated(40, "up", "Volume"), (40, 0))  # "up to 40" means 40

    def test_steps_stay_in_range(self):
        self.assertEqual(system._target(95, None, 10), 100)
        self.assertEqual(system._target(5, None, -10), 0)
        self.assertEqual(system._target(50, 20, 0), 20)

    def test_bad_calls_fail_before_touching_anything(self):
        untouched = mock.Mock(side_effect=AssertionError("should not be reached"))
        with mock.patch.object(system, "_speakers", untouched), \
             mock.patch.object(system.subprocess, "run", untouched), \
             mock.patch.object(system, "_keybd_event", untouched), \
             mock.patch.object(system, "_foreground_window", untouched):
            for call in (lambda: system.set_volume(), lambda: system.set_volume(level=101),
                         lambda: system.mute(on="yes"), lambda: system.set_brightness(change="sideways"),
                         lambda: system.media("rewind"), lambda: system.snap_window("diagonal")):
                with self.assertRaises(ToolError):
                    call()


class MediaTests(unittest.TestCase):
    def test_sends_one_media_key_press(self):
        with mock.patch.object(system, "_keybd_event") as key:
            self.assertEqual(system.media("next"), "Skipped to the next track.")
        self.assertEqual(key.call_args_list, [mock.call(0xB0, 0, 1, 0), mock.call(0xB0, 0, 3, 0)])


class BrightnessTests(unittest.TestCase):
    def run_with(self, stdout: str, **kwargs) -> tuple[str, list]:
        done = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
        with mock.patch.object(system.subprocess, "run", return_value=done) as run:
            return system.set_brightness(**kwargs), run.call_args

    def test_step_up_in_one_safe_powershell_call(self):
        result, call = self.run_with("60 70\n", change="up")
        self.assertEqual(result, "Brightness at 70%.")
        args, kwargs = call
        self.assertTrue(args[0][0].lower().endswith("powershell.exe"))
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["creationflags"], 0x08000000)
        script = base64.b64decode(args[0][args[0].index("-EncodedCommand") + 1]).decode("utf-16-le")
        self.assertIn("$level = -1; $step = 10", script)

    def test_same_level_says_so(self):
        self.assertEqual(self.run_with("40 40\n", level=40)[0], "Brightness is already at 40%.")

    def test_unsupported_display_is_explained(self):
        with self.assertRaisesRegex(ToolError, "doesn't let Windows"):
            self.run_with("unsupported\n", level=50)

    def test_a_wmi_failure_is_read_from_stdout_not_clixml(self):
        # PowerShell's own errors reach stderr as "#< CLIXML" when run with -EncodedCommand
        with self.assertRaisesRegex(ToolError, r"^Windows wouldn't change the brightness: Not supported\.$"):
            self.run_with("error Not supported \n", change="down")
        with self.assertRaisesRegex(ToolError, "PowerShell gave no usable answer"):
            self.run_with("", level=50)


class WindowTests(unittest.TestCase):
    def test_halves_of_the_work_area(self):
        work = (0, 0, 2560, 1560)
        self.assertEqual(system._half(work, "left"), (0, 0, 1280, 1560))
        self.assertEqual(system._half(work, "right"), (1280, 0, 1280, 1560))
        # invisible resize borders are added back so the visible edge meets the screen edge
        self.assertEqual(system._half(work, "left", (9, 0, 9, 9)), (-9, 0, 1298, 1569))
        self.assertEqual(system._half((2560, 0, 4480, 1040), "right"), (3520, 0, 960, 1040))


class TextTests(unittest.TestCase):
    def test_battery(self):
        self.assertEqual(system._describe_power(PowerState(True, 80, False)), "Battery at 80%, plugged in.")
        self.assertEqual(system._describe_power(PowerState(False, 15, True)),
                         "Battery at 15%, on battery power, battery saver on.")
        self.assertEqual(system._describe_power(PowerState(None, None, False)),
                         "Windows isn't reporting a battery level.")

    def test_time(self):
        self.assertEqual(system._describe_time(datetime(2026, 10, 4, 14, 5)),
                         "It's 2:05 PM on Sunday, October 4th, 2026.")
        self.assertEqual(system._describe_time(datetime(2026, 1, 1, 0, 0)),
                         "It's 12:00 AM on Thursday, January 1st, 2026.")
        self.assertEqual([system._ordinal(d) for d in (2, 3, 11, 12, 13, 22, 23)],
                         ["2nd", "3rd", "11th", "12th", "13th", "22nd", "23rd"])


class PngTests(unittest.TestCase):
    def test_round_trip(self):
        bgra = bytes([1, 2, 3, 255, 4, 5, 6, 255, 7, 8, 9, 255, 10, 11, 12, 255])
        rgb = system._bgra_to_rgb(bgra)
        self.assertEqual(rgb, bytes([3, 2, 1, 6, 5, 4, 9, 8, 7, 12, 11, 10]))
        png = system._png(2, 2, rgb)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        chunks, at = {}, 8
        while at < len(png):
            size, kind = struct.unpack(">I4s", png[at:at + 8])
            data = png[at + 8:at + 8 + size]
            self.assertEqual(struct.unpack(">I", png[at + 8 + size:at + 12 + size])[0], zlib.crc32(kind + data))
            chunks[kind], at = data, at + 12 + size
        self.assertEqual(struct.unpack(">II", chunks[b"IHDR"][:8]), (2, 2))
        self.assertEqual(zlib.decompress(chunks[b"IDAT"]), b"\x00" + rgb[:6] + b"\x00" + rgb[6:])

    def test_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            when = datetime(2026, 10, 4, 14, 5)
            first = system._screenshot_path(Path(tmp), when)
            first.write_bytes(b"x")
            self.assertEqual(system._screenshot_path(Path(tmp), when).name, "Screenshot 2026-10-04 14-05-00 (2).png")


class RealLaptopTests(unittest.TestCase):
    """Reads the real devices. The single write is restored before the test ends."""

    def test_reads_volume_and_mute(self):
        try:
            volume, muted = system.get_volume(), system.is_muted()
        except ToolError as e:
            self.skipTest(str(e))
        self.assertTrue(0 <= volume <= 100)
        self.assertIsInstance(muted, bool)

    def test_volume_write_lands_exactly_and_is_restored(self):
        try:
            with system._speakers() as speakers:
                original = system._get_scalar(speakers)
                probe = original - 0.01 if original >= 0.01 else original + 0.01  # 1%: inaudible
                try:
                    system._set_scalar(speakers, probe)
                    self.assertAlmostEqual(system._get_scalar(speakers), probe, places=3)
                finally:
                    system._set_scalar(speakers, original)
                self.assertAlmostEqual(system._get_scalar(speakers), original, places=4)
        except ToolError as e:
            self.skipTest(str(e))

    def test_asking_for_the_current_volume_changes_nothing(self):
        try:
            if system.is_muted():
                self.skipTest("muted: set_volume would unmute")
            volume = system.get_volume()
        except ToolError as e:
            self.skipTest(str(e))
        self.assertEqual(system.set_volume(level=volume), f"Volume is already at {volume}%.")

    def test_reads_brightness(self):
        try:
            level = system.get_brightness()
        except ToolError as e:
            self.skipTest(str(e))
        self.assertTrue(0 <= level <= 100)

    def test_battery_and_time(self):
        self.assertRegex(system.battery_status(), r"^(Battery at \d+%|Windows isn't reporting)")
        self.assertTrue(system.current_time().startswith("It's "))

    def test_screenshot_covers_every_screen_in_real_pixels(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(system, "SCREENSHOT_DIR", Path(tmp)):
            result = system.screenshot()
            path = Path(result.removeprefix("Screenshot saved to "))
            png = path.read_bytes()
        with system._physical_pixels():
            screen = (system._user32.GetSystemMetrics(78), system._user32.GetSystemMetrics(79))
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), screen)


if __name__ == "__main__":
    unittest.main()
