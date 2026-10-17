"""File tools, run only inside a temporary folder that stands in for the user's home.

Nothing here touches real files: HOME and the known-folder lookup are patched to a temp dir,
os.startfile is a mock, and SHFileOperationW is replaced by a fake Recycle Bin (another temp
dir), so no test can ever send a real file to the real Recycle Bin.

Run: .venv\\Scripts\\python.exe -m unittest discover -s tests -t .
"""
import _winapi
import ctypes
import os
import shutil
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

try:
    import ultron.tools.intents as intents
    from ultron.tools import files, registry
    from ultron.tools.registry import Level, ToolError
except ModuleNotFoundError as e:  # the tool registry isn't there yet
    if e.name not in ("ultron.tools.registry", "ultron.tools.intents"):
        raise
    files = None

DAY = 86400
MAIN = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")


def setUpModule():
    if files is None:
        raise unittest.SkipTest("ultron.tools.registry isn't there yet")


class FilesCase(unittest.TestCase):
    """A fresh fake home with the six main folders, a fake Recycle Bin and a mocked startfile."""

    def setUp(self) -> None:
        self.home = self._tempdir()
        for name in MAIN:
            (self.home / name).mkdir()
        self.bin = self._tempdir()
        self._patch("ultron.tools.files.HOME", self.home)
        self.shell_folder = self._patch("ultron.tools.files._shell_folder", return_value=None)
        self.startfile = self._patch("ultron.tools.files.os.startfile", create=True)
        self.shfileop = self._patch("ultron.tools.files._SHFileOperationW",
                                    side_effect=AssertionError("a test reached the real Recycle Bin"))
        self._patch("ultron.tools.files._last", [])
        self._patch("ultron.tools.files._undo", [])
        self.recycled: list[tuple[int, int, str]] = []

    def _tempdir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name).resolve()

    def _patch(self, target: str, new=mock.DEFAULT, **kwargs):
        patcher = mock.patch(target, new, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def make(self, rel: str, text: str = "x", age_days: float = 0) -> Path:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))  # bytes: write_text would turn \n into \r\n
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))
        return path

    def fake_recycle_bin(self, code: int = 0) -> None:
        """SHFileOperationW stand-in: records the call and moves the file into self.bin."""
        def shfileop(op_ref):
            op = op_ref._obj
            path = ctypes.wstring_at(op.pFrom)
            self.recycled.append((op.wFunc, op.fFlags, ctypes.wstring_at(op.pFrom, len(path) + 2)))
            if code == 0:
                shutil.move(path, self.bin / Path(path).name)
            return code
        self.shfileop.side_effect = shfileop

    def opened(self) -> list[Path]:
        return [Path(c.args[0]) for c in self.startfile.call_args_list]


class RegistrationTests(unittest.TestCase):
    def test_levels(self):
        expected = {"find_files": Level.SAFE, "open_file": Level.SAFE, "open_folder": Level.SAFE,
                    "list_folder": Level.SAFE, "read_text_file": Level.SAFE, "undo_last": Level.SAFE,
                    "disk_space": Level.SAFE, "move_file": Level.CONFIRM, "copy_file": Level.CONFIRM,
                    "rename_file": Level.CONFIRM, "delete_file": Level.CONFIRM}
        for name, level in expected.items():
            with self.subTest(tool=name):
                spec = registry.get(name)
                self.assertIsNotNone(spec)
                self.assertIs(spec.level, level)
                self.assertFalse(spec.cloud)

    def test_folder_is_an_enum(self):
        props = registry.get("find_files").parameters["properties"]
        self.assertEqual(props["folder"]["enum"], ["anywhere", "desktop", "documents", "downloads",
                                                   "pictures", "music", "videos"])

    def test_recycle_flags_never_mean_permanent(self):
        self.assertTrue(files.RECYCLE_FLAGS & files.FOF_ALLOWUNDO)
        self.assertTrue(files.RECYCLE_FLAGS & files.FOF_NOCONFIRMATION)
        self.assertTrue(files.RECYCLE_FLAGS & files.FOF_SILENT)
        self.assertTrue(files.RECYCLE_FLAGS & files.FOF_WANTNUKEWARNING)


