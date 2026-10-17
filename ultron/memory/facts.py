"""Facts about the user: save, change, forget, and say what is known.

Every change goes through _upsert or forget, which write fact_history in the same transaction,
so the record of what Ultron once knew can never drift from what he knows now. Nothing here caps
the number or length of facts: SivaSafe's 380-character cut and oldest-first deletion were the
reason it "forgot" things, and a few thousand short rows cost SQLite nothing.
"""
import logging
import re

from ultron.memory.db import CATEGORIES, RANK, Store, get_db, now
from ultron.memory.words import match_expr

log = logging.getLogger(__name__)


def clean_key(key: str) -> str:
    """snake_case, letters and digits only ("Favourite Food!" -> "favourite_food")."""
    return re.sub(r"[^0-9a-z]+", "_", key.lower()).strip("_")[:64]


def clean_category(category: str) -> str:
    cat = clean_key(category)
    return cat if cat in CATEGORIES else "notes"


def _record(conn, fact_id: int, category: str, key: str, old: str | None, new: str | None,
            reason: str) -> None:
    conn.execute("INSERT INTO fact_history(fact_id, category, key, old_value, new_value, changed_at,"
                 " reason) VALUES (?, ?, ?, ?, ?, ?, ?)", (fact_id, category, key, old, new, now(), reason))


def _upsert(db: Store, category: str, key: str, value: str, confidence: float, source: str) -> str:
    """Insert or change one fact. Returns "added", "updated" or "same"."""
    stamp = now()
    with db.tx() as conn:
        row = conn.execute("SELECT id, value FROM facts WHERE category = ? AND key = ?",
                           (category, key)).fetchone()
        if row is None:
            cur = conn.execute("INSERT INTO facts(category, key, value, confidence, source, created,"
                               " updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (category, key, value, confidence, source, stamp, stamp))
            _record(conn, cur.lastrowid, category, key, None, value, "added")
            outcome = "added"
        elif row["value"].strip().casefold() == value.strip().casefold():
            # Hearing it again is evidence it is still true: refresh recency, keep the old wording.
            conn.execute("UPDATE facts SET updated = ?, confidence = MAX(confidence, ?) WHERE id = ?",
                         (stamp, confidence, row["id"]))
            outcome = "same"
        else:
            conn.execute("UPDATE facts SET value = ?, confidence = ?, source = ?, updated = ? WHERE id = ?",
                         (value, confidence, source, stamp, row["id"]))
            _record(conn, row["id"], category, key, row["value"], value, "updated")
            outcome = "updated"
        db.generation += 1
    return outcome


def remember(facts: list[dict], db: Store | None = None, source: str = "chat",
             confidence: float = 0.8) -> int:
    """Save facts shaped like {"category", "key", "value"} (an optional "confidence" wins over
    the default). Returns how many were new or changed. Bad entries are skipped, never fatal."""
    db = db or get_db()
    changed = 0
    for fact in facts:
        key = clean_key(str(fact.get("key", "")))
        value = str(fact.get("value", "")).strip()
        if not key or not value:
            continue
        category = clean_category(str(fact.get("category", "notes")))
        try:
            conf = float(fact.get("confidence", confidence))
        except (TypeError, ValueError):
            conf = confidence
        if _upsert(db, category, key, value, conf, source) != "same":
            changed += 1
            log.info("memory: %s.%s saved", category, key)
    return changed


def _matching(db: Store, query: str) -> list:
    expr = match_expr(query, all_terms=True)
    if expr is None:
        return []
    return db.rows("SELECT f.id, f.category, f.key, f.value FROM facts_fts JOIN facts f"
                   " ON f.id = facts_fts.rowid WHERE facts_fts MATCH ?", (expr,))


def forget(query: str, db: Store | None = None) -> str:
    """Delete the facts that match every content word of `query` and say what went.

    The deleted values stay in fact_history (reason "forgotten"), so a misheard command can be
    undone by hand; they just stop reaching the model.
    """
    db = db or get_db()
    query = re.sub(r"^\s*(please\s+)?forget\s+(about\s+)?", "", query, flags=re.I).strip(" .!?")
    if match_expr(query) is None:
        return "Tell me what to forget."
    found = _matching(db, query)
    if not found:
        return f"I don't have anything about {re.sub(r'\bmy\b', 'your', query, flags=re.I)}."
    with db.tx() as conn:
        for row in found:
            conn.execute("DELETE FROM facts WHERE id = ?", (row["id"],))
            _record(conn, row["id"], row["category"], row["key"], row["value"], None, "forgotten")
        db.generation += 1
    labels = ", ".join(spoken_key(r["key"]) for r in found[:5])
    more = f" and {len(found) - 5} more" if len(found) > 5 else ""
    return f"Forgotten: {labels}{more}."


def spoken_key(key: str) -> str:
    return key.replace("_", " ")


def ranked_facts(db: Store, limit: int = -1):
    """Facts, most important first: identity, then preferences, ..., newest first within each.
    limit -1 means all of them. The ORDER BY matches the facts_rank index, so no sort is needed."""
    return db.rows(f"SELECT id, category, key, value FROM facts ORDER BY {RANK}, updated DESC,"
                   " id DESC LIMIT ?", (limit,))


def what_do_you_know(db: Store | None = None, limit: int = 8) -> str:
    """A short spoken answer to "what do you know about me": the count plus the top facts."""
    db = db or get_db()
    count = db.value("SELECT COUNT(*) FROM facts")
    if not count:
        return "Nothing yet. Tell me about yourself and I'll remember it."
    parts = "; ".join(f"your {spoken_key(f['key'])} is {f['value']}" for f in ranked_facts(db, limit))
    return f"I know {count} thing{'s' if count != 1 else ''} about you. {parts[0].upper()}{parts[1:]}."
