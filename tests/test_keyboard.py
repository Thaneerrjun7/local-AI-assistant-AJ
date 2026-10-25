"""core/keyboard.py without touching the desktop: SendInput is replaced by a recorder that reads
back the INPUT array it is given, and the keyboard layout by a fixed US-like map.

Run: .venv\\Scripts\\python.exe -m unittest tests.test_keyboard
"""
import ctypes
import unittest
from unittest import mock

from aj.core import keyboard

VK_SHIFT, VK_CONTROL, VK_RETURN = 0x10, 0x11, 0x0D
UNICODE, KEYUP = 0x4, 0x2


def us_key(char: str, layout: int) -> tuple[int, bool] | None:
    """A small US layout: letters, digits, space and "!" (Shift+1)."""
    if char.isascii() and char.isalpha():
        return ord(char.upper()), char.isupper()
    if char.isascii() and char.isdigit():
        return ord(char), False
    return {" ": (0x20, False), "!": (0x31, True), ",": (0xBC, False)}.get(char)


class FakeClock:
    """Stands in for the time module inside core/keyboard.py: sleeping moves the clock on."""

    def __init__(self):
        self.t, self.sleeps = 0.0, 0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.t += seconds


class Recorder:
    """Stands in for SendInput: keeps each call's events as (vk, scan, flags, extra info)."""

    def __init__(self, accept: int | None = None):
        self.calls: list[list[tuple[int, int, int, int]]] = []
        self.accept = accept

    def __call__(self, count, array, size):
        assert size == ctypes.sizeof(keyboard._Input)
        self.calls.append([(array[i].ki.wVk, array[i].ki.wScan, array[i].ki.dwFlags, array[i].ki.dwExtraInfo)
                           for i in range(count)])
        assert all(array[i].type == 1 for i in range(count))  # INPUT_KEYBOARD
        return count if self.accept is None else self.accept

    @property
    def events(self) -> list[tuple[int, int, int, int]]:
        return [e for call in self.calls for e in call]

    def keys(self) -> list[tuple]:
        """('vk', vk, 'down'/'up') or ('u', utf-16 unit, 'down'/'up') for each event."""
        out = []
        for vk, scan, flags, _ in self.events:
            state = "up" if flags & KEYUP else "down"
            out.append(("u", scan, state) if flags & UNICODE else ("vk", vk, state))
        return out


