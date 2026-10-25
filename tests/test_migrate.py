"""The rename to AJ (core/migrate.py, core/secrets.py): an old install's data folder, memory
database, backups, log and Gemini key move to the new names, and nothing is ever overwritten,
merged or lost. Every test works in a temporary folder and with fake credentials; the real
%APPDATA% and Credential Manager are never touched."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aj.core import migrate, secrets

OLD = migrate.OLD
OLD_DB = f"{OLD.lower()}.db"


class MigrateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.new = self.base / "AJ"
        self.old = self.base / OLD

    def run_it(self, running=False):
        return migrate.run(self.new, running=lambda: running)

    def old_install(self):
        """An old install as it was found on 2026-10-22: a WAL database with uncheckpointed rows."""
        self.old.mkdir()
        (self.old / "backups").mkdir()
        (self.old / "logs").mkdir()
        (self.old / "lines.json").write_text("{}", encoding="utf-8")
        (self.old / "logs" / f"{OLD.lower()}.log").write_text("old log\n", encoding="utf-8")
        (self.old / "logs" / "listener.log").write_text("listener\n", encoding="utf-8")
        for day in ("20261021", "20261022"):
            (self.old / "backups" / f"{OLD.lower()}-{day}.db").write_bytes(b"backup " + day.encode())
        con = sqlite3.connect(self.old / OLD_DB)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")  # the rows stay in the -wal file
        con.execute("CREATE TABLE messages (text TEXT)")
        con.executemany("INSERT INTO messages VALUES (?)", [("hi",), ("open whatsapp",), ("thanks",)])
        con.commit()
        return con  # left open by the caller while the -wal must exist


class DataFolderTests(MigrateCase):
    def test_the_whole_old_install_moves_and_every_memory_survives(self):
        con = self.old_install()
        self.assertTrue((self.old / f"{OLD_DB}-wal").exists())
        # A crashed process leaves the -wal behind; copy that state, since closing would checkpoint.
        snapshot = self.base / "snapshot"
        snapshot.mkdir()
        for suffix in ("", "-wal", "-shm"):
            (snapshot / f"{OLD_DB}{suffix}").write_bytes((self.old / f"{OLD_DB}{suffix}").read_bytes())
        con.close()
        for suffix in ("", "-wal", "-shm"):
            (self.old / f"{OLD_DB}{suffix}").unlink(missing_ok=True)
            (self.old / f"{OLD_DB}{suffix}").write_bytes((snapshot / f"{OLD_DB}{suffix}").read_bytes())

        notes = self.run_it()
        self.assertFalse(self.old.exists())
        self.assertEqual((self.new / "lines.json").read_text(encoding="utf-8"), "{}")
        self.assertTrue((self.new / "aj.db-wal").exists())
        self.assertFalse((self.new / OLD_DB).exists())
        con = sqlite3.connect(self.new / "aj.db")
        self.assertEqual(con.execute("SELECT count(*) FROM messages").fetchone()[0], 3)
        con.close()
        self.assertEqual(sorted(p.name for p in (self.new / "backups").iterdir()),
                         ["aj-20261021.db", "aj-20261022.db"])
        self.assertEqual((self.new / "backups" / "aj-20261022.db").read_bytes(), b"backup 20261022")
        self.assertEqual((self.new / "logs" / "aj.log").read_text(encoding="utf-8"), "old log\n")
        self.assertTrue((self.new / "logs" / "listener.log").exists())
        self.assertEqual(len(notes), 5, notes)  # the folder, the database, two backups, the log

    def test_running_it_again_does_nothing(self):
        self.old_install().close()
        self.run_it()
        self.assertEqual(self.run_it(), [])

    def test_nothing_moves_while_the_old_install_runs(self):
        self.old_install().close()
        notes = self.run_it(running=True)
        self.assertTrue(self.old.exists())
        self.assertFalse(self.new.exists())
        self.assertIn("running", notes[0])

    def test_two_folders_are_never_merged(self):
        self.old_install().close()
        self.new.mkdir()
        (self.new / "apps_index.json").write_text("[]", encoding="utf-8")
        notes = self.run_it()
        self.assertTrue((self.old / OLD_DB).exists())
        self.assertEqual([p.name for p in self.new.iterdir()], ["apps_index.json"])
        self.assertIn("both", notes[0])

    def test_a_fresh_install_has_nothing_to_move(self):
        self.assertEqual(self.run_it(), [])
        self.assertFalse(self.new.exists())

    def test_an_existing_backup_or_log_is_never_replaced(self):
        self.new.mkdir()
        (self.new / "backups").mkdir()
        (self.new / "logs").mkdir()
        (self.new / "backups" / f"{OLD.lower()}-20261022.db").write_bytes(b"old")
        (self.new / "backups" / "aj-20261022.db").write_bytes(b"new")
        (self.new / "logs" / f"{OLD.lower()}.log").write_text("old", encoding="utf-8")
        (self.new / "logs" / "aj.log").write_text("new", encoding="utf-8")
        self.run_it()
        self.assertEqual((self.new / "backups" / "aj-20261022.db").read_bytes(), b"new")
        self.assertEqual((self.new / "backups" / f"{OLD.lower()}-20261022.db").read_bytes(), b"old")
        self.assertEqual((self.new / "logs" / "aj.log").read_text(encoding="utf-8"), "new")


class DatabaseTests(MigrateCase):
    def setUp(self):
        super().setUp()
        self.new.mkdir()
        self.db = self.new / "aj.db"

    def test_an_open_database_stays_where_it_is_and_is_used_there(self):
        con = sqlite3.connect(self.new / OLD_DB)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (x)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
        try:
            self.assertEqual(migrate.settle_db(self.db), self.new / OLD_DB)
            self.assertFalse(self.db.exists())
            self.assertFalse(Path(f"{self.db}-wal").exists())  # what moved first was put back
            self.assertTrue((self.new / f"{OLD_DB}-wal").exists())
        finally:
            con.close()

    def test_a_stray_new_wal_keeps_the_old_database_whole(self):
        (self.new / OLD_DB).write_bytes(b"db")
        (self.new / f"{OLD_DB}-wal").write_bytes(b"wal")
        Path(f"{self.db}-wal").write_bytes(b"stray")
        self.assertEqual(migrate.settle_db(self.db), self.new / OLD_DB)
        self.assertEqual((self.new / f"{OLD_DB}-wal").read_bytes(), b"wal")
        self.assertEqual(Path(f"{self.db}-wal").read_bytes(), b"stray")

    def test_the_new_database_wins_once_it_exists(self):
        (self.new / OLD_DB).write_bytes(b"old")
        self.db.write_bytes(b"new")
        self.assertEqual(migrate.settle_db(self.db), self.db)
        self.assertEqual((self.new / OLD_DB).read_bytes(), b"old")

    def test_no_wal_moves_the_database_alone(self):
        (self.new / OLD_DB).write_bytes(b"db")
        self.assertEqual(migrate.settle_db(self.db), self.db)
        self.assertEqual(self.db.read_bytes(), b"db")
        self.assertEqual(os.listdir(self.new), ["aj.db"])


class SecretTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        patches = [mock.patch.object(secrets, "_read", side_effect=self.store.get),
                   mock.patch.object(secrets, "write_secret", side_effect=self.store.__setitem__)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_the_new_entry_is_read_first(self):
        self.store.update({secrets.GEMINI: "new", f"{OLD}/Gemini": "old"})
        self.assertEqual(secrets.read_secret(secrets.GEMINI), "new")

    def test_an_old_key_is_copied_forward_and_left_in_place(self):
        self.store[f"{OLD}/Gemini"] = "KEY"
        self.assertEqual(secrets.read_secret(secrets.GEMINI), "KEY")
        self.assertEqual(self.store, {f"{OLD}/Gemini": "KEY", "AJ/Gemini": "KEY"})

    def test_no_key_anywhere_is_none(self):
        self.assertIsNone(secrets.read_secret(secrets.GEMINI))
        self.assertEqual(self.store, {})

    def test_a_failed_copy_still_answers(self):
        self.store[f"{OLD}/Gemini"] = "KEY"
        with mock.patch.object(secrets, "write_secret", side_effect=OSError("denied")), \
                self.assertLogs("aj.core.secrets", "WARNING"):
            self.assertEqual(secrets.read_secret(secrets.GEMINI), "KEY")

    def test_other_targets_have_no_fallback(self):
        self.store[f"{OLD}/Other"] = "x"
        self.assertIsNone(secrets.read_secret("AJ/Other"))


class LogsOnlyTests(MigrateCase):
    """The review of 2026-10-22: the listener, AJ (Speak) or the assistant started under the new
    name while an old-name process held its mutex, and made %APPDATA%\\AJ\\logs. That folder is
    merged back and the old install moves as usual; nothing is deleted."""

    def early_logs(self):
        (self.new / "logs").mkdir(parents=True)
        (self.new / "logs" / "speak.log").write_text("speak, new\n", encoding="utf-8")
        (self.new / "logs" / "listener.log").write_text("listener, new\n", encoding="utf-8")

    def test_the_logs_are_merged_and_the_old_install_moves(self):
        self.old_install().close()
        self.early_logs()
        notes = self.run_it()
        self.assertFalse(self.old.exists())
        self.assertTrue((self.new / "aj.db").exists())
        logs = {p.name: p.read_text(encoding="utf-8") for p in (self.new / "logs").iterdir()}
        self.assertEqual(logs["speak.log"], "speak, new\n")
        self.assertEqual(logs["listener.log"], "listener\n")       # the old one keeps its name
        self.assertEqual(logs["listener (AJ).log"], "listener, new\n")  # the new one is kept beside it
        self.assertEqual(logs["aj.log"], "old log\n")
        self.assertTrue(any("merged" in n for n in notes))
        self.assertFalse(any("both" in n for n in notes))

    def test_nothing_moves_while_the_old_install_runs(self):
        self.old_install().close()
        self.early_logs()
        notes = self.run_it(running=True)
        self.assertTrue((self.old / OLD_DB).exists())
        self.assertEqual(sorted(p.name for p in (self.new / "logs").iterdir()), ["listener.log", "speak.log"])
        self.assertIn("running", notes[0])

    def test_a_log_that_cant_move_puts_everything_back(self):
        self.old_install().close()
        self.early_logs()
        real_rename = migrate._rename

        def busy(src, dst):
            if src.name == "speak.log":
                raise PermissionError("in use")
            real_rename(src, dst)

        with mock.patch.object(migrate, "_rename", busy):
            notes = self.run_it()
        self.assertEqual(sorted(p.name for p in (self.new / "logs").iterdir()), ["listener.log", "speak.log"])
        self.assertEqual(sorted(p.name for p in (self.old / "logs").iterdir()), ["listener.log", f"{OLD.lower()}.log"])
        self.assertTrue((self.old / OLD_DB).exists())
        self.assertIn("couldn't merge", notes[0])

    def test_more_than_logs_is_never_merged(self):
        self.old_install().close()
        self.early_logs()
        (self.new / "apps_index.json").write_text("[]", encoding="utf-8")
        self.assertFalse(migrate.only_logs(self.new))
        self.assertIn("both", self.run_it()[0])


if __name__ == "__main__":
    unittest.main()
