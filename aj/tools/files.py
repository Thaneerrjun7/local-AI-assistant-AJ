"""File tools: find, open, list and read files, and move, copy, rename or delete them, with undo.

Ported from SivaSafe's file_controller.py, its best action file, keeping its design (home-only
roots, protected folders, delete to the Recycle Bin) and fixing what it left open:
- Everything stays inside the user folder. A path is resolved first, so "..", symlinks and
  junctions can't step outside, then checked: Windows, Program Files and ProgramData, AppData,
  hidden dot-folders (.ssh, .aws, .gnupg ...), "$" folders and reserved device names are
  refused, and paths with ".." or network shares are refused before anything is looked up.
- The main folders (home, Desktop, Documents, Downloads, Pictures, Music, Videos) can be opened
  and filled but never moved, renamed or deleted, and neither can a folder that holds one, or
  AJ's own folder.
- Delete goes to the Recycle Bin only, through SHFileOperationW with FOF_ALLOWUNDO. There is no
  permanent delete anywhere in this file. SivaSafe needed the send2trash package for this; ctypes
  does it with nothing to install.
- Every move, copy and rename goes on an undo stack (this session only), so "undo" puts things
  back. A delete can't be pulled back out of the Recycle Bin from here, and undo says so.
- The main folders come from Windows (SHGetKnownFolderPath), so a Desktop or Documents that
  OneDrive has moved is still the one that's found.
- find_files walks breadth-first under a time and entry budget, never follows links, skips
  hidden and system folders and caches like node_modules, and ranks by how well the name
  matches, then by how recently it changed. SivaSafe stopped after 500 folders in whatever order
  the disk gave them and ranked nothing. The last list is remembered, so "open number 2" works.
- open_file is SAFE (no confirmation), so it opens documents only: programs, scripts and
  shortcuts are refused, and a model-written path can't run an installer from Downloads.
- move, copy, rename and delete take a path or a name, never a list number: the confirmation
  question then names the exact file. The fast path turns "delete number 2" into that path
  before the question is asked.
- read_text_file reads plain text only (txt, md, csv, log, code, anything without NUL bytes).
  PDF, Word, Excel and PowerPoint need third-party packages, so they are refused with a clear
  message for now.
"""
import ctypes
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Literal

from aj.config import APP_DIR
from aj.tools.registry import Level, ToolError, tool

HOME = Path(os.environ.get("USERPROFILE") or Path.home())

Folder = Literal["anywhere", "desktop", "documents", "downloads", "pictures", "music", "videos"]

MAX_RESULTS = 8       # remembered and listed by find_files
LIST_MAX = 25         # entries list_folder reads out
READ_CHARS = 2000     # read_text_file returns at most this much
UNDO_DEPTH = 20
_WALK_SECONDS = 3.0   # find_files' search budget, inside its 8 s tool timeout
_WALK_ENTRIES = 80_000

# Windows' ids for the main folders (KNOWNFOLDERID). "home" is HOME itself.
_KNOWN_IDS = {
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}
_ALIASES = {
    "desktop": "desktop", "documents": "documents", "document": "documents", "docs": "documents",
    "downloads": "downloads", "download": "downloads", "pictures": "pictures", "photos": "pictures",
    "music": "music", "videos": "videos", "video": "videos", "home": "home", "user": "home",
}

# Never entered or touched, wherever they sit under a root (dot and "$" names are refused too).
_PRIVATE = {"appdata", "application data", "local settings"}
# Not worth searching: huge, generated, and never what anyone means by "my files".
_SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "bower_components", "venv", "onedrivetemp"}
_HIDDEN_OR_SYSTEM = 0x2 | 0x4  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
_SYSTEM_ENV = ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")

# open_file runs without confirmation, so it opens documents, never anything that runs code.
_PROGRAMS = frozenset(
    ".exe .com .bat .cmd .ps1 .psm1 .vbs .vbe .js .jse .wsf .wsh .hta .msi .msp .msc .scr .pif .cpl "
    ".reg .jar .lnk .url .appref-ms .application .inf .scf .py .pyw".split())
# Text lives inside these, but getting it out needs third-party packages.
_NOT_YET = {".pdf": "PDF", ".doc": "Word", ".docx": "Word", ".odt": "OpenDocument", ".rtf": "Rich Text",
            ".ppt": "PowerPoint", ".pptx": "PowerPoint", ".xls": "Excel", ".xlsx": "Excel", ".epub": "e-book"}
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_REAL_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_isreserved: Callable[[str], bool] = getattr(os.path, "isreserved", lambda name: False)


# ---- Windows calls --------------------------------------------------------------------------

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _pack_ = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 1  # shellapi.h packs it this way
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p),
                ("pTo", ctypes.c_void_p), ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


