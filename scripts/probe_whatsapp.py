"""Watch WhatsApp Desktop load through UI Automation, to learn what tools/whatsapp.py should wait for.

The user (2026-10-22): WhatsApp has a "loading chats" period the first time it opens, 0 to 100%,
that can take a while on his Wi-Fi, and the assistant must act only after it. SivaSafe judged
"ready" by sending screenshots to Gemini; this reads the window's accessibility tree locally.

    .venv\\Scripts\\python.exe scripts\\probe_whatsapp.py [--search Appa] [--no-open] [--keep-open]

1. If WhatsApp is in front, the user is using it: only its loaded state is read, no keys.
   If it runs in the background, it is closed first, so the load can be watched from the start.
2. It is launched, and its web page is sampled every 0.3 s; each change is printed with the time.
3. Once loaded, through tools/whatsapp.py's own steps: a nonsense search shows what "no results"
   looks like and Escape clears it; then the name is searched, the results are described, Enter
   is pressed ONCE (only while the focus is verified to be in the search box, holding the name),
   and the open chat's name and the focused element are read. Nothing is typed in a chat.
4. WhatsApp is closed again if it was closed before (it keeps running in the background after
   its window closes, so its process is ended), and the window that was in front comes back.

Found with it on 2026-10-22 (six launches): see tools/whatsapp.py's docstring.

Privacy: chat previews and messages are never printed. Names are shown only for edit boxes,
short button/heading/tab names without digits, and short interface phrases ("Loading...",
"No chats ... found"). Everything else is shown as its length and whether it holds the searched
name. Nothing is saved. Keys are typed only after the focus is checked to be WhatsApp's search
box, never the message box.
"""
import argparse
import collections
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Before aj reads it: the app index it builds goes to a temporary folder, never the user's
# %APPDATA%\AJ (the review of 2026-10-22).
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="aj-probe-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aj.core import keyboard, uia  # noqa: E402
from aj.tools import apps, whatsapp  # noqa: E402

SAMPLE = 0.3
LOAD_LIMIT = 180.0
_LOAD_WORDS = re.compile(r"load|search|%|connect|sync|wait|start|offline|retry|progress|updat|no (?:chats|results)"
                         r"|found|encrypt|keep your phone|open whatsapp|log ?in|qr|link", re.IGNORECASE)
_SHOWN_TYPES = {"Edit", "ProgressBar", "Header", "HeaderItem", "Button", "TabItem", "MenuItem"}
_SHOWN_ROLES = {"heading", "progressbar", "searchbox", "textbox", "button", "tab", "dialog", "alert", "status"}
_ROW_ROLES = {"row", "gridcell", "listitem", "option", "cell", "treeitem"}
# Short interface texts that may be printed whole; anything else could be a message.
_UI_PHRASES = re.compile(r"(?:loading[a-z .]*|[\d.]+ ?%|no (?:chats|contacts|messages|results)[a-z ,]*(?: found)?"
                         r"|connecting[a-z .]*|syncing[a-z .]*|starting[a-z .]*|updating[a-z .]*"
                         r"|end-to-end encrypted|whatsapp|search[a-z ]{0,30})", re.IGNORECASE)
_SAFE_WORDS = {"chats", "contacts", "messages", "groups", "communities", "unread", "favourites", "favorites",
               "all", "archived", "status", "channels", "calls", "settings", "search"}


def _shown(node: uia.Node, query: str = "") -> str:
    """The node's name if it may be printed, else its length and whether it holds the query.
    A web app's button or row is named after its text, which can be a chat preview, so names
    with digits (times, dates) or over 40 characters are masked even for buttons."""
    name = node.name.strip()
    if not name:
        return "''"
    if node.aria_role in _ROW_ROLES or node.control_type in {"ListItem", "DataItem"}:
        return _masked(name, query)
    if name.lower() in _SAFE_WORDS or _UI_PHRASES.fullmatch(name):
        return repr(name)
    if node.control_type == "Edit" or node.aria_role in {"searchbox", "textbox"}:
        return repr(name[:60])  # an edit box's label ("Search input textbox"), never its text
    short = len(name) <= 40 and not any(c.isdigit() for c in name)
    if short and (node.control_type in _SHOWN_TYPES or node.aria_role in _SHOWN_ROLES):
        return repr(name)
    return _masked(name, query)


def _masked(name: str, query: str) -> str:
    hit = f", has {query!r}" if query and query.lower() in name.lower() else ""
    exact = " (exactly the query)" if query and name.lower() == query.lower() else ""
    return f"<{len(name)} chars{hit}{exact}>"


