"""Apps by voice: open, close and switch to the programs on this laptop.

Ported from SivaSafe's actions/open_app.py, keeping its nickname knowledge and fixing the rest:
- SivaSafe ran `start {app_name}` through cmd.exe (shell=True), so a model-written name was a
  command line. Here nothing goes through a shell: shortcuts are opened with os.startfile, and
  Store apps with explorer.exe and "shell:AppsFolder\\<AppID>" as one argument.
- SivaSafe matched names by substring (any alias inside the name, or the name inside any alias)
  and, failing that, typed the name into the Start menu. Here a name must be an installed app's
  name, a nickname from _COMMON, or a near spelling (difflib) that no other app is equally near.
  Anything else is "I can't find an app called X", never a guess.
- SivaSafe slept 1.5 s and said "Opened" whether or not anything appeared. Here open_app answers
  at once ("Opening WhatsApp.") while a background thread launches the app and watches for its
  window or process for up to VERIFY_SECS, then reports the verdict through on_verify.

The app index (Start Menu shortcuts plus Get-StartApps, which adds the Store apps) is cached in
%APPDATA%\\AJ\\apps_index.json, so a lookup is a dictionary lookup. The slow part, PowerShell's
Get-StartApps (about 2 s), only ever runs on a background thread.

An app that is already open is brought to the front instead of being started twice. Windows are
told apart by process name and AppUserModelID, so a Chrome web app isn't mistaken for Chrome.
AJ's own Start Menu shortcut is left out of the index: opening it would turn him off.
"""
import ctypes
import difflib
import json
import logging
import os
import re
import struct
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from aj.config import APP_DIR, DATA_DIR
from aj.tools.registry import Level, ToolError, tool

log = logging.getLogger(__name__)

NO_WINDOW = 0x08000000
CACHE_PATH = DATA_DIR / "apps_index.json"
VERIFY_SECS = 8.0     # how long a launched app gets to show a window or a process
REFRESH_WAIT_SECS = 10.0  # for the index rebuild before a launch is called failed
_POLL_SECS = 0.25
_CLOSE_WAIT = 2.0     # how long close_app waits to see the windows go
_REFRESH_GAP = 300.0  # a lookup miss rebuilds the index at most this often
_FIRST_SCAN_WAIT = 3.0  # first run only: how long a miss waits for the Store apps to be read
_CACHE_VERSION = 1

