"""What the model is told about the user on each turn.

context_for(user_text) returns two blocks, in this order on purpose:
1. Core facts: who the user is and what they like (identity and preferences), filled up to ~2,400
   characters (~600 tokens). This block only changes when a fact changes, so placed right after
   the persona prefix it stays in llama-server's prompt cache from turn to turn.
2. Three to five things that match this turn's words (FTS5, bm25): other facts, past session
   summaries, and older things the user said.

Plans, people, projects and notes are only ever recalled by a match (2026-10-19). In the core
block, sent with every turn, "meet Dad for dinner tonight at 8" made the model bring the dinner
up in answer to "open WhatsApp", "is it true?" and "relax". And only the user's own past words
are recalled, never AJ's: his made-up "battery 17 percent" from one session came back as
"the 17 percent you mentioned earlier" in the next.

The budget limits what is SENT, never what is KEPT: a fact that does not fit in the core block is
still in the database and still comes back the moment the conversation touches it. That is the
difference from SivaSafe, whose 2,200-character limit deleted facts to stay under it.

Why search one word at a time: an OR query must score every row that contains any of its words,
so one common word ("music") makes it slower as the log grows (2.6 ms for 2,000 matching rows,
so ~30 ms a year in). Each word instead fetches only its newest PER_TERM matches, then the scores
are summed per row, which is exactly how bm25 combines words. Rare, telling words still reach
their oldest matches; common words cost a fixed amount. Recall stays well under 10 ms at any size.
"""
import sqlite3
from datetime import datetime

from aj.memory.db import Store, get_db
from aj.memory.facts import ranked_facts, spoken_key
from aj.memory.words import content_words

CORE_BUDGET = 2400
ITEM_CHARS = 300      # display cut for one recalled item; the stored text is never cut
SKIP_RECENT = 12      # the newest messages are already in the chat history the model sees
PER_TERM = 50         # newest matches fetched per search word
MAX_TERMS = 12
_MAX_CORE_ROWS = 400  # more one-line facts than 2,400 characters can ever hold
CORE_CATEGORIES = ("identity", "preferences")  # true every turn; the rest only when the turn is about them


def context_for(user_text: str, db: Store | None = None, budget: int = CORE_BUDGET,
                matches: int = 5) -> str:
    """Memory text for the system prompt, or "" when nothing is known yet."""
    db = db or get_db()
    core_ids, core = _core_block(db, budget)
    related = _related(db, user_text, core_ids, matches)
    parts = []
    if core:
        parts.append("What you know about the user:\n" + core)
    if related:
        parts.append("Possibly relevant from earlier:\n" + "\n".join(related))
    return "\n\n".join(parts)


def _core_block(db: Store, budget: int) -> tuple[frozenset[int], str]:
    """Cached until a fact changes, so the common turn costs a dictionary lookup."""
    key = (db.generation, budget)  # read before the query, so a racing write forces a rebuild
    if db.cache.get("core", (None,))[0] == key:
        return db.cache["core"][1]
    ids, lines, used = [], [], 0
    for row in ranked_facts(db, _MAX_CORE_ROWS):
        if row["category"] not in CORE_CATEGORIES:
            continue
        line = f"- {spoken_key(row['key'])}: {row['value']}"
        if used + len(line) + 1 > budget:
            continue  # a long one is skipped, not cut; shorter ones after it may still fit
        ids.append(row["id"])
        lines.append(line)
        used += len(line) + 1
    result = (frozenset(ids), "\n".join(lines))
    db.cache["core"] = (key, result)
    return result


def _related(db: Store, user_text: str, skip_ids: frozenset[int], limit: int) -> list[str]:
    terms = content_words(user_text)[:MAX_TERMS]
    if not terms:
        return []
    try:
        found = (_facts(db, terms, skip_ids)[:3] + _summaries(db, terms)[:2]
                 + _messages(db, terms)[:3])
    except sqlite3.OperationalError:
        return []  # a search problem must never cost the user an answer
    return [_clip(item) for item in found[:limit]]


def search(db: Store, table: str, terms: list[str], max_rowid: int = 2**62,
           skip: frozenset[int] = frozenset(), top: int = 3) -> list[int]:
    """Rowids of `table` best matching any of `terms`, best first (summed per-word bm25)."""
    scores: dict[int, float] = {}
    for term in terms:
        rows = db.rows(f"SELECT rowid, rank FROM {table}_fts WHERE {table}_fts MATCH ?"
                       " AND rowid <= ? ORDER BY rowid DESC LIMIT ?",
                       (f'"{term}"', max_rowid, PER_TERM + len(skip)))
        for rowid, rank in rows:
            if rowid not in skip:
                scores[rowid] = scores.get(rowid, 0.0) + rank  # rank is -bm25: lower is better
    return sorted(scores, key=lambda r: (scores[r], -r))[:top]


def _facts(db: Store, terms: list[str], skip_ids: frozenset[int]) -> list[str]:
    out = []
    for rowid in search(db, "facts", terms, skip=skip_ids):
        row = db.rows("SELECT key, value FROM facts WHERE id = ?", (rowid,))
        if row:
            out.append(f"- {spoken_key(row[0]['key'])}: {row[0]['value']}")
    return out


def _summaries(db: Store, terms: list[str]) -> list[str]:
    out = []
    for rowid in search(db, "summaries", terms, top=2):
        row = db.rows("SELECT text, at FROM summaries WHERE rowid = ?", (rowid,))
        if row:
            out.append(f"- conversation on {_day(row[0]['at'])}: {row[0]['text']}")
    return out


def _messages(db: Store, terms: list[str]) -> list[str]:
    newest = db.value("SELECT COALESCE(MAX(id), 0) FROM messages")
    if newest <= SKIP_RECENT:
        return []
    expr = " OR ".join(f'"{t}"' for t in terms)
    out = []
    for rowid in search(db, "messages", terms, max_rowid=newest - SKIP_RECENT):
        row = db.rows("SELECT m.role, m.at, snippet(messages_fts, 0, '', '', '...', 24) AS bit"
                      " FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
                      " WHERE messages_fts MATCH ? AND messages_fts.rowid = ? AND m.role = 'user'", (expr, rowid))
        if row:
            out.append(f"- the user said on {_day(row[0]['at'])}: {row[0]['bit']}")
    return out


def _day(stamp: str) -> str:
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    return f"{when:%b} {when.day}"


def _clip(text: str) -> str:
    return text if len(text) <= ITEM_CHARS else text[:ITEM_CHARS - 3].rstrip() + "..."