def _describe(node: uia.Node, query: str = "") -> str:
    head = node.control_type or "?"
    if node.aria_role:
        head += f"[{node.aria_role}]"
    bits = [head]
    if node.automation_id:
        bits.append(f"aid={re.sub(r'[0-9]{6,}', '<num>', node.automation_id)[:60]!r}")
    if node.class_name:
        bits.append(f"cls={node.class_name[:40]!r}")
    bits.append(f"name={_shown(node, query)}")
    if not node.enabled:
        bits.append("DISABLED")
    if node.focused:
        bits.append("FOCUSED")
    if node.range_value is not None and node.control_type in {"ProgressBar", "Slider"}:
        bits.append(f"value={node.range_value:g}")
    if node.has_value:
        bits.append("value-pattern" + ("(ro)" if node.read_only else "(rw)"))
    if node.has_text:
        bits.append("text-pattern")
    if node.offscreen:
        bits.append("offscreen")
    return " ".join(bits)


def _interesting(node: uia.Node) -> bool:
    if node.control_type in {"Edit", "ProgressBar"} or node.aria_role in {"progressbar", "searchbox", "textbox",
                                                                         "heading", "alert", "status", "dialog"}:
        return True
    name = node.name.strip()
    return (bool(name) and len(name) <= 60 and bool(_LOAD_WORDS.search(name))
            and node.aria_role not in _ROW_ROLES and node.control_type not in {"ListItem", "DataItem"})


def _summary(nodes: list[uia.Node]) -> tuple:
    counts = collections.Counter(n.control_type for n in nodes)
    roles = collections.Counter(n.aria_role for n in nodes if n.aria_role in _ROW_ROLES)
    shown = tuple(_describe(n) for n in nodes if _interesting(n))
    return (len(nodes), tuple(sorted(counts.items())), tuple(sorted(roles.items())), shown)


def _print_summary(t: float, summary: tuple) -> None:
    total, counts, roles, shown = summary
    print(f"\n[{t:6.1f}s] {total} elements; types {dict(counts)}; row roles {dict(roles)}")
    for line in shown:
        print("    ", line)


def _whatsapp() -> apps.App:
    app = apps.resolve("whatsapp", strict=True)
    if app is None:
        sys.exit("WhatsApp isn't in the app index.")
    return app


def _running_pids(app: apps.App) -> list[int]:
    return [pid for pid, exe in apps._running_processes() if exe in app.exes]


def _close(app: apps.App) -> None:
    for w in apps.find_windows(app):
        apps._post_close(w.hwnd)
    deadline = time.monotonic() + 4
    while _running_pids(app) and time.monotonic() < deadline:
        time.sleep(0.25)
    if _running_pids(app):
        print("WhatsApp still running after its window closed: ending its process")
        for exe in sorted({exe for _, exe in apps._running_processes() if exe in app.exes}):
            subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True)
        time.sleep(1.0)
    print("WhatsApp closed:", not _running_pids(app))


def _search_box(nodes: list[uia.Node]) -> uia.Node | None:
    edits = [n for n in nodes if n.control_type == "Edit" or n.aria_role in {"searchbox", "textbox"}]
    named = [n for n in edits if "search" in (n.name + n.automation_id).lower() or n.aria_role == "searchbox"]
    return named[0] if named else None


def _same(a: uia.Node | None, b: uia.Node | None) -> bool:
    """The same element: by UI Automation's runtime id (an edit box's name can change as it fills)."""
    if not (a and b):
        return False
    if a.runtime_id and b.runtime_id:
        return a.runtime_id == b.runtime_id
    return (a.control_type, a.automation_id, a.name, a.aria_role) == (b.control_type, b.automation_id, b.name,
                                                                      b.aria_role)


def _web_root(hwnd: int) -> uia.Node | None:
    """WhatsApp's web page. Scanning from the window itself finds the page twice over (WinUI and
    Chromium both list it, and FindAll multiplies it: 25,804 elements for 332, live 2026-10-22)."""
    return uia.find(hwnd, automation_id="RootWebArea") if hwnd else None


def _scan(hwnd: int) -> list[uia.Node] | None:
    root = _web_root(hwnd)
    return uia.scan(root) if root else None


def _watch_load(hwnd_of, started: float) -> None:
    last, stable = None, 0
    while time.monotonic() - started < LOAD_LIMIT:
        hwnd = hwnd_of()  # every time: a splash window may give way to the main one
        nodes = _scan(hwnd)
        t = time.monotonic() - started
        if nodes is None:
            summary = ("no window yet" if not hwnd else "window, no web page yet",)
        else:
            summary = _summary(nodes)
        if summary != last:
            if len(summary) == 1:
                print(f"\n[{t:6.1f}s] {summary[0]}")
            else:
                _print_summary(t, summary)
            last, stable = summary, 0
        else:
            stable += 1
        box = _search_box(nodes or [])
        if box and box.enabled and stable >= 10 and not any(n.control_type == "ProgressBar" for n in nodes):
            print(f"\n[{t:6.1f}s] unchanged for {stable} samples with a search box and no progress bar")
            return
        time.sleep(SAMPLE)
    print("gave up waiting for the load")