class RankingTests(FilesCase):
    def test_name_scores(self):
        self.assertEqual(files.name_score("budget", "Budget.xlsx"), 100)
        self.assertEqual(files.name_score("budget xlsx", "budget.xlsx"), 100)
        self.assertEqual(files.name_score("budget", "Budget_2026.xlsx"), 80)
        self.assertEqual(files.name_score("budget", "old budget notes.txt"), 65)
        self.assertEqual(files.name_score("notes budget", "old budget notes.txt"), 45)
        self.assertEqual(files.name_score("resume", "Résumé-final.pdf"), 80)
        self.assertEqual(files.name_score("tax", "syntax.txt"), 0)
        self.assertEqual(files.name_score("budget", "report.txt"), 0)

    def test_best_match_first_then_recency(self):
        self.make("Documents/budget.xlsx", age_days=400)
        self.make("Desktop/Budget 2026.xlsx", age_days=0)
        self.make("Downloads/old budget notes.txt", age_days=1)
        self.make("Documents/report.txt")
        text = files.find_files("my budget file")
        lines = text.splitlines()
        self.assertIn('"budget"', lines[0])
        self.assertTrue(lines[1].startswith(r"1. Documents\budget.xlsx"))
        self.assertTrue(lines[2].startswith(r"2. Desktop\Budget 2026.xlsx"))
        self.assertTrue(lines[3].startswith(r"3. Downloads\old budget notes.txt"))
        self.assertEqual(len(lines), 4)

    def test_equal_names_newest_first(self):
        self.make("Desktop/report.txt", age_days=90)
        self.make("Documents/report.txt", age_days=2)
        lines = files.find_files("report").splitlines()
        self.assertTrue(lines[1].startswith(r"1. Documents\report.txt"))
        self.assertTrue(lines[2].startswith(r"2. Desktop\report.txt"))

    def test_skips_hidden_private_cache_and_linked_folders(self):
        self.make("Documents/.secret/budget.txt")
        self.make("Documents/node_modules/budget.js")
        self.make("AppData/Roaming/budget.txt")
        hidden = self.make("Documents/stash/budget.md").parent
        ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)
        outside = self._tempdir()
        (outside / "budget.doc").write_text("x")
        link = self.home / "Desktop" / "link"
        _winapi.CreateJunction(str(outside), str(link))
        self.addCleanup(os.rmdir, link)  # removes the junction only, never what it points to
        self.make("Pictures/budget.png")
        text = files.find_files("budget")
        self.assertIn(r"Pictures\budget.png", text)
        for leaked in (".secret", "node_modules", "AppData", "stash", "budget.doc"):
            self.assertNotIn(leaked, text)

    def test_one_folder_only(self):
        self.make("Documents/budget.xlsx")
        self.make("Downloads/budget.csv")
        text = files.find_files("budget", folder="downloads")
        self.assertIn("budget.csv", text)
        self.assertNotIn("budget.xlsx", text)

    def test_folders_are_found_too(self):
        (self.home / "Documents" / "Taxes 2025").mkdir()
        self.assertIn(r"1. Documents\Taxes 2025 (folder, changed today)", files.find_files("taxes"))

    def test_nothing_found_clears_the_list(self):
        self.make("Documents/budget.xlsx")
        files.find_files("budget")
        self.assertIn("Nothing called", files.find_files("zebra"))
        with self.assertRaisesRegex(ToolError, "no list"):
            files.open_file("1")

    def test_budget_running_out_is_said(self):
        self.make("Documents/a/b/budget.txt")
        with mock.patch.object(files, "_WALK_ENTRIES", 1):
            self.assertIn("name a folder", files.find_files("budget"))

    def test_empty_query_refused(self):
        with self.assertRaises(ToolError):
            files.find_files("   ")


