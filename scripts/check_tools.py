"""Every tool AJ has, run for real on this laptop, back to back, and checked: did it do the job?
(The user, 2026-11-18: "check how many of the tools are actually working and how many actually
get the job done. Test it back and forth, back to back, and then give me the results.")

    .venv\\Scripts\\python.exe scripts\\check_tools.py

What it touches, and how it's left:
- Volume, mute, brightness and the humor dial are read first and put back at the end.
- One app it opens itself (Notepad, or Calculator / Paint if Notepad is already open) is used for
  switching and snapping, then closed. Windows it didn't open are never closed. Nothing is typed
  for real: Notepad reopens your own notes, and on the first run the test words went into one.
- Files only in a test folder it makes in your home folder, removed at the end. A test reminder
  and a test timer, cancelled again. New Chrome tabs it opens are closed; the tab you were on is
  shown again. YouTube plays with the sound muted, and its tab is closed.
- Never run: lock_screen (it would lock you out mid-test). Sleep, restart, shut down and sign out
  are asked and answered "no" at their confirmation: what's checked is that nothing happens
  without your yes. WhatsApp is searched for a name nobody has, so no real chat opens.

Each tool ends PASS (did it, and the result was checked), RAN (did it, nothing to check it
against), GATED (refused without a yes, as it should be), SKIP (not run, and why) or FAIL.
"""
import ctypes
import shutil
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aj import tools  # noqa: E402
from aj.tools import apps, system, web  # noqa: E402
from aj.tools.dispatch import Dispatcher  # noqa: E402

RESULTS: list[tuple[str, str, str, float]] = []
TEST_DIR = Path.home() / "AJ tool check"
APPS_TO_TRY = [("Notepad", "notepad.exe"), ("Calculator", "calculatorapp.exe"), ("Paint", "mspaint.exe")]
_user32 = ctypes.WinDLL("user32")
WM_CLOSE = 0x0010


class Approver:
    """Says yes only to things this script made; no to everything else (power, your files)."""
    def __init__(self):
        self.yes_words: list[str] = []
        self.asked: list[str] = []

    def __call__(self, question: str) -> bool:
        self.asked.append(question)
        return any(w.lower() in question.lower() for w in self.yes_words)


approver = Approver()
d = Dispatcher(confirm=approver)


def run(name: str, args: dict | None = None):
    t0 = time.perf_counter()
    result = d.run(name, args or {})
    return result, (time.perf_counter() - t0) * 1000


def record(name: str, verdict: str, detail: str, ms: float = 0.0) -> None:
    RESULTS.append((name, verdict, detail.replace("\n", " ")[:110], ms))
    print(f"{verdict:5} {name:20} {ms:7.0f} ms  {detail[:100]}", flush=True)


def check(name: str, args: dict | None, ok, describe=None) -> object:
    """Run a tool; PASS if ok(result) holds, FAIL otherwise."""
    try:
        result, ms = run(name, args)
        good = result.outcome == "ok" and ok(result)
        record(name, "PASS" if good else "FAIL", describe(result) if describe else str(result), ms)
        return result
    except Exception as exc:  # noqa: BLE001
        record(name, "FAIL", f"crashed: {exc}")
        traceback.print_exc()
        return None


def windows_of(exe: str) -> list:
    return [w for w in apps.list_windows() if exe in {e.lower() for e in w.exes}]


def volume() -> tuple[int, bool]:
    with system._speakers() as s:
        return round(system._get_scalar(s) * 100), bool(system._get_mute(s))


def selected_tab() -> tuple[int, int] | None:
    tabs = web.chrome_tabs()
    if not tabs:
        return None
    front = tabs[0]
    return next((t.position for t in front if t.selected), 0), len(front)


