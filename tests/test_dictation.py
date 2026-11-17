"""Dictation (core/dictation.py, tools/dictation.py, the pipeline's capture mode): what's claimed,
what's typed and where, what he hears, and when it refuses. A fake desktop and a fake Gemini
throughout: nothing is typed, focused or sent anywhere."""
import base64
import io
import json
import queue
import threading
import time
import unittest
import wave
from unittest import mock

import numpy as np

from aj.config import DictationSettings, Settings
from aj.core import dictation as core
from aj.core import gemini, keyboard
from aj.core.audio import MIC_RATE
from aj.core.uia import Node
from aj.core.pipeline import Session, Status, TurnTimes, pick_status
from aj.tools import dictation as tools_dictation
from aj.tools import intents

CHROME, WHATSAPP, WEB, OURS = 100, 200, 300, 1


class FakeWindow:
    def __init__(self, hwnd, title, exes=(), aumids=()):
        self.hwnd, self.title, self.exes, self.aumids = hwnd, title, frozenset(exes), frozenset(aumids)


class FakeDesktop:
    """The desktop as core.Desktop sees it. It can type and press Backspace, and nothing else: it
    has no way to press Enter at all. Typing goes in at the caret and Backspace deletes before it,
    as in a real box; `box` is the focused element's runtime id (another id: the focus moved to
    another element of the same window, the address bar say); `multi` makes it a box that takes
    line breaks (a document)."""

    def __init__(self, front=CHROME, editable=True, value="", multi=False):
        self.front_hwnd, self.can_edit, self.text = front, editable, value
        self.caret = len(value)
        self.box, self.multi = (7, 1), multi
        self.typed_multiline: list[bool] = []
        self.speak_busy = False             # AJ (Speak) is listening
        self.dictating_said: list[bool] = []
        self.caret_reads = 0
        self.exes = {CHROME: "chrome.exe", WHATSAPP: "whatsapp.root.exe", WEB: "chrome.exe", OURS: "python.exe"}
        self.titles = {CHROME: "Gmail - Google Chrome", WHATSAPP: "WhatsApp", WEB: "WhatsApp - Google Chrome",
                       OURS: "AJ"}
        self.wins = [FakeWindow(CHROME, self.titles[CHROME], ["chrome.exe"]),
                     FakeWindow(WHATSAPP, "WhatsApp", ["whatsapp.root.exe"], ["5319275a.whatsappdesktop!app"])]
        self.typed: list[str] = []
        self.backspaces = 0
        self.focused: list[int] = []
        self.can_focus = True
        self.on_type = None  # called after each piece is typed (to move the focus away mid-way)

    def front(self):
        return self.front_hwnd

    def focus(self, hwnd):
        self.focused.append(hwnd)
        if self.can_focus:
            self.front_hwnd = hwnd
        return self.can_focus

    def exe(self, hwnd):
        return self.exes.get(hwnd, "")

    def title(self, hwnd):
        return self.titles.get(hwnd, "")

    def ours(self, hwnd):
        return hwnd == OURS

    def alive(self, hwnd):
        return bool(hwnd)

    def windows(self):
        return self.wins

    def editable(self):
        return self.can_edit

    def focused_box(self):
        if self.can_edit is None:
            return None  # UI Automation can't tell
        name = "Type a message to Appa" if self.front_hwnd == WHATSAPP else ""
        return Node(control_type="Document" if self.multi else "Edit", name=name, has_value=bool(self.can_edit),
                    read_only=not self.can_edit, focusable=True, runtime_id=self.box)

    def value(self):
        return self.text

    def caret_at_end(self, box=None):
        self.caret_reads += 1
        if box is not None and box.runtime_id != self.box:
            return False
        return self.caret == len(self.text)

    def speak_listening(self):
        return self.speak_busy

    def dictating(self, on):
        self.dictating_said.append(on)

    def type(self, text, multiline=False):
        self.typed.append(text)
        self.typed_multiline.append(multiline)
        self.text = self.text[:self.caret] + text + self.text[self.caret:]
        self.caret += len(text)
        if self.on_type:
            self.on_type()

    def backspace(self):
        self.backspaces += 1
        if self.caret:
            self.text = self.text[:self.caret - 1] + self.text[self.caret:]
            self.caret -= 1


class FakeEars:
    def __init__(self, text="hello from parakeet"):
        self.text, self.calls = text, 0

    def transcribe(self, samples, rate=MIC_RATE):
        self.calls += 1
        return self.text


def speech(secs=1.0):
    return np.full(int(MIC_RATE * secs), 0.1, np.float32)


def heard(text):
    """gemini.transcribe answering `text`."""
    return mock.patch.object(gemini, "transcribe", return_value=text)


def gemini_down():
    return mock.patch.object(gemini, "transcribe", side_effect=gemini.GeminiUnavailable("no key"))


def polished(text):
    return mock.patch.object(gemini, "generate", return_value=text)


