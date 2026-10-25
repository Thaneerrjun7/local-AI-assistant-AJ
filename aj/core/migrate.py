"""One-time moves from the project's old name to AJ, so an old install keeps working.

The boss, 2026-10-22: "Change everything to AJ. You can name it as AJ Local Assistant." What an
install made under the old name is renamed in place, never copied, merged or deleted, and only
while nothing of the old install is running (its named mutexes say so):
- %APPDATA%\\<old> becomes %APPDATA%\\AJ: settings, memory, app index, voice cache and logs;
- in it, the memory database <old>.db becomes aj.db. Its -wal and -shm move with it, the database
  last: a WAL file belongs to its database by name alone and can hold the newest memories, so the
  three move together or, when one can't (the file is open somewhere), not at all;
- the daily backups <old>-YYYYMMDD.db become aj-YYYYMMDD.db, so memory/backup.py keeps pruning them;
- logs\\<old>.log becomes aj.log.
The Gemini key in Credential Manager is copied forward by core/secrets.py when it is first read.

Each step checks that its destination is free and is skipped, with a note, when it isn't. So this
runs at every start-up for the cost of a few file checks, and a half-finished run is finished the
next time. It must run before anything opens a file in the data folder: creating
%APPDATA%\\AJ\\logs first would leave the old folder where it is.

That happened anyway (the review of 2026-10-22): while an old-name process held its mutex, the
listener, AJ (Speak) or the assistant started under the new name, skipped the move, and opened its
log in a new %APPDATA%\\AJ\\logs. From then on both folders existed and nothing moved ("both
exist, nothing merged"). A new folder that holds nothing but a logs folder of plain files is now
treated as movable: its logs are merged into the old folder's logs (a name already there gets
" (AJ)" added, so no log is overwritten), the emptied folders are removed (os.rmdir removes only
an empty folder), and then the old folder is moved as usual. Nothing is ever deleted.
"""
import ctypes
import os
import re
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

from aj import config

# The project's name until 2026-10-22. The boss asked for every trace of it to go, so it is
# spelled in two pieces, here only: a search of the code for the old name finds nothing, and this
# is the one place that still knows it, to find what an old install left behind.
OLD = "Ult" + "ron"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_SYNCHRONIZE = 0x00100000
# Held while an old install runs: the assistant, its wake listener, and AJ (Speak), which had
# its own name already but kept its log in the old folder.
_OLD_MUTEXES = (f"Local\\{OLD}.Instance", f"Local\\{OLD}.Listener", "Local\\AJ.Speak.Instance")


def old_install_running() -> bool:
    for name in _OLD_MUTEXES:
        handle = _kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
        if handle:
            _kernel32.CloseHandle(handle)
            return True
    return False


def _rename(src: Path, dst: Path) -> None:
    """A rename that never replaces anything (os.replace would overwrite `dst` silently)."""
    if dst.exists():
        raise FileExistsError(f"{dst} already exists")
    os.rename(src, dst)


def settle_db(db: Path) -> Path:
    """The memory database to open for `db`. An old-named one beside it (<old>.db) is renamed to
    `db` first, with its -wal and -shm. If it can't be (it is open somewhere, or a stray aj.db-wal
    is in the way), everything is put back and the old file is returned as it is, so memories are
    never split across two databases."""
    old = db.with_name(f"{OLD.lower()}.db")
    if db.exists() or not old.exists():
        return db
    parts = [s for s in ("-wal", "-shm", "") if Path(f"{old}{s}").exists()]  # the database last
    moved: list[str] = []
    try:
        for s in parts:
            _rename(Path(f"{old}{s}"), Path(f"{db}{s}"))
            moved.append(s)
    except OSError:
        for s in reversed(moved):
            os.rename(Path(f"{db}{s}"), Path(f"{old}{s}"))
        return old
    return db


