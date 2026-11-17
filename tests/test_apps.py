"""App tools against a fake Start Menu. No test launches, focuses or closes a real app.

os.startfile, subprocess.Popen, the focus call and WM_CLOSE are all replaced with mocks that
fail loudly if reached unexpectedly. The only real Windows calls are read-only (listing windows
and processes) in RealLaptopTests.
Run: .venv\\Scripts\\python.exe -m unittest discover -s tests -t .
"""
import json
import os
import re
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aj.config import APP_DIR
from aj.tools import apps, intents, registry, web
from aj.tools.apps import App, Window
from aj.tools.registry import Level, ToolError

_CLSID = bytes.fromhex("0114020000000000c000000000000046")


def make_lnk(path: Path, target: str = "", args: str = "", workdir: str = "", ansi: bool = False,
             env: str = "", relative: str = "") -> Path:
    """A minimal but valid MS-SHLLINK file, built by hand so the tests need no COM."""
    flags, body = 0x80, b""  # IsUnicode
    if target:
        flags |= 0x2  # HasLinkInfo
        if ansi:
            base = target.encode("mbcs") + b"\0"
            info = struct.pack("<7I", 0x1C + len(base) + 1, 0x1C, 1, 0, 0x1C, 0, 0x1C + len(base)) + base + b"\0"
        else:
            wide = target.encode("utf-16-le") + b"\0\0"
            offsets = (0x24, 0x25, 0x26, 0x26 + len(wide))  # ANSI base, ANSI suffix, wide base, wide suffix
            tail = b"\0\0" + wide + b"\0\0"
            info = struct.pack("<9I", 0x24 + len(tail), 0x24, 1, 0, offsets[0], 0, offsets[1], *offsets[2:]) + tail
        body += info
    for bit, text in ((0x8, relative), (0x10, workdir), (0x20, args)):
        if text:
            flags |= bit
            body += struct.pack("<H", len(text)) + text.encode("utf-16-le")
    extra = b""
    if env:
        flags |= 0x200
        extra += struct.pack("<II", 0x314, 0xA0000001) + env.encode("mbcs").ljust(260, b"\0") \
            + env.encode("utf-16-le").ljust(520, b"\0")
    header = struct.pack("<I", 0x4C) + _CLSID + struct.pack("<I", flags) + bytes(0x4C - 0x18)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + body + extra + b"\0\0\0\0")
    return path


