"""Memory: schema, upserts with history, no caps, recall relevance and speed, forget, backups,
and the fact extractor's defences against invented facts.

Every test uses a database in a temporary folder; the module also points DB_PATH at a temporary
file, so even an accidental get_db() can never touch the real %APPDATA%\\Ultron\\ultron.db.
The model is never called: HTTP is mocked.
Run: python -m unittest discover -s tests -t .
"""
import json
import sqlite3
import statistics
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from ultron.memory import (Store, backup, context_for, db as dbmod, extract_facts, forget,
                           learn_in_background, log_turn, remember, summarize_session,
                           what_do_you_know)
from ultron.memory.recall import CORE_BUDGET, ITEM_CHARS, SKIP_RECENT

_GUARD = tempfile.TemporaryDirectory()
_patch = mock.patch.object(dbmod, "DB_PATH", Path(_GUARD.name) / "guard.db")


def setUpModule() -> None:
    _patch.start()
    dbmod._default = None


def tearDownModule() -> None:
    if dbmod._default is not None:
        dbmod._default.close()
        dbmod._default = None
    _patch.stop()
    _GUARD.cleanup()


def fake_reply(content: str) -> mock.MagicMock:
    """What urlopen returns for a non-streaming llama-server chat completion."""
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    resp.__enter__.return_value = resp
    return resp


def chat(*lines: str) -> list[dict]:
    """Alternate user / assistant turns."""
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(lines)]


class TempDb(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.tmp.name) / "nested" / "ultron.db")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def history(self) -> list:
        return self.db.rows("SELECT * FROM fact_history ORDER BY id")


class SchemaTest(TempDb):
    def test_created_on_first_use_with_wal_and_fts(self) -> None:
        self.assertTrue(self.db.path.exists())
        self.assertEqual(self.db.value("PRAGMA journal_mode"), "wal")
        names = {r[0] for r in self.db.rows("SELECT name FROM sqlite_master")}
        for table in ("facts", "fact_history", "messages", "summaries",
                      "facts_fts", "messages_fts", "summaries_fts"):
            self.assertIn(table, names)

    def test_reopening_keeps_everything(self) -> None:
        remember([{"category": "identity", "key": "name", "value": "Alex"}], self.db)
        self.db.close()
        self.db = Store(self.db.path)
        self.assertIn("name: Alex", context_for("hi", self.db))


class FactsTest(TempDb):
    def test_upsert_keeps_history(self) -> None:
        self.assertEqual(remember([{"category": "identity", "key": "City", "value": "Toronto"}], self.db), 1)
        self.assertEqual(remember([{"category": "identity", "key": "city", "value": "toronto "}], self.db), 0)
        self.assertEqual(remember([{"category": "identity", "key": "city", "value": "Pune"}], self.db), 1)
        rows = self.db.rows("SELECT key, value FROM facts")
        self.assertEqual([(r["key"], r["value"]) for r in rows], [("city", "Pune")])
        hist = [(h["reason"], h["old_value"], h["new_value"]) for h in self.history()]
        self.assertEqual(hist, [("added", None, "Toronto"), ("updated", "Toronto", "Pune")])

    def test_bad_entries_are_skipped_and_unknown_category_is_notes(self) -> None:
        n = remember([{"key": "", "value": "x"}, {"category": "identity", "key": "age"},
                      {"category": "weird", "key": "shoe size", "value": "10"}], self.db)
        self.assertEqual(n, 1)
        row = self.db.rows("SELECT category, key FROM facts")[0]
        self.assertEqual((row["category"], row["key"]), ("notes", "shoe_size"))

    def test_forget_deletes_matches_but_keeps_a_record(self) -> None:
        remember([{"category": "preferences", "key": "favorite_food", "value": "biryani"},
                  {"category": "preferences", "key": "favorite_game", "value": "Valorant"}], self.db)
        reply = forget("forget my favorite food", self.db)
        self.assertIn("favorite food", reply)
        keys = [r["key"] for r in self.db.rows("SELECT key FROM facts")]
        self.assertEqual(keys, ["favorite_game"])  # all words must match: not every favorite_*
        last = self.history()[-1]
        self.assertEqual((last["reason"], last["old_value"], last["key"]),
                         ("forgotten", "biryani", "favorite_food"))
        self.assertNotIn("biryani", context_for("what food do I like", self.db))
        self.assertIn("don't have anything", forget("unicorns", self.db))
        self.assertEqual(forget("forget it", self.db), "Tell me what to forget.")

    def test_what_do_you_know(self) -> None:
        self.assertIn("Nothing yet", what_do_you_know(self.db))
        remember([{"category": "preferences", "key": "favorite_food", "value": "ramen"},
                  {"category": "identity", "key": "name", "value": "Alex"}], self.db)
        said = what_do_you_know(self.db)
        self.assertTrue(said.startswith("I know 2 things about you. Your name is Alex"), said)
        self.assertIn("favorite food is ramen", said)

    def test_threads_can_write_and_read_at_once(self) -> None:
        errors = []

        def writer(n: int) -> None:
            try:
                for i in range(100):
                    remember([{"category": "notes", "key": f"t{n}_{i}", "value": f"v{i}"}], self.db)
                    context_for(f"v{i}", self.db)
            except Exception as exc:  # noqa: BLE001 - the test reports any failure below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.db.value("SELECT COUNT(*) FROM facts"), 400)


