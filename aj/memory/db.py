"""The memory database: one SQLite file (WAL) at config.DB_PATH, created on first use.

Why SQLite and not SivaSafe's long_term.json: SivaSafe rewrote one JSON file on every change,
capped it at 2,200 characters, cut each value to 380 and deleted the oldest facts to stay under
the cap, so it forgot things by design. Here nothing has a size cap and nothing is deleted
automatically. Changes and "forget" commands write the old value to fact_history first, so even
a deliberate forget can be undone by hand.

Why one connection behind a lock: the voice thread reads (recall) while a background thread
writes (fact extraction, summaries). A single serialized connection is the simplest correct
option, and every statement here takes well under a millisecond. WAL keeps a crash from
corrupting the file, and synchronous=NORMAL is durable against AJ crashing or being closed
(only a power cut can lose the last write).

A write-ahead file (aj.db-wal) belongs to its database by name alone: SQLite replays whatever
aj.db-wal holds into aj.db when it opens it, without checking that the two belong together. On
2026-10-22 a stray aj.db-wal holding a brand-new database (24 pages: the schema and one turn, of
a session started 15:37:01) was found next to the real aj.db (37 pages, 224 messages). Opened,
it would have written the empty database's first page over the real one's. So Store refuses
(UnsafeDatabase), never deleting anything, when a -wal sits beside a main file that is missing or
empty (SQLite would delete it, or build on it), or when the -wal's last commit describes a
database smaller than the main file already is (it can't be that file's newer state: AJ never
shrinks the database). Memory is then off, and the log says which file to look at.
"""
import sqlite3
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from aj.config import DB_PATH
from aj.core.migrate import settle_db

