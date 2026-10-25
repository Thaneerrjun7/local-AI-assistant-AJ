"""Background fact extraction: after AJ replies, pick out what the user said about their life.

Phase 1 measured Qwen3-4B with a loose prompt and the JSON grammar: 100% valid JSON and 78% recall,
but it invented a fact in 14 of 14 chats that contained none. A memory that fills up with things
you never said is worse than one that forgets, so this module defends in four layers:

1. Skip the model entirely when no user line speaks in the first person ("open Chrome", "what
   time is it"). Facts about your life almost always come with I/my/me/we.
2. A tight prompt with worked examples where the right answer is {"facts": []}.
3. The grammar makes the model copy the user's exact words ("quote") BEFORE it may write a fact,
   so every fact is anchored to something actually said.
4. Code checks the anchor: the quote must appear in the USER's lines (not AJ's), the value
   must share a word with them, and passing moods are dropped. Anything that fails is discarded.

Runs on llama-server slot 1 (see slot.py) from a daemon thread, so it never delays speech.
"""
import json
import logging
import threading
import urllib.error

from aj.memory import slot
from aj.memory.db import CATEGORIES, Store
from aj.memory.facts import clean_key, remember
from aj.memory.words import FIRST_PERSON, content_words, normalize, strip_notes, words

log = logging.getLogger(__name__)

MAX_CHAT_CHARS = 6000  # slot 1 has 4,096 tokens (8,192 split over 2 slots); keep the newest text

SYSTEM = """You save long-term facts about the user from a short chat between the user and AJ, their voice assistant.

Save a fact ONLY when the user plainly states it about themselves or about the people, pets, places or things in their life, AND it will still be true next month. Durable facts: name, age, birthday, city, school, job, family, friends, partner, pets, likes and dislikes, favorites, hobbies, projects they are working on, plans for a specific time, birthdays of people they know.

Never save:
- requests or commands ("open Chrome", "play some music", "set a timer")
- questions, including questions about the user ("what's my name?")
- small talk, greetings, thanks, jokes, reactions ("lol", "nice", "that's funny")
- passing moods or states ("I'm tired", "I'm hungry", "I'm bored today")
- anything AJ says, and anything about AJ
- guesses or conclusions: if the user did not say it, it is not a fact

Most chats contain no durable facts. Then the correct answer is exactly {"facts": []}. An empty answer is normal and good; inventing a fact is the worst mistake you can make.

For each fact write:
- quote: the user's exact words that state the fact, copied character for character
- category: identity (name, age, birthday, city, school, job), preferences (likes, dislikes, favorites, hobbies), relationships (people and pets in their life), projects (things they are building or working on), plans (upcoming events and goals), notes (any other durable fact)
- key: a short snake_case label, specific enough that different facts get different keys: name, age, birthday, city, school, job, favorite_food, favorite_game, best_friend, sister, mom_birthday, pet_dog, project_robot_arm, plan_trip_to_japan
- value: the fact itself in a few of the user's words: "Toronto", "ramen", "Kabir", "a beagle named Coco".
"""

SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "maxItems": 6, "items": {
        "type": "object",
        "properties": {  # quote comes first so the grammar makes the model anchor before it claims
            "quote": {"type": "string", "maxLength": 240},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "key": {"type": "string", "maxLength": 48},
            "value": {"type": "string", "maxLength": 160}},
        "required": ["quote", "category", "key", "value"],
        "additionalProperties": False}}},
    "required": ["facts"],
    "additionalProperties": False,
}

# Worked examples, as earlier turns of the same task. Two of four are empty on purpose.
EXAMPLES = [
    ("User: Open Chrome please\nAJ: Done.\nUser: What's the weather like tomorrow?\n"
     "AJ: Rain, twelve degrees.", []),
    ("User: Ugh I'm so tired today lol\nAJ: Then sleep.\nUser: Play something chill\n"
     "AJ: Playing lo-fi.", []),
    ("User: Do you remember what my sister's name is?\nAJ: You never told me.\n"
     "User: Haha fair. What time is it?\nAJ: Ten past nine.", []),
    ("User: My sister Ananya just started at the University of Waterloo.\nAJ: Noted.\n"
     "User: Also I could eat ramen every single day.\nAJ: Concerning, but noted.",
     [{"quote": "My sister Ananya just started at the University of Waterloo",
       "category": "relationships", "key": "sister", "value": "Ananya, at the University of Waterloo"},
      {"quote": "I could eat ramen every single day", "category": "preferences",
       "key": "favorite_food", "value": "ramen"}]),
]