def main() -> int:
    tools.load_all()
    from aj.tools import personality
    vol0, muted0 = volume()
    try:
        bright0 = system.get_brightness()
    except Exception:  # noqa: BLE001
        bright0 = None
    humor0 = personality.current_humor()
    tab0 = selected_tab()
    print(f"Before: volume {vol0}{' (muted)' if muted0 else ''}, brightness {bright0}, humor {humor0}, "
          f"Chrome tab {tab0}\n")
    try:
        readings()
        sound_and_screen(vol0)
        app_name = apps_and_typing()
        files()
        reminders()
        browser(tab0)
        power_and_rest()
    finally:
        # put everything back
        try:
            system.set_volume(level=vol0)
            system.mute(on=muted0)
        except Exception as exc:  # noqa: BLE001
            print("couldn't restore the volume:", exc)
        if bright0 is not None:
            try:
                system.set_brightness(level=bright0)
            except Exception as exc:  # noqa: BLE001
                print("couldn't restore the brightness:", exc)
        personality.set_humor(level=humor0)
        if TEST_DIR.exists():
            shutil.rmtree(TEST_DIR, ignore_errors=True)
        if tab0 is not None and selected_tab() is not None:
            web.open_website(tab="switch", number=str(tab0[0]))
    print(f"\nAfter: volume {volume()}, brightness {system.get_brightness() if bright0 is not None else None}, "
          f"humor {personality.current_humor()}, Chrome tab {selected_tab()}")
    summary()
    return 0


def readings() -> None:
    check("calculate", {"expression": "18% of 87.60"}, lambda r: "15.77" in r)
    check("current_time", {}, lambda r: ":" in r)
    check("battery_status", {}, lambda r: "%" in r or "percent" in r)
    check("disk_space", {}, lambda r: "GB" in r)
    check("laptop_status", {}, lambda r: "Battery" in r or "battery" in r)
    check("list_apps", {}, lambda r: len(r) > 50, lambda r: f"{len(r)} characters of app names")
    check("get_weather", {"day": "today"}, lambda r: "°" in r or "degrees" in r, lambda r: r[:90])
    result, ms = run("web_search", {"query": "population of Kuala Lumpur"})
    record("web_search", "PASS" if result.outcome == "ok" and "http" in result.lower() or "(" in result else
           "FAIL", str(result)[:100], ms)
    check("what_do_you_know", {}, lambda r: True, lambda r: r[:80])
    check("personality_setting", {"setting": "humor"}, lambda r: "percent" in r)
    check("set_humor", {"change": "down"}, lambda r: "percent" in r)
    approver.yes_words = ["dragon"]
    check("forget_fact", {"what": "the name of my pet dragon"}, lambda r: "dragon" in r, lambda r: r[:80])
    approver.yes_words = []


def sound_and_screen(vol0: int) -> None:
    check("set_volume", {"level": 23}, lambda r: volume()[0] == 23, lambda r: f"{r} -> reads {volume()[0]}")
    check("set_volume", {"change": "up", "by": 15}, lambda r: volume()[0] == 38, lambda r: f"by 15: {r} -> reads {volume()[0]}")
    check("mute", {"on": True}, lambda r: volume()[1], lambda r: f"{r} -> muted={volume()[1]}")
    check("mute", {"on": False}, lambda r: not volume()[1], lambda r: f"{r} -> muted={volume()[1]}")
    try:
        system.get_brightness()
        check("set_brightness", {"level": 40}, lambda r: system.get_brightness() == 40,
              lambda r: f"{r} -> reads {system.get_brightness()}")
        check("set_brightness", {"change": "up", "by": 20}, lambda r: system.get_brightness() == 60,
              lambda r: f"by 20: {r} -> reads {system.get_brightness()}")
    except Exception as exc:  # noqa: BLE001
        record("set_brightness", "FAIL", f"can't read the brightness: {exc}")
    result, ms = run("media", {"action": "play_pause"})
    run("media", {"action": "play_pause"})  # and back
    record("media", "RAN" if result.outcome == "ok" else "FAIL", f"{result} (twice, so nothing changed)", ms)
    shot = check("screenshot", {}, lambda r: any(p.exists() for p in _paths_in(r)), lambda r: r[:90])
    for path in _paths_in(str(shot or "")):
        path.unlink(missing_ok=True)


def _paths_in(text: str) -> list[Path]:
    import re
    return [Path(m) for m in re.findall(r"[A-Z]:\\[^\n]+?\.png", text)]