class RecallTest(TempDb):
    def test_empty_memory_gives_empty_context(self) -> None:
        self.assertEqual(context_for("hello", self.db), "")

    def test_identity_and_preferences_come_first(self) -> None:
        remember([{"category": "notes", "key": "shoe_size", "value": "10"},
                  {"category": "preferences", "key": "favorite_food", "value": "ramen"},
                  {"category": "identity", "key": "name", "value": "Alex"}], self.db)
        text = context_for("hi", self.db)
        self.assertLess(text.index("name: Alex"), text.index("favorite food: ramen"))
        self.assertLess(text.index("favorite food: ramen"), text.index("shoe size: 10"))

    def test_fact_outside_the_core_block_comes_back_when_relevant(self) -> None:
        remember([{"category": "identity", "key": f"filler_{i}", "value": f"filler value {i}"}
                  for i in range(300)], self.db)  # far more than the core budget holds
        remember([{"category": "relationships", "key": "pet_dog", "value": "a beagle named Coco"}], self.db)
        self.assertNotIn("Coco", context_for("what's the weather", self.db))
        related = context_for("should I walk my dog", self.db).split("Possibly relevant")[1]
        self.assertIn("a beagle named Coco", related)

    def test_old_messages_and_summaries_are_recalled_but_recent_ones_are_not_repeated(self) -> None:
        log_turn("s1", "user", "I have a Valorant tournament on Saturday", self.db)
        log_turn("s1", "assistant", "Try not to throw.", self.db)
        with self.db.tx() as conn:
            conn.execute("INSERT INTO summaries(session_id, text, at) VALUES ('s0',"
                         " 'The user planned a trip to Lisbon.', '2026-09-01T10:00:00')")
        for i in range(SKIP_RECENT):
            log_turn("s2", "user", f"recent chatter number {i} about lisbon", self.db)
        text = context_for("how did the valorant tournament go, and lisbon?", self.db)
        self.assertIn("Valorant tournament on Saturday", text)
        self.assertIn("conversation on Sep 1: The user planned a trip to Lisbon.", text)
        self.assertNotIn("recent chatter", text)  # already in the chat history the model sees


