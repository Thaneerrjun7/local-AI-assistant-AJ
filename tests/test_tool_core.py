"""Tool core: schemas from type hints, the dispatcher's gate, the audit log, and the fast path.

Uses tiny fake tools defined here (all named fake_*), never the real tool modules' side effects.
Run: python -m unittest discover -s tests -t .
"""
import json
import logging
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Literal, Optional
from unittest import mock

import aj.tools as tools
from aj.tools import intents, registry
from aj.tools.dispatch import Dispatcher
from aj.tools.registry import Level, ToolError, tool

CALLS: list[tuple] = []
RELEASE = threading.Event()  # lets the deliberately slow tool finish once the test is over


@tool(level=Level.SAFE, timeout=1.0)
def fake_volume(level: int, mode: Literal["set", "up", "down"] = "set", note: Optional[str] = None,
                ratio: float | None = None, loud: bool = False) -> str:
    """Change the speaker volume.

    level: percent, 0 to 100
    mode (str): set it exactly, or nudge it up or down
    """
    CALLS.append(("fake_volume", level, mode, note, ratio, loud))
    return f"volume {level} {mode}"


@tool(level=Level.CONFIRM)
def fake_delete(path: str, permanent: bool = False) -> str:
    """Delete a file."""
    CALLS.append(("fake_delete", path, permanent))
    return f"deleted {path}"


@tool(level=Level.BLOCKED)
def fake_shutdown() -> str:
    """Shut the laptop down."""
    CALLS.append(("fake_shutdown",))
    return "shutting down"


@tool(level=Level.LOCKED)
def fake_click(x: int, y: int) -> str:
    """Click the mouse."""
    CALLS.append(("fake_click", x, y))
    return "clicked"


@tool(timeout=0.05)
def fake_slow() -> str:
    """Take far too long."""
    RELEASE.wait(5)
    return "finally"


@tool
def fake_fails(why: str, when: str | None) -> str:
    """Fail on purpose."""
    if why == "user":
        raise ToolError("There's no app called Spotfy.")
    raise RuntimeError("boom")


@tool()
def fake_open(name: str) -> str:
    """Open an app."""
    return f"opening {name}"


FAKE_INTENTS = types.ModuleType("fake_intents")
FAKE_INTENTS.INTENTS = [
    (r"(open|launch|start) (?P<name>.+)", "fake_open", lambda m: {"name": m["name"]}),
    (r"(?:set )?(?:the )?volume (?:to )?(?P<level>\d+)(?: percent)?", "fake_volume",
     lambda m: {"level": m["level"], "mode": None}),
    (r"turn it (?P<mode>up|down)", "fake_volume", lambda m: None),  # always declines
    (r"turn it (?P<mode>up|down)", "fake_volume", lambda m: {"level": 10, "mode": m["mode"]}),
    (r"crash (?P<x>.+)", "fake_open", lambda m: 1 / 0),
    (r"crash (?P<x>.+)", "fake_open", lambda m: {"name": "fallback"}),
    (r"ghost (?P<x>.+)", "fake_no_such_tool", lambda m: {}),
    (r"(unclosed", "fake_open", lambda m: {}),
]


def tearDownModule() -> None:
    RELEASE.set()
    for spec in registry.specs():
        if spec.name.startswith("fake_"):
            registry.unregister(spec.name)