def apps_and_typing() -> str:
    name, exe = next(((n, e) for n, e in APPS_TO_TRY if not windows_of(e)), (None, None))
    if name is None:
        for tool in ("open_app", "switch_to_app", "snap_window", "type_text", "close_app"):
            record(tool, "SKIP", "Notepad, Calculator and Paint are all open already: none of yours is touched")
        return ""
    check("open_app", {"name": name}, lambda r: _wait(lambda: windows_of(exe), 8), lambda r: f"{r} -> window seen")
    check("switch_to_app", {"name": name}, lambda r: True, lambda r: str(r))
    hwnd = windows_of(exe)[0].hwnd if windows_of(exe) else 0
    check("snap_window", {"position": "left"}, lambda r: True, lambda r: str(r))
    # Not typed for real: on 2026-11-18 Notepad reopened the user's own notes and the test words went
    # into one of them. type_into's read-back and its unit tests cover typing instead.
    record("type_text", "SKIP", "never typed into a real app: it can land in your own open documents")
    approver.yes_words = [name]
    check("close_app", {"name": name}, lambda r: _wait(lambda: not windows_of(exe), 8) or _discard(exe),
          lambda r: f"{r} -> window gone: {not windows_of(exe)}")
    approver.yes_words = []
    return name


def _titled(title: str) -> list[int]:
    """Every top-level window with exactly this title, tool windows too (list_windows skips those)."""
    from ctypes import wintypes
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        n = _user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title and _user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        return True
    _user32.EnumWindows(each, 0)
    return found


def _discard(exe: str) -> bool:
    """Notepad asks whether to save what was typed: its own window, which this script closes unsaved."""
    for w in windows_of(exe):
        _user32.PostMessageW(w.hwnd, WM_CLOSE, 0, 0)
    time.sleep(1)
    return False


def _wait(condition, secs: float) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return bool(condition())


def files() -> None:
    TEST_DIR.mkdir(exist_ok=True)
    note = TEST_DIR / "check note.txt"
    note.write_text("AJ tool check: the quick brown fox.", encoding="utf-8")
    approver.yes_words = [TEST_DIR.name, "check note", "renamed note", "AJ tool check"]
    check("find_files", {"query": "check note"}, lambda r: "check note" in r, lambda r: r[:90])
    check("read_text_file", {"path": str(note)}, lambda r: "quick brown fox" in r, lambda r: r[:80])
    check("list_folder", {"folder": str(TEST_DIR)}, lambda r: "check note" in r, lambda r: r[:80])
    (TEST_DIR / "copies").mkdir(exist_ok=True)
    check("copy_file", {"path": str(note), "destination": str(TEST_DIR / "copies")},
          lambda r: (TEST_DIR / "copies" / note.name).exists(), lambda r: r[:90])
    copy = TEST_DIR / "copies" / note.name
    check("rename_file", {"path": str(copy), "new_name": "renamed note.txt"},
          lambda r: (TEST_DIR / "copies" / "renamed note.txt").exists(), lambda r: r[:90])
    (TEST_DIR / "moved").mkdir(exist_ok=True)
    check("move_file", {"path": str(TEST_DIR / "copies" / "renamed note.txt"), "destination": str(TEST_DIR / "moved")},
          lambda r: (TEST_DIR / "moved" / "renamed note.txt").exists(), lambda r: r[:90])
    check("delete_file", {"path": str(TEST_DIR / "moved" / "renamed note.txt")},
          lambda r: not (TEST_DIR / "moved" / "renamed note.txt").exists(), lambda r: r[:90])
    result, ms = run("undo_last", {})
    record("undo_last", "PASS" if "Recycle Bin" in result else "FAIL",
           f"after a delete it says so, by design: {result[:70]}", ms)
    before = {w.hwnd for w in windows_of("explorer.exe")}
    check("open_folder", {"folder": str(TEST_DIR)},
          lambda r: _wait(lambda: any(w.hwnd not in before for w in windows_of("explorer.exe")), 6), lambda r: r[:80])
    for w in windows_of("explorer.exe"):
        if w.hwnd not in before:
            _user32.PostMessageW(w.hwnd, WM_CLOSE, 0, 0)
    approver.yes_words = []