class RememberedListTests(FilesCase):
    def setUp(self) -> None:
        super().setUp()
        self.first = self.make("Documents/budget.xlsx", age_days=30)
        self.second = self.make("Desktop/budget notes.txt", age_days=1)
        files.find_files("budget")

    def test_open_by_number(self):
        for said in ("2", "#2", "number 2", "the second one", "2nd", "the last one"):
            with self.subTest(said=said):
                self.startfile.reset_mock()
                self.assertEqual(files.open_file(said), "Opened budget notes.txt.")
                self.assertEqual(self.opened(), [self.second])

    def test_open_by_name_from_the_list(self):
        files.open_file("budget")  # the stem of result 1
        self.assertEqual(self.opened(), [self.first])

    def test_out_of_range(self):
        with self.assertRaisesRegex(ToolError, "1 to 2"):
            files.open_file("5")

    def test_open_folder_of_a_result(self):
        self.assertEqual(files.open_folder("1"), "Opened Documents in File Explorer.")
        self.assertEqual(self.opened(), [self.home / "Documents"])

    def test_changes_need_a_path_not_a_number(self):
        for call in (lambda: files.delete_file("2"), lambda: files.move_file("1", "music"),
                     lambda: files.rename_file("the first one", "x")):
            with self.assertRaisesRegex(ToolError, "not its number"):
                call()
        self.shfileop.assert_not_called()
        self.assertTrue(self.first.exists() and self.second.exists())


class PathSafetyTests(FilesCase):
    def refused(self, call, pattern: str = "") -> None:
        with self.assertRaisesRegex(ToolError, pattern):
            call()

    def test_outside_home_refused(self):
        outside = self._tempdir() / "secret.txt"
        outside.write_text("x")
        self.refused(lambda: files.open_file(str(outside)), "outside your user folder")
        self.refused(lambda: files.read_text_file(r"C:\Windows\win.ini"), "Windows system folder")

    def test_traversal_and_network_paths_refused(self):
        self.make("Documents/a.txt")
        self.refused(lambda: files.open_file(r"Documents\..\..\a.txt"), r"'\.\.'")
        self.refused(lambda: files.open_file(str(self.home / "Documents" / ".." / "Documents" / "a.txt")), r"'\.\.'")
        self.refused(lambda: files.open_file(r"\\server\share\a.txt"), "network")
        self.startfile.assert_not_called()

    def test_private_places_refused(self):
        self.make("AppData/Roaming/app.json")
        self.make(".ssh/id_rsa")
        self.make("Documents/notes.txt")
        self.refused(lambda: files.read_text_file(r"AppData\Roaming\app.json"), "hidden or system")
        self.refused(lambda: files.read_text_file(r"~\.ssh\id_rsa"), "hidden or system")
        self.refused(lambda: files.read_text_file(str(self.home / ".ssh" / "id_rsa")), "hidden or system")
        self.refused(lambda: files.read_text_file(r"Documents\notes.txt:secret"), "hidden or system")

    def test_junction_out_of_home_refused(self):
        outside = self._tempdir()
        (outside / "secret.txt").write_text("x")
        link = self.home / "Documents" / "escape"
        _winapi.CreateJunction(str(outside), str(link))
        self.addCleanup(os.rmdir, link)
        self.refused(lambda: files.read_text_file(r"Documents\escape\secret.txt"), "outside your user folder")

    def test_main_folders_cant_be_moved_renamed_or_deleted(self):
        self.fake_recycle_bin()
        self.refused(lambda: files.delete_file("documents"), "main folders")
        self.refused(lambda: files.delete_file(str(self.home)), "main folders")
        self.refused(lambda: files.move_file("Desktop", "documents"), "main folders")
        self.refused(lambda: files.rename_file("my downloads folder", "old"), "main folders")
        self.assertEqual(self.recycled, [])
        self.assertTrue(all((self.home / name).is_dir() for name in MAIN))

    def test_folder_holding_a_main_folder_is_protected(self):
        onedrive_desktop = self.home / "OneDrive" / "Desktop"
        onedrive_desktop.mkdir(parents=True)
        self.shell_folder.side_effect = lambda name: onedrive_desktop if name == "desktop" else None
        self.fake_recycle_bin()
        self.refused(lambda: files.delete_file("OneDrive"), "main folders")
        self.assertEqual(self.recycled, [])

    def test_ultrons_own_folder_is_protected(self):
        own = self.home / "Downloads" / "Ultron"
        self.make(r"Downloads\Ultron\ultron\engine.py")
        self.make(r"Downloads\notes.txt")
        self.fake_recycle_bin()
        with mock.patch.object(files, "APP_DIR", own):
            self.refused(lambda: files.delete_file(r"Downloads\Ultron"), "Ultron's own")
            self.refused(lambda: files.delete_file(r"Downloads\Ultron\ultron\engine.py"), "Ultron's own")
            self.refused(lambda: files.move_file(r"Downloads\notes.txt", r"Downloads\Ultron"), "Ultron's own")
        self.assertEqual(self.recycled, [])

    def test_programs_and_scripts_are_not_opened(self):
        for name in ("setup.exe", "run.bat", "invoice.pdf.exe", "tool.ps1", "Spotify.lnk", "script.py"):
            self.make(f"Downloads/{name}")
            with self.subTest(name=name):
                self.refused(lambda: files.open_file(rf"Downloads\{name}"), "program or script")
        self.startfile.assert_not_called()

    def test_documents_open_by_path_name_or_folder(self):
        doc = self.make("Desktop/plan.docx")
        files.open_file(str(doc))
        files.open_file(r"Desktop\plan.docx")
        files.open_file("plan.docx")  # a bare name at the top of a main folder
        files.open_folder("downloads")
        self.assertEqual(self.opened(), [doc, doc, doc, self.home / "Downloads"])

    def test_ambiguous_bare_name(self):
        self.make("Desktop/todo.txt")
        self.make("Documents/todo.txt")
        self.refused(lambda: files.open_file("todo.txt"), "more than one")