# Order matters: recall and what_do_you_know put earlier categories first.
CATEGORIES = ("identity", "preferences", "relationships", "projects", "plans", "notes")
# Category priority as SQL. The facts_rank index is built on this exact expression, so ranking
# thousands of facts is an index walk instead of a sort (7.6 ms -> well under 1 ms at 5,000).
RANK = "(CASE category " + " ".join(f"WHEN '{c}' THEN {i}" for i, c in enumerate(CATEGORIES)) + " ELSE 99 END)"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS facts(
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'chat',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    UNIQUE(category, key)
);
CREATE INDEX IF NOT EXISTS facts_rank ON facts({RANK}, updated DESC, id DESC);
CREATE TABLE IF NOT EXISTS fact_history(
    id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fact_history_fact ON fact_history(fact_id);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS summaries(
    session_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    at TEXT NOT NULL
);
"""

# External-content FTS5 indexes, kept in sync by triggers so no code path can forget to.
_FTS = {
    "facts": ("id", ["category", "key", "value"]),
    "messages": ("id", ["text"]),
    "summaries": ("rowid", ["text"]),
}
TOKENIZER = "porter unicode61 remove_diacritics 2"  # porter: "birthdays" finds "birthday"


def _fts_sql(table: str, rowid: str, cols: list[str]) -> str:
    fts = f"{table}_fts"
    names = ", ".join(cols)
    new = ", ".join(f"new.{c}" for c in cols)
    old = ", ".join(f"old.{c}" for c in cols)
    return f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5({names}, content='{table}',
    content_rowid='{rowid}', tokenize='{TOKENIZER}');
CREATE TRIGGER IF NOT EXISTS {table}_ai AFTER INSERT ON {table} BEGIN
    INSERT INTO {fts}(rowid, {names}) VALUES (new.{rowid}, {new});
END;
CREATE TRIGGER IF NOT EXISTS {table}_ad AFTER DELETE ON {table} BEGIN
    INSERT INTO {fts}({fts}, rowid, {names}) VALUES ('delete', old.{rowid}, {old});
END;
CREATE TRIGGER IF NOT EXISTS {table}_au AFTER UPDATE ON {table} BEGIN
    INSERT INTO {fts}({fts}, rowid, {names}) VALUES ('delete', old.{rowid}, {old});
    INSERT INTO {fts}(rowid, {names}) VALUES (new.{rowid}, {new});
END;
"""


def now() -> str:
    """Local time to the second, ISO 8601: sorts correctly as text and reads well in a DB viewer."""
    return datetime.now().isoformat(timespec="seconds")


class UnsafeDatabase(RuntimeError):
    """The database and its write-ahead file don't belong together; nothing was opened."""


_WAL_MAGIC = (0x377F0682, 0x377F0683)
_WAL_HEADER, _FRAME_HEADER = 32, 24


def wal_commit_pages(wal: Path) -> int | None:
    """How many pages the database has after the last commit in this write-ahead file, or None
    when it holds no commit (empty, or not a WAL at all). Frames from before the WAL was last
    restarted carry other salts and are skipped, as SQLite skips them."""
    with open(wal, "rb") as fh:
        header = fh.read(_WAL_HEADER)
        if len(header) < _WAL_HEADER:
            return None
        magic, _version, page_size, _seq, salt1, salt2 = struct.unpack(">6I", header[:24])
        if magic not in _WAL_MAGIC or page_size < 512 or page_size & (page_size - 1):
            return None
        last = None
        while True:
            frame = fh.read(_FRAME_HEADER)
            if len(frame) < _FRAME_HEADER:
                return last
            _page, commit_size, s1, s2 = struct.unpack(">4I", frame[:16])
            if (s1, s2) != (salt1, salt2):
                return last
            if commit_size:
                last = commit_size
            fh.seek(page_size, 1)


def check_wal(path: Path) -> str:
    """Why `path` and the -wal beside it must not be opened together, or "" when they may."""
    wal = Path(f"{path}-wal")
    if not wal.exists() or wal.stat().st_size == 0:
        return ""
    if not path.exists() or path.stat().st_size == 0:
        return (f"{wal.name} is there but {path.name} is missing or empty, so the write-ahead file would be "
                f"lost or taken for a new database")
    pages = wal_commit_pages(wal)
    if pages is None:
        return ""  # no commit in it: SQLite ignores it
    with open(path, "rb") as fh:
        header = fh.read(100)
    page_size = int.from_bytes(header[16:18], "big") if len(header) >= 18 else 0
    page_size = 65536 if page_size == 1 else page_size
    if page_size and pages < path.stat().st_size // page_size:
        return (f"{wal.name} describes a database of {pages} pages, smaller than {path.name} "
                f"({path.stat().st_size // page_size} pages): it isn't that database's write-ahead file")
    return ""


class Store:
    """One open memory database. Every public method is safe to call from any thread."""

    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        unsafe = check_wal(self.path)
        if unsafe:  # never delete or move it here: the user decides what it is
            raise UnsafeDatabase(f"memory is off: {unsafe}. Move {self.path.name}-wal aside to start memory again.")
        self._lock = threading.RLock()
        self.generation = 0  # bumped on every fact change
        self.cache: dict = {}  # data derived from facts (recall's core block), keyed by generation
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None,
                                     timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            script = SCHEMA + "".join(_fts_sql(t, r, c) for t, (r, c) in _FTS.items())
            self._conn.executescript(script)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: all of it lands, or none of it does."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def value(self, sql: str, params: tuple = ()):
        """First column of the first row, or None."""
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def backup_to(self, target: Path) -> None:
        """Consistent copy through SQLite's online backup API (safe while other threads write)."""
        dst = sqlite3.connect(target)
        try:
            with self._lock:
                self._conn.backup(dst)
        finally:
            dst.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_default: Store | None = None
_default_lock = threading.Lock()


def get_db() -> Store:
    """The shared store at DB_PATH, opened (and created if needed) on first use. A database still
    under the project's old name is renamed to DB_PATH first (core/migrate.py); if it can't be,
    it is opened where it is rather than starting an empty one beside it."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Store(settle_db(DB_PATH))
        return _default