class IntentTests(unittest.TestCase):
    """What the fast path claims, and what it leaves alone."""

    def setUp(self):
        self.desk = core.Dictation(desktop=FakeDesktop())
        self.saved, tools_dictation.desk = tools_dictation.desk, self.desk
        intents.collect([tools_dictation])

    def tearDown(self):
        tools_dictation.desk = self.saved
        intents.collect()

    def test_dictation_without_the_words(self):
        for said in ("Write this down.", "Write this down for me.", "Type this for me.", "Take dictation.",
                     "Dictate.", "AJ, write this down.", "Hey Computer, take this down", "Type for me.",
                     "I want to dictate something.", "Write down what I say.", "Type what I'm saying."):
            self.assertEqual(intents.match(said), ("start_dictation", {"polish": False}), said)

    def test_dictation_into_whatsapp_and_polished(self):
        self.assertEqual(intents.match("Computer, write this down for me on WhatsApp."),
                         ("start_dictation", {"polish": False, "app": "whatsapp"}))
        self.assertEqual(intents.match("Write this down and make it sound nicer."),
                         ("start_dictation", {"polish": True}))
        self.assertEqual(intents.match("Take dictation on WhatsApp and make it more polite"),
                         ("start_dictation", {"polish": True, "app": "whatsapp"}))

    def test_typing_in_one_go(self):
        self.assertEqual(intents.match("Type I'll be late."), ("type_text", {"text": "ill be late", "polish": False}))
        self.assertEqual(intents.match("Type that I'll be late, make it polite."),
                         ("type_text", {"text": "ill be late", "polish": True}))
        self.assertEqual(intents.match("Type see you at 5 on WhatsApp"),
                         ("type_text", {"text": "see you at 5", "polish": False, "app": "whatsapp"}))

    def test_type_that_types_what_was_kept_else_starts_dictation(self):
        self.assertEqual(intents.match("Type that."), ("start_dictation", {"polish": False}))
        self.desk._ready = ("Hi Amma, I'm on my way.", time.monotonic())
        self.assertEqual(intents.match("Type that."), ("type_that", {"polish": False}))
        self.assertEqual(intents.match("Type it in."), ("type_that", {"polish": False}))

    def test_make_it_nicer_only_right_after_typing(self):
        self.assertIsNone(intents.match("Make it nicer."))  # about his own reply: the model's business
        self.desk._typed = core.Outcome(True, "typed", core.TYPED, "im late", CHROME)
        for said in ("Make it nicer.", "Make that sound more refined and polite.", "Make it more polite.",
                     "Polish it."):
            self.assertEqual(intents.match(said), ("polish_typed", {}), said)

    def test_what_it_leaves_alone(self):
        for said in ("Write a message to mum saying I'll be late.", "What type of laptop is this?", "Type of laptop",
                     "Type a message to Appa.", "Write me a poem.", "Make it louder.", "Note this down."):
            self.assertIsNone(intents.match(said), said)


class StripCommandTests(unittest.TestCase):
    def test_the_command_words_come_off(self):
        cases = {
            "Computer, type I'll be late, make it polite.": "I'll be late.",
            "Type that I'll be late.": "I'll be late.",
            "Type that sounds good.": "That sounds good.",
            "AJ, type hello there.": "Hello there.",
            "Okay, type out: see you at 5": "See you at 5",  # he began a sentence: so does the message
            "okay type out see you at 5": "see you at 5",
            "Type this: Hi Amma.": "Hi Amma.",
            "Type a message saying I'm on my way.": "I'm on my way.",
            "Type hi Appa\nsee you soon.": "Hi Appa\nsee you soon.",
            "Type I'll call you, and make it nicer.": "I'll call you.",
        }
        for said, words in cases.items():
            self.assertEqual(core.strip_command(said), words, said)

    def test_the_app_comes_off_only_when_named(self):
        self.assertEqual(core.strip_command("Type I'm coming on WhatsApp.", app="whatsapp"), "I'm coming.")
        self.assertEqual(core.strip_command("Type I'm coming on WhatsApp."), "I'm coming on WhatsApp.")

    def test_no_command_no_change(self):
        for text in ("Hello there.", "Typed it.", "Typing is fun."):
            self.assertEqual(core.strip_command(text), text)


class SmallRuleTests(unittest.TestCase):
    def test_cancel_is_the_whole_answer(self):
        for said in ("Cancel.", "Never mind.", "No.", "Nothing.", "Okay, cancel that.", "Forget it", "Nope",
                     "Stop."):
            self.assertTrue(core.is_cancel(said), said)
        for said in ("No worries, see you tomorrow.", "Stop by the shop later.", "Nothing much, you?",
                     "Cancel my booking for tomorrow please"):
            self.assertFalse(core.is_cancel(said), said)

    def test_spoken_punctuation_left_as_words(self):
        cases = {  # as Parakeet wrote them, live 2026-10-22
            "The meeting moved to Thursday. New line. Please bring the report.":
                "The meeting moved to Thursday.\nPlease bring the report.",
            "My flight lands at 1015 in the morning full stop can you pick me up question mark.":
                "My flight lands at 1015 in the morning. Can you pick me up?",
            "Hi Amma comma how are you": "Hi Amma, how are you",
            "Dear Sir new paragraph thank you": "Dear Sir\n\nThank you",
            "They launched a new line of shoes.": "They launched a new line of shoes.",
            "Hi Appa, I'll be home by 7:30 tonight.": "Hi Appa, I'll be home by 7:30 tonight.",
        }
        for said, text in cases.items():
            self.assertEqual(core.said_punctuation(said), text, said)

    def test_a_drafted_message_can_be_typed(self):
        self.assertEqual(core.draft_in('Here: "Hi Amma, I\'m running late tonight." You send it.'),
                         "Hi Amma, I'm running late tonight.")
        self.assertEqual(core.draft_in("Tokyo. Big city."), "")

    def test_text_as_it_will_be_typed(self):
        self.assertEqual(core.as_typed("  Hi\r\nthere\tyou\x07\n\n"), "Hi\nthere you")

    def test_one_backspace_each(self):
        self.assertTrue(core.one_key_each("Hi Appa,\nsee you at 7:30. Café"))
        self.assertFalse(core.one_key_each("thanks 👍"))
        self.assertFalse(core.one_key_each("é"))  # a combining accent

    def test_wav(self):
        data = core.wav_bytes(np.array([0.0, 0.5, -0.5, 2.0, -2.0], np.float32))
        with wave.open(io.BytesIO(data)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()),
                             (1, 2, MIC_RATE, 5))
            pcm = np.frombuffer(w.readframes(5), dtype="<i2")
        self.assertEqual(list(pcm), [0, 16383, -16383, 32767, -32767])  # clipped, never wrapped

    def test_pieces_are_joined_with_a_pause(self):
        joined = core.joined([speech(1.0), speech(0.5)])
        self.assertEqual(len(joined), int(MIC_RATE * (1.5 + core.JOIN_GAP_SECS)))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply_with(parts, finish="STOP"):
    return FakeResponse(json.dumps({"candidates": [{"content": {"parts": parts}, "finishReason": finish}]}).encode())