FO_DELETE = 0x3
FOF_SILENT = 0x4
FOF_NOCONFIRMATION = 0x10
FOF_ALLOWUNDO = 0x40          # this is what makes it the Recycle Bin and not a permanent delete
FOF_NOERRORUI = 0x400
# Without this, NOCONFIRMATION lets Windows quietly delete for good anything too big for the bin.
# With it, Windows asks first, and that question is the user's to answer, never AJ's.
FOF_WANTNUKEWARNING = 0x4000
RECYCLE_FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
_DRIVE_FIXED = 3

_shell32 = ctypes.WinDLL("shell32")
_ole32 = ctypes.WinDLL("ole32")
_kernel32 = ctypes.WinDLL("kernel32")
_SHFileOperationW = _shell32.SHFileOperationW
_SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
_SHFileOperationW.restype = ctypes.c_int
_SHGetKnownFolderPath = _shell32.SHGetKnownFolderPath
_SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
_SHGetKnownFolderPath.restype = ctypes.HRESULT  # ctypes raises OSError on a failed HRESULT
_CoTaskMemFree = _ole32.CoTaskMemFree
_CoTaskMemFree.argtypes = [ctypes.c_void_p]
_CoTaskMemFree.restype = None
_GetDriveTypeW = _kernel32.GetDriveTypeW
_GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_GetDriveTypeW.restype = wintypes.UINT


class _RecycleError(ToolError):
    """The Recycle Bin refused; the file was left where it was."""


def _recycle(path: Path) -> None:
    """Send one file or folder to the Recycle Bin, or raise. Never a permanent delete."""
    text = str(path)
    names = ctypes.create_unicode_buffer(text, len(text) + 2)  # a list of one, ended by two NULs
    op = _SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=ctypes.addressof(names), fFlags=RECYCLE_FLAGS)
    code = _SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted:
        raise _RecycleError(f"Windows couldn't put {path.name} in the Recycle Bin "
                            f"(code {code:#x}), so I left it where it is.")
    if os.path.lexists(path):
        raise _RecycleError(f"{path.name} is still there: Windows didn't move it to the Recycle Bin.")


def _shell_folder(name: str) -> Path | None:
    """Where Windows keeps a main folder (OneDrive may have moved it), or None if it won't say."""
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(_KNOWN_IDS[name]).bytes_le)
    out = ctypes.c_void_p()
    try:
        _SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        return Path(ctypes.wstring_at(out.value)) if out.value else None
    except OSError:
        return None
    finally:
        if out.value:  # freed whether or not the call succeeded, as its documentation asks
            _CoTaskMemFree(out)


# ---- where AJ may go --------------------------------------------------------------------

def folder_path(name: str) -> Path:
    """A main folder by its short name: "home", "desktop", "documents", ..."""
    if name == "home":
        return HOME
    return _shell_folder(name) or HOME / name.capitalize()


def _main_folders() -> list[Path]:
    return [HOME.resolve(), *(folder_path(n).resolve() for n in _KNOWN_IDS)]


def _roots() -> list[Path]:
    """Home, plus any main folder that Windows keeps somewhere outside it."""
    roots: list[Path] = []
    for folder in _main_folders():
        if not any(folder.is_relative_to(r) for r in roots):
            roots.append(folder)
    return roots


def _system_dirs() -> list[Path]:
    return [Path(v).resolve() for key in _SYSTEM_ENV if (v := os.environ.get(key))]


def _private(part: str) -> bool:
    """A path component AJ never enters: hidden, system, secret or a device name."""
    low = part.lower()
    return (low.startswith((".", "$")) or low in _PRIVATE or low.startswith("ntuser")
            or ":" in part or _isreserved(part))


def _safe(path: Path, change: bool = False) -> Path:
    """The real path behind `path` if AJ may use it, else ToolError.

    change: it is about to be moved, renamed or deleted, or created by a move or copy.
    """
    try:
        real = path.resolve()  # follows "..", symlinks and junctions to where they really lead
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolError(f"I can't make sense of the path '{path}'.") from exc
    if str(real).startswith("\\\\"):
        raise ToolError("I only work with files on this laptop, not network shares.")
    if any(real.is_relative_to(d) for d in _system_dirs()):
        raise ToolError("That's a Windows system folder, and I don't touch those.")
    root = next((r for r in _roots() if real.is_relative_to(r)), None)
    if root is None:
        raise ToolError(f"{real} is outside your user folder, so I won't touch it.")
    if any(_private(part) for part in real.relative_to(root).parts):
        raise ToolError(f"{_display(real)} is in a hidden or system location, and I stay out of those.")
    if change:
        _check_changeable(real)
    return real


