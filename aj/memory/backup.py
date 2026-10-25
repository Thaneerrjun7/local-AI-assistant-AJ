"""Daily copies of the memory database, the last seven kept.

Why: everything AJ knows about you is one file. A bad disk sector, a buggy migration or a
mistaken "forget" should cost at most a day. The copy is made with SQLite's online backup API,
which is consistent even while another thread is writing (copying the file by hand while WAL is
active can produce a broken copy). One file per day, so calling this at every launch is cheap
and simply refreshes today's copy. Only old BACKUPS are rotated out; facts never are.
"""
import logging
import os
from datetime import date
from pathlib import Path

from aj.memory.db import Store, get_db

log = logging.getLogger(__name__)

KEEP = 7


def backup(db: Store | None = None, keep: int = KEEP, today: date | None = None) -> Path:
    """Copy the database to <db folder>/backups/aj-YYYYMMDD.db and return that path.

    For the real database the folder is DATA_DIR/backups.
    """
    db = db or get_db()
    folder = db.path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"aj-{(today or date.today()):%Y%m%d}.db"
    partial = target.with_suffix(".db.partial")
    partial.unlink(missing_ok=True)
    db.backup_to(partial)
    os.replace(partial, target)  # a crash mid-copy never leaves a half-written backup behind
    _rotate(folder, keep)
    return target


def _rotate(folder: Path, keep: int) -> None:
    """Delete all but the newest `keep` backups (YYYYMMDD names sort by date)."""
    for old in sorted(folder.glob("aj-????????.db"))[:-keep]:
        try:
            old.unlink()
        except OSError as exc:
            log.warning("memory: could not remove old backup %s: %s", old.name, exc)