_SYSTEM_ROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
_POWERSHELL = str(_SYSTEM_ROOT / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
_EXPLORER = str(_SYSTEM_ROOT / "explorer.exe")

# AJ sets this to hear how a launch went: on_verify(app name, ok). Called once per launch,
# from a background thread. ok is False when the launch failed or nothing appeared in time.
on_verify: Callable[[str, bool], None] | None = None

# "Opening WhatsApp." for the most common installed apps, word for word what open_app returns,
# so the voice cache can render them ahead of time. Updated in place whenever the index changes.
PRERENDER: list[str] = []


# ---- nicknames -----------------------------------------------------------------------------
# (what people say, the Start Menu names it goes by (first installed one wins), process names
# that mean it is running). Most used first: PRERENDER takes the first 15 that are installed.
# Kept from SivaSafe's alias table, minus its macOS/Linux columns and its web-only entries.

_COMMON: list[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = [
    (("whatsapp",), ("WhatsApp",), ("whatsapp.exe", "whatsapp.root.exe")),
    (("chrome", "google chrome"), ("Google Chrome",), ("chrome.exe",)),
    (("spotify",), ("Spotify",), ("spotify.exe",)),
    (("discord",), ("Discord",), ("discord.exe",)),
    (("steam",), ("Steam",), ("steam.exe", "steamwebhelper.exe")),
    (("vs code", "vscode", "code", "visual studio code"), ("Visual Studio Code",), ("code.exe",)),
    (("file explorer", "explorer", "windows explorer", "files explorer"), ("File Explorer",), ("explorer.exe",)),
    (("settings", "windows settings", "pc settings"), ("Settings",), ("systemsettings.exe",)),
    (("calculator", "calc"), ("Calculator",), ("calculatorapp.exe", "calc.exe")),
    (("notepad",), ("Notepad",), ("notepad.exe",)),
    (("word", "microsoft word", "ms word"), ("Word", "Microsoft Word"), ("winword.exe",)),
    (("excel", "microsoft excel", "ms excel"), ("Excel", "Microsoft Excel"), ("excel.exe",)),
    (("powerpoint", "microsoft powerpoint", "ms powerpoint"), ("PowerPoint", "Microsoft PowerPoint"),
     ("powerpnt.exe",)),
    (("edge", "microsoft edge"), ("Microsoft Edge",), ("msedge.exe",)),
    (("teams", "microsoft teams", "ms teams"), ("Microsoft Teams",), ("ms-teams.exe", "teams.exe")),
    (("outlook", "microsoft outlook"), ("Outlook", "Outlook (new)", "Outlook (classic)"), ("olk.exe", "outlook.exe")),
    (("terminal", "windows terminal"), ("Terminal", "Windows Terminal"), ("windowsterminal.exe",)),
    (("firefox", "mozilla firefox"), ("Firefox", "Mozilla Firefox"), ("firefox.exe",)),
    (("brave", "brave browser"), ("Brave",), ("brave.exe",)),
    (("telegram",), ("Telegram", "Telegram Desktop"), ("telegram.exe",)),
    (("slack",), ("Slack",), ("slack.exe",)),
    (("zoom",), ("Zoom", "Zoom Workplace"), ("zoom.exe",)),
    (("vlc", "vlc player"), ("VLC media player",), ("vlc.exe",)),
    (("onenote",), ("OneNote",), ("onenote.exe",)),
    (("task manager",), ("Task Manager",), ("taskmgr.exe",)),
    (("command prompt", "cmd"), ("Command Prompt",), ()),  # its window belongs to Terminal or conhost
    (("powershell",), ("Windows PowerShell",), ()),
    (("paint", "ms paint"), ("Paint",), ("mspaint.exe",)),
    (("snipping tool",), ("Snipping Tool",), ("snippingtool.exe",)),
    (("camera",), ("Camera",), ("windowscamera.exe",)),
    (("photos",), ("Photos",), ("photos.exe",)),
    (("microsoft store", "app store", "store"), ("Microsoft Store",), ("winstore.app.exe",)),
    (("xbox",), ("Xbox", "XBOX"), ("xboxpcapp.exe",)),
    (("control panel",), ("Control Panel",), ()),
    (("obs", "obs studio"), ("OBS Studio",), ("obs64.exe",)),
    (("davinci", "davinci resolve", "resolve"), ("DaVinci Resolve",), ("resolve.exe",)),
    (("notion",), ("Notion",), ("notion.exe",)),
    (("obsidian",), ("Obsidian",), ("obsidian.exe",)),
    (("github desktop",), ("GitHub Desktop",), ("githubdesktop.exe",)),
    (("clock", "alarms"), ("Clock",), ()),
    (("sticky notes",), ("Sticky Notes",), ()),
    (("media player", "windows media player"), ("Media Player",), ()),
    (("claude", "claude app", "claude ai"), ("Claude",), ("claude.exe",)),
]
_PRERENDER_COUNT = 15


# ---- names ---------------------------------------------------------------------------------

_APOSTROPHES = re.compile(r"['‘’`]")
_NOT_ALNUM = re.compile(r"[\W_]+")
_PARENS = re.compile(r"\s*\([^)]*\)")
_VENDORS = ("microsoft ", "google ", "adobe ", "mozilla ", "windows ")
# "Bring up the Photos thing" (2026-10-23, the honesty bench: the model searched files for it)
_FILLER = re.compile(r"^(?:the|my|a|an)\s+|\s+(?:app|application|program|window|thing)$", re.IGNORECASE)
# Start Menu entries that aren't apps you'd ask for, or that change something when started.
_JUNK = re.compile(r"\b(?:(?:un)?install\w*|remove|reset|repair|readme|read me|manuals?|documentation|docs"
                   r"|release notes|website|faqs?|what is new|whats new|license|changelog)\b", re.IGNORECASE)
_DOC_TYPES = {".txt", ".htm", ".html", ".chm", ".pdf", ".md", ".rtf", ".url", ".ini", ".log", ".xml"}
_WEB_SCHEMES = {"http", "https", "ftp", "file"}

_CUTOFF = 0.84         # a tool call: "spotfy" is Spotify, but "teams" is not Steam (0.80)
_STRICT_CUTOFF = 0.9   # the zero-LLM fast path acts with nobody checking, so only near-exact
_TIE = 0.04            # two apps this close to the best score make the name ambiguous
_MIN_FUZZY = 4         # "cmd", "obs": too short to guess at


def _key(text: str) -> str:
    """'Visual Studio Code' -> 'visualstudiocode', 'whats app' -> 'whatsapp', 'Films & TV' -> 'filmsandtv'."""
    text = _APOSTROPHES.sub("", text.lower()).replace("&", " and ").replace("+", " plus ")
    return _NOT_ALNUM.sub("", text)


def _variants(name: str) -> list[str]:
    """'the steam app' -> ['the steam app', 'steam']: the name as said, then without filler words.
    A name said twice counts once: "open word, word" (live, 2026-10-23) -> 'word'."""
    text = " ".join(str(name).split()).strip(" .,!?\"'")
    out = [text]
    words = _NOT_ALNUM.sub(" ", text.lower()).split()
    half = len(words) // 2
    if half and len(words) % 2 == 0 and words[:half] == words[half:]:
        text = " ".join(words[:half])
        out.append(text)
    while (shorter := _FILLER.sub("", text).strip()) and shorter != text:
        out.append(shorter)
        text = shorter
    return out


def _derived(name: str) -> list[str]:
    """Other names an app answers to: 'Microsoft Teams' -> 'Teams', 'Python 3.13 (64-bit)' -> 'Python 3.13'."""
    base = _PARENS.sub("", name).strip()
    out = [base, base.replace("&", " ")]
    low = base.lower()
    out += [base[len(v):] for v in _VENDORS if low.startswith(v) and len(base) > len(v) + 2]
    if low.endswith(" app") and len(base) > 6:
        out.append(base[:-4])
    return [a for a in out if a.strip()]


def _is_junk(name: str) -> bool:
    return not name.strip() or bool(_JUNK.search(name)) or _key(name) == "aj"


# ---- how a name sounds ---------------------------------------------------------------------
# Speech-to-text writes down what it heard, and live (2026-10-23) it heard "open cloud" for
# Claude and "open wood" for Word: the user doesn't sound the r in "word", as most English
# speakers outside America don't. Spelling can't match those, so a name that matches no app is
# also tried by its sound: consonants only, voiced and voiceless alike (d/t, b/p, g/k, v/f, z/s),
# an r only before a vowel, and one letter for a leading vowel. It counts only when exactly one
# installed app sounds that way ("world" is not Word: its l stays).

_DIGRAPHS = (("chr", "kr"), ("tch", "S"), ("dge", "j"), ("tion", "Sn"), ("sh", "S"), ("ch", "S"), ("ph", "f"),
             ("gh", ""), ("ck", "k"), ("th", "0"), ("wh", "w"), ("qu", "kw"))
_VOICING = str.maketrans({"c": "k", "q": "k", "x": "ks", "z": "s", "v": "f", "d": "t", "b": "p", "g": "k"})
_MIN_SOUND = 2          # consonant sounds: "wt" (Word) is enough, "t" isn't
_SOUND_SPELLING = 0.5   # and a sound-alike must share this much of the spelling (difflib ratio)


def sound_of(name: str) -> str:
    """'Claude' and 'cloud' -> 'klt'; 'Word' and 'wood' -> 'wt'; 'WhatsApp' -> 'wtsp'."""
    text = _key(name)
    if not text:
        return ""
    for spelled, said in _DIGRAPHS:
        text = text.replace(spelled, said)
    text = re.sub(r"c(?=[eiy])", "s", text)
    text = re.sub(r"g(?=[eiy])", "j", text)
    text = re.sub(r"r(?![aeiou])", "", text)       # "word" said "wo'd"
    text = re.sub(r"[wy](?![aeiou])", "", text)    # "window": the last w is part of the vowel
    text = re.sub(r"(?<!^)h", "", text).translate(_VOICING)
    lead = "a" if text[:1] in tuple("aeiou") else "y" if text[:1] == "y" else ""  # "YouTube": a y, not a vowel
    return re.sub(r"(.)\1+", r"\1", lead + re.sub(r"[aeiouy]", "", text[1:] if lead == "y" else text))


# ---- reading shortcuts ---------------------------------------------------------------------

@dataclass(frozen=True)
class Shortcut:
    target: str = ""        # the program or file it starts, when it names one
    arguments: str = ""
    working_dir: str = ""


def _u16(data: bytes, at: int) -> int:
    return struct.unpack_from("<H", data, at)[0]


def _u32(data: bytes, at: int) -> int:
    return struct.unpack_from("<I", data, at)[0]


def _wide_z(data: bytes, start: int, end: int | None = None) -> str:
    end = len(data) if end is None else min(end, len(data))
    for i in range(start, end - 1, 2):
        if data[i] == 0 and data[i + 1] == 0:
            end = i
            break
    return data[start:end].decode("utf-16-le", "replace")


def _ansi_z(data: bytes, start: int) -> str:
    stop = data.find(b"\0", start)
    return data[start:stop if stop >= 0 else len(data)].decode("mbcs", "replace")


def read_shortcut(path: Path) -> Shortcut:
    """A .lnk file's target, arguments and working folder, read straight from its bytes (the
    MS-SHLLINK format): no COM, no shell. Raises ValueError for a file that isn't a shell link."""
    data = Path(path).read_bytes()
    try:
        if len(data) < 0x4C or _u32(data, 0) != 0x4C:
            raise ValueError("not a shell link")
        flags = _u32(data, 0x14)
        at = 0x4C
        if flags & 0x1:                       # HasLinkTargetIDList
            at += 2 + _u16(data, at)
        target = ""
        if flags & 0x2:                       # HasLinkInfo
            size = _u32(data, at)
            info = data[at:at + size]
            if _u32(info, 8) & 0x1:           # VolumeIDAndLocalBasePath
                wide = _u32(info, 4) >= 0x24 and _u32(info, 28)
                if wide:
                    target = _wide_z(info, _u32(info, 28)) + _wide_z(info, _u32(info, 32))
                else:
                    target = _ansi_z(info, _u32(info, 16)) + _ansi_z(info, _u32(info, 24))
            at += size
        texts: dict[int, str] = {}
        for bit in (0x4, 0x8, 0x10, 0x20, 0x40):  # name, relative path, working dir, arguments, icon
            if flags & bit:
                size = _u16(data, at) * (2 if flags & 0x80 else 1)
                raw = data[at + 2:at + 2 + size]
                if len(raw) < size:
                    raise ValueError("truncated string data")
                texts[bit] = raw.decode("utf-16-le" if flags & 0x80 else "mbcs", "replace")
                at += 2 + size
        while at + 8 <= len(data) and not target:  # extra data: the %ProgramFiles%-style target
            size = _u32(data, at)
            if size < 8:
                break
            if _u32(data, at + 4) == 0xA0000001 and size >= 0x314:
                target = os.path.expandvars(_wide_z(data, at + 8 + 260, at + 8 + 260 + 520))
            at += size
    except struct.error as exc:
        raise ValueError(f"truncated shell link ({exc})") from None
    if not target and 0x8 in texts:
        target = os.path.normpath(os.path.join(os.path.dirname(path), texts[0x8]))
    return Shortcut(target, texts.get(0x20, ""), texts.get(0x10, ""))


def _read_url_file(path: Path) -> str:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().lower().startswith("url="):
            return line.split("=", 1)[1].strip()
    return ""


_SQUIRREL = re.compile(r'--processStart(?:AndWait)?\s+"?([^"]+?\.exe)', re.IGNORECASE)


def _exes_of(link: Shortcut) -> tuple[str, ...]:
    """The process a shortcut starts. Squirrel apps (Discord, Slack) start via Update.exe."""
    exe = os.path.basename(link.target).lower()
    if exe == "update.exe" and (m := _SQUIRREL.search(link.arguments)):
        return (os.path.basename(m.group(1)).lower(),)
    return (exe,) if exe.endswith(".exe") else ()


def _points_at_aj(link: Shortcut) -> bool:
    home = str(APP_DIR).lower()
    return any(home in part.lower() for part in (link.target, link.arguments, link.working_dir))


# ---- the index -----------------------------------------------------------------------------

@dataclass(frozen=True)
class App:
    name: str                       # as the Start menu shows it, and as AJ says it
    kind: str                       # "shortcut" (a .lnk or .url file) or "appid" (shell:AppsFolder)
    target: str                     # the shortcut's path, or the AppID
    exes: tuple[str, ...] = ()      # lowercase process names that mean it's running
    aumid: str = ""                 # its AppUserModelID, when it has one
    aliases: tuple[str, ...] = ()   # other names it answers to, e.g. its shortcut's file name


@dataclass(frozen=True)
class _Index:
    apps: tuple[App, ...]
    complete: bool                  # Get-StartApps has been merged in (Store apps are known)
    keys: dict[str, App] = field(default_factory=dict)   # canonical name -> app
    claimed: frozenset[str] = frozenset()                # every app's AppUserModelID, lowercase
    # first letter + sound_of(a name) -> (the name, its app) for each name that sounds so ("cklt": Claude)
    sounds: dict[str, tuple[tuple[str, App], ...]] = field(default_factory=dict)


def _start_menu_roots() -> list[Path]:
    roots = []
    for var in ("APPDATA", "ProgramData"):
        if base := os.environ.get(var):
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return roots


def _from_shortcut(path: Path) -> App | None:
    name = path.stem.strip()
    if _is_junk(name):
        return None
    if path.suffix.lower() == ".url":
        url = _read_url_file(path)
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        # Web links are open_website's job; steam://rungameid/... and the like are apps.
        return None if not scheme or scheme in _WEB_SCHEMES else App(name, "shortcut", str(path))
    try:
        link = read_shortcut(path)
    except (OSError, ValueError) as exc:
        log.debug("can't read shortcut %s: %s", path, exc)
        link = Shortcut()
    if _points_at_aj(link) or os.path.splitext(link.target)[1].lower() in _DOC_TYPES:
        return None
    return App(name, "shortcut", str(path), exes=_exes_of(link))


def scan_shortcuts(roots: Iterable[Path]) -> list[App]:
    """Every launchable .lnk/.url under the Start Menu folders (not the Startup folder)."""
    apps = []
    for root in roots:
        if not root.is_dir():
            continue
        for folder, _, files in os.walk(root):
            if [p.lower() for p in Path(folder).relative_to(root).parts[:1]] == ["startup"]:
                continue  # things that start with Windows, not things you ask for
            for file in sorted(files):
                if file.lower().endswith((".lnk", ".url")) and (app := _from_shortcut(Path(folder, file))):
                    apps.append(app)
    return apps


_START_APPS_SCRIPT = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                      "ConvertTo-Json -Compress -InputObject @(Get-StartApps | Select-Object Name, AppID)")


def _query_start_apps() -> list[tuple[str, str]]:
    """(name, AppID) for everything on the Start menu, Store apps included. Takes about 2 s."""
    done = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", _START_APPS_SCRIPT],
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=30, creationflags=NO_WINDOW)
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
        raise OSError(f"Get-StartApps exited with {done.returncode}: {detail[0] if detail else 'no message'}")
    items = json.loads(done.stdout.decode("utf-8-sig", "replace").strip() or "[]")
    if isinstance(items, dict):
        items = [items]
    return [(str(i.get("Name") or "").strip(), str(i.get("AppID") or "").strip())
            for i in items if isinstance(i, dict)]