class BigMemoryTest(unittest.TestCase):
    """5,000 facts through the real remember() path, plus 2,000 logged messages."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Store(Path(cls.tmp.name) / "big.db")
        cats = ["identity", "preferences", "relationships", "projects", "plans", "notes"]
        cls.facts = [{"category": cats[i % 6], "key": f"fact_{i}", "value": f"value {i} topic{i % 97}"}
                     for i in range(5000)]
        cls.long_value = "x" * 5000  # SivaSafe cut values at 380 characters
        cls.facts.append({"category": "notes", "key": "essay", "value": cls.long_value})
        remember(cls.facts, cls.db)
        with cls.db.tx() as conn:
            conn.executemany("INSERT INTO messages(session_id, role, text, at) VALUES (?, ?, ?, ?)",
                             [("s", "user", f"message {i} about topic{i % 97} and music",
                               "2026-10-01T12:00:00") for i in range(2000)])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()
        cls.tmp.cleanup()

    def test_no_cap_nothing_dropped_or_cut(self) -> None:
        self.assertEqual(self.db.value("SELECT COUNT(*) FROM facts"), 5001)
        self.assertEqual(self.db.value("SELECT value FROM facts WHERE key = 'essay'"), self.long_value)
        self.assertEqual(self.db.value("SELECT value FROM facts WHERE key = 'fact_0'"), "value 0 topic0")

    def test_context_size(self) -> None:
        text = context_for("tell me about topic42 and music", self.db)
        core, related = text.split("\n\nPossibly relevant from earlier:\n")
        self.assertLessEqual(len(core.split("\n", 1)[1]), CORE_BUDGET)
        items = related.split("\n")
        self.assertTrue(3 <= len(items) <= 5, items)
        self.assertTrue(all(len(item) <= ITEM_CHARS for item in items))
        self.assertTrue(any("topic42" in item for item in items))

    def test_context_speed(self) -> None:
        def median_ms(rebuild_core: bool) -> float:
            times = []
            for i in range(20):
                if rebuild_core:
                    self.db.generation += 1  # as if a fact had just changed
                start = time.perf_counter()
                context_for(f"what about topic{i} and some music", self.db)
                times.append((time.perf_counter() - start) * 1000)
            return statistics.median(times)

        self.assertLess(median_ms(rebuild_core=False), 10)
        self.assertLess(median_ms(rebuild_core=True), 10)


class BackupTest(TempDb):
    def test_daily_files_and_only_seven_kept(self) -> None:
        remember([{"category": "identity", "key": "name", "value": "Alex"}], self.db)
        start = date(2026, 1, 1)
        for d in range(10):
            backup(self.db, today=start + timedelta(days=d))
        backup(self.db, today=start + timedelta(days=9))  # same day again just refreshes it
        files = sorted(p.name for p in (self.db.path.parent / "backups").iterdir())
        self.assertEqual(files, [f"ultron-202601{d:02d}.db" for d in range(4, 11)])
        copy = sqlite3.connect(self.db.path.parent / "backups" / files[-1])
        try:
            self.assertEqual(copy.execute("SELECT value FROM facts WHERE key = 'name'").fetchone(),
                             ("Alex",))
        finally:
            copy.close()


class ExtractorTest(TempDb):
    URLOPEN = "ultron.memory.slot.urllib.request.urlopen"

    def test_no_first_person_means_no_model_call(self) -> None:
        with mock.patch(self.URLOPEN) as urlopen:
            facts = extract_facts("http://x", chat("Open Chrome please", "Done.",
                                                   "What's the weather like?", "Cloudy."))
        self.assertEqual(facts, [])
        urlopen.assert_not_called()

    def test_no_fact_chat_returns_empty(self) -> None:
        with mock.patch(self.URLOPEN, return_value=fake_reply('{"facts": []}')):
            self.assertEqual(extract_facts("http://x", chat("I'm so tired today lol", "Sleep then.")), [])

    def test_invented_and_passing_facts_are_dropped(self) -> None:
        invented = {"facts": [
            {"quote": "I love hiking", "category": "preferences", "key": "hobby", "value": "hiking"},
            {"quote": "I'm so tired today", "category": "notes", "key": "mood", "value": "tired"},
            {"quote": "I'm so tired today", "category": "identity", "key": "job", "value": "nurse"},
            {"quote": "You are a robot", "category": "notes", "key": "assistant", "value": "a robot"}]}
        turns = chat("I'm so tired today", "You are a robot, I am not.")
        with mock.patch(self.URLOPEN, return_value=fake_reply(json.dumps(invented))):
            self.assertEqual(extract_facts("http://x", turns), [])

    def test_real_fact_is_kept_and_request_uses_slot_1_with_grammar(self) -> None:
        reply = {"facts": [{"quote": "I just moved to Toronto", "category": "identity",
                            "key": "City", "value": "Toronto"}]}
        turns = chat("I just moved to Toronto. [Stay Ultron: answer only what was asked.]", "Cold choice.")
        with mock.patch(self.URLOPEN, return_value=fake_reply(json.dumps(reply))) as urlopen:
            facts = extract_facts("http://x", turns)
        self.assertEqual(facts, [{"category": "identity", "key": "city", "value": "Toronto"}])
        body = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual(body["id_slot"], 1)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertIs(body["chat_template_kwargs"]["enable_thinking"], False)
        self.assertNotIn("Stay Ultron", body["messages"][-1]["content"])

    def test_server_down_returns_empty(self) -> None:
        with mock.patch(self.URLOPEN, side_effect=urllib.error.URLError("refused")), \
                self.assertLogs("ultron.memory.extractor", "WARNING"):
            self.assertEqual(extract_facts("http://x", chat("My sister is Maya", "Noted.")), [])

    def test_learn_in_background_saves(self) -> None:
        reply = {"facts": [{"quote": "my best friend Kabir", "category": "relationships",
                            "key": "best_friend", "value": "Kabir"}]}
        with mock.patch(self.URLOPEN, return_value=fake_reply(json.dumps(reply))):
            learn_in_background("http://x", chat("my best friend Kabir is visiting", "Lock the door."),
                                self.db).join(5)
        self.assertEqual(self.db.value("SELECT value FROM facts WHERE key = 'best_friend'"), "Kabir")


class SessionTest(TempDb):
    URLOPEN = ExtractorTest.URLOPEN

    def test_summary_is_stored_and_recallable(self) -> None:
        log_turn("s1", "user", "Help me plan my chemistry exam revision", self.db)
        with mock.patch(self.URLOPEN) as urlopen:
            self.assertIsNone(summarize_session("http://x", "s1", self.db))  # one turn: too short
            urlopen.assert_not_called()
        log_turn("s1", "assistant", "Start with organic chemistry.", self.db)
        log_turn("s1", "user", "  ", self.db)  # empty turns are not logged
        self.assertEqual(self.db.value("SELECT COUNT(*) FROM messages"), 2)
        summary = "The user planned chemistry exam revision, starting with organic chemistry."
        with mock.patch(self.URLOPEN, return_value=fake_reply(summary)) as urlopen:
            self.assertEqual(summarize_session("http://x", "s1", self.db), summary)
        self.assertEqual(json.loads(urlopen.call_args[0][0].data)["id_slot"], 1)
        self.assertIn(summary, context_for("when is my chemistry exam", self.db))


if __name__ == "__main__":
    unittest.main()