class MoveRenameUndoTests(FilesCase):
    def test_move_and_undo(self):
        src = self.make("Desktop/a.txt", "hello")
        self.assertEqual(files.move_file(r"Desktop\a.txt", "my documents"), "Moved a.txt to Documents.")
        moved = self.home / "Documents" / "a.txt"
        self.assertFalse(src.exists())
        self.assertEqual(moved.read_text(), "hello")
        self.assertEqual(files.undo_last(), "Put a.txt back in Desktop.")
        self.assertEqual(src.read_text(), "hello")
        self.assertFalse(moved.exists())
        self.assertEqual(files.undo_last(), "There's nothing to undo.")

    def test_move_into_a_subfolder_by_relative_path(self):
        self.make("Downloads/scan.pdf")
        (self.home / "Documents" / "Taxes").mkdir()
        self.assertEqual(files.move_file(r"Downloads\scan.pdf", r"Documents\Taxes"), "Moved scan.pdf to Taxes.")
        self.assertTrue((self.home / "Documents" / "Taxes" / "scan.pdf").exists())

    def test_move_never_overwrites(self):
        self.make("Desktop/a.txt", "new")
        self.make("Documents/a.txt", "old")
        with self.assertRaisesRegex(ToolError, "won't overwrite"):
            files.move_file(r"Desktop\a.txt", "documents")
        self.assertEqual((self.home / "Documents" / "a.txt").read_text(), "old")
        self.assertEqual(files._undo, [])

    def test_move_folder_into_itself_refused(self):
        (self.home / "Documents" / "Work" / "Sub").mkdir(parents=True)
        with self.assertRaisesRegex(ToolError, "inside itself"):
            files.move_file(r"Documents\Work", r"Documents\Work\Sub")

    def test_missing_destination(self):
        self.make("Desktop/a.txt")
        with self.assertRaisesRegex(ToolError, "can't find"):
            files.move_file(r"Desktop\a.txt", r"Documents\Nope")

    def test_rename_keeps_extension_and_undoes(self):
        src = self.make("Documents/report.docx")
        self.assertEqual(files.rename_file(r"Documents\report.docx", "final report"),
                         "Renamed report.docx to final report.docx.")
        self.assertTrue((self.home / "Documents" / "final report.docx").exists())
        self.assertEqual(files.undo_last(), "Renamed final report.docx back to report.docx.")
        self.assertTrue(src.exists())

    def test_rename_case_only(self):
        self.make("Documents/budget.txt")
        files.rename_file(r"Documents\budget.txt", "Budget.txt")
        self.assertEqual(os.listdir(self.home / "Documents"), ["Budget.txt"])
        files.undo_last()
        self.assertEqual(os.listdir(self.home / "Documents"), ["budget.txt"])

    def test_bad_new_names_refused(self):
        self.make("Documents/a.txt")
        self.make("Documents/b.txt")
        for bad in ("../x", r"sub\x", "a:b", "con", "  ", "..", "what?"):
            with self.subTest(new_name=bad), self.assertRaises(ToolError):
                files.rename_file(r"Documents\a.txt", bad)
        with self.assertRaisesRegex(ToolError, "won't overwrite"):
            files.rename_file(r"Documents\a.txt", "b")
        with self.assertRaisesRegex(ToolError, "hidden or system"):
            files.rename_file(r"Documents\a.txt", ".bashrc")
        self.assertTrue((self.home / "Documents" / "a.txt").exists())

    def test_undo_when_the_file_has_moved_on(self):
        self.make("Desktop/a.txt")
        files.move_file(r"Desktop\a.txt", "documents")
        (self.home / "Documents" / "a.txt").unlink()
        with self.assertRaisesRegex(ToolError, "isn't where I left it"):
            files.undo_last()
        self.assertEqual(files.undo_last(), "There's nothing to undo.")  # the stale entry was dropped

    def test_undo_refuses_to_overwrite_a_new_file(self):
        self.make("Desktop/a.txt", "moved")
        files.move_file(r"Desktop\a.txt", "documents")
        self.make("Desktop/a.txt", "a new one")
        with self.assertRaisesRegex(ToolError, "left both alone"):
            files.undo_last()
        self.assertEqual((self.home / "Desktop" / "a.txt").read_text(), "a new one")

    def test_undo_goes_back_in_order(self):
        self.make("Desktop/a.txt")
        files.move_file(r"Desktop\a.txt", "documents")
        files.rename_file(r"Documents\a.txt", "b")
        files.undo_last()
        files.undo_last()
        self.assertTrue((self.home / "Desktop" / "a.txt").exists())
        self.assertEqual(os.listdir(self.home / "Documents"), [])