def _appid_kind(appid: str) -> str:
    """'url' (steam://..., https://...), 'path' ({FOLDERID}\\app.exe, C:\\...) or 'aumid'."""
    if re.match(r"^[a-z][a-z0-9+.-]*://", appid, re.IGNORECASE):
        return "url"
    return "path" if "\\" in appid or "/" in appid else "aumid"


def merge(shortcuts: list[App], start_apps: list[tuple[str, str]]) -> list[App]:
    """One app list from the shortcut scan and Get-StartApps.

    A Start-app that matches a shortcut (same name, or the same program) lends it its display
    name and AppUserModelID; the rest (Store apps, mostly) become "appid" entries.
    """
    result = list(shortcuts)
    slot = {_key(a.name): i for i, a in reversed(list(enumerate(result)))}
    merged: set[int] = set()
    for name, appid in start_apps:
        if not appid or _is_junk(name):
            continue
        kind = _appid_kind(appid)
        if kind == "url" or (kind == "path" and os.path.splitext(appid)[1].lower() in _DOC_TYPES):
            continue  # web links, and steam:// games that already have their .url shortcut
        exe = os.path.basename(appid).lower() if kind == "path" and appid.lower().endswith(".exe") else ""
        aumid = appid if kind == "aumid" else ""
        i = slot.get(_key(name))
        if i is None and exe:
            same_program = [j for j, a in enumerate(result) if j not in merged and a.kind == "shortcut" and exe in a.exes]
            i = same_program[0] if len(same_program) == 1 else None
        if i is not None:
            if i in merged:
                continue  # a second Start-app with this name (two "Calendar"s): keep the first
            merged.add(i)
            old = result[i]
            aliases = old.aliases + ((old.name,) if _key(old.name) != _key(name) else ())
            exes = old.exes + ((exe,) if exe and exe not in old.exes else ())
            result[i] = replace(old, name=name, aumid=old.aumid or aumid, exes=exes, aliases=aliases)
        else:
            merged.add(len(result))
            slot[_key(name)] = len(result)
            result.append(App(name, "appid", appid, exes=(exe,) if exe else (), aumid=aumid))
    return _tidy(result)