class SchemaTests(unittest.TestCase):
    def test_schema_from_type_hints(self):
        fn = registry.get("fake_volume").openai()["function"]
        self.assertEqual(fn["description"], "Change the speaker volume.")
        props = fn["parameters"]["properties"]
        self.assertEqual(props["level"], {"type": "integer", "description": "percent, 0 to 100"})
        self.assertEqual(props["mode"]["enum"], ["set", "up", "down"])
        self.assertEqual(props["mode"]["type"], "string")
        self.assertEqual(props["mode"]["description"], "set it exactly, or nudge it up or down")
        self.assertEqual(props["note"], {"type": "string"})
        self.assertEqual(props["ratio"], {"type": "number"})
        self.assertEqual(props["loud"], {"type": "boolean"})
        self.assertEqual(fn["parameters"]["required"], ["level"])  # Optional / defaulted are not

    def test_optional_without_default_is_not_required(self):
        self.assertEqual(registry.get("fake_fails").parameters["required"], ["why"])
        self.assertNotIn("required", registry.get("fake_shutdown").parameters)

    def test_bare_decorator_and_defaults(self):
        spec = registry.get("fake_fails")
        self.assertEqual((spec.level, spec.timeout, spec.cloud), (Level.SAFE, 5.0, False))

    def test_openai_tools_filters_keeps_order_and_hides_unrunnable(self):
        names = [t["function"]["name"] for t in registry.openai_tools(
            ["fake_delete", "fake_volume", "nope", "fake_shutdown", "fake_click", "fake_delete"])]
        self.assertEqual(names, ["fake_delete", "fake_volume"])
        everything = {t["function"]["name"] for t in registry.openai_tools()}
        self.assertTrue({"fake_volume", "fake_delete", "fake_open"} <= everything)
        self.assertFalse({"fake_shutdown", "fake_click"} & everything)

    def test_bad_definitions_fail_at_import(self):
        def no_hint(x) -> str:
            """Doc."""
        def no_doc(x: str) -> str:
            pass
        def a_list(x: list[str]) -> str:
            """Doc."""
        def wide_union(x: int | str) -> str:
            """Doc."""
        def mixed_literal(x: Literal["a", 1]) -> str:
            """Doc."""
        for func in (no_hint, no_doc, a_list, wide_union, mixed_literal):
            with self.subTest(func.__name__), self.assertRaises(TypeError):
                tool()(func)
            self.assertIsNone(registry.get(func.__name__))

    def test_duplicate_name_from_another_function_is_refused(self):
        def fake_open(name: str) -> str:
            """A second open."""
        with self.assertRaises(ValueError):
            tool()(fake_open)
        self.assertEqual(registry.get("fake_open").description, "Open an app.")


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        CALLS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self.tmp.name) / "logs" / "audit.jsonl"
        self.questions: list[str] = []
        self.answer = True
        self.d = Dispatcher(self.ask, self.audit)

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, question: str) -> bool:
        self.questions.append(question)
        return self.answer

    def audit_lines(self) -> list[dict]:
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]

    def test_safe_tool_runs_with_coerced_args(self):
        r = self.d.run("fake_volume", {"level": "35", "mode": "UP", "ratio": "0.5", "loud": "yes"})
        self.assertEqual((r, r.outcome, r.ok), ("volume 35 up", "ok", True))
        self.assertEqual(CALLS, [("fake_volume", 35, "up", None, 0.5, True)])

    def test_more_coercion(self):
        self.d.run("fake_volume", {"level": 40.0, "note": 7, "mode": None})
        self.d.run("fake_volume", '{"level": "50%"}')
        self.d.run("fake_volume", json.dumps(json.dumps({"level": 60})))  # double-encoded
        self.assertEqual([c[1:4] for c in CALLS], [(40, "set", "7"), (50, "set", None), (60, "set", None)])

    def test_bad_args_are_refused_with_a_helpful_message(self):
        cases = {
            "wrong type": {"level": "loud"},
            "not whole": {"level": 3.5},
            "bad enum": {"level": 3, "mode": "sideways"},
            "missing": {},
            "extra": {"level": 3, "colour": "red"},
            "bool is not a number": {"level": True},
            "list": {"level": [1]},
            "bad json": "{level: 3",
            "not an object": "[1, 2]",
        }
        for label, args in cases.items():
            with self.subTest(label):
                r = self.d.run("fake_volume", args)
                self.assertEqual(r.outcome, "bad_args")
                self.assertIn("Bad arguments for fake_volume", r)
                self.assertIn('fake_volume(level: integer, mode?: "set" | "up" | "down"', r)
        self.assertEqual(CALLS, [])
        self.assertIn("level is required", self.d.run("fake_volume", {}))
        self.assertIn("unknown argument 'colour'", self.d.run("fake_volume", {"level": 3, "colour": "red"}))

    def test_unknown_tool_is_refused(self):
        r = self.d.run("format_disk", {"drive": "C"})
        self.assertEqual(r.outcome, "unknown")
        self.assertIn("no tool called 'format_disk'", r)
        self.assertNotIn("fake_shutdown", r)  # the refusal only lists tools that can run

    def test_blocked_and_locked_never_run(self):
        blocked, locked = self.d.run("fake_shutdown"), self.d.run("fake_click", {"x": 1, "y": 2})
        self.assertEqual((blocked.outcome, locked.outcome), ("blocked", "locked"))
        self.assertIn("blocked", blocked)
        self.assertIn("password", locked)
        self.assertEqual(CALLS, [])
        self.assertEqual(self.questions, [])

    def test_confirm_yes_runs_with_the_exact_arguments(self):
        r = self.d.run("fake_delete", {"path": "C:\\notes.txt", "permanent": "true"})
        self.assertEqual(r, "deleted C:\\notes.txt")
        self.assertEqual(self.questions, ['About to run fake_delete: path "C:\\notes.txt", permanent yes. Go ahead?'])

    def test_confirm_no_does_not_run(self):
        self.answer = False
        r = self.d.run("fake_delete", {"path": "a.txt"})
        self.assertEqual(r.outcome, "declined")
        self.assertEqual(CALLS, [])

    def test_confirm_needs_a_real_yes(self):
        for answer in ("yes", 1, None):
            self.answer = answer
            self.assertEqual(self.d.run("fake_delete", {"path": "a.txt"}).outcome, "declined")

        def broken(question: str) -> bool:
            raise RuntimeError("no microphone")
        with self.assertLogs("aj.tools.dispatch", logging.ERROR):
            r = Dispatcher(broken, self.audit).run("fake_delete", {"path": "a.txt"})
        self.assertEqual(r.outcome, "declined")
        self.assertEqual(CALLS, [])

    def test_bad_args_are_refused_before_asking(self):
        self.assertEqual(self.d.run("fake_delete", {}).outcome, "bad_args")
        self.assertEqual(self.questions, [])

    def test_timeout(self):
        t0 = time.perf_counter()
        r = self.d.run("fake_slow")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(r.outcome, "timeout")
        self.assertIn("took too long", r)

    def test_tool_errors_and_crashes(self):
        user = self.d.run("fake_fails", {"why": "user"})
        self.assertEqual((user, user.outcome), ("There's no app called Spotfy.", "error"))
        with self.assertLogs("aj.tools.dispatch", logging.ERROR):
            crash = self.d.run("fake_fails", {"why": "bug"})
        self.assertEqual(crash.outcome, "crash")
        self.assertIn("RuntimeError: boom", crash)

    def test_every_call_is_audited(self):
        self.d.run("fake_volume", '{"level": 5}')
        self.d.run("nope", "{}")
        self.d.run("fake_shutdown", {})
        self.answer = False
        self.d.run("fake_delete", {"path": "a.txt"})
        lines = self.audit_lines()
        self.assertEqual([(e["tool"], e["level"], e["outcome"]) for e in lines],
                         [("fake_volume", "SAFE", "ok"), ("nope", None, "unknown"),
                          ("fake_shutdown", "BLOCKED", "blocked"), ("fake_delete", "CONFIRM", "declined")])
        for entry in lines:
            self.assertTrue({"time", "tool", "args", "level", "outcome", "ms"} <= entry.keys())
            self.assertIsInstance(entry["ms"], float)
        self.assertEqual(lines[0]["args"], {"level": 5})  # the coerced args that actually ran
        self.assertNotIn("detail", lines[0])
        self.assertIn("said no", lines[3]["detail"])