class CopyDeleteTests(FilesCase):
    def test_copy_into_same_folder_and_undo_to_recycle_bin(self):
        src = self.make("Documents/budget.xlsx", "data")
        self.fake_recycle_bin()
        self.assertEqual(files.copy_file(r"Documents\budget.xlsx", "documents"),
                         "Copied budget.xlsx to Documents as budget - Copy.xlsx.")
        copy = self.home / "Documents" / "budget - Copy.xlsx"
        self.assertEqual(copy.read_text(), "data")
        self.assertIn("Recycle Bin", files.undo_last())
        self.assertFalse(copy.exists())
        self.assertEqual(src.read_text(), "data")
        self.assertEqual([op[2] for op in self.recycled], [str(copy) + "\0\0"])
        self.assertEqual((self.bin / copy.name).read_text(), "data")

    def test_copy_folder(self):
        self.make("Documents/Trip/day1.txt", "sun")
        files.copy_file(r"Documents\Trip", "desktop")
        self.assertEqual((self.home / "Desktop" / "Trip" / "day1.txt").read_text(), "sun")
        self.assertTrue((self.home / "Documents" / "Trip" / "day1.txt").exists())

    def test_delete_goes_to_the_recycle_bin(self):
        target = self.make("Downloads/old.zip", "zip")
        self.fake_recycle_bin()
        self.assertEqual(files.delete_file(r"Downloads\old.zip"), "old.zip is in the Recycle Bin.")
        self.assertEqual(len(self.recycled), 1)
        func, flags, names = self.recycled[0]
        self.assertEqual(func, files.FO_DELETE)
        for flag in (files.FOF_ALLOWUNDO, files.FOF_NOCONFIRMATION, files.FOF_SILENT):
            self.assertTrue(flags & flag)
        self.assertEqual(names, str(target) + "\0\0")  # a double-NUL-terminated list of one path
        self.assertFalse(target.exists())
        self.assertIn("Restore", files.undo_last())
        self.assertEqual(files.undo_last(), "There's nothing to undo.")

    def test_failed_recycle_leaves_the_file(self):
        target = self.make("Downloads/old.zip")
        self.fake_recycle_bin(code=0x78)
        with self.assertRaisesRegex(ToolError, "left it where it is"):
            files.delete_file(r"Downloads\old.zip")
        self.assertTrue(target.exists())
        self.assertEqual(files._undo, [])

    def test_recycle_reported_but_file_still_there(self):
        target = self.make("Downloads/stuck.txt")
        self.shfileop.side_effect = lambda op_ref: 0  # claims success, moves nothing
        with self.assertRaisesRegex(ToolError, "still there"):
            files.delete_file(r"Downloads\stuck.txt")
        self.assertTrue(target.exists())
        self.assertEqual(files._undo, [])


