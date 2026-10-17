"""Word splitting shared by recall, forget and the extractor's grounding check.

Why one place: recall, "forget X" and the hallucination filter must agree on what counts as a
meaningful word, otherwise a fact could be findable by one and invisible to another. Splitting
on letters and digits only also makes every token safe to quote inside an FTS5 query, so user
speech can never inject FTS syntax.
"""
import re

_WORD = re.compile(r"[^\W_]+")

# Words that carry no topic: they would make every FTS search match everything.
STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be been before being below between
both but by can could did do does doing done down during each else ever few for from further get
got had has have having he her here hers herself him himself his how i if in into is it its itself
just know let like ll me might more most much must my myself no nor not now of off on once only or
other our ours ourselves out over own please re really remember s same say she should so some such
t tell than thank thanks that the their theirs them themselves then there these they thing things
this those through to too ultron under until up us ve very want was we were what whats when where
which while who whom why will with would yeah yes you your yours yourself yourselves m d okay ok
""".split())

# As words() yields them: "I'm" -> "i", "m"; "Im" (speech-to-text often drops the apostrophe) -> "im".
FIRST_PERSON = frozenset("i im ive me my mine myself we our ours us".split())


def words(text: str) -> list[str]:
    """Lowercase letter/digit runs, in order ("I'm 17!" -> ["i", "m", "17"])."""
    return _WORD.findall(text.lower())


def content_words(text: str) -> list[str]:
    """Words that say what the text is about: no stopwords, no single letters, no repeats."""
    out = [w for w in words(text) if w not in STOPWORDS and (len(w) > 1 or w.isdigit())]
    return list(dict.fromkeys(out))


def match_expr(text: str, all_terms: bool = False, limit: int = 12) -> str | None:
    """An FTS5 MATCH expression for `text`, or None when it has no content words.

    any-term (OR) suits recall, where one shared word is a reason to surface something.
    all-terms (AND) suits forget, where "forget my favorite food" must not wipe every favorite_*.
    """
    terms = content_words(text)[:limit]
    if not terms:
        return None
    quoted = [f'"{t}"' for t in terms]
    return " ".join(quoted) if all_terms else " OR ".join(quoted)


_TRAILING_NOTE = re.compile(r"(\s*\[[^\[\]]*\])+\s*$")


def strip_notes(text: str) -> str:
    """Drop trailing "[...]" blocks. The pipeline appends a "[Stay Ultron: ...]" reminder to the
    user's words in chat history; it is not something the user said, so it must never be logged
    as theirs or quoted as evidence of a fact. Speech-to-text never produces square brackets."""
    return _TRAILING_NOTE.sub("", text).strip()


def normalize(text: str) -> str:
    """Lowercase words joined by single spaces, for punctuation-blind substring checks."""
    return " ".join(words(text))
