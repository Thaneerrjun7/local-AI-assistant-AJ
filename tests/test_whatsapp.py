"""tools/whatsapp.py against a fake WhatsApp: no window is opened, no key is pressed.

FakeWhatsApp stands in for core/uia.py, core/keyboard.py and the parts of tools/apps.py the tool
uses, and behaves the way WhatsApp did live on 2026-10-22: a splash with a progress bar, then the
search box and the chat list; keys typed after the first focus can be dropped; a search box
holding text loses its name; Enter in the search box opens the top result and moves the focus to
"Type a message to <name>". The clock is fake too, so the 120 s load wait takes no time.
Run: .venv\\Scripts\\python.exe -m unittest tests.test_whatsapp
"""
import threading
import unittest
from dataclasses import replace
from unittest import mock

from aj import tools
from aj.core import keyboard as real_keyboard
from aj.core.uia import Node
from aj.tools import intents, registry, whatsapp
from aj.tools.apps import App, Window
from aj.tools.registry import Level, ToolError

HWND = 100
APP = App("WhatsApp", "appid", "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App", exes=("whatsapp.root.exe",),
          aumid="5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App")
ROOT = Node(control_type="Document", automation_id="RootWebArea", runtime_id=(42, 0))
BOX = Node(control_type="Edit", name="Search or start a new chat", class_name="html-input xdj266r", aria_role="textbox",
           has_value=True, has_text=True, focusable=True, runtime_id=(42, 1), bounds=(141, 121, 309, 22))
GRID = Node(control_type="DataGrid", name="Chat list", aria_role="grid", runtime_id=(42, 2), bounds=(69, 203, 391, 800))
HEADING = Node(control_type="Text", name="Chats", aria_role="heading", runtime_id=(42, 3))
SPLASH = [Node(control_type="ProgressBar", aria_role="progressbar", range_value=40.0, runtime_id=(42, 4)),
          Node(control_type="Text", name="End-to-end encrypted", aria_role="description", runtime_id=(42, 5))]