def _tidy(apps: list[App]) -> list[App]:
    """Sorted by name, one entry per name, with the process names from _COMMON added."""
    hints = {_key(n): exes for _, names, exes in _COMMON for n in names}
    out: dict[str, App] = {}
    for app in sorted(apps, key=lambda a: (a.name.casefold(), a.kind != "shortcut")):
        k = _key(app.name)
        if k in out:
            continue
        extra = tuple(e for e in hints.get(k, ()) if e not in app.exes)
        out[k] = replace(app, exes=app.exes + extra) if extra else app
    return list(out.values())


def _make_index(apps: Iterable[App], complete: bool) -> _Index:
    apps = tuple(apps)
    exact: dict[str, App] = {}
    for app in apps:                                     # 1. exact names
        exact.setdefault(_key(app.name), app)
    keys = dict(exact)
    for spoken, names, _ in _COMMON:                     # 2. nicknames, for apps that are installed
        app = next((exact[k] for k in map(_key, names) if k in exact), None)
        for alias in spoken if app else ():
            keys.setdefault(_key(alias), app)
    for app in apps:                                     # 3. shortcut file names, shortened names
        for alias in (*app.aliases, *_derived(app.name)):
            keys.setdefault(_key(alias), app)
    keys.pop("", None)
    claimed = frozenset(a.aumid.lower() for a in apps if a.aumid)
    sounds: dict[str, tuple[tuple[str, App], ...]] = {}
    for key, app in keys.items():
        sound = sound_of(key)
        if len(sound) >= _MIN_SOUND:
            sounds[key[0] + sound] = (*sounds.get(key[0] + sound, ()), (key, app))
    return _Index(apps, complete, keys, claimed, sounds)


def _save_cache(path: Path, apps: Iterable[App], complete: bool) -> None:
    data = {"version": _CACHE_VERSION, "complete": complete, "saved": time.time(),
            "apps": [asdict(a) for a in apps]}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