def _check_changeable(real: Path) -> None:
    for main in _main_folders():
        if real == main or main.is_relative_to(real):
            raise ToolError(f"{_label(real)} is one of your main folders (or holds one), "
                            "so I won't move, rename or delete it.")
    app = APP_DIR.resolve()
    if real.is_relative_to(app) or app.is_relative_to(real):
        raise ToolError("That's my own folder, so I leave it alone.")


# ---- turning what was said into a path ------------------------------------------------------

_last: list[Path] = []          # the last numbered list (find_files or list_folder)
_undo: list["_Change"] = []
_LOCK = threading.Lock()        # tools run on worker threads

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "last": -1,
}
_INDEX = re.compile(r"(?:the )?(?:(?:number|no|result|item|file|#) ?)?"
                    r"(?:(?P<n>\d{1,3})(?:st|nd|rd|th)?|(?P<w>[a-z]+))(?: one| result| file| item)?")


def _index_of(text: str) -> int | None:
    """2 for "2", "#2", "2nd", "number two", "the second one"; -1 for "the last one"; else None."""
    found = _INDEX.fullmatch(" ".join(text.lower().replace(".", " ").split()))
    if found is None:
        return None
    return int(found["n"]) if found["n"] else _NUMBER_WORDS.get(found["w"])


def _remember(paths: list[Path]) -> None:
    with _LOCK:
        _last[:] = paths


def _remembered(text: str) -> Path:
    index = _index_of(text)
    with _LOCK:
        last = list(_last)
    if not last:
        raise ToolError(f"There's no list to pick '{text}' from yet. Search or list a folder first.")
    if index == -1:
        index = len(last)
    if not 1 <= index <= len(last):
        raise ToolError(f"Pick a number from 1 to {len(last)}.")
    return last[index - 1]


def _folder_name(text: str) -> str | None:
    """'my documents', 'the Downloads folder' -> 'documents', 'downloads'."""
    words = text.lower().split()
    if words and words[0] in ("my", "the"):
        words = words[1:]
    if words and words[-1] in ("folder", "directory"):
        words = words[:-1]
    return _ALIASES.get(" ".join(words))


def _candidates(raw: str) -> list[Path]:
    if raw.startswith("~"):
        return [HOME / raw[1:].lstrip("\\/")]
    path = Path(raw)
    if path.drive or path.root:  # absolute, or tied to a drive: never relative to home
        return [path]
    out = [HOME / path]
    head, *rest = path.parts
    if (folder := _folder_name(head)) is not None:
        out.append(folder_path(folder).joinpath(*rest))  # "documents\x" where Documents was moved
    return out


def _by_name(name: str) -> Path | None:
    """A bare name: one of the last results (with or without its extension), or a file at the
    top of a main folder."""
    want = name.lower()
    with _LOCK:
        last = list(_last)
    hits = [p for p in last if want in (p.name.lower(), p.stem.lower())]
    if not hits:
        hits = [f / name for f in map(folder_path, _KNOWN_IDS) if (f / name).exists()]
    hits = list(dict.fromkeys(hits))
    if len(hits) > 1:
        raise ToolError(f"There's more than one {name}: {', '.join(_display(p) for p in hits[:4])}. Which one?")
    return hits[0] if hits else None


def _locate(text: str, *, change: bool = False, by_number: bool = False) -> Path:
    """An existing, allowed path for what was said: a list number (when by_number), a main
    folder's name, a path (absolute, or relative to home) or a bare name. Else ToolError."""
    raw = text.strip().strip("\"'").strip()
    if not raw:
        raise ToolError("Which file or folder?")
    if _index_of(raw) is not None:
        if not by_number:
            raise ToolError("Use the file's path or name from the list, not its number, "
                            "so the confirmation says exactly what changes.")
        return _existing(_safe(_remembered(raw), change))
    if (folder := _folder_name(raw)) is not None:
        return _existing(_safe(folder_path(folder), change))
    if raw.startswith(("\\\\", "//")):
        raise ToolError("I only work with files on this laptop, not network shares.")
    if ".." in PureWindowsPath(raw).parts:
        raise ToolError("Paths with '..' in them aren't allowed.")
    for candidate in _candidates(raw):
        real = _safe(candidate, change)
        if real.exists():
            return real
    if len(PureWindowsPath(raw).parts) == 1 and (found := _by_name(raw)) is not None:
        return _existing(_safe(found, change))
    raise ToolError(f"I can't find '{raw}'. Search for it with find_files first.")


def _existing(path: Path) -> Path:
    if not path.exists():
        raise ToolError(f"{path.name} isn't there any more.")
    return path


def _display(path: Path) -> str:
    """A path as short as it can be while still pointing at one place: relative to home."""
    home = HOME.resolve()
    if path == home or not path.is_relative_to(home):
        return str(path)
    return str(path.relative_to(home))