def write_url(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-8")


def build_start_menu(root: Path) -> None:
    make_lnk(root / "Google Chrome.lnk", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    make_lnk(root / "Discord Inc" / "Discord.lnk", r"C:\Users\me\AppData\Local\Discord\Update.exe",
             args="--processStart Discord.exe")
    make_lnk(root / "Steam" / "Steam.lnk", r"C:\Program Files (x86)\Steam\steam.exe")
    write_url(root / "Steam" / "Steam Support Center.url", "http://support.steampowered.com/")
    write_url(root / "Steam" / "Resident Evil 3.url", "steam://rungameid/952060")
    make_lnk(root / "Visual Studio Code" / "Visual Studio Code.lnk", r"C:\Users\me\AppData\Local\Programs\VS Code\Code.exe")
    make_lnk(root / "VideoLAN" / "VLC media player.lnk", r"C:\Program Files\VideoLAN\VLC\vlc.exe")
    make_lnk(root / "VideoLAN" / "Documentation.lnk", r"C:\Program Files\VideoLAN\VLC\Documentation.url")
    make_lnk(root / "VideoLAN" / "VLC media player - reset preferences and cache files.lnk",
             r"C:\Program Files\VideoLAN\VLC\vlc.exe", args="--reset-config")
    make_lnk(root / "Node.js" / "Uninstall Node.js.lnk", r"C:\Windows\System32\msiexec.exe")
    make_lnk(root / "Accessibility" / "Magnify.lnk", r"C:\WINDOWS\system32\magnify.exe")
    make_lnk(root / "Old Tool.lnk", r"C:\Tools\oldtool.exe", ansi=True)
    make_lnk(root / "Env Tool.lnk", env=r"%SystemRoot%\envtool.exe")
    make_lnk(root / "Startup" / "AULA F75.lnk", r"C:\Program Files (x86)\AULA\OemDrv.exe")
    make_lnk(root / "AJ.lnk", r"C:\Python\pythonw.exe", workdir=str(APP_DIR))
    make_lnk(root / "Voice Assistant.lnk", r"C:\Python\pythonw.exe", args=f'"{APP_DIR}\\run.py"')
    make_lnk(root / "File Explorer.lnk", r"C:\WINDOWS\explorer.exe")
    (root / "Broken.lnk").write_bytes(b"this is not a shell link")


START_APPS = [
    ("WhatsApp", "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"),
    ("Spotify", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"),
    ("Google Chrome", "Chrome"),
    ("Discord", "com.squirrel.Discord.Discord"),
    ("Steam", "Valve.Steam.Client"),
    ("Visual Studio Code", "Microsoft.VisualStudioCode"),
    ("Magnifier", r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\magnify.exe"),
    ("Calendar", "Microsoft.M365Companions_8wekyb3d8bbwe!Calendar"),
    ("Calendar", "microsoft.windowscommunicationsapps_8wekyb3d8bbwe!microsoft.windowslive.calendar"),
    ("Mail", "microsoft.windowscommunicationsapps_8wekyb3d8bbwe!microsoft.windowslive.mail"),
    ("Settings", "windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel"),
    ("Microsoft Teams", "MSTeams_8wekyb3d8bbwe!MSTeams"),
    ("Python 3.13 (64-bit)", r"C:\Users\me\AppData\Local\Programs\Python\Python313\python.exe"),
    ("File Explorer", "Microsoft.Windows.Explorer"),
    ("YouTube", "Chrome._crx_agimnkijcaahngcdmfeangaknmldooml"),
    ("Steam Support Center", "http://support.steampowered.com/"),
    ("Resident Evil 3", "steam://rungameid/952060"),
    ("AJ", "Microsoft.AutoGenerated.{989CCCCB-2B1D-CA79-A64A-9A06061A9496}"),
    ("Uninstall qBittorrent", r"{6D809377-6AF0-444B-8957-A3773F02200E}\qBittorrent\uninst.exe"),
    ("Git Release Notes", r"{6D809377-6AF0-444B-8957-A3773F02200E}\Git\ReleaseNotes.html"),
]


class AppsCase(unittest.TestCase):
    """A fake Start Menu and index for every test, and nothing real that could start or close an app."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "Programs"
        build_start_menu(cls.root)
        cls.shortcuts = apps.scan_shortcuts([cls.root])
        cls.apps = apps.merge(cls.shortcuts, START_APPS)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        saved = apps._current, list(apps.PRERENDER)
        self.addCleanup(self._restore, saved)
        self.refresh = self._patch("aj.tools.apps.refresh")
        self._patch("aj.tools.apps._started", True)
        self._patch("aj.tools.apps._start_menu_roots", return_value=[self.root])
        self.startfile = self._patch("aj.tools.apps.os.startfile", create=True,
                                     side_effect=AssertionError("a test tried to start a real app"))
        self.popen = self._patch("aj.tools.apps.subprocess.Popen",
                                 side_effect=AssertionError("a test tried to start a real process"))
        self.focus = self._patch("aj.tools.apps._focus", side_effect=AssertionError("a test tried to focus a window"))
        self.post_close = self._patch("aj.tools.apps._post_close",
                                      side_effect=AssertionError("a test tried to close a window"))
        self.windows = self._patch("aj.tools.apps.list_windows", return_value=[])
        self.index = apps._install(self.apps, True)

    @staticmethod
    def _restore(saved):
        apps._current, apps.PRERENDER[:] = saved[0], saved[1]

    def _patch(self, target: str, *args, **kwargs):
        patcher = mock.patch(target, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def app(self, name: str) -> App:
        return next(a for a in self.apps if a.name == name)


class ShortcutTests(AppsCase):
    def test_reads_unicode_ansi_environment_and_relative_targets(self):
        self.assertEqual(apps.read_shortcut(self.root / "Google Chrome.lnk").target,
                         r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        self.assertEqual(apps.read_shortcut(self.root / "Old Tool.lnk").target, r"C:\Tools\oldtool.exe")
        self.assertEqual(apps.read_shortcut(self.root / "Env Tool.lnk").target,
                         os.path.expandvars(r"%SystemRoot%\envtool.exe"))
        with tempfile.TemporaryDirectory() as tmp:
            link = make_lnk(Path(tmp, "Rel.lnk"), relative=r"..\bin\rel.exe")
            self.assertEqual(apps.read_shortcut(link).target, os.path.normpath(os.path.join(tmp, r"..\bin\rel.exe")))

    def test_arguments_and_working_folder(self):
        link = apps.read_shortcut(self.root / "Discord Inc" / "Discord.lnk")
        self.assertEqual(link.arguments, "--processStart Discord.exe")
        self.assertEqual(apps.read_shortcut(self.root / "AJ.lnk").working_dir, str(APP_DIR))

    def test_not_a_shortcut(self):
        with self.assertRaises(ValueError):
            apps.read_shortcut(self.root / "Broken.lnk")
        with tempfile.TemporaryDirectory() as tmp:
            cut = Path(tmp, "cut.lnk")
            cut.write_bytes((self.root / "Google Chrome.lnk").read_bytes()[:0x60])
            with self.assertRaises(ValueError):
                apps.read_shortcut(cut)

    def test_squirrel_apps_are_known_by_the_program_they_start(self):
        self.assertEqual(apps._exes_of(apps.Shortcut(r"C:\x\Update.exe", "--processStart Discord.exe")),
                         ("discord.exe",))
        self.assertEqual(apps._exes_of(apps.Shortcut(r"C:\x\Code.exe")), ("code.exe",))
        self.assertEqual(apps._exes_of(apps.Shortcut(r"C:\x\notes.txt")), ())


class IndexTests(AppsCase):
    def test_scan_keeps_apps_and_drops_the_rest(self):
        names = {a.name for a in self.shortcuts}
        self.assertTrue({"Google Chrome", "Discord", "Steam", "Resident Evil 3", "VLC media player", "Magnify",
                         "Old Tool", "Env Tool", "File Explorer", "Broken"} <= names, names)
        for dropped in ["Steam Support Center", "Documentation", "VLC media player - reset preferences and cache files",
                        "Uninstall Node.js", "AULA F75", "AJ", "Voice Assistant"]:
            self.assertNotIn(dropped, names)
        self.assertEqual(next(a for a in self.shortcuts if a.name == "Broken").exes, ())

    def test_merge_adds_store_apps_and_app_ids(self):
        chrome, whatsapp = self.app("Google Chrome"), self.app("WhatsApp")
        self.assertEqual((chrome.kind, chrome.aumid, chrome.exes), ("shortcut", "Chrome", ("chrome.exe",)))
        self.assertEqual((whatsapp.kind, whatsapp.target), ("appid", "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"))
        self.assertIn("whatsapp.exe", whatsapp.exes)  # from the nickname table
        self.assertEqual(self.app("Discord").exes, ("discord.exe",))
        self.assertEqual(self.app("Steam").exes, ("steam.exe", "steamwebhelper.exe"))

    def test_merge_by_program_renames_and_keeps_the_old_name(self):
        magnifier = self.app("Magnifier")
        self.assertEqual((magnifier.kind, magnifier.aliases), ("shortcut", ("Magnify",)))
        self.assertFalse(any(a.name == "Magnify" for a in self.apps))

    def test_merge_drops_duplicates_links_docs_and_itself(self):
        names = [a.name for a in self.apps]
        self.assertEqual(names.count("Calendar"), 1)
        self.assertEqual(self.app("Calendar").aumid, "Microsoft.M365Companions_8wekyb3d8bbwe!Calendar")
        self.assertEqual(names, sorted(names, key=str.casefold))
        for dropped in ["Steam Support Center", "AJ", "Uninstall qBittorrent", "Git Release Notes"]:
            self.assertNotIn(dropped, names)
        self.assertEqual(self.app("Resident Evil 3").kind, "shortcut")  # its .url file, not steam:// raw

    def test_rebuild_writes_a_cache_that_loads_back(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(apps, "CACHE_PATH", Path(tmp) / "apps_index.json"), \
             mock.patch.object(apps, "_query_start_apps", return_value=START_APPS):
            apps._install([], False)
            apps._rebuild()
            data = json.loads(apps.CACHE_PATH.read_text(encoding="utf-8"))
            loaded, complete = apps._load_cache(apps.CACHE_PATH)
        self.assertTrue(apps._current.complete and complete and data["complete"])
        self.assertEqual(loaded, self.apps)
        self.assertIn("Opening WhatsApp.", apps.PRERENDER)

    def test_a_failed_store_scan_never_replaces_a_full_index(self):
        full = apps._current
        with mock.patch.object(apps, "_query_start_apps", side_effect=OSError("powershell missing")), \
             mock.patch.object(apps, "_save_cache") as save, self.assertLogs("aj.tools.apps", "WARNING"):
            apps._rebuild()
        self.assertIs(apps._current, full)
        save.assert_not_called()

    def test_bad_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "apps_index.json")
            path.write_text("{not json", encoding="utf-8")
            with self.assertLogs("aj.tools.apps", "WARNING"):
                self.assertIsNone(apps._load_cache(path))
            path.write_text(json.dumps({"version": 99, "apps": []}), encoding="utf-8")
            self.assertIsNone(apps._load_cache(path))
            self.assertIsNone(apps._load_cache(Path(tmp, "missing.json")))

    def test_get_start_apps_is_one_safe_powershell_call(self):
        out = json.dumps({"Name": "Spotify", "AppID": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"}).encode()
        done = subprocess.CompletedProcess([], 0, stdout=out, stderr=b"")
        with mock.patch.object(apps.subprocess, "run", return_value=done) as run:
            self.assertEqual(apps._query_start_apps(), [("Spotify", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify")])
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertTrue(args[0][0].lower().endswith("powershell.exe"))
        self.assertIn("Get-StartApps", args[0][-1])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["creationflags"], 0x08000000)

    def test_prerender_lines_follow_the_common_apps(self):
        self.assertEqual(apps.PRERENDER[:4], ["Opening WhatsApp.", "Opening Google Chrome.", "Opening Spotify.",
                                              "Opening Discord."])
        self.assertLessEqual(len(apps.PRERENDER), 15)
        self.assertNotIn("Opening Microsoft Edge.", apps.PRERENDER)  # not installed here


class ResolveTests(AppsCase):
    def test_names_nicknames_and_shortened_names(self):
        for said, expected in [
            ("WhatsApp", "WhatsApp"), ("whats app", "WhatsApp"), ("chrome", "Google Chrome"),
            ("vs code", "Visual Studio Code"), ("code", "Visual Studio Code"), ("teams", "Microsoft Teams"),
            ("magnify", "Magnifier"), ("the steam app", "Steam"), ("python 3.13", "Python 3.13 (64-bit)"),
            ("explorer", "File Explorer"), ("vlc", "VLC media player"), ("Resident Evil 3", "Resident Evil 3"),
        ]:
            with self.subTest(said=said):
                self.assertEqual(apps.resolve(said).name, expected)
                self.assertEqual(apps.resolve(said, strict=True).name, expected)

    def test_near_spellings(self):
        self.assertEqual(apps.resolve("spotfy", strict=True).name, "Spotify")
        self.assertEqual(apps.resolve("discort").name, "Discord")
        self.assertIsNone(apps.resolve("discort", strict=True))  # the fast path only takes near-exact names

    def test_no_loose_matching(self):
        # "steem" was here until 2026-10-23: it sounds just like Steam, so it now is (SoundTests).
        for said in ["youtube music", "gmail", "note", "spot", "a jar", "aj", "uninstall node js",
                     "google", "edge", "c"]:
            with self.subTest(said=said):
                self.assertIsNone(apps.resolve(said))

    def test_teams_is_never_steam(self):
        apps._install([a for a in self.apps if a.name != "Microsoft Teams"], True)
        self.assertIsNone(apps.resolve("teams"))

    def test_ambiguous_names_ask(self):
        apps._install([*self.apps, App("Snapchat", "appid", "Snap.Chat!App"), App("Snapshot", "appid", "Snap.Shot!App")],
                      True)
        self.assertIsNone(apps.resolve("snapchot"))
        with self.assertRaisesRegex(ToolError, r"Did you mean Snap(chat|shot) or Snap(chat|shot)\?"):
            apps.open_app("snapchot")

    def test_unknown_app(self):
        with self.assertRaisesRegex(ToolError, r"^I can't find an app called Fortnite\.$"):
            apps.open_app("Fortnite")
        with self.assertRaisesRegex(ToolError, "Which app"):
            apps.open_app("  ")

    def test_a_miss_refreshes_the_index_now_and_then(self):
        with mock.patch.object(apps, "_last_refresh", float("-inf")):
            self.assertIsNone(apps.resolve("brand new app"))
        self.refresh.assert_called_once()
        self.refresh.reset_mock()
        with mock.patch.object(apps, "_last_refresh", time.monotonic()):
            apps.resolve("brand new app")
        self.refresh.assert_not_called()


class WindowMatchTests(AppsCase):
    def window(self, exes, aumids=(), title="x") -> Window:
        return Window(1, title, frozenset(exes), frozenset(aumids))

    def belongs(self, window: Window, name: str) -> bool:
        return apps._belongs(window, self.app(name), self.index.claimed)

    def test_a_chrome_web_app_is_not_chrome(self):
        pwa = self.window({"chrome.exe"}, {"chrome._crx_agimnkijcaahngcdmfeangaknmldooml"})
        self.assertFalse(self.belongs(pwa, "Google Chrome"))
        self.assertTrue(self.belongs(pwa, "YouTube"))
        self.assertTrue(self.belongs(self.window({"chrome.exe"}, {"chrome"}), "Google Chrome"))
        self.assertTrue(self.belongs(self.window({"chrome.exe"}), "Google Chrome"))

    def test_store_apps_by_app_id(self):
        frame = self.window({"applicationframehost.exe", "systemsettings.exe"},
                            {"windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel"})
        self.assertTrue(self.belongs(frame, "Settings"))
        self.assertFalse(self.belongs(frame, "Spotify"))
        self.assertTrue(self.belongs(self.window({"spotify.exe"}, {"spotifyab.spotifymusic_zpdnekdrzrea0!spotify"}),
                                     "Spotify"))

    def test_by_process_name(self):
        self.assertTrue(self.belongs(self.window({"explorer.exe"}), "File Explorer"))
        self.assertFalse(self.belongs(self.window({"notepad.exe"}), "File Explorer"))

    def test_find_windows_keeps_the_stack_order(self):
        windows = [Window(5, "a", frozenset({"chrome.exe"}), frozenset()), Window(6, "b", frozenset({"vlc.exe"}), frozenset()),
                   Window(7, "c", frozenset({"chrome.exe"}), frozenset())]
        self.assertEqual([w.hwnd for w in apps.find_windows(self.app("Google Chrome"), windows)], [5, 7])


class OpenAppTests(AppsCase):
    def setUp(self):
        super().setUp()
        self.background = self._patch("aj.tools.apps._launch_in_background")

    def test_already_open_is_brought_to_the_front(self):
        self.windows.return_value = [Window(7, "Spotify Premium", frozenset({"spotify.exe"}), frozenset())]
        self.focus.side_effect, self.focus.return_value = None, True
        self.assertEqual(apps.open_app("spotify"), "Switching to Spotify.")
        self.focus.assert_called_once_with(7)
        self.background.assert_not_called()

    def test_focus_refused_is_said_plainly(self):
        self.windows.return_value = [Window(7, "Spotify", frozenset({"spotify.exe"}), frozenset())]
        self.focus.side_effect, self.focus.return_value = None, False
        self.assertIn("wouldn't let me bring it to the front", apps.open_app("spotify"))
        self.background.assert_not_called()

    def test_not_open_launches_in_the_background_and_answers_at_once(self):
        self.windows.return_value = [Window(3, "Notes", frozenset({"notepad.exe"}), frozenset())]
        t0 = time.perf_counter()
        reply = apps.open_app("whatsapp")
        self.assertLess(time.perf_counter() - t0, 0.05)
        self.assertEqual(reply, "Opening WhatsApp.")
        self.background.assert_called_once_with(self.app("WhatsApp"), {3})
        self.focus.assert_not_called()

    def test_stale_or_tampered_entries_are_refused(self):
        apps._install([*self.apps, App("Gone", "shortcut", str(self.root / "Gone.lnk")),
                       App("Elsewhere", "shortcut", r"C:\Windows\System32\cmd.exe"),
                       App("Sneaky", "appid", 'x" /c calc'), App("Switch", "appid", "/select,C:\\")], True)
        with self.assertRaisesRegex(ToolError, "uninstalled"):
            apps.open_app("Gone")
        self.refresh.assert_called()
        with self.assertRaisesRegex(ToolError, "isn't in the Start menu"):
            apps.open_app("Elsewhere")
        for name in ("Sneaky", "Switch"):
            with self.assertRaisesRegex(ToolError, "app ID"):
                apps.open_app(name)
        self.background.assert_not_called()

    def test_registered_levels(self):
        self.assertEqual(registry.get("open_app").level, Level.SAFE)
        self.assertEqual(registry.get("switch_to_app").level, Level.SAFE)
        self.assertEqual(registry.get("close_app").level, Level.CONFIRM)


class LaunchTests(AppsCase):
    def setUp(self):
        super().setUp()
        self._patch("aj.tools.apps.VERIFY_SECS", 0.2)
        self._patch("aj.tools.apps._POLL_SECS", 0.01)
        self.running = self._patch("aj.tools.apps._is_running", return_value=False)
        self.verdicts = self._patch("aj.tools.apps.on_verify")

    def test_shortcuts_go_through_startfile_and_store_apps_through_explorer(self):
        self.startfile.side_effect = None
        apps._launch(self.app("Steam"))
        self.startfile.assert_called_once_with(self.app("Steam").target)
        self.popen.side_effect = None
        apps._launch(self.app("WhatsApp"))
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0][1:], ["shell:AppsFolder\\5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"])
        self.assertTrue(args[0][0].lower().endswith("explorer.exe"))
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["creationflags"], 0x08000000)

    def run_launch(self, name: str, windows_over_time: list[list[Window]]):
        self.windows.side_effect = windows_over_time + [windows_over_time[-1]] * 100
        with mock.patch.object(apps, "_launch", return_value=None) as launch:
            apps._launch_and_verify(self.app(name), {1})
        launch.assert_called_once()

    def test_a_window_appearing_is_success(self):
        spotify = Window(9, "Spotify", frozenset({"spotify.exe"}), frozenset())
        self.run_launch("Spotify", [[], [], [spotify]])
        self.verdicts.assert_called_once_with("Spotify", True)
        self.refresh.assert_not_called()

    def test_a_renamed_app_is_checked_again_with_a_fresh_index(self):
        # Live (2026-10-18): WhatsApp had updated, its process was renamed, the old index entry
        # didn't recognise the new window, and a launch that worked was reported as failed.
        from dataclasses import replace
        renamed = replace(self.app("WhatsApp"), exes=("whatsapp.root.exe",), aumid="")
        window = Window(9, "Chats", frozenset({"whatsapp.root.exe"}), frozenset())

        def rebuild():
            apps._install([renamed if a.name == "WhatsApp" else a for a in self.apps], True)
            return mock.Mock()

        self.refresh.side_effect = rebuild
        self.run_launch("WhatsApp", [[window]])
        self.verdicts.assert_called_once_with("WhatsApp", True)

    def test_a_new_window_with_its_name_counts(self):
        self.run_launch("Resident Evil 3", [[], [Window(9, "Resident Evil 3", frozenset({"re3.exe"}), frozenset())]])
        self.verdicts.assert_called_once_with("Resident Evil 3", True)

    def test_an_old_window_with_its_name_does_not(self):
        old = Window(1, "Spotify - lyrics in the browser", frozenset({"chrome.exe"}), frozenset())
        with self.assertLogs("aj.tools.apps", "WARNING"):
            self.run_launch("Spotify", [[old]])
        self.verdicts.assert_called_once_with("Spotify", False)
        self.refresh.assert_called_once()

    def test_a_process_without_a_window_counts_at_the_deadline(self):
        self.running.return_value = True
        self.run_launch("Discord", [[]])
        self.verdicts.assert_called_once_with("Discord", True)

    def test_nothing_appearing_is_reported(self):
        with self.assertLogs("aj.tools.apps", "WARNING"):
            self.run_launch("WhatsApp", [[]])
        self.verdicts.assert_called_once_with("WhatsApp", False)

    def test_unrecognisable_apps_are_not_called_failures(self):
        self.run_launch("Resident Evil 3", [[]])  # steam:// game: nothing to look for
        self.verdicts.assert_called_once_with("Resident Evil 3", True)

    def test_windows_refusing_the_launch_is_reported(self):
        with mock.patch.object(apps, "_launch", side_effect=OSError("no such file")), \
             self.assertLogs("aj.tools.apps", "WARNING"):
            apps._launch_and_verify(self.app("Steam"), set())
        self.verdicts.assert_called_once_with("Steam", False)

    def test_a_broken_hook_is_logged_not_raised(self):
        self.verdicts.side_effect = RuntimeError("speaker unplugged")
        spotify = Window(9, "Spotify", frozenset({"spotify.exe"}), frozenset())
        with self.assertLogs("aj.tools.apps", "ERROR"):
            self.run_launch("Spotify", [[spotify]])


class SwitchAndCloseTests(AppsCase):
    def setUp(self):
        super().setUp()
        self._patch("aj.tools.apps._CLOSE_WAIT", 0.2)
        self.chrome = [Window(4, "Inbox", frozenset({"chrome.exe"}), frozenset()),
                       Window(8, "News", frozenset({"chrome.exe"}), frozenset())]

    def test_switch(self):
        self.windows.return_value = self.chrome
        self.focus.side_effect, self.focus.return_value = None, True
        self.assertEqual(apps.switch_to_app("chrome"), "Switching to Google Chrome.")
        self.focus.assert_called_once_with(4)

    def test_switch_to_an_app_that_isnt_open(self):
        with self.assertRaisesRegex(ToolError, "Spotify isn't open"):
            apps.switch_to_app("spotify")

    def test_close_asks_every_window_to_close(self):
        self.windows.return_value = self.chrome
        self.post_close.side_effect, self.post_close.return_value = None, True
        with mock.patch.object(apps, "_still_shown", return_value=False):
            self.assertEqual(apps.close_app("chrome"), "Google Chrome has been closed.")
        self.assertEqual([c.args[0] for c in self.post_close.call_args_list], [4, 8])

    def test_close_says_when_the_app_holds_on(self):
        self.windows.return_value = self.chrome
        self.post_close.side_effect, self.post_close.return_value = None, True
        with mock.patch.object(apps, "_still_shown", return_value=True):
            self.assertIn("still open", apps.close_app("chrome"))

    def test_close_refused_by_an_admin_window(self):
        self.windows.return_value = self.chrome
        self.post_close.side_effect, self.post_close.return_value = None, False
        with mock.patch.object(apps.ctypes, "get_last_error", return_value=5):
            with self.assertRaisesRegex(ToolError, "administrator"):
                apps.close_app("chrome")

    def test_close_an_app_that_isnt_open(self):
        with self.assertRaisesRegex(ToolError, "Discord isn't open"):
            apps.close_app("discord")
        self.post_close.assert_not_called()


class IntentTests(AppsCase):
    def setUp(self):
        super().setUp()
        saved = intents._rules
        self.addCleanup(setattr, intents, "_rules", saved)
        self.accepted = intents.collect([apps, web])  # tools.MODULES order: apps before web

    def test_every_rule_compiles_and_names_a_real_tool(self):
        self.assertEqual(self.accepted, len(apps.INTENTS) + len(web.INTENTS))

    def test_commands_it_claims(self):
        for said, expected in [
            ("Open WhatsApp.", ("open_app", {"name": "WhatsApp"})),
            ("Hey Computer, launch Spotify please", ("open_app", {"name": "Spotify"})),
            ("start up steam", ("open_app", {"name": "Steam"})),
            ("fire up vs code", ("open_app", {"name": "Visual Studio Code"})),
            ("open the steam app", ("open_app", {"name": "Steam"})),
            ("open spotfy", ("open_app", {"name": "Spotify"})),
            ("can you open file explorer for me", ("open_app", {"name": "File Explorer"})),
            ("close chrome", ("close_app", {"name": "Google Chrome"})),
            ("Quit Discord.", ("close_app", {"name": "Discord"})),
            ("switch to discord", ("switch_to_app", {"name": "Discord"})),
            ("switch back to chrome", ("switch_to_app", {"name": "Google Chrome"})),
            ("focus on spotify", ("switch_to_app", {"name": "Spotify"})),
        ]:
            with self.subTest(said=said):
                self.assertEqual(intents.match(said), expected)

    def test_unknown_names_are_left_for_web_and_the_model(self):
        # The fixture has YouTube installed as a Chrome app (as on the user's laptop), so "open
        # youtube" opens that app; "go to youtube" claims only open apps, so it falls to web.py.
        self.assertEqual(intents.match("open youtube"), ("open_app", {"name": "YouTube"}))
        self.assertEqual(intents.match("go to youtube"), ("open_website", {"url_or_site": "youtube"}))
        self.assertEqual(intents.match("open gmail"), ("open_website", {"url_or_site": "gmail"}))  # a site, not an app
        # "open discort" was here until 2026-10-23: it sounds just like Discord, so it now is (SoundTests).
        for said in ["open a jar", "start a timer", "close the door", "close discort", "open aj",
                     "shut down", "how do I open a jar", "open chrome and search for cats", "close the window",
                     "start", "open"]:
            with self.subTest(said=said):
                self.assertIsNone(intents.match(said))

    def test_go_to_only_claims_open_apps(self):
        self.assertIsNone(intents.match("go to spotify"))
        self.windows.return_value = [Window(7, "Spotify", frozenset({"spotify.exe"}), frozenset())]
        self.assertEqual(intents.match("go to spotify"), ("switch_to_app", {"name": "Spotify"}))

    def test_a_command_after_other_words(self):
        # The user's session of 2026-10-23: each of these went to the model, which refused.
        for said, expected in [
            ("Actually, you know what? I changed my mind. Open settings.", ("open_app", {"name": "Settings"})),
            ("Jarvis open chrome for me.", ("open_app", {"name": "Google Chrome"})),
            ("All right, open Spotify.", ("open_app", {"name": "Spotify"})),
            ("No no open whatsapp, whatsapp.", ("open_app", {"name": "WhatsApp"})),
            ("What apps can you open? List them all out.", ("list_apps", {})),
            ("What else apps can you access?", ("list_apps", {})),
            ("Which apps can you open?", ("list_apps", {})),
            ("List all the apps.", ("list_apps", {})),
            ("What apps do I have installed?", ("list_apps", {})),
            ("Bring up the Spotify thing please.", ("open_app", {"name": "Spotify"})),
        ]:
            with self.subTest(said=said):
                self.assertEqual(intents.match(said), expected)

    def test_a_command_taken_back_is_not_run(self):
        for said in ["Open Chrome. No, don't.", "I don't want you to open Chrome.", "Don't open Spotify.",
                     "Open Spotify? No."]:
            with self.subTest(said=said):
                self.assertIsNone(intents.match(said))

    def test_patterns_are_plain_strings(self):
        for pattern, _, build in apps.INTENTS:
            self.assertIsInstance(pattern, str)
            re.compile(pattern)
            self.assertTrue(callable(build))


class SoundTests(AppsCase):
    """Misheard names (the user's session of 2026-10-23): "open cloud" for Claude, "open wood" for Word."""

    def setUp(self):
        super().setUp()
        saved = intents._rules
        self.addCleanup(setattr, intents, "_rules", saved)
        intents.collect([apps, web])
        apps._install([*self.apps, App("Claude", "appid", "Claude_pzs8sxrjxfjjc!Claude", aumid="Claude_pzs8sxrjxfjjc!Claude"),
                       App("Word", "appid", "Microsoft.Office.WINWORD.EXE.15", exes=("winword.exe",)),
                       App("Mail", "appid", "microsoft.windowscommunicationsapps_8wekyb3d8bbwe!mail")], True)

    def test_how_names_sound(self):
        for a, b in [("Claude", "cloud"), ("Word", "wood"), ("Steam", "steem"), ("WhatsApp", "watsap"),
                     ("Discord", "discort"), ("Weather", "whether")]:
            with self.subTest(a=a, b=b):
                self.assertEqual(apps.sound_of(a), apps.sound_of(b))
        for a, b in [("Word", "world"), ("Teams", "Steam"), ("YouTube", "type"), ("Code", "Claude")]:
            with self.subTest(a=a, b=b):
                self.assertNotEqual(apps.sound_of(a), apps.sound_of(b))

    def test_a_misheard_name_is_the_one_app_it_sounds_like(self):
        for said, expected in [("cloud", "Claude"), ("wood", "Word"), ("steem", "Steam"), ("discard", "Discord"),
                               ("watsap", "WhatsApp")]:
            with self.subTest(said=said):
                self.assertEqual(apps.resolve(said).name, expected)
        self.assertIsNone(apps.resolve("cloud", strict=True))  # the fast path only after "open"

    def test_words_that_only_roughly_sound_like_an_app_are_nothing(self):
        # Measured on the user's 147 apps: "what" sounds like Word and "court" like (VS) Code.
        for said in ["what", "court", "cat", "good", "door", "timer", "photo"]:
            with self.subTest(said=said):
                self.assertIsNone(apps.resolve(said))

    def test_the_fast_path_takes_a_sound_alike_after_open_only(self):
        for said, expected in [("Open cloud.", "Claude"), ("No open cloud.", "Claude"), ("Open wood.", "Word"),
                               ("No no open word, word.", "Word")]:
            with self.subTest(said=said):
                self.assertEqual(intents.match(said), ("open_app", {"name": expected}))
        for said in ["But what?", "close wood", "switch to cloud", "open a jar"]:
            with self.subTest(said=said):
                self.assertIsNone(intents.match(said))

    def test_list_apps(self):
        said = apps.list_apps()
        self.assertTrue(said.startswith(f"{len(apps._current.apps)} apps are installed, and I can open any of them, "
                                        "such as WhatsApp, Google Chrome, Spotify"), said)
        self.assertTrue(said.endswith("Name one and I'll open it."))
        self.assertEqual(apps.list_apps("visual"), "Installed: Visual Studio Code.")
        self.assertEqual(apps.list_apps("cloud"), "Installed: Claude.")
        self.assertEqual(apps.list_apps("photoshop"), "No installed app is called photoshop.")
        self.assertIsNotNone(registry.get("list_apps"))
        self.assertEqual(registry.get("list_apps").level, Level.SAFE)


class RealLaptopTests(unittest.TestCase):
    """Read-only: list what is open on this laptop. Nothing is started, focused or closed."""

    def test_lists_windows_but_never_its_own(self):
        windows = apps.list_windows()
        self.assertTrue(all(isinstance(w.hwnd, int) and w.title for w in windows))
        self.assertEqual([w for w in windows if apps._pid(w.hwnd) == os.getpid()], [])

    def test_lists_processes(self):
        names = {exe for _, exe in apps._running_processes()}
        self.assertIn(apps._process_info(os.getpid())[0], names)


if __name__ == "__main__":
    unittest.main()