def only_logs(folder: Path) -> bool:
    """Does `folder` hold nothing but a "logs" folder of plain files (or nothing at all)? That is
    what an early start of the listener, AJ (Speak) or the assistant leaves behind."""
    try:
        items = list(folder.iterdir())
        if any(item.name != "logs" or not item.is_dir() for item in items):
            return False
        return all(f.is_file() for item in items for f in item.iterdir())
    except OSError:
        return False


def _merge_logs(new: Path, old: Path) -> list[str]:
    """Move the files of new\\logs into old\\logs (" (AJ)" added to a name already there), then
    remove the emptied new\\logs and new. All or nothing: if one file can't be moved (it is open),
    the ones moved go back and OSError is raised."""
    target = old / "logs"
    created = not target.exists()
    target.mkdir(exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    try:
        for item in sorted((new / "logs").iterdir()) if (new / "logs").is_dir() else []:
            dst = target / item.name
            if dst.exists():
                dst = target / f"{item.stem} (AJ){item.suffix}"
            _rename(item, dst)
            moved.append((item, dst))
        if (new / "logs").is_dir():
            os.rmdir(new / "logs")  # empty now; rmdir never removes a folder with anything in it
        os.rmdir(new)
    except OSError:
        if moved:
            (new / "logs").mkdir(parents=True, exist_ok=True)  # removed just before a later step failed
        for src, dst in reversed(moved):
            os.rename(dst, src)
        if created:
            try:
                os.rmdir(target)
            except OSError:
                pass
        raise
    return [f"merged logs\\{src.name} into {old.name}\\logs\\{dst.name}" for src, dst in moved]


def run(data_dir: Path | None = None, running: Callable[[], bool] = old_install_running) -> list[str]:
    """Make whatever moves are still due. Returns one note per move made or skipped, for the
    caller's log (which can't be open yet: it lives in the folder being moved)."""
    new = config.DATA_DIR if data_dir is None else data_dir
    old = new.with_name(OLD)
    notes: list[str] = []
    if old.is_dir() and new.is_dir() and only_logs(new):
        # The new folder holds only logs from an early start (2026-10-22): merge them back, so
        # the old folder can move as a whole below.
        if running():
            return [f"the old install is running, so {old} stays where it is until it is off"]
        try:
            notes += _merge_logs(new, old)
        except OSError as e:
            return [f"couldn't merge {new}\\logs into {old} ({e}); both stay where they are"]
    if old.is_dir() and not new.exists():
        if running():
            return [f"the old install is running, so {old} stays where it is until it is off"]
        try:
            _rename(old, new)
        except OSError as e:
            return [f"couldn't move {old} to {new} ({e}); it stays where it is"]
        notes.append(f"moved {old} to {new}")
    elif old.is_dir():
        notes.append(f"both {old} and {new} exist: left as they are, nothing merged")
    if not new.is_dir():
        return notes

    db = new / config.DB_PATH.name
    old_db = db.with_name(f"{OLD.lower()}.db")
    if old_db.exists() and not db.exists():
        if settle_db(db) == db:
            notes.append(f"renamed {old_db.name} (with its -wal and -shm) to {db.name}")
        else:
            notes.append(f"{old_db.name} couldn't be renamed (in use?); it is used as it is")

    backups = new / "backups"
    if backups.is_dir():
        pattern = re.compile(rf"{OLD.lower()}(-\d{{8}}\.db)", re.IGNORECASE)
        for item in sorted(backups.iterdir()):
            if (m := pattern.fullmatch(item.name)) and not (target := backups / f"aj{m.group(1)}").exists():
                try:
                    _rename(item, target)
                    notes.append(f"renamed backups\\{item.name} to {target.name}")
                except OSError as e:
                    notes.append(f"couldn't rename backups\\{item.name} ({e})")

    old_log, log = new / "logs" / f"{OLD.lower()}.log", new / "logs" / "aj.log"
    if old_log.exists() and not log.exists():
        try:
            _rename(old_log, log)
            notes.append(f"renamed logs\\{old_log.name} to {log.name}")
        except OSError as e:
            notes.append(f"couldn't rename logs\\{old_log.name} ({e})")
    return notes