class IntentTests(unittest.TestCase):
    def setUp(self):
        with self.assertLogs("aj.tools.intents", logging.WARNING) as logs:
            self.accepted = intents.collect([FAKE_INTENTS])
        self.skipped = logs.output

    def test_normalize(self):
        cases = {
            "Hey Computer, please open WhatsApp now!": "open whatsapp",
            "Computer, can you open Spotify for me, please?": "open spotify",
            "What's the time?": "whats the time",
            "Set volume to 35%.": "set volume to 35",
            "Remind me at 5:30.": "remind me at 5:30",
            "  Brightness   2.5, NOW. ": "brightness 2.5",
            "Open the to-do list": "open the to do list",
            "hey computer": "computer",
            "Hey Computer, do me a favor, open settings.": "open settings",  # live, 2026-10-07 and 18
            "Can you do me a favour and go ahead and open chrome": "open chrome",
            "Uh, lock the screen.": "lock the screen",  # live, 2026-10-19
            "You open YouTube.": "open youtube",
            "": "",
        }
        for said, expected in cases.items():
            with self.subTest(said):
                self.assertEqual(intents.normalize(said), expected)

    def test_match(self):
        self.assertEqual(intents.match("Hey Computer, open WhatsApp."), ("fake_open", {"name": "whatsapp"}))
        self.assertEqual(intents.match("Set the volume to 35 percent please"), ("fake_volume", {"level": "35"}))

    def test_whole_transcript_must_match(self):
        for said in ("How do I open a jar?", "tell me a story", "volume", ""):
            with self.subTest(said):
                self.assertIsNone(intents.match(said))

    def test_declining_and_crashing_builders_fall_through(self):
        self.assertEqual(intents.match("Turn it up"), ("fake_volume", {"level": 10, "mode": "up"}))
        with self.assertLogs("aj.tools.intents", logging.ERROR):
            self.assertEqual(intents.match("crash it please"), ("fake_open", {"name": "fallback"}))

    def test_broken_rules_are_skipped_at_collect(self):
        self.assertEqual(self.accepted, len(FAKE_INTENTS.INTENTS) - 2)
        self.assertEqual(len(self.skipped), 2)  # the unknown tool and the bad regex
        self.assertIsNone(intents.match("ghost town"))

    def test_fast_path_matched_args_pass_the_dispatcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Dispatcher(lambda q: True, Path(tmp) / "audit.jsonl")
            self.assertEqual(d.run(*intents.match("volume 35")), "volume 35 set")

    def test_fast_path_is_fast(self):
        module = types.ModuleType("many")
        module.INTENTS = [(rf"do thing {i} (?P<name>.+)", "fake_open", lambda m: {"name": m["name"]})
                          for i in range(60)] + FAKE_INTENTS.INTENTS[:2]
        self.assertEqual(intents.collect([module]), 62)
        said = ["Hey Computer, open WhatsApp now!", "Computer, tell me something interesting about octopuses please."]
        n = 500
        t0 = time.perf_counter()
        for i in range(n):
            intents.match(said[i % 2])
        per_call_ms = (time.perf_counter() - t0) * 1000 / n
        self.assertLess(per_call_ms, 2.0)


