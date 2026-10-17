"""The conversation log: every turn, plus a short summary per session.

Why keep every turn: facts hold what is true about you; the log holds what happened ("what did I
ask you yesterday?"). Recall searches both. A summary per session gives recall one short,
well-worded item instead of dozens of fragments, and is written at idle on slot 1 so it never
competes with a reply.
"""
import logging
import urllib.error

from ultron.memory import slot
from ultron.memory.db import Store, get_db, now
from ultron.memory.words import strip_notes

log = logging.getLogger(__name__)

MAX_TRANSCRIPT_CHARS = 8000  # ~2,000 tokens: fits slot 1's 4,096 with room for the reply

SUMMARY_SYSTEM = (
    "Summarize this conversation between the user and Ultron, their voice assistant, in two or "
    "three plain sentences. Say what the user asked for, did, or said about their life and plans, "
    "and anything Ultron promised or found out for them. Write about the user in the third person "
    "(\"The user ...\"). Skip greetings and small talk. If nothing of substance happened, write "
    "exactly: Small talk only."
)


def log_turn(session_id: str, role: str, text: str, db: Store | None = None) -> None:
    """Append one turn. role is "user" or "assistant"; empty text is ignored."""
    text = strip_notes(text)
    if not text:
        return
    db = db or get_db()
    with db.tx() as conn:
        conn.execute("INSERT INTO messages(session_id, role, text, at) VALUES (?, ?, ?, ?)",
                     (session_id, role, text, now()))


def transcript(session_id: str, db: Store | None = None) -> str:
    """The session as "User: ..." / "Ultron: ..." lines, newest text kept if it is long."""
    db = db or get_db()
    rows = db.rows("SELECT role, text FROM messages WHERE session_id = ? ORDER BY id", (session_id,))
    lines = [f"{'User' if r['role'] == 'user' else 'Ultron'}: {r['text']}" for r in rows]
    return "\n".join(lines)[-MAX_TRANSCRIPT_CHARS:]


def summarize_session(base_url: str, session_id: str, db: Store | None = None,
                      timeout: float = 120) -> str | None:
    """Write (or rewrite) the session's summary via slot 1 and return it.

    None when the session has fewer than two turns or the model cannot be reached (logged).
    Calling it again later in the same session replaces the summary with a fuller one.
    """
    db = db or get_db()
    turns = db.value("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,))
    if turns < 2:
        return None
    messages = [{"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": transcript(session_id, db)}]
    try:
        summary = slot.complete(base_url, messages, max_tokens=200, timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        log.warning("memory: session summary failed: %s", exc)
        return None
    if not summary:
        return None
    with db.tx() as conn:
        conn.execute("INSERT INTO summaries(session_id, text, at) VALUES (?, ?, ?) ON CONFLICT"
                     "(session_id) DO UPDATE SET text = excluded.text, at = excluded.at",
                     (session_id, summary, now()))
    return summary
