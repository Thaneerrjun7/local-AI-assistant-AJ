"""The memory database: one SQLite file (WAL) at config.DB_PATH, created on first use.

Why SQLite and not SivaSafe's long_term.json: SivaSafe rewrote one JSON file on every change,
capped it at 2,200 characters, cut each value to 380 and deleted the oldest facts to stay under
the cap, so it forgot things by design. Here nothing has a size cap and nothing is deleted
automatically. Changes and "forget" commands write the old value to fact_history first, so even
a deliberate forget can be undone by hand.

Why one connection behind a lock: the voice thread reads (recall) while a background thread
writes (fact extraction, summaries). A single serialized connection is the simplest correct
option, and every statement here takes well under a millisecond. WAL keeps a crash from
corrupting the file, and synchronous=NORMAL is durable against Ultron crashing or being closed
(only a power cut can lose the last write).
"""
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ultron.config import DB_PATH

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


class Store:
    """One open memory database. Every public method is safe to call from any thread."""

    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
    """The shared store at DB_PATH, opened (and created if needed) on first use."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Store(DB_PATH)
        return _default