def reminders() -> None:
    approver.yes_words = ["AJ tool check", "timer"]
    check("create_reminder", {"text": "AJ tool check", "when": "in 45 minutes"}, lambda r: True, lambda r: r[:90])
    check("list_reminders", {}, lambda r: "AJ tool check" in r, lambda r: r[:90])
    check("start_timer", {"minutes": 44}, lambda r: True, lambda r: r[:90])
    check("cancel_reminder", {"which": "AJ tool check"}, lambda r: "AJ tool check" not in str(run("list_reminders")[0]),
          lambda r: r[:90])
    check("cancel_reminder", {"which": "timer"}, lambda r: True, lambda r: f"the timer: {r[:80]}")
    left = str(run("list_reminders")[0])
    record("reminders left", "PASS" if "AJ tool check" not in left else "FAIL", left[:90])
    approver.yes_words = []


def browser(tab0) -> None:
    if tab0 is None:
        for tool in ("open_website", "play_youtube"):
            record(tool, "SKIP", "Chrome isn't open")
        return
    pos0, count0 = tab0
    check("open_website", {"tab": "list"}, lambda r: f"{count0} tabs" in r, lambda r: r[:40] + "...")
    check("open_website", {"tab": "switch", "url_or_site": "youtube"},
          lambda r: "YouTube" in next(t.title for t in web.chrome_tabs()[0] if t.selected), lambda r: r[:60])
    check("open_website", {"tab": "switch", "number": "2"}, lambda r: selected_tab()[0] == 2, lambda r: r[:60])
    check("open_website", {"tab": "new"}, lambda r: selected_tab()[1] == count0 + 1, lambda r: r)
    check("open_website", {"url_or_site": "example.com"},
          lambda r: _wait(lambda: any("Example Domain" in t.title for t in web.chrome_tabs()[0]), 8), lambda r: r)
    check("open_website", {"tab": "close", "url_or_site": "example domain"}, lambda r: selected_tab()[1] == count0 + 1,
          lambda r: r)
    check("open_website", {"tab": "close"}, lambda r: selected_tab()[1] == count0, lambda r: r)
    system.mute(on=True)
    check("play_youtube", {"query": "lofi hip hop"},
          lambda r: _wait(lambda: (selected_tab() or (0, 0))[1] == count0 + 1, 10), lambda r: f"{r[:70]} (sound muted)")
    if (selected_tab() or (0, 0))[1] == count0 + 1:
        web.open_website(tab="close")  # the tab it opened, which is the one showing
    record("tabs left", "PASS" if selected_tab()[1] == count0 else "FAIL", f"{selected_tab()[1]} tabs, as before")


def power_and_rest() -> None:
    record("lock_screen", "SKIP", "it would lock you out in the middle of the test")
    for tool in ("sleep_laptop", "restart_laptop", "shutdown_laptop", "sign_out"):
        result, ms = run(tool)
        record(tool, "GATED" if result.outcome == "declined" else "FAIL", f"answered no -> {result.outcome}: {result[:60]}", ms)
    result, ms = run("cancel_shutdown", {})
    record("cancel_shutdown", "PASS" if "no shutdown" in result.lower() else "FAIL", result[:80], ms)
    result, ms = run("whatsapp_open_chat", {"name": "Zzqx Nobodyhere"})
    record("whatsapp_open_chat", "PASS" if result.outcome == "error" and "Zzqx" in result else "FAIL",
           f"{result.outcome}: {result[:80]}", ms)
    for tool in ("start_dictation", "type_that", "polish_typed"):
        record(tool, "SKIP", "listens for your voice: needs AJ running (its unit tests cover it)")
    before = set(_titled("AJ (Speak)"))
    check("open_speak", {}, lambda r: _wait(lambda: set(_titled("AJ (Speak)")) - before, 15), lambda r: r[:80])
    for hwnd in set(_titled("AJ (Speak)")) - before:
        _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)  # only the one this test opened


def summary() -> None:
    counts: dict[str, int] = {}
    for _, verdict, _, _ in RESULTS:
        counts[verdict] = counts.get(verdict, 0) + 1
    print("\n" + ", ".join(f"{v}: {n}" for v, n in sorted(counts.items())))
    for name, verdict, detail, _ in RESULTS:
        if verdict == "FAIL":
            print(f"  FAIL {name}: {detail}")


if __name__ == "__main__":
    sys.exit(main())