def _label(path: Path) -> str:
    """'Documents', 'your home folder', or the folder's own name, for spoken replies."""
    real = path.resolve()
    if real == HOME.resolve():
        return "your home folder"
    for name in _KNOWN_IDS:
        if real == folder_path(name).resolve():
            return name.capitalize()
    return path.name


# ---- small helpers --------------------------------------------------------------------------

def _size(n: float) -> str:
    units, i = ("bytes", "KB", "MB", "GB", "TB"), 0
    while n >= 1024 and i < len(units) - 1:
        n, i = n / 1024, i + 1
    return f"{n:.0f} {units[i]}" if i == 0 or n >= 10 else f"{n:.1f} {units[i]}"


def _age(mtime: float, now: float) -> str:
    days = int((now - mtime) // 86400)
    if days < 1:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return datetime.fromtimestamp(mtime).strftime("in %B %Y")


def _kind(entry: os.DirEntry) -> str | None:
    """'dir', 'file', or None for links and junctions, which are never followed."""
    try:
        if entry.is_symlink() or entry.is_junction():
            return None
        if entry.is_dir(follow_symlinks=False):
            return "dir"
        if entry.is_file(follow_symlinks=False):
            return "file"
    except OSError:
        pass
    return None


def _visible(entry: os.DirEntry) -> bool:
    """Not hidden, not system, not private (the directory listing already holds the attributes)."""
    if _private(entry.name) or entry.name.startswith("~$"):  # ~$ are Office lock files
        return False
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return not attributes & _HIDDEN_OR_SYSTEM


def _mtime(entry: os.DirEntry) -> float:
    try:
        return entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        return 0.0


def _start(path: Path) -> None:
    """ShellExecute: the file's own app, or Explorer for a folder. No shell, no console."""
    try:
        os.startfile(str(path))
    except OSError as exc:
        raise ToolError(f"Windows couldn't open {path.name} ({exc.strerror or exc}).") from exc


# ---- find_files -----------------------------------------------------------------------------

def _words(text: str) -> str:
    """'Résumé_2026-final.PDF' -> 'resume 2026 final pdf'."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return " ".join(w for w in re.split(r"[\W_]+", text.lower()) if w)


def _query_words(query: str) -> list[str]:
    """The words worth matching: "the budget dot pdf file" -> ["budget", "pdf"]."""
    words = _words(query).split()
    words = [w for w in words if w != "dot"] or words  # "budget dot pdf", as it was spoken
    while len(words) > 1 and words[0] in ("my", "the", "a", "an", "some"):
        words.pop(0)
    while len(words) > 1 and words[-1] in ("file", "files", "folder", "document"):
        words.pop()
    return words


def name_score(query: str, name: str) -> int:
    """How well a file name matches what was asked: 100 exact, 0 not at all."""
    q, n = _words(query), _words(name)
    stem = _words(name.rsplit(".", 1)[0]) if "." in name else n
    if not q:
        return 0
    if q in (n, stem):
        return 100
    if stem.startswith(q):
        return 80
    if f" {q}" in f" {n}":  # the whole phrase, starting at a word
        return 65
    words = n.split()
    if all(any(w.startswith(t) for w in words) for t in q.split()):
        return 45  # every word, in any order
    return 0


def _recency(mtime: float, now: float) -> float:
    """Up to 15 points: changed today 15, a month ago 7.5, a year ago almost nothing."""
    return 15 * 0.5 ** (max(0.0, now - mtime) / (30 * 86400))


@dataclass(frozen=True)
class _Hit:
    path: Path
    score: float
    mtime: float
    is_dir: bool


def _walk(starts: list[Path], visit: Callable[[os.DirEntry, str], None]) -> bool:
    """Breadth-first over the start folders, shallow files first. False if the budget ran out."""
    deadline = time.monotonic() + _WALK_SECONDS
    queue, seen, entries = deque(starts), set(), 0
    while queue:
        folder = queue.popleft()
        key = os.path.normcase(str(folder))
        if key in seen:
            continue
        seen.add(key)
        try:
            with os.scandir(folder) as listing:
                for entry in listing:
                    entries += 1
                    kind = _kind(entry) if _visible(entry) else None
                    if kind is None or (kind == "dir" and entry.name.lower() in _SKIP_DIRS):
                        continue
                    if kind == "dir":
                        queue.append(Path(entry.path))
                    visit(entry, kind)
        except OSError:
            continue
        if entries >= _WALK_ENTRIES or time.monotonic() > deadline:
            return False
    return True


def _search(words: list[str], starts: list[Path]) -> tuple[list[_Hit], bool]:
    query, now, hits = " ".join(words), time.time(), []

    def visit(entry: os.DirEntry, kind: str) -> None:
        score = name_score(query, entry.name)
        if score:
            mtime = _mtime(entry)
            hits.append(_Hit(Path(entry.path), score + _recency(mtime, now), mtime, kind == "dir"))

    finished = _walk(starts, visit)
    hits.sort(key=lambda h: (-h.score, -h.mtime))
    return hits[:MAX_RESULTS], finished


def _search_starts(folder: str) -> list[Path]:
    names = list(_KNOWN_IDS) + ["home"] if folder == "anywhere" else [folder]
    starts = [_safe(folder_path(n)) for n in names]
    starts = [s for s in starts if s.is_dir()]
    if not starts:
        raise ToolError(f"Your {folder.capitalize()} folder doesn't exist.")
    return starts


@tool(level=Level.SAFE, timeout=8.0)
def find_files(query: str, folder: Folder = "anywhere") -> str:
    """Find files or folders by name in the user's folders, best match first. The list is numbered, and open_file, open_folder and read_text_file accept those numbers.

    query: part of the name, e.g. "budget" or "trip photos"; an extension like pdf narrows it
    folder: where to look; anywhere means the whole user folder
    """
    words = _query_words(query)
    if not words:
        raise ToolError("Tell me what the file is called.")
    hits, finished = _search(words, _search_starts(folder))
    _remember([h.path for h in hits])
    where = "your files" if folder == "anywhere" else f"{folder.capitalize()}"
    asked = " ".join(words)
    if not hits:
        tail = "" if finished else " (in the part I had time to search; name a folder to look deeper)"
        return f'Nothing called "{asked}" in {where}{tail}.'
    now = time.time()
    lines = [f'Best matches for "{asked}" in {where}:']
    lines += [f"{i}. {_display(h.path)} ({'folder, ' if h.is_dir else ''}changed {_age(h.mtime, now)})"
              for i, h in enumerate(hits, 1)]
    if not finished:
        lines.append(f"(I stopped after {_WALK_SECONDS:g} seconds; name a folder to search it fully.)")
    return "\n".join(lines)


# ---- open, list, read -----------------------------------------------------------------------

@tool(level=Level.SAFE, timeout=5.0)
def open_file(path_or_index: str) -> str:
    """Open a document, photo, video or other file in its usual app. Programs and scripts are refused.

    path_or_index: a number from the last list ("2"), a file name, or a path
    """
    path = _locate(path_or_index, by_number=True)
    if path.is_file() and path.suffix.lower() in _PROGRAMS:
        raise ToolError(f"{path.name} is a program or script, and I only open documents. "
                        "Ask me to open the app instead.")
    _start(path)
    return f"Opened {path.name if path.is_file() else _label(path)}."


@tool(level=Level.SAFE, timeout=5.0)
def open_folder(folder: str) -> str:
    """Open a folder in File Explorer. Given a file, opens the folder it is in.

    folder: a folder name like downloads or desktop, a path, or a number from the last list
    """
    path = _locate(folder, by_number=True)
    if path.is_file():
        path = path.parent
    _start(path)
    return f"Opened {_label(path)} in File Explorer."


@tool(level=Level.SAFE, timeout=5.0)
def list_folder(folder: str) -> str:
    """List what is in a folder: subfolders first, then files, newest first. The list is numbered for open_file.

    folder: a folder name like downloads or desktop, a path, or a number from the last list
    """
    path = _locate(folder, by_number=True)
    if not path.is_dir():
        raise ToolError(f"{path.name} is a file, not a folder.")
    try:
        with os.scandir(path) as listing:
            entries = [(e, k) for e in listing if _visible(e) and (k := _kind(e))]
    except OSError as exc:
        raise ToolError(f"Couldn't look inside {_label(path)} ({exc.strerror or exc}).") from exc
    if not entries:
        return f"{_label(path)} is empty."
    entries.sort(key=lambda ek: (ek[1] != "dir", -_mtime(ek[0])))
    shown = entries[:LIST_MAX]
    _remember([Path(e.path) for e, _ in shown])
    now = time.time()
    lines = [f"{_label(path)} has {len(entries)} item{'s' if len(entries) != 1 else ''}:"]
    for i, (entry, kind) in enumerate(shown, 1):
        if kind == "dir":
            lines.append(f"{i}. {entry.name} (folder)")
        else:
            st = entry.stat(follow_symlinks=False)
            lines.append(f"{i}. {entry.name} ({_size(st.st_size)}, changed {_age(st.st_mtime, now)})")
    if len(entries) > LIST_MAX:
        lines.append(f"...and {len(entries) - LIST_MAX} more.")
    return "\n".join(lines)


def _decode(data: bytes, cut_short: bool) -> str:
    """UTF-16 (with a BOM), UTF-8, or failing those the old Windows code page."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        if cut_short and exc.end == len(data):  # a character cut in half where the read stopped
            try:
                return data[:exc.start].decode("utf-8-sig")
            except UnicodeDecodeError:
                pass
        return data.decode("cp1252", "replace")


@tool(level=Level.SAFE, timeout=5.0)
def read_text_file(path: str) -> str:
    """Read the start (up to 2000 characters) of a plain text file: txt, md, csv, log or code. Not PDF or Office files.

    path: a number from the last list, a file name, or a path
    """
    file = _locate(path, by_number=True)
    if file.is_dir():
        raise ToolError(f"{file.name} is a folder. Ask me to list it instead.")
    kind = _NOT_YET.get(file.suffix.lower())
    if kind:
        raise ToolError(f"{file.name} is a {kind} file, and so far I can only read plain text "
                        "(txt, md, csv, log or code).")
    try:
        size = file.stat().st_size
        with file.open("rb") as fh:
            data = fh.read(READ_CHARS * 4 + 4)  # enough bytes for READ_CHARS characters of UTF-8
    except OSError as exc:
        raise ToolError(f"Couldn't read {file.name} ({exc.strerror or exc}).") from exc
    if b"\0" in data and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ToolError(f"{file.name} isn't a text file, so I can't read it out.")
    text = _decode(data, cut_short=size > len(data)).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return f"{file.name} is empty."
    cut = len(text) > READ_CHARS or size > len(data)
    text = text[:READ_CHARS]
    note = f"\n[That is only the start of {file.name}, which is {_size(size)}.]" if cut else ""
    return f"{file.name}:\n{text}{note}"


# ---- move, copy, rename, delete, undo -------------------------------------------------------

@dataclass(frozen=True)
class _Change:
    kind: Literal["move", "rename", "copy", "delete"]
    before: Path          # where it was (for a copy: the original)
    after: Path | None    # where it is now (for a copy: the new copy; None after a delete)


def _record(change: _Change) -> None:
    with _LOCK:
        _undo.append(change)
        del _undo[:-UNDO_DEPTH]


def _destination(text: str) -> Path:
    folder = _locate(text)
    if not folder.is_dir():
        raise ToolError(f"{folder.name} is a file, not a folder to put things in.")
    return folder


def _check_target(src: Path, folder: Path, target: Path) -> Path:
    if folder == src or folder.is_relative_to(src):
        raise ToolError(f"I can't put {src.name} inside itself.")
    if os.path.lexists(target):
        raise ToolError(f"There's already a {target.name} in {_label(folder)}, and I won't overwrite it.")
    return _safe(target, change=True)


def _copy_name(src: Path) -> Path:
    """Explorer's naming for a copy in the same folder: 'budget - Copy.xlsx', '... - Copy (2)'."""
    stem, suffix = (src.stem, src.suffix) if src.is_file() else (src.name, "")
    for n in range(1, 100):
        target = src.with_name(f"{stem} - Copy{'' if n == 1 else f' ({n})'}{suffix}")
        if not os.path.lexists(target):
            return target
    raise ToolError(f"There are too many copies of {src.name} already.")


@tool(level=Level.CONFIRM, timeout=60.0)
def move_file(path: str, destination: str) -> str:
    """Move a file or folder into another folder. undo_last can move it back.

    path: the file or folder to move: a path, or a name from the last list
    destination: the folder to move it into, e.g. documents or Documents\\Taxes
    """
    src = _locate(path, change=True)
    folder = _destination(destination)
    if folder == src.parent:
        return f"{src.name} is already in {_label(folder)}."
    target = _check_target(src, folder, folder / src.name)
    try:
        shutil.move(str(src), str(target))
    except OSError as exc:
        raise ToolError(f"Couldn't move {src.name} ({exc.strerror or exc}).") from exc
    _record(_Change("move", src, target))
    return f"Moved {src.name} to {_label(folder)}."


@tool(level=Level.CONFIRM, timeout=120.0)
def copy_file(path: str, destination: str) -> str:
    """Copy a file or folder into another folder. undo_last sends the copy to the Recycle Bin.

    path: the file or folder to copy: a path, or a name from the last list
    destination: the folder to copy it into, e.g. desktop or Documents\\Backup
    """
    src = _locate(path)
    folder = _destination(destination)
    target = _copy_name(src) if folder == src.parent else folder / src.name
    target = _check_target(src, folder, target)
    try:
        if src.is_dir():
            shutil.copytree(src, target, symlinks=True)  # links are copied as links, never followed
        else:
            shutil.copy2(src, target)
    except OSError as exc:
        if os.path.lexists(target):  # a half-made copy: undo can still bin it
            _record(_Change("copy", src, target))
        raise ToolError(f"Couldn't copy {src.name} ({exc.strerror or exc}).") from exc
    _record(_Change("copy", src, target))
    return f"Copied {src.name} to {_label(folder)}" + (f" as {target.name}." if target.name != src.name else ".")


def _new_name(new_name: str, src: Path) -> str:
    """A safe file name; the old extension is kept if none was given ("final" -> "final.docx")."""
    name = new_name.strip().strip("\"'").strip().rstrip(" .")
    if not name or _BAD_NAME.search(name) or _isreserved(name):
        raise ToolError(f"'{new_name}' can't be a file name: no slashes, colons or other special characters.")
    if src.is_file() and src.suffix and not _REAL_SUFFIX.search(name):
        name += src.suffix
    return name


@tool(level=Level.CONFIRM, timeout=10.0)
def rename_file(path: str, new_name: str) -> str:
    """Rename a file or folder where it is. undo_last changes the name back.

    path: the file or folder to rename: a path, or a name from the last list
    new_name: just the new name, no folder; leave off the extension to keep the old one
    """
    src = _locate(path, change=True)
    target = src.with_name(_new_name(new_name, src))
    if str(target) == str(src):
        return f"It's already called {src.name}."
    case_only = os.path.normcase(target) == os.path.normcase(src)
    if not case_only and os.path.lexists(target):
        raise ToolError(f"There's already a {target.name} next to it, and I won't overwrite it.")
    _safe(target, change=True)  # checked, but not replaced: resolving would undo a change of case
    try:
        os.rename(src, target)
    except OSError as exc:
        raise ToolError(f"Couldn't rename {src.name} ({exc.strerror or exc}).") from exc
    _record(_Change("rename", src, target))
    return f"Renamed {src.name} to {target.name}."


@tool(level=Level.CONFIRM, timeout=60.0)
def delete_file(path: str) -> str:
    """Send a file or folder to the Recycle Bin. Nothing is ever deleted permanently.

    path: the file or folder to delete: a path, or a name from the last list
    """
    target = _locate(path, change=True)
    _recycle(target)
    _record(_Change("delete", target, None))
    return f"{target.name} is in the Recycle Bin."


def _reverse(change: _Change) -> str:
    """Undo one change, or raise ToolError if things have moved on since (it's dropped then)."""
    before, after = change.before, change.after
    if change.kind == "delete":
        return (f"{before.name} went to the Recycle Bin, and I can't take things back out of it. "
                "Open the Recycle Bin, right-click it and choose Restore.")
    if after is None or not os.path.lexists(after):
        raise ToolError(f"{after.name if after else before.name} isn't where I left it any more, "
                        "so there's nothing to undo.")
    if change.kind == "copy":
        _recycle(after)  # even a copy goes to the Recycle Bin, never straight to nothing
        return f"Took back the copy of {before.name}: it's in the Recycle Bin."
    same_file = os.path.normcase(before) == os.path.normcase(after)  # a rename that only changed case
    if not same_file and os.path.lexists(before):
        raise ToolError(f"Something called {before.name} is back in {_label(before.parent)}, "
                        "so I left both alone.")
    if not before.parent.is_dir():
        raise ToolError(f"The folder {before.name} came from is gone, so I can't put it back.")
    shutil.move(str(after), str(before))
    if change.kind == "rename":
        return f"Renamed {after.name} back to {before.name}."
    return f"Put {before.name} back in {_label(before.parent)}."


@tool(level=Level.SAFE, timeout=60.0)
def undo_last() -> str:
    """Undo the last file move, copy or rename made since AJ started."""
    with _LOCK:
        change = _undo.pop() if _undo else None
    if change is None:
        return "There's nothing to undo."
    try:
        return _reverse(change)
    except (OSError, _RecycleError) as exc:  # locked or busy: keep it, so undo can be tried again
        with _LOCK:
            _undo.append(change)
        detail = exc if isinstance(exc, ToolError) else f"Couldn't undo that ({exc.strerror or exc})."
        raise ToolError(f"{detail} Try again in a moment.") from exc


# ---- disk_space -----------------------------------------------------------------------------

def _fixed_drives() -> list[str]:
    drives = [d for d in os.listdrives() if _GetDriveTypeW(d) == _DRIVE_FIXED]
    return drives or [HOME.anchor]


@tool(level=Level.SAFE, timeout=5.0)
def disk_space() -> str:
    """How much free space is left on the laptop's drives."""
    lines = []
    for drive in _fixed_drives():
        try:
            usage = shutil.disk_usage(drive)
        except OSError:
            continue
        used = usage.used / usage.total if usage.total else 0
        name = drive.rstrip("\\")
        lines.append(f"{name} has {_size(usage.free)} free of {_size(usage.total)} ({used:.0%} used).")
    if not lines:
        raise ToolError("Windows wouldn't say how full the drives are.")
    return "\n".join(lines)


# ---- fast-path intents ----------------------------------------------------------------------
# Matched in full against the normalized transcript (see intents.py): lowercase, no punctuation,
# and "hey computer", "please", "can you" ... already trimmed off both ends. Only clear commands
# are claimed: fuzzy requests ("open my budget spreadsheet") go to the model, which can search
# first. A list number is turned into the path here, so a confirmation names the exact file.

_MY = r"(?:my |the )?"
_ART = r"(?:my |the |a |an )?"
_KNOWN = r"(?P<f>desktop|documents|downloads|pictures|photos|music|videos)"
_IN = rf"(?: (?:in|on|under) {_MY}{_KNOWN}(?: folder)?)?"
_FIND = r"(?:find|search for|look for|locate|where is|wheres|where did i (?:put|save))"
_KINDS = r"(?P<k>file|files|document|documents|folder|spreadsheet|presentation|pdf)"
_PICK = (r"(?:the )?(?:(?:number|no|result|item|file) )?(?P<n>\d{1,2}|"
         + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))  # "fourth" before "four"
         + r")(?:st|nd|rd|th)?(?: one| result| file| item)?")