class LoadAllTests(unittest.TestCase):
    def test_missing_and_broken_modules_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "fake_broken.py").write_text(
                "from aj.tools.registry import tool\n"
                "@tool()\n"
                "def fake_half_loaded() -> str:\n"
                '    """Registered before the crash."""\n'
                "raise RuntimeError('broken on purpose')\n", encoding="utf-8")
            Path(tmp, "fake_good.py").write_text(
                "from aj.tools.registry import tool\n"
                "@tool()\n"
                "def fake_good_tool() -> str:\n"
                '    """Works."""\n'
                "    return 'ok'\n"
                "INTENTS = [(r'good', 'fake_good_tool', lambda m: {})]\n", encoding="utf-8")
            with mock.patch.object(tools, "MODULES", ["fake_missing", "fake_broken", "fake_good"]), \
                 mock.patch.object(tools, "__path__", [*tools.__path__, tmp]), \
                 self.assertLogs("aj.tools", logging.WARNING) as logs:
                loaded = tools.load_all()
                matched = intents.match("good")
        sys.modules.pop("aj.tools.fake_good", None)
        self.assertEqual(loaded, ["fake_good"])
        self.assertEqual(matched, ("fake_good_tool", {}))
        self.assertIsNone(registry.get("fake_half_loaded"))  # fail closed: no half-loaded module
        self.assertTrue(any("fake_missing" in line for line in logs.output))
        self.assertTrue(any("fake_broken" in line for line in logs.output))

    def test_real_modules_never_crash_loading(self):
        logging.disable(logging.CRITICAL)
        try:
            loaded = tools.load_all()
        finally:
            logging.disable(logging.NOTSET)
        self.assertTrue(set(loaded) <= set(tools.MODULES))


class ReviewFixTests(unittest.TestCase):
    """The review of 2026-10-22: a tool the dispatcher gave up on is told to stop, and a tool's
    private arguments (a dictation's words) reach the audit log only as their length."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        self.d = Dispatcher(lambda question: True, self.audit)
        self.stopped = threading.Event()

        def fake_typing(text: str) -> str:
            """Type some words."""
            return "Typed it."

        def fake_waits() -> str:
            """Wait until told to stop."""
            if registry.cancel_event().wait(2.0):
                self.stopped.set()
            return "late"

        tool(timeout=1.0, private=("text",))(fake_typing)
        tool(timeout=0.05)(fake_waits)
        for name in ("fake_typing", "fake_waits"):
            self.addCleanup(registry.unregister, name)

    def test_a_timed_out_tool_is_told_to_stop(self):
        result = self.d.run("fake_waits")
        self.assertEqual(result.outcome, "timeout")
        self.assertTrue(self.stopped.wait(1.0), "the abandoned tool was never told to stop")
        self.assertIsNone(registry.cancel_event())  # outside a tool call there is none

    def test_private_arguments_are_kept_as_their_length_only(self):
        words = "Hi Amma, I'm running late"
        self.d.run("fake_typing", {"text": words})
        self.d.run("fake_typing", '{"text": "secret words", "bogus": 1}')  # refused: still hidden
        self.d.run("fake_typing", '{"text": "unterminated')               # not even JSON
        raw = self.audit.read_text(encoding="utf-8")
        for secret in ("Amma", "secret", "unterminated"):
            self.assertNotIn(secret, raw)
        lines = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(lines[0]["args"], {"text": f"({len(words)} characters)"})
        self.assertEqual(lines[1]["args"], {"text": "(12 characters)", "bogus": 1})
        self.assertIn("characters, not shown", lines[2]["args"])

    def test_type_text_declares_its_words_private(self):
        tools.load_all()
        self.assertEqual(registry.get("type_text").private, ("text",))
        self.assertEqual(registry.get("fake_typing").private, ("text",))


if __name__ == "__main__":
    unittest.main()