class GeminiTranscribeTests(unittest.TestCase):
    """The request gemini.transcribe sends, over a fake HTTP layer."""

    def send(self, model, response=None):
        sent = []

        def urlopen(req, timeout=None):
            sent.append(req)
            return response or reply_with([{"audioTranscription": {"text": "Hi Appa."}}])

        with mock.patch.object(gemini, "read_secret", return_value="SECRET-KEY"), \
                mock.patch.object(gemini.urllib.request, "urlopen", side_effect=urlopen):
            text = gemini.transcribe(b"RIFF-audio", hint="It is a WhatsApp message.", model=model)
        return text, sent[0], json.loads(sent[0].data)

    def test_the_transcription_model(self):
        text, req, body = self.send("gemini-3.5-transcribe")
        self.assertEqual(text, "Hi Appa.")  # read from audioTranscription
        self.assertTrue(req.full_url.endswith("/models/gemini-3.5-transcribe:generateContent"))
        self.assertNotIn("SECRET-KEY", req.full_url)  # the key goes in a header, never the URL
        self.assertEqual(req.get_header("X-goog-api-key"), "SECRET-KEY")
        audio, prompt = body["contents"][0]["parts"]
        self.assertEqual(audio["inline_data"], {"mime_type": "audio/wav",
                                                "data": base64.b64encode(b"RIFF-audio").decode()})
        self.assertNotIn("systemInstruction", body)  # refused by this model: the rules go in the prompt
        self.assertIn("Spell names as they are heard", prompt["text"])
        self.assertIn("It is a WhatsApp message.", prompt["text"])
        self.assertEqual(body["generationConfig"]["temperature"], 0)
        self.assertNotIn("thinkingConfig", body["generationConfig"])  # refused by this model too

    def test_a_flash_model(self):
        text, req, body = self.send("gemini-3.8-flash", reply_with([{"text": "Hi Appa. "}]))
        self.assertEqual(text, "Hi Appa.")
        self.assertIn("Appa, Amma, Suhinish", body["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingBudget": 0})

    def test_half_an_answer_is_no_answer(self):
        with self.assertRaises(gemini.GeminiUnavailable):
            self.send("gemini-3.5-transcribe", reply_with([{"text": "Hi Appa, Hi Appa, Hi"}], finish="MAX_TOKENS"))

    def test_no_key_never_reaches_the_network(self):
        with mock.patch.object(gemini, "read_secret", return_value=None), \
                mock.patch.object(gemini.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(gemini.GeminiUnavailable):
                gemini.transcribe(b"RIFF")
        urlopen.assert_not_called()


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self.s = DictationSettings()

    def test_gemini_first(self):
        with heard("Hi Appa, I'll be home by 7:30 tonight."):
            out = core.transcribe([speech(2.0)], local=FakeEars().transcribe, settings=self.s)
        self.assertEqual(out, core.Heard("Hi Appa, I'll be home by 7:30 tonight.", "gemini"))

    def test_the_offline_ears_when_gemini_cant(self):
        ears = FakeEars("hi apple")
        with gemini_down():
            out = core.transcribe([speech(1.0), speech(1.0)], local=ears.transcribe, settings=self.s)
        self.assertEqual(out, core.Heard("hi apple hi apple", "offline"))  # each VAD piece on its own
        self.assertEqual(ears.calls, 2)

    def test_a_transcript_too_long_for_the_audio_is_not_trusted(self):
        with heard("word " * 200):
            out = core.transcribe([speech(1.0)], local=FakeEars("hi").transcribe, settings=self.s)
        self.assertEqual(out.engine, "offline")

    def test_busy_gemini_gets_one_more_try(self):
        busy = gemini.GeminiUnavailable("HTTP 503: high demand", 503)
        with mock.patch.object(gemini, "transcribe", side_effect=[busy, "Hi Amma."]) as call, \
                mock.patch.object(core, "BUSY_RETRY_SECS", 0):
            out = core.transcribe([speech(1.0)], settings=self.s)
        self.assertEqual((out.text, call.call_count), ("Hi Amma.", 2))

    def test_a_refusal_is_not_retried(self):
        refused = gemini.GeminiUnavailable("HTTP 400: bad request", 400)
        with mock.patch.object(gemini, "transcribe", side_effect=refused) as call:
            out = core.transcribe([speech(1.0)], local=FakeEars("hi").transcribe, settings=self.s)
        self.assertEqual((out.engine, call.call_count), ("offline", 1))


class TypingTests(unittest.TestCase):
    def make(self, front=CHROME, **kw):
        self.desktop = FakeDesktop(front, **kw)
        return core.Dictation(desktop=self.desktop, ears=FakeEars())

    def test_one_go_typing_hears_its_own_audio(self):
        desk = self.make()
        desk.lend([speech(2.0)], "Type I'll be late.", command=True)
        with heard("Computer, type I'll be late, Amma."):
            line = desk.type_words("ill be late")
        self.assertEqual("".join(self.desktop.typed), "I'll be late, Amma.")
        self.assertEqual(line, "Typed it.")

    def test_whatsapp_gets_told_to_press_send(self):
        desk = self.make(WHATSAPP)
        self.assertEqual(desk.type_words("On my way."), "Typed it in WhatsApp. Press send when you're ready.")
        desk = self.make(WEB)  # WhatsApp Web, in Chrome: told by its title
        self.assertEqual(desk.type_words("On my way."), "Typed it in WhatsApp. Press send when you're ready.")

    def test_never_sent_and_never_enter(self):
        desk = self.make(WHATSAPP)
        line = desk.type_words("Hi Appa,\nsee you at 7:30.\r\n")
        self.assertNotIn("sent", line.lower())
        self.assertEqual("".join(self.desktop.typed), "Hi Appa,\nsee you at 7:30.")  # a line break, no Enter
        self.assertEqual(self.desktop.typed_multiline, [True])  # WhatsApp's message box takes line breaks
        self.assertTrue(all("\r" not in piece for piece in self.desktop.typed))
        self.assertEqual(self.desktop.backspaces, 0)

    def test_a_line_break_is_a_space_outside_a_multiline_box(self):
        """Shift+Enter submits a single-line Chrome input or the Run box (the review of 2026-10-22)."""
        desk = self.make(CHROME)
        desk.type_words("Hi Appa,\n\nsee you soon.")
        self.assertEqual(self.desktop.typed, ["Hi Appa, see you soon."])
        self.assertEqual(self.desktop.typed_multiline, [False])
        self.assertEqual(desk._typed.text, "Hi Appa, see you soon.")  # what "make it nicer" looks for
        desk = self.make(CHROME, multi=True)  # a document takes them
        desk.type_words("Hi Appa,\nsee you soon.")
        self.assertEqual(self.desktop.typed, ["Hi Appa,\nsee you soon."])
        self.assertEqual(self.desktop.typed_multiline, [True])

    def test_the_focus_moving_inside_the_same_window_stops_it(self):
        """A click into the address bar is the same window: the box is followed by its runtime id."""
        desk = self.make()
        self.desktop.on_type = lambda: setattr(self.desktop, "box", (7, 99))
        with self.assertRaises(core.NotDone) as said:
            desk.type_words("word " * 40)
        self.assertEqual(str(said.exception), core.MOVED_AWAY)
        self.assertEqual(len(self.desktop.typed), 1)
        self.assertFalse(desk.has_ready())  # part of it is in: typing it all again would double it

    def test_a_cancel_stops_it_before_the_window_comes_forward(self):
        desk = self.make(WHATSAPP)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(core.NotDone) as said:
            desk.type_words("Hi.", cancel=stop)
        self.assertEqual(str(said.exception), core.STOPPED)
        self.assertEqual((self.desktop.typed, self.desktop.focused), ([], []))

    def test_a_cancel_mid_way_stops_before_the_next_piece(self):
        desk = self.make()
        stop = threading.Event()
        self.desktop.on_type = stop.set  # the dispatcher gives up while the first piece goes in
        with self.assertRaises(core.NotDone) as said:
            desk.type_words("word " * 40, cancel=stop)
        self.assertEqual(str(said.exception), core.STOPPED_PART)
        self.assertEqual(len(self.desktop.typed), 1)
        self.assertFalse(desk.has_ready())

    def test_abort_stops_typing_in_flight(self):
        """"Stop" from the user, or talking over the assistant: Dictation.abort()."""
        desk = self.make()
        self.desktop.on_type = desk.abort
        with self.assertRaises(core.NotDone) as said:
            desk.type_words("word " * 40)
        self.assertEqual(str(said.exception), core.STOPPED_PART)
        self.assertEqual(len(self.desktop.typed), 1)
        self.assertEqual(desk.type_words("And again."), "Typed it.")  # an earlier abort never blocks the next one
        self.assertFalse(desk.abort())  # nothing in flight now

    def test_a_cancelled_arming_arms_nothing(self):
        desk = self.make(WHATSAPP)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(core.NotDone) as said:
            desk.arm(cancel=stop)
        self.assertEqual(str(said.exception), core.STOPPED)
        self.assertIsNone(desk.armed)
        late = threading.Event()  # the tool timed out while the window was being brought forward
        self.desktop.focus = lambda hwnd: late.set() or True
        with self.assertRaises(core.NotDone):
            desk.arm(cancel=late)
        self.assertIsNone(desk.armed)

    def test_a_cancel_stops_gemini_retries(self):
        stop = threading.Event()
        calls = []

        def busy(*a, **k):
            calls.append(1)
            stop.set()  # the dispatcher gives up during the first try
            raise gemini.GeminiUnavailable("high demand", code=503)

        with mock.patch.object(gemini, "transcribe", side_effect=busy), mock.patch.object(core.time, "sleep"):
            heard = core.transcribe([speech(1.0)], local=lambda p: "offline words", cancel=stop)
        self.assertEqual((heard.text, len(calls)), ("", 1))
        with mock.patch.object(gemini, "generate") as generate:
            self.assertIsNone(core.polish("i late", cancel=stop))
        generate.assert_not_called()

    def test_a_held_key_types_nothing_and_keeps_the_words(self):
        desk = self.make()

        def held(text, multiline=False):
            raise keyboard.ModifiersHeld("Ctrl is still held down, so no keys were sent")

        self.desktop.type = held
        with self.assertRaises(core.NotDone) as said:
            desk.type_words("Hi Amma.")
        self.assertEqual(str(said.exception), core.KEYS_HELD)
        self.assertTrue(desk.has_ready())  # "type that" once he lets go

    def test_offline_ears_are_owned_up_to(self):
        desk = self.make()
        desk.lend([speech(2.0)], "", command=False)
        with gemini_down():
            line = desk.type_words("")
        self.assertEqual("".join(self.desktop.typed), "hello from parakeet")
        self.assertEqual(line, f"{core.OFFLINE} Typed it.")

    def test_one_go_offline_types_the_command_transcript_minus_the_command(self):
        desk = self.make()
        desk.lend([speech(2.0)], "Type I'll be late.", command=True)
        with gemini_down():
            line = desk.type_words("ill be late")
        self.assertEqual("".join(self.desktop.typed), "I'll be late.")
        self.assertTrue(line.startswith(core.OFFLINE))

    def test_no_text_box_no_typing_and_the_words_are_kept(self):
        for editable in (False, None):  # None: UI Automation can't tell, so no stray keys either
            desk = self.make(editable=editable)
            with self.assertRaises(core.NotDone) as said:
                desk.type_words("Hi Amma.")
            self.assertEqual(str(said.exception), core.CLICK_FIRST)
            self.assertEqual(self.desktop.typed, [])
            self.assertTrue(desk.has_ready())
        self.desktop.can_edit = True  # he clicks into the box and says "type that"
        self.assertEqual(desk.type_ready(), "Typed it.")
        self.assertEqual(self.desktop.typed, ["Hi Amma."])
        self.assertFalse(desk.has_ready())

    def test_typed_into_the_assistants_box_goes_to_the_window_before(self):
        desk = self.make(OURS)
        desk.watch.seen(WHATSAPP)
        desk.watch.seen(OURS)  # his own window never counts
        desk.type_words("On my way.")
        self.assertEqual(self.desktop.focused, [WHATSAPP])

    def test_nowhere_to_type(self):
        desk = self.make(OURS)
        with self.assertRaises(core.NotDone) as said:
            desk.type_words("Hi.")
        self.assertEqual(str(said.exception), core.NO_WINDOW)

    def test_polished_or_said_plainly_when_it_couldnt(self):
        desk = self.make()
        with polished("I'm sorry, I'll be a little late."):
            self.assertEqual(desk.type_words("i late", polish=True), "Typed the polished version.")
        self.assertEqual(self.desktop.typed[-1], "I'm sorry, I'll be a little late.")
        with mock.patch.object(gemini, "generate", side_effect=gemini.GeminiUnavailable("offline")):
            self.assertEqual(desk.type_words("i late", polish=True), core.TYPED_UNPOLISHED)
        self.assertEqual(self.desktop.typed[-1], "i late")

    def test_moving_away_mid_typing_stops_it(self):
        desk = self.make()
        self.desktop.on_type = lambda: setattr(self.desktop, "front_hwnd", WHATSAPP)
        with self.assertRaises(core.NotDone) as said:
            desk.type_words("word " * 40)
        self.assertEqual(str(said.exception), core.MOVED_AWAY)
        self.assertEqual(len(self.desktop.typed), 1)
        self.assertFalse(desk.has_ready())  # part of it is in: typing it all again would double it

    def test_pieces_never_end_in_a_line_break(self):
        text = ("line one is here\n" * 12).strip()
        pieces = core._chunks(text)
        self.assertEqual("".join(pieces), text)
        self.assertTrue(all(not p.endswith("\n") for p in pieces))

    def test_waiting_for_dictation(self):
        desk = self.make(WHATSAPP)
        self.assertEqual(desk.arm(polish=True), core.GO_AHEAD)
        self.assertEqual(desk.armed, core.Request(WHATSAPP, True, ""))
        self.assertEqual(desk.disarm(), core.Request(WHATSAPP, True, ""))
        self.assertIsNone(desk.armed)

    def test_a_dictation_needs_a_text_box_first(self):
        desk = self.make(editable=False)
        with self.assertRaises(core.NotDone) as said:
            desk.arm()
        self.assertEqual(str(said.exception), core.CLICK_FIRST_ARM)
        self.assertIsNone(desk.armed)

    def test_a_named_app_is_found(self):
        desk = self.make(CHROME)
        desk.arm(app="whatsapp")
        self.assertEqual(desk.armed.hwnd, WHATSAPP)
        self.desktop.wins, self.desktop.front_hwnd = [], CHROME
        with self.assertRaises(core.NotDone) as said:
            desk.arm(app="whatsapp")
        self.assertEqual(str(said.exception), "WhatsApp isn't open.")

    def real_keys(self, exe: str, focused: Node, text: str) -> tuple[core.Outcome, list[tuple[int, bool]]]:
        """core.type_into on the REAL core.Desktop and the REAL keyboard module, with only SendInput
        (and the window and UI Automation reads) faked: what keys would reach the window. The
        review of 2026-10-22: a test that fakes type_text itself can't see a stray Enter."""
        sent: list[tuple[int, bool]] = []  # (virtual key, key down)

        def send_input(count, array, size):
            for i in range(count):
                ki = array[i].ki
                if not ki.dwFlags & 0x4:  # a key, not a Unicode character
                    sent.append((ki.wVk, not ki.dwFlags & 0x2))
            return count

        us = {" ": (0x20, False), ",": (0xBC, False), ".": (0xBE, False)}
        with mock.patch.object(keyboard, "_send_input", send_input), \
                mock.patch.object(keyboard, "_key_for", lambda c, layout: us.get(c) or (
                    (ord(c.upper()), c.isupper()) if c.isascii() and c.isalpha() else None)), \
                mock.patch.object(keyboard, "_layout", lambda: 0), \
                mock.patch.object(keyboard, "_caps_lock_on", lambda: False), \
                mock.patch.object(keyboard, "_key_held", lambda vk: False), \
                mock.patch.object(keyboard, "screen_locked", lambda: False), \
                mock.patch.object(keyboard, "focus", return_value=True), \
                mock.patch.object(keyboard, "front_window", return_value=4242), \
                mock.patch.object(keyboard, "window_exe", return_value=exe), \
                mock.patch("aj.core.uia.focused_node", return_value=focused), \
                mock.patch.object(core.Desktop, "alive", return_value=True), \
                mock.patch.object(core.Desktop, "title", return_value=""):
            out = core.type_into(4242, text)
        return out, sent

    def test_the_real_keyboard_sends_no_enter_into_a_single_line_box(self):
        chrome_input = Node(control_type="Edit", has_value=True, focusable=True, runtime_id=(1, 2),
                            aria_properties="readonly=false;required=false")
        out, sent = self.real_keys("chrome.exe", chrome_input, "Hi Appa,\nsee you soon.\n")
        self.assertTrue(out.ok)
        self.assertEqual(out.text, "Hi Appa, see you soon.")
        self.assertNotIn(0x0D, [vk for vk, _ in sent])  # no Enter, not even Shift+Enter
        self.assertIn((0x20, True), sent)  # the line break went in as a space

    def test_the_real_keyboard_sends_shift_enter_only_into_a_multiline_box(self):
        message_box = Node(control_type="Edit", name="Type a message to Appa", has_value=True, has_text=True,
                           focusable=True, runtime_id=(1, 3))
        document = Node(control_type="Document", has_text=True, focusable=True, runtime_id=(1, 4))
        for exe, box in (("whatsapp.root.exe", message_box), ("winword.exe", document)):
            with self.subTest(exe=exe):
                out, sent = self.real_keys(exe, box, "Hi Appa,\nsee you soon.")
                self.assertTrue(out.ok)
                self.assertEqual([vk for vk, _ in sent].count(0x0D), 2)  # one press: down and up
                shift = False
                for vk, down in sent:
                    if vk == 0x10:
                        shift = down
                    if vk == 0x0D:
                        self.assertTrue(shift, "Enter without Shift")


class ReplaceTests(unittest.TestCase):
    def typed(self, front=CHROME, text="i late sorry", before="Hello. "):
        self.desktop = FakeDesktop(front, value=before)
        desk = core.Dictation(desktop=self.desktop)
        desk.type_words(text)
        return desk

    def test_the_caret_not_at_the_end_deletes_nothing(self):
        """Backspace deletes before the caret: with the caret moved back, the user's own words would
        go (the review of 2026-10-22). It says so and offers "type that"."""
        desk = self.typed()
        self.desktop.caret = 3  # he clicked back into "Hello."
        before = self.desktop.text
        with polished("Sorry, I'm running late."), self.assertRaises(core.NotDone) as said:
            desk.polish_last()
        self.assertEqual(str(said.exception), core.NOT_AT_END)
        self.assertIn("type that", core.NOT_AT_END)
        self.assertEqual((self.desktop.backspaces, self.desktop.text), (0, before))
        self.assertEqual(desk._ready[0], "Sorry, I'm running late.")

    def test_a_caret_that_cant_be_read_deletes_nothing(self):
        desk = self.typed()
        self.desktop.caret_at_end = lambda box=None: None
        with polished("Sorry, I'm running late."), self.assertRaises(core.NotDone):
            desk.polish_last()
        self.assertEqual(self.desktop.backspaces, 0)

    def test_the_caret_moving_mid_way_stops_the_deleting(self):
        desk = self.typed(text="i am running a little late sorry")
        original = self.desktop.backspace

        def click_back():
            original()
            if self.desktop.backspaces == 3:
                self.desktop.caret = 2  # he clicks back into the text mid-way

        self.desktop.backspace = click_back
        with polished("Sorry, I'm running a little late."), self.assertRaises(core.NotDone) as said:
            desk.polish_last()
        self.assertEqual(str(said.exception), core.CHECK_BOX)
        self.assertEqual(self.desktop.backspaces, 3)  # stopped before the very next one
        self.assertTrue(self.desktop.text.startswith("Hello. "))  # not one character of his own text went
        self.assertNotIn("Sorry", self.desktop.text)  # and nothing typed after the surprise

    def test_another_box_in_the_same_window_stops_the_deleting(self):
        desk = self.typed(text="i am running a little late sorry")
        original = self.desktop.backspace

        def address_bar():
            original()
            self.desktop.box = (7, 99)

        self.desktop.backspace = address_bar
        with polished("Sorry, I'm running a little late."), self.assertRaises(core.NotDone):
            desk.polish_last()
        self.assertEqual(self.desktop.backspaces, 1)

    def test_the_caret_is_read_before_every_backspace_and_stays_quick(self):
        """One caret read per Backspace (the review of 2026-10-22). With the fake desktop 100
        characters take a few milliseconds; the real cost is one UI Automation read per key."""
        desk = self.typed(text="x" * 100)
        with polished("Y" * 100):
            t0 = time.perf_counter()
            desk.polish_last()
            took = time.perf_counter() - t0
        self.assertEqual(self.desktop.backspaces, 100)
        self.assertEqual(self.desktop.caret_reads, 100)  # once before deleting, then before each of the other 99
        self.assertLess(took, 0.5)

    def test_safe_replace(self):
        desk = self.typed(WHATSAPP)
        with polished("Sorry, I'm running late."):
            line = desk.polish_last()
        self.assertEqual(self.desktop.backspaces, len("i late sorry"))
        self.assertEqual(self.desktop.text, "Hello. Sorry, I'm running late.")
        self.assertEqual(line, "Replaced it with the polished version. Press send when you're ready.")

    def test_the_box_changed(self):
        desk = self.typed()
        self.desktop.text += " and more"
        with polished("Sorry, I'm running late."), self.assertRaises(core.NotDone) as said:
            desk.polish_last()
        self.assertEqual(str(said.exception), core.CANT_REPLACE)
        self.assertEqual(self.desktop.backspaces, 0)
        self.assertEqual(desk._ready[0], "Sorry, I'm running late.")  # "type that" puts it after

    def test_another_window_is_in_front(self):
        desk = self.typed()
        self.desktop.front_hwnd = WHATSAPP
        with polished("Sorry, I'm running late."), self.assertRaises(core.NotDone):
            desk.polish_last()
        self.assertEqual(self.desktop.backspaces, 0)

    def test_text_a_backspace_can_eat_more_of(self):
        desk = self.typed(text="ok 👍")
        with polished("Okay, thank you 👍"), self.assertRaises(core.NotDone):
            desk.polish_last()
        self.assertEqual(self.desktop.backspaces, 0)

    def test_an_unreadable_box(self):
        desk = self.typed()
        self.desktop.value = lambda: None
        with polished("Sorry, I'm running late."), self.assertRaises(core.NotDone):
            desk.polish_last()
        self.assertEqual(self.desktop.backspaces, 0)

    def test_stops_when_the_box_doesnt_read_as_expected(self):
        desk = self.typed()
        original = self.desktop.backspace

        def greedy():
            original()
            self.desktop.text = self.desktop.text[:-1]  # one Backspace took two characters

        self.desktop.backspace = greedy
        with polished("Sorry, I'm running late."), self.assertRaises(core.NotDone) as said:
            desk.polish_last()
        self.assertEqual(str(said.exception), core.CHECK_BOX)
        self.assertNotIn("Sorry", self.desktop.text)  # nothing typed after the surprise

    def test_already_fine(self):
        desk = self.typed(text="Sorry, I'm running late.")
        with polished("Sorry, I'm running late."):
            self.assertEqual(desk.polish_last(), core.ALREADY_FINE)
        self.assertEqual(self.desktop.backspaces, 0)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.desktop = FakeDesktop(editable=False)
        self.saved, tools_dictation.desk = tools_dictation.desk, core.Dictation(desktop=self.desktop)

    def tearDown(self):
        tools_dictation.desk = self.saved

    def test_not_done_becomes_a_tool_error(self):
        from aj.tools.registry import ToolError, get
        with self.assertRaises(ToolError):
            tools_dictation.type_text("hi")
        self.assertTrue(all(get(n).cloud for n in ("start_dictation", "type_text", "type_that", "polish_typed")))


# ---- the pipeline -------------------------------------------------------------------------------

class FakeMic:
    def __init__(self):
        self.blocks: queue.Queue = queue.Queue()

    def open(self):
        pass

    def close(self):
        pass


class ScriptedEars:
    """Each fed block that carries a marker comes back as one finished utterance."""
    hearing_speech = False

    def __init__(self):
        self.transcribed = []

    def reset(self):
        pass

    def feed(self, block):
        return [block] if block[0] > 0 else []

    def transcribe(self, samples, rate=MIC_RATE):
        self.transcribed.append(samples)
        return "what time is it"


class FakeDesk:
    def __init__(self, armed=True):
        self.armed = object() if armed else None
        self.heard_audio = "unset"
        self.disarmed = 0

    def hear(self, pieces):
        self.heard_audio = pieces

    def disarm(self):
        self.disarmed += 1
        request, self.armed = self.armed, None
        return request


class FakeBrain:
    base = "http://127.0.0.1:1"

    def __init__(self, desk):
        self.cancel = threading.Event()
        self.desk = desk
        self.dictated, self.asked = [], []

    def dictate(self, pieces, mouth, times, capped=False):
        self.desk.disarm()
        self.dictated.append((pieces, capped))
        return ("Hi Amma.", "Typed it.") if pieces else ("", core.NOTHING_HEARD)

    def reply(self, text, mouth, times):
        self.asked.append(text)
        return "It's noon."


def wait_until(condition, timeout=3.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class CaptureTests(unittest.TestCase):
    def start(self, armed=True):
        settings = Settings()
        settings.dictation.silence_secs, settings.dictation.start_secs = 0.15, 0.3
        self.desk, self.ears, self.mic = FakeDesk(armed), ScriptedEars(), FakeMic()
        self.brain, self.stop = FakeBrain(self.desk), threading.Event()
        self.statuses, self.turns = [], []
        self.session = Session(settings, self.ears, self.brain, None, self.mic, self.stop,
                               on_turn=lambda text, reply, times: self.turns.append((text, reply)),
                               on_status=self.statuses.append, dictation=self.desk)
        self.thread = threading.Thread(target=self.session.run, daemon=True)
        self.thread.start()

    def tearDown(self):
        if hasattr(self, "stop"):
            self.stop.set()
            self.thread.join(2)

    def say(self, n):
        for _ in range(n):
            self.mic.blocks.put(np.ones(512, np.float32))

    def test_his_next_words_are_typed_not_answered(self):
        self.start()
        self.assertTrue(wait_until(lambda: Status.DICTATING in self.statuses))
        self.say(2)  # two pieces, with a pause shorter than the dictation's
        # The log gets the length only (the review of 2026-10-22); the window gets the words (on_heard).
        self.assertTrue(wait_until(lambda: self.turns == [("(dictation, 8 characters)", "Typed it.")]))
        pieces, capped = self.brain.dictated[0]
        self.assertEqual((len(pieces), capped), (2, False))  # one dictation, both pieces
        self.assertEqual(self.ears.transcribed, [])  # never through Parakeet or the model
        self.assertEqual(self.brain.asked, [])

    def test_nothing_said_turns_dictation_off(self):
        self.start()
        self.assertTrue(wait_until(lambda: self.brain.dictated == [(None, False)]))
        self.assertEqual(self.turns, [("(dictation)", core.NOTHING_HEARD)])

    def test_mic_off_drops_the_dictation(self):
        self.start()
        self.assertTrue(wait_until(lambda: Status.DICTATING in self.statuses))
        self.session.set_listening(False)
        self.assertTrue(wait_until(lambda: self.desk.disarmed == 1))
        self.assertEqual(self.brain.dictated, [])

    def test_a_normal_turn_carries_its_audio(self):
        self.start(armed=False)
        self.say(1)
        self.assertTrue(wait_until(lambda: self.brain.asked == ["what time is it"]))
        self.assertEqual(len(self.desk.heard_audio), 1)  # for "type <words>": Gemini hears it

    def test_status(self):
        self.assertEqual(pick_status(True, False, False, False, dictating=True), Status.DICTATING)
        self.assertEqual(pick_status(True, False, True, False, dictating=True), Status.DICTATING)
        self.assertEqual(pick_status(True, True, False, True, dictating=True), Status.SPEAKING)  # "Go ahead."
        self.assertEqual(pick_status(False, False, False, False, dictating=True), Status.MIC_OFF)


class PrivacyTests(unittest.TestCase):
    """The review of 2026-10-22: a dictation's words never reach aj.log (engine.print_turn writes
    what on_turn is given) or memory; the window still shows them (on_heard)."""

    def test_a_typed_dictation_is_logged_as_its_length(self):
        class TypingBrain(FakeBrain):
            typed_turn = None

            def reply(self, text, mouth, times):
                self.typed_turn = len(text)  # armed: the typed words were the message
                return "Typed it."

        settings = Settings()
        brain, stop, turns, heard = TypingBrain(FakeDesk(armed=False)), threading.Event(), [], []
        session = Session(settings, ScriptedEars(), brain, None, FakeMic(), stop, listening=False,
                          on_turn=lambda text, reply, times: turns.append((text, reply)), on_heard=heard.append)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        session.ask("Hi Amma, see you at 7")  # typed into the window while a dictation waited
        said = "Hi Amma, see you at 7"
        self.assertTrue(wait_until(lambda: turns == [(f"(dictation, {len(said)} characters)", "Typed it.")]))
        self.assertEqual(heard, ["Hi Amma, see you at 7"])  # on screen only
        stop.set()
        thread.join(2)

    def test_the_brain_marks_one_go_typing_and_keeps_it_out_of_memory(self):
        from aj.core.brain import Brain
        from aj.tools.dispatch import ToolResult

        class Dispatcher:
            def run(self, name, args=None):
                return ToolResult("Typed it.", "ok")

        memory = mock.Mock()
        memory.context_for.return_value = ""
        brain = Brain("http://127.0.0.1:1", Settings(), dispatcher=Dispatcher(), memory=memory,
                      dictation=core.Dictation(desktop=FakeDesktop()))
        with mock.patch("aj.tools.intents.match", return_value=("type_text", {"text": "ill be late"})):
            brain.reply("Type I'll be late", None, TurnTimes(0.0))
        self.assertEqual(brain.typed_turn, len("ill be late"))
        memory.after_turn.assert_not_called()
        with mock.patch("aj.tools.intents.match", return_value=("current_time", {})):
            brain.reply("what time is it", None, TurnTimes(0.0))
        self.assertIsNone(brain.typed_turn)
        memory.after_turn.assert_called_once()

    def test_stop_from_the_user_aborts_typing_in_flight(self):
        from aj.core.brain import Brain
        desk = mock.Mock(armed=None)
        desk.turn_audio = None
        brain = Brain("http://127.0.0.1:1", Settings(), dispatcher=None, dictation=desk)
        with mock.patch.object(brain, "_conversation", return_value="Okay."):
            brain.reply("stop", None, TurnTimes(0.0))
            desk.abort.assert_called_once()
            brain.reply("what's the weather like", None, TurnTimes(0.0))
        desk.abort.assert_called_once()

    def test_talking_over_the_assistant_aborts_typing_in_flight(self):
        desk = FakeDesk(armed=False)
        desk.abort = mock.Mock()
        session = Session(Settings(), ScriptedEars(), FakeBrain(desk), None, FakeMic(), threading.Event(),
                          dictation=desk)
        session._barge = mock.Mock()
        session._cut_in()
        desk.abort.assert_called_once()

    def test_turning_off_aborts_typing_in_flight(self):
        from aj.engine import Engine
        engine = Engine(Settings())
        with mock.patch.object(engine.dictation, "abort") as abort:
            engine.turn_off()
        abort.assert_called_once()


class MicShareTests(unittest.TestCase):
    """AJ (Speak) and the assistant share one microphone (the review of 2026-10-22): while AJ (Speak)
    takes a dictation, the assistant drops what it hears; and only one of them takes dictation."""

    def test_the_named_events(self):
        from aj.core import mic_share
        self.assertTrue(mic_share._name("x").startswith("Local\\AJTest"))  # never the real names in a test
        flag = mic_share.Flag("Speak.Listening.Test")
        self.addCleanup(flag.close)
        self.assertFalse(mic_share.is_set("Speak.Listening.Test"))
        flag.set()
        self.assertTrue(mic_share.is_set("Speak.Listening.Test"))
        flag.clear()
        self.assertFalse(mic_share.is_set("Speak.Listening.Test"))
        flag.set()
        flag.close()  # the process that raised it ends: nobody stays deaf
        self.assertFalse(mic_share.is_set("Speak.Listening.Test"))

    def test_the_assistant_drops_what_ends_while_speak_listens(self):
        settings = Settings()
        busy = threading.Event()
        busy.set()
        brain, stop, turns, heard = FakeBrain(FakeDesk(armed=False)), threading.Event(), [], []
        mic = FakeMic()
        session = Session(settings, ScriptedEars(), brain, None, mic, stop,
                          on_turn=lambda text, reply, times: turns.append(text), on_heard=heard.append,
                          speak_listening=busy.is_set)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(2)))
        mic.blocks.put(np.ones(512, np.float32))  # "hi amma, see you soon", said to AJ (Speak)
        time.sleep(0.3)
        self.assertEqual((brain.asked, turns, heard), ([], [], []))  # not answered, logged or shown
        self.assertEqual(session.ears.transcribed, [])  # not even transcribed
        busy.clear()
        mic.blocks.put(np.ones(512, np.float32))
        self.assertTrue(wait_until(lambda: brain.asked == ["what time is it"]))

    def test_no_dictation_while_speak_listens(self):
        desktop = FakeDesktop(WHATSAPP)
        desk = core.Dictation(desktop=desktop)
        desktop.speak_busy = True
        with self.assertRaises(core.NotDone) as said:
            desk.arm()
        self.assertEqual(str(said.exception), core.SPEAK_BUSY)
        self.assertIsNone(desk.armed)
        self.assertEqual(desktop.dictating_said, [])
        desktop.speak_busy = False
        desk.arm()
        self.assertEqual(desktop.dictating_said, [True])  # AJ (Speak) is told to stay out of it
        desk.disarm()
        self.assertEqual(desktop.dictating_said, [True, False])
        desk.disarm()  # nothing armed: nothing more said
        self.assertEqual(desktop.dictating_said, [True, False])


if __name__ == "__main__":
    unittest.main()