def _loaded_report(hwnd: int, query: str) -> list[uia.Node]:
    nodes = _scan(hwnd) or []
    print(f"\n=== loaded state: {len(nodes)} elements ===")
    for n in nodes:
        if n.control_type in {"Edit", "ProgressBar", "Header", "HeaderItem", "DataGrid", "List", "Table",
                              "Document"} or n.automation_id or _interesting(n):
            print("    ", _describe(n, query))
    return nodes


def _focus_line(label: str) -> uia.Node | None:
    now = uia.focused_node()
    print(f"     {label}:", _describe(now) if now else None)
    return now


def _bounds(n: uia.Node) -> str:
    return f"bounds={tuple(round(v) for v in n.bounds) if n.bounds else None}"


def _diff(before: list[uia.Node], after: list[uia.Node], query: str, limit: int = 60) -> None:
    old = collections.Counter(_describe(n, query) for n in before)
    shown = 0
    for n in after:
        d = _describe(n, query)
        if old[d] > 0:
            old[d] -= 1
            continue
        print("    ", d, _bounds(n))
        shown += 1
        if shown >= limit:
            print("     ...")
            break


def _probe_keys(hwnd: int, query: str, open_chat: bool) -> None:
    """The search and the Enter, through tools/whatsapp.py's own steps, so this checks the tool.

    What the first runs found (2026-10-22): Ctrl+F did nothing after a launch (the keys were with
    WinUI's frame); UIA SetFocus on the search box moved the page's focus, but keys typed after the
    first SetFocus were dropped and landed after a second; Unicode key events never landed."""
    nodes = _loaded_report(hwnd, query)
    box = whatsapp.search_box(nodes)
    print("\nsearch box:", _describe(box) if box else None, "| state:", whatsapp.screen_state(nodes))
    if box is None:
        return
    for name, enter in (("zqxv probe nothing", False), (query, open_chat)):
        print(f"\n=== searching {name!r} ===")
        before = whatsapp._shape(_scan(hwnd) or [])
        t0 = time.monotonic()
        try:
            whatsapp._type_search(hwnd, box, name)
        except Exception as exc:
            print("typing failed:", exc)
            return
        print(f"  typed and read back in {time.monotonic() - t0:.1f} s; the focus is in",
              (uia.focused_node() or uia.Node()).class_name or "an element with no class name")
        after = whatsapp._results(hwnd, before)
        print(f"  {len(whatsapp.result_rows(after))} result rows; no results: {whatsapp.no_results(after)}")
        for n in after:
            if _interesting(n) or n.control_type in {"DataGrid", "List"}:
                print("    ", _describe(n, name), _bounds(n))
        if whatsapp.no_results(after):
            keyboard.press("escape")
            time.sleep(0.5)
            print("  after Escape, the box is empty:", uia.value(box) == "")
            continue
        if not enter:
            continue
        print("\n=== Enter, once, in the search box ===")
        opened, header = whatsapp._open_top(hwnd, box)
        focus = _focus_line("focus after Enter")
        print(f"  opened: {opened}; header {'equals the query' if header and header.lower() == name.lower() else repr(header) if header is None else f'<{len(header)} chars>'}"
              f"; focused_editable(): {uia.focused_editable()}; is the message box: {whatsapp._is_message_box(focus)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search", default="Appa")
    parser.add_argument("--no-open", action="store_true", help="don't press Enter on the results")
    parser.add_argument("--keep-open", action="store_true", help="leave WhatsApp running at the end")
    args = parser.parse_args()

    app = _whatsapp()
    before_front = keyboard.front_window()
    windows = apps.find_windows(app)
    was_running = bool(_running_pids(app))
    print(f"WhatsApp running: {was_running}; windows: {len(windows)}; "
          f"in front: {bool(windows) and windows[0].hwnd == before_front}")
    if windows and windows[0].hwnd == before_front:
        print("The user has WhatsApp in front: reading its loaded state only, no keys.")
        _loaded_report(windows[0].hwnd, args.search)
        return
    if was_running:
        print("WhatsApp is running in the background: closing it to watch a load from the start")
        _close(app)

    started = time.monotonic()
    apps._launch(app)

    def hwnd_of() -> int:
        mine = apps.find_windows(app)
        return mine[0].hwnd if mine else 0

    try:
        _watch_load(hwnd_of, started)
        hwnd = hwnd_of()
        if hwnd:
            print("window exe:", keyboard.window_exe(hwnd), "| title:", apps._title(hwnd))
            _probe_keys(hwnd, args.search, not args.no_open)
    finally:
        if not args.keep_open and not was_running:
            _close(app)
        if before_front:
            print("restored the window that was in front:", keyboard.focus(before_front))


if __name__ == "__main__":
    main()