class ReadAndListTests(FilesCase):
    def test_reads_the_start_of_a_long_file(self):
        self.make("Documents/notes.md", "word " * 1000)
        text = files.read_text_file(r"Documents\notes.md")
        body = text.split("\n", 1)[1]
        self.assertTrue(text.startswith("notes.md:\n"))
        self.assertIn("only the start", text)
        self.assertEqual(len(body.split("\n[")[0]), files.READ_CHARS)

    def test_short_csv_whole(self):
        self.make("Documents/data.csv", "a,b\r\n1,2\r\n")
        self.assertEqual(files.read_text_file(r"Documents\data.csv"), "data.csv:\na,b\n1,2\n")

    def test_utf16_and_cp1252(self):
        (self.home / "Documents" / "u16.txt").write_bytes("héllo".encode("utf-16"))
        (self.home / "Documents" / "old.txt").write_bytes("caf\xe9".encode("cp1252"))
        self.assertIn("héllo", files.read_text_file(r"Documents\u16.txt"))
        self.assertIn("café", files.read_text_file(r"Documents\old.txt"))

    def test_pdf_office_and_binary_refused(self):
        self.make("Documents/report.pdf")
        self.make("Documents/deck.pptx")
        (self.home / "Pictures" / "cat.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0")
        with self.assertRaisesRegex(ToolError, "PDF"):
            files.read_text_file(r"Documents\report.pdf")
        with self.assertRaisesRegex(ToolError, "PowerPoint"):
            files.read_text_file(r"Documents\deck.pptx")
        with self.assertRaisesRegex(ToolError, "isn't a text file"):
            files.read_text_file(r"Pictures\cat.png")

    def test_list_folder_folders_first_newest_first_and_numbered(self):
        (self.home / "Downloads" / "Setup").mkdir()
        self.make("Downloads/old.zip", age_days=10)
        self.make("Downloads/new.pdf", age_days=0)
        self.make("Downloads/.hidden.txt")
        self.make("Downloads/desktop.ini")
        ctypes.windll.kernel32.SetFileAttributesW(str(self.home / "Downloads" / "desktop.ini"), 0x2 | 0x4)
        lines = files.list_folder("downloads").splitlines()
        self.assertEqual(lines[0], "Downloads has 3 items:")
        self.assertEqual(lines[1], "1. Setup (folder)")
        self.assertTrue(lines[2].startswith("2. new.pdf ("))
        self.assertTrue(lines[3].startswith("3. old.zip ("))
        files.open_file("3")
        self.assertEqual(self.opened(), [self.home / "Downloads" / "old.zip"])

    def test_list_empty_and_file(self):
        self.assertEqual(files.list_folder("music"), "Music is empty.")
        self.make("Music/song.mp3")
        with self.assertRaisesRegex(ToolError, "not a folder"):
            files.list_folder(r"Music\song.mp3")