# Keys that only ever hold a passing state; the prompt forbids them, this enforces it.
TRANSIENT_KEYS = frozenset({"mood", "feeling", "current_mood", "current_feeling", "state", "status",
                            "energy", "energy_level", "current_state", "current_activity",
                            "activity", "request", "question", "command", "greeting"})


def render(turns: list) -> tuple[str, str]:
    """(the chat as "User: ..." / "AJ: ..." lines, the user's words alone)."""
    lines, said = [], []
    for turn in turns:
        role, text = _role_text(turn)
        if not text:
            continue
        if role == "user":
            said.append(text)
        lines.append(f"{'User' if role == 'user' else 'AJ'}: {text}")
    chat = "\n".join(lines)
    return chat[-MAX_CHAT_CHARS:], "\n".join(said)


def _role_text(turn) -> tuple[str, str]:
    if isinstance(turn, dict):
        text = turn.get("content", turn.get("text", ""))
        return str(turn.get("role", "")), strip_notes(str(text or ""))
    role, text = turn
    return str(role), strip_notes(str(text or ""))


def extract_facts(base_url: str, turns: list, timeout: float = 60) -> list[dict]:
    """Durable facts the user stated in `turns`, as [{"category", "key", "value"}].

    turns: [{"role": "user" | "assistant", "content": str}, ...] (or (role, text) pairs),
    usually just the latest exchange. Returns [] when there is nothing, or when the model
    cannot be reached (logged): a missed fact is recoverable, a crash in the background is not.
    """
    chat, said = render(turns)
    if not FIRST_PERSON.intersection(words(said)):
        return []
    messages = [{"role": "system", "content": SYSTEM}]
    for example_chat, example_facts in EXAMPLES:
        messages += [{"role": "user", "content": example_chat},
                     {"role": "assistant", "content": json.dumps({"facts": example_facts})}]
    messages.append({"role": "user", "content": chat})
    try:
        text = slot.complete(base_url, messages, max_tokens=400, schema=SCHEMA, timeout=timeout)
        found = json.loads(text).get("facts", [])
    except (urllib.error.URLError, OSError, ValueError, KeyError, AttributeError) as exc:
        log.warning("memory: fact extraction failed: %s", exc)
        return []
    return grounded(found if isinstance(found, list) else [], said)


def grounded(facts: list, said: str) -> list[dict]:
    """Only the facts whose quote and value really come from the user's own words."""
    said_norm = normalize(said)
    said_words = {_stem(w) for w in words(said)}
    out, seen = [], set()
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        key = clean_key(str(fact.get("key", "")))
        value = str(fact.get("value", "")).strip()
        category = str(fact.get("category", ""))
        if not key or not value or key in TRANSIENT_KEYS or (category, key) in seen:
            continue
        if not _quote_ok(str(fact.get("quote", "")), said_norm, said_words):
            log.info("memory: dropped %s (quote not in the user's words)", key)
            continue
        if not any(_stem(w) in said_words for w in content_words(value)):
            log.info("memory: dropped %s (value not in the user's words)", key)
            continue
        seen.add((category, key))
        out.append({"category": category, "key": key, "value": value})
    return out


def _quote_ok(quote: str, said_norm: str, said_words: set[str]) -> bool:
    quote_norm = normalize(quote)
    if not quote_norm:
        return False
    if quote_norm in said_norm:
        return True
    # Small copying slips ("I am" for "I'm") are fine; a quote mostly of new words is not.
    qwords = quote_norm.split()
    return len(qwords) >= 3 and sum(_stem(w) in said_words for w in qwords) / len(qwords) >= 0.75


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def learn_in_background(base_url: str, turns: list, db: Store | None = None) -> threading.Thread:
    """Extract and save facts from `turns` on a daemon thread; call it after AJ replies."""
    snapshot = list(turns)  # the caller's history keeps changing while this runs

    def work() -> None:
        try:
            facts = extract_facts(base_url, snapshot)
            if facts:
                remember(facts, db)
        except Exception:
            log.exception("memory: background learning failed")

    thread = threading.Thread(target=work, name="memory-learn", daemon=True)
    thread.start()
    return thread