class KeyboardTestCase(unittest.TestCase):
    caps = False

    def setUp(self):
        self.recorder = Recorder()
        for patch in (mock.patch.object(keyboard, "_send_input", self.recorder),
                      mock.patch.object(keyboard, "_key_for", us_key),
                      mock.patch.object(keyboard, "_layout", lambda: 0),
                      mock.patch.object(keyboard, "_caps_lock_on", lambda: self.caps),
                      mock.patch.object(keyboard, "_key_held", lambda vk: False),
                      mock.patch.object(keyboard, "screen_locked", lambda: False),
                      mock.patch.object(keyboard.time, "sleep", lambda s: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def assert_no_bare_enter(self):
        shift_down = False
        for kind, code, state in self.recorder.keys():
            if kind == "vk" and code == VK_SHIFT:
                shift_down = state == "down"
            if kind == "vk" and code == VK_RETURN:
                self.assertTrue(shift_down, "Enter was sent without Shift")
            if kind == "u":
                self.assertNotIn(code, (0x0A, 0x0D), "a newline went in as a character")


class TypeTextTests(KeyboardTestCase):
    def test_letters_go_as_real_keys_with_shift_for_capitals(self):
        keyboard.type_text("Hi")
        self.assertEqual(self.recorder.keys(), [
            ("vk", VK_SHIFT, "down"), ("vk", ord("H"), "down"), ("vk", ord("H"), "up"), ("vk", VK_SHIFT, "up"),
            ("vk", ord("I"), "down"), ("vk", ord("I"), "up")])

    def test_caps_lock_is_allowed_for(self):
        self.caps = True
        keyboard.type_text("Ab1")
        self.assertEqual(self.recorder.keys(), [
            ("vk", ord("A"), "down"), ("vk", ord("A"), "up"),                     # Caps makes it capital
            ("vk", VK_SHIFT, "down"), ("vk", ord("B"), "down"), ("vk", ord("B"), "up"), ("vk", VK_SHIFT, "up"),
            ("vk", ord("1"), "down"), ("vk", ord("1"), "up")])                    # digits ignore Caps

    def test_characters_without_a_key_go_as_unicode(self):
        keyboard.type_text("é")
        self.assertEqual(self.recorder.keys(), [("u", 0xE9, "down"), ("u", 0xE9, "up")])

    def test_emoji_goes_as_a_surrogate_pair_in_one_call(self):
        keyboard.type_text("\U0001F600")
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.keys(), [("u", 0xD83D, "down"), ("u", 0xD83D, "up"),
                                                ("u", 0xDE00, "down"), ("u", 0xDE00, "up")])

    def test_newline_becomes_shift_enter_only_in_a_multiline_box(self):
        keyboard.type_text("a\nb", multiline=True)
        self.assertEqual(self.recorder.keys(), [
            ("vk", ord("A"), "down"), ("vk", ord("A"), "up"),
            ("vk", VK_SHIFT, "down"), ("vk", VK_RETURN, "down"), ("vk", VK_RETURN, "up"), ("vk", VK_SHIFT, "up"),
            ("vk", ord("B"), "down"), ("vk", ord("B"), "up")])

    def test_newline_is_a_space_anywhere_else(self):
        """Shift+Enter submits a single-line Chrome input or the Run box, as Enter would (the review
        of 2026-10-22), so outside a known multi-line box no Enter of any kind is sent."""
        keyboard.type_text("a\n\nb \n c")
        self.assertEqual([k for k in self.recorder.keys() if k[2] == "down"],
                         [("vk", ord("A"), "down"), ("vk", 0x20, "down"), ("vk", ord("B"), "down"),
                          ("vk", 0x20, "down"), ("vk", ord("C"), "down")])
        self.assertNotIn(VK_RETURN, [code for kind, code, _ in self.recorder.keys() if kind == "vk"])

    def test_never_a_bare_enter(self):
        for multiline in (False, True):
            for text in ("hello\n", "a\r\nb", "\r", "x\rx", "\n\n\nend\n\n", "line two", "a\x0db\x0ac"):
                with self.subTest(text=text, multiline=multiline):
                    self.recorder.calls.clear()
                    keyboard.type_text(text, multiline=multiline)
                    self.assert_no_bare_enter()
                    if not multiline:
                        self.assertNotIn(VK_RETURN, [code for kind, code, _ in self.recorder.keys() if kind == "vk"])

    def test_trailing_newlines_are_dropped(self):
        keyboard.type_text("done\n\n")
        self.assertNotIn(("vk", VK_RETURN, "down"), self.recorder.keys())

    def test_control_characters_and_tabs(self):
        keyboard.type_text("\x1b\x00a\tb\x7f\x85")
        self.assertEqual([k for k in self.recorder.keys() if k[2] == "down"],
                         [("vk", ord("A"), "down"), ("vk", 0x20, "down"), ("vk", ord("B"), "down")])

    def test_nothing_to_type_sends_nothing(self):
        keyboard.type_text("\n\r\x00")
        self.assertEqual(self.recorder.calls, [])

    def test_long_text_goes_in_chunks_without_splitting_a_character(self):
        keyboard.type_text("\U0001F600" * 20 + "a" * 20)
        self.assertEqual(len(self.recorder.calls), 3)  # 40 characters, 16 per call
        for call in self.recorder.calls:
            units = [scan for _, scan, flags, _ in call if flags & UNICODE and not flags & KEYUP]
            for i, unit in enumerate(units):  # every high surrogate is followed by its low one
                if 0xD800 <= unit <= 0xDBFF:
                    self.assertTrue(i + 1 < len(units) and 0xDC00 <= units[i + 1] <= 0xDFFF)

    def test_every_event_is_marked_as_its_own(self):
        keyboard.type_text("A \U0001F600\nb")
        keyboard.press("ctrl+f")
        self.assertTrue(all(extra == keyboard._EXTRA_INFO for *_, extra in self.recorder.events))

    def test_refused_input_raises(self):
        self.recorder.accept = 0
        with self.assertRaises(OSError):
            keyboard.type_text("a")

    def test_nothing_goes_to_a_locked_laptop(self):
        with mock.patch.object(keyboard, "screen_locked", lambda: True):
            with self.assertRaises(OSError):
                keyboard.type_text("my message")
            with self.assertRaises(OSError):
                keyboard.press("enter")
        self.assertEqual(self.recorder.calls, [])

    def test_waits_for_held_modifiers(self):
        """Ctrl is let go 0.3 s in: nothing is sent before then, and then the letter alone."""
        clock = FakeClock()
        sent_at = []
        recorder = self.recorder

        def send(count, array, size):
            sent_at.append(clock.t)
            return recorder(count, array, size)

        with mock.patch.object(keyboard, "time", clock), mock.patch.object(keyboard, "_send_input", send), \
                mock.patch.object(keyboard, "_key_held", lambda vk: vk == VK_CONTROL and clock.t < 0.3):
            keyboard.type_text("a")
        self.assertGreaterEqual(min(sent_at), 0.3)  # it really waited for the release
        self.assertLess(max(sent_at), keyboard.MODIFIER_WAIT)
        self.assertGreater(clock.sleeps, 5)  # polled, not one long sleep
        self.assertEqual(self.recorder.keys(), [("vk", ord("A"), "down"), ("vk", ord("A"), "up")])

    def test_a_modifier_still_held_after_the_wait_sends_nothing(self):
        """Until 2026-10-22 it typed anyway after the wait, and with Ctrl down letters are shortcuts."""
        clock = FakeClock()
        with mock.patch.object(keyboard, "time", clock), \
                mock.patch.object(keyboard, "_key_held", lambda vk: vk == VK_CONTROL):
            with self.assertRaises(keyboard.ModifiersHeld) as caught:
                keyboard.type_text("a")
            with self.assertRaises(OSError):  # ModifiersHeld is an OSError, so callers' refusals catch it
                keyboard.press("enter")
        self.assertIn("Ctrl", str(caught.exception))
        self.assertGreaterEqual(clock.t, keyboard.MODIFIER_WAIT)
        self.assertEqual(self.recorder.calls, [])


class PressTests(KeyboardTestCase):
    def test_combination(self):
        keyboard.press("ctrl+f")
        self.assertEqual(self.recorder.keys(), [("vk", VK_CONTROL, "down"), ("vk", ord("F"), "down"),
                                                ("vk", ord("F"), "up"), ("vk", VK_CONTROL, "up")])

    def test_named_keys(self):
        for name, vk in (("enter", 0x0D), ("escape", 0x1B), ("backspace", 0x08), ("tab", 0x09), ("down", 0x28)):
            with self.subTest(name=name):
                self.recorder.calls.clear()
                keyboard.press(name)
                self.assertEqual(self.recorder.keys(), [("vk", vk, "down"), ("vk", vk, "up")])

    def test_shift_enter(self):
        keyboard.press("shift+enter")
        self.assert_no_bare_enter()

    def test_extended_keys_are_flagged(self):
        keyboard.press("home")
        self.assertTrue(all(flags & keyboard._KEYEVENTF_EXTENDEDKEY for _, _, flags, _ in self.recorder.events))
        self.recorder.calls.clear()
        keyboard.press("a")
        self.assertFalse(any(flags & keyboard._KEYEVENTF_EXTENDEDKEY for _, _, flags, _ in self.recorder.events))

    def test_unknown_keys_raise_before_anything_is_sent(self):
        for keys in ("ctrl+nope", "hyper+a", "", "ctrl+", "+a"):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                keyboard.press(keys)
        self.assertEqual(self.recorder.calls, [])


class LayoutTests(unittest.TestCase):
    def test_key_for_reads_vkkeyscan(self):
        cases = {0x0041: (0x41, False), 0x0141: (0x41, True), 0x0641: None, -1: None, 0x000D: None}
        for found, expected in cases.items():
            with self.subTest(found=hex(found)), \
                    mock.patch.object(keyboard._user32, "VkKeyScanExW", lambda char, layout, f=found: f):
                self.assertEqual(keyboard._key_for("a", 0), expected)
        self.assertIsNone(keyboard._key_for("\U0001F600", 0))  # outside the BMP: never a key

    def test_input_structure_has_the_size_windows_expects(self):
        self.assertEqual(ctypes.sizeof(keyboard._Input), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


class WindowTests(unittest.TestCase):
    """Read-only calls on the real desktop."""

    def test_lock_check_answers(self):
        self.assertIn(keyboard.screen_locked(), (True, False))

    def test_no_window(self):
        self.assertEqual(keyboard.window_exe(0), "")
        self.assertFalse(keyboard.focus(0, timeout=0.1))

    def test_front_window_is_read(self):
        front = keyboard.front_window()
        self.assertIsInstance(front, int)
        if front:
            self.assertTrue(keyboard.window_exe(front).endswith(".exe") or keyboard.window_exe(front) == "")


if __name__ == "__main__":
    unittest.main()