class DiskSpaceTests(unittest.TestCase):
    def test_fixed_drives_only(self):
        usage = namedtuple("usage", "total used free")
        gb = 1024 ** 3
        with mock.patch.object(files.os, "listdrives", return_value=["C:\\", "E:\\"], create=True), \
             mock.patch.object(files, "_GetDriveTypeW", side_effect=lambda d: 3 if d == "C:\\" else 2), \
             mock.patch.object(files.shutil, "disk_usage", return_value=usage(500 * gb, 400 * gb, 100 * gb)):
            self.assertEqual(files.disk_space(), "C: has 100 GB free of 500 GB (80% used).")


class IntentTests(FilesCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = intents._rules
        cls.accepted = intents.collect([files])

    @classmethod
    def tearDownClass(cls):
        intents._rules = cls.saved

    def test_every_rule_compiles_and_names_a_real_tool(self):
        self.assertEqual(self.accepted, len(files.INTENTS))

    def test_phrasings_reach_the_right_tool(self):
        said = {
            "Undo.": ("undo_last", {}),
            "undo that please": ("undo_last", {}),
            "Ultron, put it back": ("undo_last", {}),
            "How much space do I have left?": ("disk_space", {}),
            "how much space do i have": ("disk_space", {}),
            "How much free space is left on my laptop?": ("disk_space", {}),
            "check disk space": ("disk_space", {}),
            "how full is my hard drive": ("disk_space", {}),
            "Open my downloads.": ("open_folder", {"folder": "downloads"}),
            "open the documents folder": ("open_folder", {"folder": "documents"}),
            "open my photos folder": ("open_folder", {"folder": "pictures"}),
            "What's on my desktop?": ("list_folder", {"folder": "desktop"}),
            "list the files in downloads": ("list_folder", {"folder": "downloads"}),
            "Where is my resume file?": ("find_files", {"query": "resume"}),
            "find my tax pdf": ("find_files", {"query": "tax pdf"}),
            "search for files named budget": ("find_files", {"query": "budget"}),
            "find the budget spreadsheet in my documents": ("find_files", {"query": "budget", "folder": "documents"}),
            "look for invoice in downloads": ("find_files", {"query": "invoice", "folder": "downloads"}),
        }
        for text, expected in said.items():
            with self.subTest(said=text):
                self.assertEqual(intents.match(text), expected)

    def test_numbers_become_exact_paths(self):
        self.make("Documents/budget.xlsx", age_days=5)
        self.make("Desktop/budget notes.txt", age_days=1)
        files.find_files("budget")
        self.assertEqual(intents.match("open number 2"), ("open_file", {"path_or_index": r"Desktop\budget notes.txt"}))
        self.assertEqual(intents.match("Open the first one."), ("open_file", {"path_or_index": r"Documents\budget.xlsx"}))
        self.assertEqual(intents.match("delete number two"), ("delete_file", {"path": r"Desktop\budget notes.txt"}))
        self.assertEqual(intents.match("read the last one"), ("read_text_file", {"path": r"Desktop\budget notes.txt"}))
        self.assertEqual(intents.match("move number 1 to my desktop"),
                         ("move_file", {"path": r"Documents\budget.xlsx", "destination": "desktop"}))
        self.assertIsNone(intents.match("open number 7"))  # not on the list: left to the model

    def test_numbers_without_a_list_are_declined(self):
        for text in ("open number 2", "delete the second one", "open the first one"):
            with self.subTest(said=text):
                self.assertIsNone(intents.match(text))

    def test_other_requests_are_left_alone(self):
        for text in ["find a restaurant nearby", "where is the nearest gas station", "open spotify",
                     "open music", "open youtube", "search for the best pizza in town", "look up python decorators",
                     "how much space does a whale need", "volume 30", "what time is it", "find me a pdf",
                     "open my budget spreadsheet", "undo my tie", "where is paris", "delete my account"]:
            with self.subTest(said=text):
                self.assertIsNone(intents.match(text))


if __name__ == "__main__":
    unittest.main()