OTHER = Node(control_type="Pane", class_name="Microsoft.UI.Content.DesktopChildSiteBridge", runtime_id=(42, 6))
CHATS = ["Appa", "Amma", "Suhinish", "James", "Family group"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeWhatsApp:
    """uia, keyboard and apps in one, for whatsapp.py's module globals."""

    def __init__(self, loading_polls: int = 0, chats: list[str] | None = None, drop_typing: int = 0,
                 window: bool = True, link: bool = False, text: str = "", header_name: bool = True,
                 available: bool = True, enter_works: bool = True):
        self.loading_polls, self.chats, self.drop_typing = loading_polls, chats or CHATS, drop_typing
        self.window, self.link, self.text, self.header_name = window, link, text, header_name
        self._available, self.enter_works = available, enter_works
        self.launched, self.polls, self.focused, self.selected, self.open_chat = False, 0, "other", False, None
        self.typed: list[tuple[str, str]] = []    # (where the focus was, text)
        self.pressed: list[tuple[str, str]] = []  # (where the focus was, keys)
        self.focus_calls = 0
        self.log: list[str] = []  # "wait" (for modifiers), "check" (the focus read), "key" (typed or pressed)
        self.held = False         # a modifier still held after the wait
        self.on_wait = None       # called during the modifier wait (to move the focus meanwhile)

    # ---- apps
    def resolve(self, name, strict=False):
        return APP if name.lower() == "whatsapp" else None

    def find_windows(self, app, windows=None):
        return [Window(HWND, "WhatsApp", frozenset({"whatsapp.root.exe"}), frozenset())] \
            if self.launched and self.window else []

    def open_app(self, name):
        self.launched = True
        return "Opening WhatsApp."

    # ---- uia
    def available(self):
        return self._available

    def find(self, target, automation_id=None, **_):
        return ROOT if target == HWND and automation_id == "RootWebArea" else None

    def matches(self) -> list[str]:
        return [c for c in self.chats if self.text.lower() in c.lower()]

    def box(self) -> Node:
        return replace(BOX, name="" if self.text else BOX.name, focused=self.focused == "box")

    def message_box(self) -> Node:
        name = f"Type a message to {self.open_chat}" if self.header_name else "Type a message"
        return Node(control_type="Edit", name=name, class_name="x1hx0egp", aria_role="textbox", has_value=True,
                    has_text=True, focusable=True, focused=self.focused == "message", runtime_id=(42, 9),
                    bounds=(580, 1016, 1062, 24))

    def scan(self, root):
        assert root is ROOT
        self.polls += 1
        if self.polls <= self.loading_polls:
            return list(SPLASH)
        if self.link:
            return [Node(control_type="Text", name="Scan the QR code to log in", runtime_id=(42, 7))]
        nodes = [HEADING, self.box(), GRID]
        shown = self.matches() if self.text else self.chats
        nodes += [Node(control_type="DataItem", aria_role="row", name=f"{c} preview text", runtime_id=(42, 100 + i))
                  for i, c in enumerate(shown)]
        if self.text and not shown:
            nodes.append(Node(control_type="Text", name="No chats, contacts or messages found", runtime_id=(42, 8)))
        if self.open_chat:
            nodes.append(self.message_box())
        return nodes

    def set_focus(self, node):
        self.focus_calls += 1
        self.focused = "box" if node.runtime_id == BOX.runtime_id else "other"
        return True

    def focused_node(self):
        self.log.append("check")
        return {"box": self.box(), "message": self.message_box() if self.open_chat else OTHER}.get(self.focused, OTHER)

    def value(self, node):
        return self.text if node.runtime_id == BOX.runtime_id else None

    # ---- keyboard
    locked = False
    ModifiersHeld = real_keyboard.ModifiersHeld

    def wait_for_modifiers(self):
        self.log.append("wait")
        if self.on_wait:
            self.on_wait()
        if self.held:
            raise real_keyboard.ModifiersHeld("Ctrl is still held down, so no keys were sent")

    def screen_locked(self):
        return self.locked

    def front_window(self):
        return HWND

    def focus(self, hwnd, timeout=2.0):
        return True

    def type_text(self, text):
        self.log.append("key")
        self.typed.append((self.focused, text))
        if self.focused != "box":
            return
        if self.drop_typing:  # live: keys after the first SetFocus went nowhere
            self.drop_typing -= 1
            return
        self.text = text if self.selected else self.text + text
        self.selected = False

    def press(self, keys):
        self.log.append("key")
        self.pressed.append((self.focused, keys))
        if self.focused != "box":
            return
        if keys == "ctrl+a":
            self.selected = True
        elif keys == "backspace" and self.selected:
            self.text, self.selected = "", False
        elif keys == "escape":
            self.text = ""
        elif keys == "enter" and self.enter_works and self.matches():
            self.open_chat, self.focused = self.matches()[0], "message"


class FlowTestCase(unittest.TestCase):
    def run_tool(self, fake: FakeWhatsApp, name: str) -> str:
        clock = Clock()
        with mock.patch.object(whatsapp, "uia", fake), mock.patch.object(whatsapp, "keyboard", fake), \
                mock.patch.object(whatsapp, "apps", fake), mock.patch.object(whatsapp, "_now", clock.now), \
                mock.patch.object(whatsapp, "_sleep", clock.sleep):
            self.elapsed = lambda: clock.t - 1000.0
            return whatsapp.whatsapp_open_chat(name)

    def assert_message_box_untouched(self, fake: FakeWhatsApp):
        self.assertEqual([t for where, t in fake.typed if where != "box"], [], "typed outside the search box")
        self.assertEqual([k for where, k in fake.pressed if where != "box"], [], "keys outside the search box")


class FlowTests(FlowTestCase):
    def test_waits_for_the_load_then_opens_the_chat(self):
        fake = FakeWhatsApp(loading_polls=40)  # 10 s of splash
        self.assertEqual(self.run_tool(fake, "Appa"), "Appa's chat is open.")
        self.assertTrue(fake.launched)
        self.assertGreaterEqual(fake.polls, 40)
        self.assertEqual(fake.typed, [("box", "Appa")])
        self.assertEqual([k for _, k in fake.pressed], ["enter"])
        self.assertEqual(fake.focused, "message")  # left in the message box for dictation
        self.assert_message_box_untouched(fake)

    def test_nothing_is_typed_while_it_loads(self):
        fake = FakeWhatsApp(loading_polls=10)
        original = fake.type_text

        def typing(text):
            self.assertGreater(fake.polls, 10, "typed before WhatsApp had loaded")
            original(text)

        fake.type_text = typing
        self.run_tool(fake, "Appa")

    def test_fast_path_names_come_back_capitalised(self):
        self.assertEqual(self.run_tool(FakeWhatsApp(), "appa"), "Appa's chat is open.")
        self.assertEqual(self.run_tool(FakeWhatsApp(), "Suhinish number"), "Suhinish's chat is open.")
        self.assertEqual(self.run_tool(FakeWhatsApp(), "jame"), "James's chat is open.")  # "James chat", s dropped

    def test_dropped_keys_are_typed_again(self):
        fake = FakeWhatsApp(drop_typing=1)  # live: the first typing after a focus went nowhere
        self.assertEqual(self.run_tool(fake, "Appa"), "Appa's chat is open.")
        self.assertEqual(fake.focus_calls, 2)
        self.assertEqual(fake.typed, [("box", "Appa"), ("box", "Appa")])
        self.assert_message_box_untouched(fake)

    def test_a_different_top_match_is_named(self):
        fake = FakeWhatsApp()
        self.assertEqual(self.run_tool(fake, "app"), "Opened Appa's chat, the top match for App.")

    def test_unreadable_header(self):
        fake = FakeWhatsApp(header_name=False)
        self.assertEqual(self.run_tool(fake, "Appa"),
                         "The top match for Appa should be open, but I couldn't confirm which chat it is.")

    def test_an_old_search_is_cleared_first(self):
        fake = FakeWhatsApp(text="Suhinish")  # the box holds the last search and has lost its name
        self.assertEqual(self.run_tool(fake, "Amma"), "Amma's chat is open.")
        self.assertEqual([k for _, k in fake.pressed], ["ctrl+a", "backspace", "enter"])
        self.assertEqual(fake.typed, [("box", "Amma")])

    def test_no_results(self):
        """A failure, not a result (the review of 2026-10-22): told it as an ok result, the model
        could still say "Opened Zed's chat", and the delivery guard would let it."""
        fake = FakeWhatsApp()
        with self.assertRaises(ToolError) as caught:
            self.run_tool(fake, "Zed")
        self.assertEqual(str(caught.exception), "No chat called Zed in WhatsApp.")
        self.assertEqual([k for _, k in fake.pressed], ["escape"])  # the search is cleared
        self.assertEqual(fake.text, "")
        self.assertIsNone(fake.open_chat)

    def test_every_key_waits_for_modifiers_then_checks_the_focus(self):
        """Modifiers first, then the focus, then the key (the review of 2026-10-22): checked the
        other way round, the focus could move during the wait."""
        for fake, name in ((FakeWhatsApp(), "Appa"), (FakeWhatsApp(text="Suhinish"), "Amma"),
                           (FakeWhatsApp(drop_typing=1), "Appa")):
            with self.subTest(name=name, text=fake.text):
                self.run_tool(fake, name)
                keys = [i for i, entry in enumerate(fake.log) if entry == "key"]
                self.assertGreaterEqual(len(keys), 2)
                for i in keys:
                    self.assertEqual(fake.log[i - 2:i], ["wait", "check"], fake.log)

    def test_a_held_modifier_means_no_keys_at_all(self):
        fake = FakeWhatsApp()
        fake.held = True
        with self.assertRaises(ToolError) as caught:
            self.run_tool(fake, "Appa")
        self.assertIn("held down", str(caught.exception))
        self.assertEqual((fake.typed, fake.pressed), ([], []))

    def test_the_focus_moving_during_the_wait_stops_enter(self):
        fake = FakeWhatsApp()

        def move_away_before_enter():
            if fake.typed:  # the name is in: now the boss clicks elsewhere while Ctrl is let go
                fake.focused = "other"

        fake.on_wait = move_away_before_enter
        with self.assertRaises(ToolError) as caught:
            self.run_tool(fake, "Appa")
        self.assertIn("lost the focus", str(caught.exception))
        self.assertNotIn("enter", [k for _, k in fake.pressed])
        self.assert_message_box_untouched(fake)

    def test_a_call_the_dispatcher_gave_up_on_presses_nothing(self):
        fake, stop = FakeWhatsApp(), threading.Event()
        stop.set()
        registry.bind_cancel(stop)
        self.addCleanup(registry.bind_cancel, None)
        with self.assertRaises(ToolError):
            self.run_tool(fake, "Appa")
        self.assertEqual((fake.typed, fake.pressed), ([], []))

    def test_still_loading_at_the_deadline(self):
        fake = FakeWhatsApp(loading_polls=10 ** 9)
        with self.assertRaises(ToolError) as caught:
            self.run_tool(fake, "Appa")
        self.assertEqual(str(caught.exception),
                         "WhatsApp is still loading your chats, probably the Wi-Fi. Ask me again in a minute.")
        self.assertLessEqual(self.elapsed(), whatsapp.LOAD_WAIT + 1)
        self.assertLess(whatsapp.LOAD_WAIT + 15, registry.get("whatsapp_open_chat").timeout)
        self.assertEqual((fake.typed, fake.pressed), ([], []))

    def test_window_never_appears(self):
        with self.assertRaises(ToolError) as caught:
            self.run_tool(FakeWhatsApp(window=False), "Appa")
        self.assertEqual(str(caught.exception), "WhatsApp didn't open.")

    def test_phone_not_linked(self):
        with self.assertRaises(ToolError) as caught:
            self.run_tool(FakeWhatsApp(link=True), "Appa")
        self.assertIn("link your phone", str(caught.exception))

    def test_typing_never_lands(self):
        fake = FakeWhatsApp(drop_typing=99)
        with self.assertRaises(ToolError) as caught:
            self.run_tool(fake, "Appa")
        self.assertEqual(str(caught.exception), "I couldn't type into WhatsApp's search box.")
        self.assertNotIn("enter", [k for _, k in fake.pressed])

    def test_enter_that_opens_nothing(self):
        fake = FakeWhatsApp(enter_works=False)
        with self.assertRaises(ToolError) as caught:
            self.run_tool(fake, "Appa")
        self.assertEqual(str(caught.exception), "I found Appa in WhatsApp, but the chat didn't open.")

    def test_locked_laptop(self):
        fake = FakeWhatsApp()
        fake.locked = True
        with self.assertRaises(ToolError) as caught:
            self.run_tool(fake, "Appa")
        self.assertEqual(str(caught.exception), "The laptop is locked, so I can't get into WhatsApp.")
        self.assertFalse(fake.launched)
        self.assertEqual((fake.typed, fake.pressed), ([], []))

    def test_without_ui_automation(self):
        with self.assertRaises(ToolError) as caught:
            self.run_tool(FakeWhatsApp(available=False), "Appa")
        self.assertIn("can't read WhatsApp", str(caught.exception))

    def test_no_name(self):
        for name in ("", "  ", " ?! "):
            with self.subTest(name=name), self.assertRaises(ToolError):
                self.run_tool(FakeWhatsApp(), name)

    def test_registration(self):
        spec = registry.get("whatsapp_open_chat")
        self.assertEqual((spec.level, spec.cloud), (Level.SAFE, False))
        self.assertGreaterEqual(spec.timeout, 150.0)
        self.assertEqual([p.name for p in spec.params], ["name"])


class PageTests(unittest.TestCase):
    def test_states(self):
        ready = [HEADING, BOX, GRID]
        self.assertEqual(whatsapp.screen_state(None), "starting")
        self.assertEqual(whatsapp.screen_state(SPLASH), "loading")
        self.assertEqual(whatsapp.screen_state(ready), "ready")
        self.assertEqual(whatsapp.screen_state([HEADING, BOX]), "nogrid")
        self.assertEqual(whatsapp.screen_state([replace(BOX, offscreen=True), GRID]), "starting")
        self.assertEqual(whatsapp.screen_state(ready + [Node(control_type="Text", name="Loading your chats")]),
                         "loading")
        # A photo downloading in the open chat has a progress bar too: that isn't the load.
        self.assertEqual(whatsapp.screen_state(ready + [SPLASH[0]]), "ready")
        # A chat preview that mentions loading is a row, not WhatsApp loading.
        self.assertEqual(whatsapp.screen_state(ready + [Node(control_type="DataItem", aria_role="row",
                                                             name="loading the car now")]), "ready")

    def test_the_message_box_is_never_the_search_box(self):
        message = Node(control_type="Edit", name="Type a message to Appa", class_name="html-input x", aria_role="textbox",
                       runtime_id=(42, 9), bounds=(580, 1016, 1062, 24))
        self.assertIsNone(whatsapp.search_box([message, GRID]))
        nameless_low = replace(BOX, name="", bounds=(141, 1000, 309, 22))  # an unnamed input below the list
        self.assertIsNone(whatsapp.search_box([nameless_low, GRID]))
        self.assertIsNone(whatsapp.search_box([replace(BOX, name="")]))  # no chat list to place it by
        filled = replace(BOX, name="")
        self.assertEqual(whatsapp.search_box([message, filled, GRID]), filled)

    def test_chat_name(self):
        def box(name):
            return Node(control_type="Edit", name=name)

        self.assertEqual(whatsapp.chat_name(box("Type a message to Appa")), "Appa")
        self.assertEqual(whatsapp.chat_name(box("Type a message to Family Group ")), "Family Group")
        self.assertIsNone(whatsapp.chat_name(box("Type a message")))
        self.assertIsNone(whatsapp.chat_name(box("Search or start a new chat")))
        self.assertIsNone(whatsapp.chat_name(Node(control_type="Button", name="Type a message to Appa")))
        self.assertIsNone(whatsapp.chat_name(None))


class NameTests(unittest.TestCase):
    def test_clean_name(self):
        cases = {"Suhinish number": "Suhinish", "Suhinish's chat": "Suhinish", "Appa on WhatsApp": "Appa",
                 "Appa’s chat on WhatsApp": "Appa", "chat with Appa": "Appa", "the chat with Amma": "Amma",
                 "Appa's": "Appa", "  Appa.  ": "Appa", "Family Group": "Family Group", "Appa's WhatsApp": "Appa",
                 "": ""}
        for said, name in cases.items():
            with self.subTest(said=said):
                self.assertEqual(whatsapp.clean_name(said), name)


class IntentTests(unittest.TestCase):
    """whatsapp.py's own rules, with the app index and WhatsApp's window faked."""

    INSTALLED = {"chrome", "google chrome", "settings", "teams", "microsoft teams", "whatsapp", "spotify", "notepad"}

    def setUp(self):
        self.on_screen = True
        for patch in (mock.patch.object(whatsapp.apps, "resolve",
                                        lambda name, strict=False: APP if name.lower() in self.INSTALLED else None),
                      mock.patch.object(whatsapp, "_whatsapp_on_screen", lambda: self.on_screen)):
            patch.start()
            self.addCleanup(patch.stop)
        intents.collect([whatsapp])
        self.addCleanup(setattr, intents, "_rules", None)  # rebuilt from every module on next use

    def claim(self, said: str):
        return intents.match(said)

    def test_naming_whatsapp_and_a_person_is_enough_with_whatsapp_closed(self):
        """The review of 2026-10-22: a phrase naming WhatsApp and a person is the fast path's,
        whether or not WhatsApp's window is open (only a bare "go to X" needs it open)."""
        self.on_screen = False
        for said, name in (("I want to talk to suhinish on whatsapp", "suhinish"),
                           ("chat with Amma on WhatsApp", "amma"), ("whatsapp Appa", "appa"),
                           ("open Appa on WhatsApp", "appa")):
            with self.subTest(said=said):
                self.assertEqual(self.claim(said), ("whatsapp_open_chat", {"name": name}))
        self.assertIsNone(self.claim("go to Appa"))
        self.assertIsNone(self.claim("I want to talk to you"))

    def test_claims(self):
        claimed = {
            "go to Appa": "appa",
            "Go to Suhinish number": "suhinish",
            "go to Appa's chat": "appa",
            "open Appa's chat": "appa",
            "open chat with Appa": "appa",
            "open the chat with Amma": "amma",
            "Open WhatsApp and go to Appa": "appa",
            "open whatsapp and open appa's chat": "appa",
            "message Appa": "appa",
            "text Appa": "appa",
            "Message Appa on WhatsApp.": "appa",
            "go to Appa on WhatsApp": "appa",
            "Computer, go to Appa on WhatsApp": "appa",
            "AJ go to Appa": "appa",
            "take me to Suhinish's chat": "suhinish",
            "whatsapp Amma": "amma",
            # The acceptance run (2026-10-22): left to the model, this got switch_to_app.
            "I want to talk to suhinish on whatsapp": "suhinish",
            "talk to Appa on WhatsApp": "appa",
            "chat with Amma on WhatsApp": "amma",
            "can I message Appa on WhatsApp": "appa",
            "open Suhinish on WhatsApp": "suhinish",
            "on WhatsApp, talk to Amma": "amma",
            "open WhatsApp and talk to Appa": "appa",
        }
        for said, name in claimed.items():
            with self.subTest(said=said):
                self.assertEqual(self.claim(said), ("whatsapp_open_chat", {"name": name}))

    def test_message_words_open_the_chat_only(self):
        for said in ("message Appa saying I'm running late", "text Appa that dinner is ready",
                     "message Appa on WhatsApp saying hi", "text appa about tomorrow"):
            with self.subTest(said=said):
                self.assertEqual(self.claim(said), ("whatsapp_open_chat", {"name": "appa"}))

    def test_declines(self):
        for said in ("go to youtube", "go to gmail com", "go to gmail dot com", "go to google", "go to settings",
                     "go to chrome", "go to google chrome", "go to downloads", "go to my documents", "go to sleep",
                     "go to the next page", "go to page 5", "go to whatsapp", "open whatsapp", "open the teams chat",
                     "open spotify", "message me", "text me back", "go to the top", "open the group chat",
                     "go to amazon com", "go to github", "go to desktop", "go to this website", "go to bed",
                     "go to appa and tell him something long winded about four words",
                     "what does go to appa mean"):
            with self.subTest(said=said):
                self.assertIsNone(self.claim(said))

    def test_bare_go_to_needs_whatsapp_on_screen(self):
        self.on_screen = False
        self.assertIsNone(self.claim("go to Appa"))
        for said in ("go to Appa on WhatsApp", "go to Appa's chat", "message Appa", "go to Suhinish number"):
            with self.subTest(said=said):
                self.assertIsNotNone(self.claim(said))

    def test_whatsapp_is_tried_first(self):
        self.assertEqual(tools.MODULES[0], "whatsapp")


if __name__ == "__main__":
    unittest.main()