def _load_cache(path: Path) -> tuple[list[App], bool] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("ignoring the unreadable app index %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
        return None
    apps = []
    for raw in data.get("apps") or ():
        try:
            if raw["kind"] not in ("shortcut", "appid"):
                continue
            apps.append(App(str(raw["name"]), raw["kind"], str(raw["target"]),
                            exes=tuple(str(e) for e in raw.get("exes") or ()), aumid=str(raw.get("aumid") or ""),
                            aliases=tuple(str(a) for a in raw.get("aliases") or ())))
        except (KeyError, TypeError):
            continue
    return apps, bool(data.get("complete"))


# ---- keeping the index fresh ---------------------------------------------------------------

_lock = threading.Lock()
_current: _Index | None = None
_worker: threading.Thread | None = None
_started = False          # the once-per-run background refresh has been kicked off
_last_refresh = float("-inf")


def _install(apps: Iterable[App], complete: bool) -> _Index:
    global _current
    index = _make_index(apps, complete)
    with _lock:
        _current = index
    PRERENDER[:] = prerender_lines(index)
    return index


def warm_up() -> None:
    """Load the index and refresh it, all on a background thread. Call at startup; the first
    lookup does the same if this wasn't called, but then pays the one-off costs itself."""
    threading.Thread(target=_get_index, name="apps-warm-up", daemon=True).start()


def _get_index() -> _Index:
    global _started
    with _lock:
        first, _started = not _started, True
    if first:
        if _current is None:
            cached = _load_cache(CACHE_PATH)
            if cached:
                _install(*cached)
            else:  # first run ever: the shortcut scan is quick, and the Store apps follow
                _install(scan_shortcuts(_start_menu_roots()), False)
        refresh()
    return _current or _make_index((), False)


def refresh() -> threading.Thread:
    """Rebuild the index on a background thread (one at a time) and save it. Returns the thread."""
    global _worker, _last_refresh
    with _lock:
        if _worker is not None and _worker.is_alive():
            return _worker
        _last_refresh = time.monotonic()
        _worker = threading.Thread(target=_rebuild, name="apps-index", daemon=True)
        _worker.start()
        return _worker


def _rebuild() -> None:
    try:
        t0 = time.perf_counter()
        list_windows()  # the first call loads the shell's DLLs (~70 ms): pay it here, not in open_app
        shortcuts = scan_shortcuts(_start_menu_roots())
        try:
            start_apps, complete = _query_start_apps(), True
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            log.warning("Get-StartApps failed (%s); indexing Start Menu shortcuts only", exc)
            start_apps, complete = [], False
        old = _current
        if not complete and old is not None and old.complete:
            return  # keep the full index rather than lose the Store apps to a hiccup
        apps = merge(shortcuts, start_apps)
        _install(apps, complete)
        _save_cache(CACHE_PATH, apps, complete)
        log.info("app index: %d apps in %.0f ms", len(apps), (time.perf_counter() - t0) * 1000)
    except Exception:
        log.exception("rebuilding the app index failed")


def _after_miss(index: _Index) -> _Index | None:
    """A name wasn't found. Maybe the app is new: refresh, at most every _REFRESH_GAP. On the
    very first run, wait briefly for the Store apps and return the fuller index."""
    with _lock:
        busy = _worker is not None and _worker.is_alive()
        stale = time.monotonic() - _last_refresh > _REFRESH_GAP
    if not (busy or stale):
        return None
    worker = refresh()
    if index.complete:
        return None
    worker.join(_FIRST_SCAN_WAIT)
    return _current if _current is not index else None


def popular(index: _Index | None = None, limit: int = _PRERENDER_COUNT) -> list[str]:
    """The names of the most used apps (_COMMON's order) that are installed here."""
    index = index or _get_index()
    names: list[str] = []
    for _, aliases, _ in _COMMON:
        app = next((index.keys[k] for k in map(_key, aliases) if k in index.keys), None)
        if app is not None and app.name not in names:
            names.append(app.name)
        if len(names) == limit:
            break
    return names


def prerender_lines(index: _Index | None = None) -> list[str]:
    """The open_app replies for the most common installed apps, for the voice cache."""
    return [f"Opening {name}." for name in popular(index)]


# ---- finding an app by name ----------------------------------------------------------------

def _by_sound(index: _Index, key: str) -> list[App]:
    """The apps a name sounds like. Same first letter, as for spellings ("cloud", Claude; "good"
    is not Code), a name of two consonants only from 4 letters up ("wood" is Word, "cat" is
    nothing), and at least half its letters in common with the app's name: "what" sounds like
    Word but isn't, "court" sounds like Code but isn't (measured on the user's 147 apps)."""
    sound = sound_of(key)
    if len(sound) < _MIN_SOUND or len(key) < (4 if len(sound) == _MIN_SOUND else 3):
        return []
    found: list[App] = []
    for name, app in index.sounds.get(key[0] + sound, ()):
        if app not in found and difflib.SequenceMatcher(None, key, name).ratio() >= _SOUND_SPELLING:
            found.append(app)
    return found


def _lookup(index: _Index, name: str, cutoff: float, by_sound: bool = True) -> list[App]:
    """[the app], [] for nothing, or two or more apps when the name is about as near to each.
    By spelling first; then, with `by_sound`, by how it sounds (sound_of)."""
    variants = _variants(name)
    for variant in variants:
        if app := index.keys.get(_key(variant)):
            return [app]
    key = _key(variants[-1])
    # Same first letter: a mishearing rarely changes it, and "gmail" must not open Mail.
    near = [k for k in difflib.get_close_matches(key, list(index.keys), n=4, cutoff=cutoff) if k[0] == key[0]] \
        if len(key) >= _MIN_FUZZY else []
    if not near:
        return _by_sound(index, key) if by_sound and key else []
    scored = [(difflib.SequenceMatcher(None, key, k).ratio(), index.keys[k]) for k in near]
    best_score, best = max(scored, key=lambda pair: pair[0])
    rivals = {_key(a.name): a for s, a in scored if best_score - s < _TIE and _key(a.name) != _key(best.name)}
    return [best, *rivals.values()]


def _match(name: str, cutoff: float, by_sound: bool = True) -> list[App]:
    index = _get_index()
    found = _lookup(index, name, cutoff, by_sound)
    if not found and (fresher := _after_miss(index)) is not None:
        found = _lookup(fresher, name, cutoff, by_sound)
    return found


def resolve(name: str, strict: bool = False, by_sound: bool | None = None) -> App | None:
    """The installed app a spoken name means, or None when there's none or it's ambiguous.
    strict (the fast path) only accepts names that are exact or one slip away, and by default
    not names that only sound like an app ("but what?" must not open Word)."""
    found = _match(name, _STRICT_CUTOFF if strict else _CUTOFF, (not strict) if by_sound is None else by_sound)
    return found[0] if len(found) == 1 else None


def _find(name: str) -> App:
    text = " ".join(str(name).split()).strip(" .,!?\"'")
    if not text:
        raise ToolError("Which app?")
    found = _match(text, _CUTOFF)
    if len(found) > 1:
        raise ToolError(f"Did you mean {found[0].name} or {found[1].name}?")
    if not found:
        raise ToolError(f"I can't find an app called {text}.")
    return found[0]


# ---- windows and processes (ctypes) --------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("reserved1", ctypes.c_ushort), ("reserved2", ctypes.c_ushort),
                ("reserved3", ctypes.c_ushort), ("pointer", ctypes.c_void_p), ("extra", ctypes.c_void_p)]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