# Words that mean the request is about something other than a file name.
_VAGUE = {"this", "that", "these", "those", "it", "me", "some", "any", "something", "anything",
          "near", "nearby", "best", "cheap", "online", "where"}


def _folder_arg(m: re.Match) -> dict:
    return {"folder": _ALIASES[m["f"]]}


def _find_args(m: re.Match) -> dict | None:
    words = _query_words(m["q"])
    if not words or _VAGUE & set(words):
        return None
    query = " ".join(words) + (" pdf" if m.groupdict().get("k") == "pdf" else "")
    folder = m.groupdict().get("f")
    return {"query": query, "folder": _ALIASES[folder] if folder else None}


def _pick(arg: str) -> Callable[[re.Match], dict | None]:
    """A builder for "<verb> number 2": the remembered path, or None (decline) without a list."""
    def build(m: re.Match) -> dict | None:
        index = _index_of(m["n"])
        with _LOCK:
            last = list(_last)
        if index is None or not last:
            return None
        index = len(last) if index == -1 else index
        if not 1 <= index <= len(last):
            return None
        args = {arg: _display(last[index - 1])}
        if m.groupdict().get("f"):
            args["destination"] = _ALIASES[m["f"]]
        return args
    return build


INTENTS: list[tuple[str, str, Callable[[re.Match], dict | None]]] = [
    (r"undo(?: (?:that|it|this|last|the last (?:one|thing|change|move|action)|what you (?:just )?did))?"
     r"|put (?:it|that) back", "undo_last", lambda m: {}),
    (r"(?:how much|whats(?: my)?|check(?: my)?|show(?: me)?(?: my)?) (?:free |disk |storage |hard drive )*"
     r"(?:space|storage)(?: (?:do i have(?: left)?|have i got(?: left)?|is left|left|is free|is there))?"
     r"(?: on (?:my |the )?(?:laptop|computer|pc|disk|drive|hard drive|ssd|c drive))?"
     r"|how full is (?:my |the )?(?:disk|drive|hard drive|ssd|laptop|computer)|(?:disk|storage) space",
     "disk_space", lambda m: {}),
    # open a main folder ("open music" alone is left alone: it may mean a music app)
    (rf"(?:open|bring up|pull up|show me|show) {_MY}(?P<f>desktop|documents|downloads)(?: folder)?",
     "open_folder", _folder_arg),
    (rf"(?:open|bring up|pull up|show me|show) {_MY}{_KNOWN} folder", "open_folder", _folder_arg),
    # list a main folder
    (rf"(?:whats|what is|what do i have|what have i got) (?:in|on) {_MY}{_KNOWN}(?: folder)?",
     "list_folder", _folder_arg),
    (rf"list(?: (?:everything|the files|all the files|the contents|whats))?(?: (?:in|on|of))? {_MY}{_KNOWN}(?: folder)?",
     "list_folder", _folder_arg),
    # find: only with a file word, an explicit folder, or "called ..."
    (rf"{_FIND} {_ART}(?:file|files|document|documents|folder) (?:called|named) (?P<q>.+?){_IN}",
     "find_files", _find_args),
    (rf"{_FIND} {_ART}(?P<q>.+?) {_KINDS}{_IN}", "find_files", _find_args),
    (rf"{_FIND} {_ART}(?P<q>.+?) (?:in|on|under) {_MY}{_KNOWN}(?: folder)?", "find_files", _find_args),
    # a number from the last list
    (rf"open {_PICK}", "open_file", _pick("path_or_index")),
    (rf"(?:read|read me|read out) {_PICK}", "read_text_file", _pick("path")),
    (rf"(?:delete|remove|trash|bin|recycle) {_PICK}", "delete_file", _pick("path")),
    (rf"move {_PICK} (?:in)?to {_MY}{_KNOWN}(?: folder)?", "move_file", _pick("path")),
    (rf"copy {_PICK} (?:in)?to {_MY}{_KNOWN}(?: folder)?", "copy_file", _pick("path")),
]
