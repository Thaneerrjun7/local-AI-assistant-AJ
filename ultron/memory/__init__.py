"""Ultron's long-term memory: one local SQLite file that is never capped and never auto-pruned.

What the user asked for: memory that "saves locally, remembers things about me, and never
forgets every time I open it". SivaSafe's memory did the opposite by design (2,200-character cap,
380-character values, oldest facts deleted). Here:

db.py         the store at config.DB_PATH (WAL, FTS5 indexes, created on first use)
facts.py      remember / forget / what_do_you_know, every change written to fact_history
recall.py     context_for(user_text): core facts + the few past items this turn is about
extractor.py  extract_facts / learn_in_background: durable facts from a chat, via slot 1
sessions.py   log_turn and summarize_session (slot 1)
backup.py     backup(): daily copy into DATA_DIR/backups, last 7 kept

Every function takes an optional `db`; without it they share one store at DB_PATH.
"""
from ultron.memory.backup import backup
from ultron.memory.db import Store, get_db
from ultron.memory.extractor import extract_facts, learn_in_background
from ultron.memory.facts import forget, remember, what_do_you_know
from ultron.memory.recall import context_for
from ultron.memory.sessions import log_turn, summarize_session

__all__ = ["Store", "get_db", "context_for", "extract_facts", "learn_in_background", "remember",
           "forget", "what_do_you_know", "log_turn", "summarize_session", "backup"]