_IID_IPropertyStore = _GUID.parse("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
_PKEY_AppUserModel_ID = _PropertyKey(_GUID.parse("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5)
_RELEASE, _GET_VALUE = 2, 5          # IUnknown::Release, IPropertyStore::GetValue
_VT_LPWSTR = 31
_COINIT_APARTMENTTHREADED, _COINIT_DISABLE_OLE1DDE = 0x2, 0x4  # what ShellExecute asks for
_GW_OWNER, _GWL_EXSTYLE, _WS_EX_TOOLWINDOW = 4, -20, 0x80
_DWMWA_CLOAKED = 14
_SW_RESTORE = 9
_WM_CLOSE = 0x0010
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x2
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_ERROR_ACCESS_DENIED = 5
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
_UWP_FRAME = "ApplicationFrameWindow"  # Store apps' windows belong to ApplicationFrameHost.exe
_HOSTS = {"explorer.exe", "applicationframehost.exe"}  # always running, so proof of nothing

_HWND = wintypes.HWND
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, _HWND, wintypes.LPARAM)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi")

_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.EnumChildWindows.argtypes = [_HWND, _WNDENUMPROC, wintypes.LPARAM]
_user32.EnumChildWindows.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [_HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
for _name in ("IsWindow", "IsWindowVisible", "IsIconic", "SetForegroundWindow", "BringWindowToTop"):
    getattr(_user32, _name).argtypes = [_HWND]
    getattr(_user32, _name).restype = wintypes.BOOL
_user32.GetWindow.argtypes = [_HWND, wintypes.UINT]
_user32.GetWindow.restype = _HWND
_user32.GetWindowLongW.argtypes = [_HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = wintypes.LONG
_user32.GetWindowTextLengthW.argtypes = [_HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [_HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [_HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = _HWND
_user32.ShowWindow.argtypes = [_HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.SwitchToThisWindow.argtypes = [_HWND, wintypes.BOOL]
_user32.SwitchToThisWindow.restype = None
_user32.PostMessageW.argtypes = [_HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.GetApplicationUserModelId.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint32), wintypes.LPWSTR]
_kernel32.GetApplicationUserModelId.restype = wintypes.LONG
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
_kernel32.Process32NextW.restype = wintypes.BOOL
_shell32.SHGetPropertyStoreForWindow.argtypes = [_HWND, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
_shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None
_ole32.PropVariantClear.argtypes = [ctypes.POINTER(_PropVariant)]
_ole32.PropVariantClear.restype = ctypes.c_long
_dwmapi.DwmGetWindowAttribute.argtypes = [_HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


@contextmanager
def _com() -> Iterator[None]:
    """COM on this thread for the duration (tools and launches run on worker threads)."""
    ours = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED | _COINIT_DISABLE_OLE1DDE) in (0, 1)
    try:
        yield
    finally:
        if ours:
            _ole32.CoUninitialize()


def _vcall(obj: ctypes.c_void_p, slot: int, argtypes: tuple, *args, restype=ctypes.c_long):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[slot])(obj, *args)


@dataclass(frozen=True)
class Window:
    hwnd: int
    title: str
    exes: frozenset[str]      # the owning process's image name (and a hosted Store app's)
    aumids: frozenset[str]    # AppUserModelIDs of the window and those processes, lowercase


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _title(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(_user32.GetWindowTextLengthW(hwnd) + 1)
    _user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _is_app_window(hwnd: int) -> bool:
    """What Alt+Tab would show: visible, unowned, titled, not a tool window, not cloaked."""
    if not _user32.IsWindowVisible(hwnd) or _user32.GetWindow(hwnd, _GW_OWNER):
        return False
    if _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW or _user32.GetWindowTextLengthW(hwnd) <= 0:
        return False
    cloaked = wintypes.DWORD()
    if _dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 \
            and cloaked.value:
        return False  # on another virtual desktop, or a suspended Store app's leftover frame
    return _class_name(hwnd) not in _SHELL_CLASSES


def _process_info(pid: int) -> tuple[str, str]:
    """(image name, AppUserModelID), lowercase; empty where Windows won't say."""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "", ""
    try:
        size = wintypes.DWORD(1024)
        path = ctypes.create_unicode_buffer(size.value)
        exe = os.path.basename(path.value).lower() if _kernel32.QueryFullProcessImageNameW(
            handle, 0, path, ctypes.byref(size)) else ""
        length = ctypes.c_uint32(256)
        appid = ctypes.create_unicode_buffer(length.value)
        aumid = appid.value.lower() if _kernel32.GetApplicationUserModelId(
            handle, ctypes.byref(length), appid) == 0 else ""
        return exe, aumid
    finally:
        _kernel32.CloseHandle(handle)


def _window_aumid(hwnd: int) -> str:
    """The AppUserModelID the taskbar groups this window under, if it has its own."""
    store = ctypes.c_void_p()
    if _shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(_IID_IPropertyStore), ctypes.byref(store)) != 0 \
            or not store.value:
        return ""
    try:
        value = _PropVariant()
        if _vcall(store, _GET_VALUE, (ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant)),
                  ctypes.byref(_PKEY_AppUserModel_ID), ctypes.byref(value)) != 0:
            return ""
        try:
            return ctypes.wstring_at(value.pointer).lower() if value.vt == _VT_LPWSTR and value.pointer else ""
        finally:
            _ole32.PropVariantClear(ctypes.byref(value))
    finally:
        _vcall(store, _RELEASE, (), restype=ctypes.c_ulong)


def _children_pids(hwnd: int, own: int) -> list[int]:
    """The processes behind a Store app's frame: its CoreWindow child belongs to the app itself."""
    found: list[int] = []

    @_WNDENUMPROC
    def visit(child, _):
        pid = _pid(child)
        if pid != own and pid not in found:
            found.append(pid)
        return True

    _user32.EnumChildWindows(hwnd, visit, 0)
    return found


def list_windows() -> list[Window]:
    """The app windows Alt+Tab would show, front to back, with who owns them. AJ's own are left out."""
    handles: list[int] = []

    @_WNDENUMPROC
    def collect(hwnd, _):
        handles.append(hwnd)
        return True

    _user32.EnumWindows(collect, 0)
    me = os.getpid()
    known: dict[int, tuple[str, str]] = {}  # pid -> (image name, AppUserModelID), asked once per pid
    windows = []
    with _com():
        for hwnd in handles:
            if not hwnd or not _is_app_window(hwnd):
                continue
            pid = _pid(hwnd)
            if pid == me:
                continue
            pids = [pid] + (_children_pids(hwnd, pid) if _class_name(hwnd) == _UWP_FRAME else [])
            for p in pids:
                if p not in known:
                    known[p] = _process_info(p)
            exes = frozenset(known[p][0] for p in pids if known[p][0])
            aumids = frozenset(a for a in (_window_aumid(hwnd), *(known[p][1] for p in pids)) if a)
            windows.append(Window(hwnd, _title(hwnd), exes, aumids))
    return windows


def _running_processes() -> list[tuple[int, str]]:
    """(pid, lowercase image name) for every process."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE:
        return []
    try:
        entry = _ProcessEntry(dwSize=ctypes.sizeof(_ProcessEntry))
        found = []
        more = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            found.append((entry.th32ProcessID, entry.szExeFile.lower()))
            more = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return found
    finally:
        _kernel32.CloseHandle(snapshot)


def _is_running(app: App) -> bool:
    """Is the app's process alive, window or not (e.g. only in the tray)?"""
    processes = _running_processes()
    if {exe for _, exe in processes} & (set(app.exes) - _HOSTS):
        return True
    packaged = app.aumid.lower() if "!" in app.aumid else ""  # Store apps: "Family_hash!App"
    return bool(packaged) and any(_process_info(pid)[1] == packaged for pid, _ in processes)


def _belongs(window: Window, app: App, claimed: frozenset[str]) -> bool:
    """Is this one of the app's windows? Its AppUserModelID decides when it has one; otherwise
    the process name does, unless the window carries another installed app's AppUserModelID
    (a Chrome web app runs as chrome.exe too, but isn't Chrome)."""
    mine = app.aumid.lower()
    if mine and mine in window.aumids:
        return True
    if not window.exes & set(app.exes):
        return False
    return not (window.aumids & claimed) - {mine}


_by_owner: tuple[int, dict[str, list[App]]] | None = None  # (id of the index, aumid/exe -> apps)


def _owners(index: _Index) -> dict[str, list[App]]:
    """Apps by AppUserModelID and process name, built once per index."""
    global _by_owner
    if _by_owner is None or _by_owner[0] != id(index):
        table: dict[str, list[App]] = {}
        for app in index.apps:
            for key in {app.aumid.lower(), *app.exes} - {""} - _HOSTS:
                table.setdefault(key, []).append(app)
        _by_owner = (id(index), table)
    return _by_owner[1]


def name_of(window: Window) -> str:
    """The app a window belongs to, as the Start menu names it, else its program's name. For the
    live laptop status (core/laptop_state.py)."""
    index = _current or _get_index()
    table = _owners(index)
    for key in (*sorted(window.aumids), *sorted(window.exes)):
        for app in table.get(key, ()):
            if _belongs(window, app, index.claimed):
                return app.name
    if "explorer.exe" in window.exes:
        return "File Explorer"
    exe = next(iter(sorted(window.exes - _HOSTS)), "")
    return exe.removesuffix(".exe").replace("_", " ").title() if exe else "an app"


def find_windows(app: App, windows: list[Window] | None = None) -> list[Window]:
    """The app's open windows, front-most first."""
    claimed = (_current or _get_index()).claimed
    return [w for w in (list_windows() if windows is None else windows) if _belongs(w, app, claimed)]


def _focus(hwnd: int) -> bool:
    """Bring a window to the front, restoring it if minimised. True if it got there.

    Windows only lets the app you're using move focus, so this thread borrows the foreground
    thread's input state for the moment of the switch. No keys are simulated.
    """
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
    front = _user32.GetForegroundWindow()
    if front == hwnd:
        return True
    me = _kernel32.GetCurrentThreadId()
    theirs = _user32.GetWindowThreadProcessId(front, None) if front else 0
    joined = bool(theirs) and theirs != me and bool(_user32.AttachThreadInput(me, theirs, True))
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if joined:
            _user32.AttachThreadInput(me, theirs, False)
    if _user32.GetForegroundWindow() != hwnd:
        _user32.SwitchToThisWindow(hwnd, True)
    return _user32.GetForegroundWindow() == hwnd


def _post_close(hwnd: int) -> bool:
    """Ask a window to close, exactly as its X button does. The app may still ask to save."""
    return bool(_user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0))


def _still_shown(hwnd: int) -> bool:
    return bool(_user32.IsWindow(hwnd) and _user32.IsWindowVisible(hwnd))


# ---- launching -----------------------------------------------------------------------------

_SAFE_APPID = re.compile(r"^[^\s\"/\\\-<>|^&%][^\"<>|\x00-\x1f]*$")  # no quotes, switches or shell characters


def _check_launchable(app: App) -> None:
    """Refuse a stale or tampered index entry before anything is started."""
    if app.kind == "shortcut":
        path = Path(app.target)
        roots = [r.resolve() for r in _start_menu_roots()]
        if not any(path.resolve().is_relative_to(r) for r in roots):
            raise ToolError(f"{app.name}'s shortcut isn't in the Start menu, so I won't run it.")
        if not path.is_file():
            refresh()
            raise ToolError(f"{app.name} seems to have been uninstalled.")
    elif app.kind != "appid" or not _SAFE_APPID.match(app.target):
        raise ToolError(f"{app.name} has an app ID I won't pass to Windows.")


def _launch(app: App) -> subprocess.Popen | None:
    """Start the app without waiting for it. Raises OSError if Windows refuses."""
    if app.kind == "shortcut":
        os.startfile(app.target)  # ShellExecute: the shortcut runs exactly as from the Start menu
        return None
    return subprocess.Popen([_EXPLORER, f"shell:AppsFolder\\{app.target}"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)


def _titled_like(title: str, app: App) -> bool:
    """A new window titled with the app's name ("notes.txt - Notepad") is probably the app."""
    words = _key(title)
    return any(len(k) >= 4 and k in words for k in map(_key, (app.name, *_derived(app.name))))


def _wait_until_open(app: App, before: set[int], timeout: float) -> bool:
    """Watch for the app's window: one of its own, or a new one with its name in the title.
    At the deadline, its process running (in the tray, say) also counts."""
    claimed = (_current or _get_index()).claimed
    deadline = time.monotonic() + timeout
    while True:
        for w in list_windows():
            if _belongs(w, app, claimed) or (w.hwnd not in before and _titled_like(w.title, app)):
                return True
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_SECS)
    if _is_running(app):
        return True
    # A steam:// game or a shortcut whose program is unknown leaves nothing to look for, and
    # Windows took the launch without complaint, so don't claim it failed.
    return not _identifiable(app)


def _identifiable(app: App) -> bool:
    """Could we recognise this app's windows or processes at all?"""
    generated = app.aumid.lower().startswith("microsoft.autogenerated.")  # never set on a window
    return bool(set(app.exes) - _HOSTS) or (bool(app.aumid) and not generated)


def _report(name: str, ok: bool) -> None:
    if ok:
        log.info("%s opened", name)
    else:
        log.warning("%s didn't open within %g s", name, VERIFY_SECS)
    hook = on_verify
    if hook is not None:
        try:
            hook(name, ok)
        except Exception:
            log.exception("on_verify failed for %s", name)


def _launch_and_verify(app: App, before: set[int]) -> None:
    try:
        with _com():
            try:
                process = _launch(app)
            except OSError as exc:
                log.warning("Windows wouldn't start %s: %s", app.name, exc)
                ok = False
            else:
                ok = _wait_until_open(app, before, VERIFY_SECS)
                if process is not None:
                    process.poll()  # explorer.exe hands over and exits; reap it
    except Exception:
        log.exception("launching %s failed", app.name)
        ok = False
    if not ok:
        ok = _recheck_with_fresh_index(app)
    _report(app.name, ok)


def _recheck_with_fresh_index(app: App) -> bool:
    """Before saying an app didn't open, rebuild the index and look again. Live (2026-10-18)
    WhatsApp had updated and its process was renamed; the old index couldn't recognise the new
    window, so a launch that worked was reported as failed."""
    refresh().join(REFRESH_WAIT_SECS)
    fresh = resolve(app.name, strict=True)
    if fresh is None or fresh == app:
        return False
    try:
        with _com():
            return bool(find_windows(fresh)) or _is_running(fresh)
    except Exception:
        log.exception("re-checking %s failed", app.name)
        return False


def _launch_in_background(app: App, before: set[int]) -> None:
    threading.Thread(target=_launch_and_verify, args=(app, before), name=f"open-{app.name}", daemon=True).start()


# ---- tools ---------------------------------------------------------------------------------

@tool(level=Level.SAFE, timeout=5.0, cloud=False)
def open_app(name: str) -> str:
    """Open an app on this laptop, or bring it to the front if it's already open.

    name: the app's name, e.g. spotify, whatsapp or vs code
    """
    app = _find(name)
    windows = list_windows()
    mine = find_windows(app, windows)
    if mine:
        if _focus(mine[0].hwnd):
            return f"Switching to {app.name}."
        return f"{app.name} is already open, but Windows wouldn't let me bring it to the front."
    _check_launchable(app)
    _launch_in_background(app, {w.hwnd for w in windows})
    return f"Opening {app.name}."


@tool(level=Level.SAFE, timeout=5.0, cloud=False)
def switch_to_app(name: str) -> str:
    """Bring an app that's already open to the front.

    name: the app's name, e.g. chrome or discord
    """
    app = _find(name)
    mine = find_windows(app)
    if not mine:
        raise ToolError(f"{app.name} isn't open.")
    if not _focus(mine[0].hwnd):
        raise ToolError(f"Windows wouldn't let me bring {app.name} to the front.")
    return f"Switching to {app.name}."


_NAMED = 12  # apps named when asked which ones he can open


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


@tool(level=Level.SAFE, timeout=5.0, cloud=False)
def list_apps(search: str = "") -> str:
    """List the apps installed on this laptop. open_app can open every one of them.

    search: part of a name, to list only the apps that match (e.g. office, adobe, python); leave it out for all
    """
    # Live (2026-10-23), asked which apps he can open, he named the two that were running
    # ("Google Chrome, Claude") and then refused to open Settings, Claude and Word.
    index = _get_index()
    if not index.apps:
        raise ToolError("I couldn't read the list of installed apps.")
    text = " ".join(str(search).split()).strip(" .,!?\"'")
    if not text:
        return (f"{len(index.apps)} apps are installed, and I can open any of them, such as "
                f"{_and(popular(index, _NAMED))}. Name one and I'll open it.")
    key = _key(text)
    found = [a for a in index.apps if key and key in _key(a.name)] or _lookup(index, text, _CUTOFF)
    if not found:
        return f"No installed app is called {text}."
    names = [a.name for a in found]
    more = f", and {len(names) - _NAMED} more" if len(names) > _NAMED else ""
    return f"Installed: {_and(names[:_NAMED])}{more}."


@tool(level=Level.CONFIRM, timeout=6.0, cloud=False)
def close_app(name: str) -> str:
    """Close an app's windows, as clicking X does (it may still ask to save first).

    name: the app's name, e.g. spotify or chrome
    """
    app = _find(name)
    mine = find_windows(app)
    if not mine:
        raise ToolError(f"{app.name} isn't open.")
    asked = [w for w in mine if _post_close(w.hwnd)]
    if not asked:
        if ctypes.get_last_error() == _ERROR_ACCESS_DENIED:
            raise ToolError(f"{app.name} runs as administrator, so Windows won't let me close it.")
        raise ToolError(f"Windows wouldn't pass the close request on to {app.name}.")
    deadline = time.monotonic() + _CLOSE_WAIT
    while any(_still_shown(w.hwnd) for w in asked):
        if time.monotonic() >= deadline:
            return f"Asked {app.name} to close, but it's still open. It may be asking you to save something."
        time.sleep(0.1)
    return f"{app.name} has been closed."  # the Piper voice says "Closed X." as "Close X." (2026-10-21)


# ---- fast-path intents ---------------------------------------------------------------------
# Matched in full against the normalized transcript (see intents.py). The builders only claim
# names of installed apps, so "open youtube" falls through to web.py and "open a jar" to the model.

def _known(m: re.Match) -> dict | None:
    app = resolve(m["name"], strict=True)
    return {"name": app.name} if app else None


def _known_or_sounds_like(m: re.Match) -> dict | None:
    """After a plain "open", a name that only sounds like one app is that app: "open cloud" is
    Claude, "open wood" is Word (live, 2026-10-23). Opening is harmless, and he says what opened."""
    app = resolve(m["name"], strict=True, by_sound=True)
    return {"name": app.name} if app else None


def _known_and_open(m: re.Match) -> dict | None:
    """'go to X' is also said of websites and folders: claim it only when X is an open app."""
    app = resolve(m["name"], strict=True)
    return {"name": app.name} if app and find_windows(app) else None


INTENTS: list[tuple[str, str, Callable[[re.Match], dict | None]]] = [
    # "What apps can you open? List them all out.", "What else apps can you access?" (live, 2026-10-23)
    (r"(?:what|which)(?: other| else| more)? (?:apps?|applications?|programs?)(?: else)? (?:can|could|do|are) "
     r"(?:you|u)(?: able to)? (?:open|access|launch|start|use|run|have)\b.*", "list_apps", lambda m: {}),
    (r"(?:list|name|tell me)(?: all)?(?: of)?(?: the| my| your)? (?:installed )?(?:apps|applications|programs)"
     r"(?: (?:you can open|installed|on (?:this|the|my) (?:laptop|computer|pc)))?", "list_apps", lambda m: {}),
    (r"what (?:apps|applications|programs) (?:are installed|do i have)(?: installed)?(?: on (?:this|the|my) "
     r"(?:laptop|computer|pc))?", "list_apps", lambda m: {}),
    (r"(?:(?:open|start)(?: up)?|launch) (?P<name>.+)", "open_app", _known_or_sounds_like),
    (r"(?:(?:fire|boot|load|bring|pull) up) (?P<name>.+)", "open_app", _known),
    # Speech-to-text heard "Open Google Chrome" as "But Google Chrome" (live, 2026-10-18). Safe
    # only because the rest must be exactly an installed app's name.
    (r"(?:but|often|oven) (?P<name>.+)", "open_app", _known),
    (r"(?:close|quit|exit|shut(?: down)?|kill)(?: out of)? (?P<name>.+)", "close_app", _known),
    (r"(?:switch|change|flip|jump|swap)(?: back| over)? to (?P<name>.+)", "switch_to_app", _known),
    (r"focus(?: on)? (?P<name>.+)", "switch_to_app", _known),
    (r"go(?: back)? to (?P<name>.+)", "switch_to_app", _known_and_open),
]


# The cached index is enough for PRERENDER at import; the refresh waits for the first lookup.
if (_cached := _load_cache(CACHE_PATH)) is not None:
    _install(*_cached)
