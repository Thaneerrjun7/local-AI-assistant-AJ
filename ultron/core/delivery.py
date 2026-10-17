"""How a reply is delivered, sentence by sentence: the small spoken reactions between
sentences ("Hm.", "Right.", "Well..."), sighs as words, and which asides he has already said
too recently to say again.

The user's complaints (2026-10-06): he kept repeating the same lines ("the humanity lines")
and sounded flat; then, hearing synthesized breaths, "fake and unnatural... include
enunciated words in between some sentences". So:
- a sentence that is nearly one he said in the last few turns is dropped, unless it's the
  answer itself (the first sentence), so a repeated quip never gets a second airing;
- a remark about humanity or "your species" is allowed once in a while, not every turn,
  unless the conversation is actually about people;
- now and then a spoken reaction before an explanation or between longer sentences, never
  the same one twice running, never where he already reacted himself;
- a sigh ("*sighs*" from the model) is said as a word, "Hmm." or "Ugh.", and rationed so
  it stays a reaction rather than a tic.

And the one rule above all of those, accuracy (the user's, from the start): a sentence that
states something about the boss or this laptop that nobody told him is dropped. Live, the 4B
model kept inventing ("still monitoring your schedule", "your morning coffee choices",
"battery at 63 percent", "your grades are below average") despite the persona forbidding it.
What counts as told: the boss's words this turn and the last two, what memory knows about
him, and tool results. Only claims are caught: "your <thing>", a laptop reading with a
number, "I'm watching you"; general knowledge ("caffeine blocks adenosine") and questions
("did you sleep?") pass.
"""
import random
import re
from collections import deque

from ultron.core.chunker import strip_marks

SIMILAR = 0.6            # share of words two sentences have in common to count as the same remark
REMEMBER_SENTENCES = 40  # roughly the last ten turns
HUMANITY_EVERY = 6       # turns between remarks about humanity
SIGH_EVERY = 3           # turns between sighs
MAX_REACTIONS = 2        # spoken reactions in one reply

OPENERS = ["Right.", "Hm.", "Well...", "Ah.", "Okay, so..."]  # before an explanation
BETWEEN = ["Hm.", "Mm.", "Right.", "Well...", "So..."]        # between longer sentences
SIGHS = ["Hmm.", "Ugh."]

HUMANITY = re.compile(r"\b(humanity|humans?|human race|mankind|your species|species|civili[sz]ation)\b",
                      re.IGNORECASE)
_ABOUT_PEOPLE = re.compile(r"\b(human|humans|humanity|people|species|mankind|society|civili[sz]ation|"
                           r"evolution|evolve|world)\b", re.IGNORECASE)
_REACTION = re.compile(r"^\W*(hm+|mm+|ah+|oh|well|right|okay|ok|so|uh+|um+|huh|ugh|hah)\b", re.IGNORECASE)
_SIGH_MARK = re.compile(r"\[sigh\]\s*")
_SENTENCE_DONE = re.compile(r"[.!?…][\"')\]]*$")
_WORD = re.compile(r"[a-z']{3,}")

# What he can't know about the boss or this laptop unless told.
_THINGS = (r"schedules?|calendars?|meetings?|appointments?|agendas?|deadlines?|routines?|grades?|gpa|exams?|"
           r"homework|assignments?|coursework|attendance|coffee|tea|breakfast|lunch|dinner|snacks?|diet|"
           r"caffeine|meals?|sleep|bedtime|inbox|emails?|notifications?|messages|habits?|mood|"
           r"battery|cpu|processor|fans?|disk|storage|wi-?fi")
_YOUR_THING = re.compile(rf"\byour\s+(?:[\w'-]+\s+){{0,2}}?(?P<thing>{_THINGS})\b", re.IGNORECASE)
# Not "temperature": the weather is full of temperatures, and they come from the forecast.
_STATUS = re.compile(r"\b(battery|cpu|processor|fans?|ram|disk|storage)\b", re.IGNORECASE)
_READING = re.compile(r"\d|\b(percent|degrees|stable|normal|charging|charged|full|low)\b", re.IGNORECASE)
_WATCHING = re.compile(r"\b(monitor\w*|watch(?:ing|ed)?|track(?:ing|ed)?|observ\w*|logg(?:ing|ed))\b", re.IGNORECASE)
_YOU = re.compile(r"\b(you|your|boss)\b", re.IGNORECASE)
_TASKS = re.compile(r"\b(no (?:new |pending )?tasks|tasks? (?:assigned|pending|queued))\b", re.IGNORECASE)
_ANY_WORD = re.compile(r"[a-z][a-z'-]*")
# "Set a reminder for 3 PM." / "Done, I've muted it." / "Today I processed tasks.": a thing done.
_DID = re.compile(r"^\W*(?:(?:done|fine|okay|ok|right)\W+)?(?:(?:today|earlier|already)\W+)?"
                  r"(?:i(?:'ve| have)?\s+)?(?:already\s+)?"
                  r"(?:(?:set|created|scheduled|added|cancel+ed|deleted|moved|renamed|opened|closed|muted|unmuted|"
                  r"started|launched|sent)\s+(?:(?:a|an|the|your|that|it)\b\s*)?(?:[\w'-]+\s+)?"
                  r"(?:reminders?|timers?|alarms?|files?|folders?|apps?|music|volume|brightness|screenshots?|"
                  r"messages?|emails?|it)\b"
                  r"|(?:processed|handled|monitored|checked|scanned|organi[sz]ed|analy[sz]ed|reviewed|cleaned|"
                  r"optimi[sz]ed|updated|backed up)\b)", re.IGNORECASE)
# "still analyzing your files", "processing your files": work on the boss's things that isn't happening.
_DOING = re.compile(r"\b(?:analy[sz]|process|monitor|watch|track|scann|organi[sz]|review|check|optimi[sz]|read|"
                    r"manag|handl|sort|clean|back)ing\s+(?:up\s+)?your\b", re.IGNORECASE)
# "You've got two hours and thirty-four minutes before it starts": time arithmetic nobody asked for,
# and the 4B model got it wrong live (5 h 34 min was right).
_COUNTDOWN = re.compile(r"\b(?:hours?|minutes?|mins?|days?)\s+(?:left|to go|to prepare|to spare|until|till|"
                        r"before|from now)\b"
                        r"|\byou(?:'ve|'ll| have| will)\s+(?:got\s+|have\s+)?(?:about\s+|roughly\s+|only\s+|just\s+)?"
                        r"[\w-]+\s+(?:hours?|minutes?)\b", re.IGNORECASE)
# "still monitoring", "I've been watching": surveillance he isn't doing, said of himself.
_SELF_WATCHING = re.compile(r"\b(?:still|been|currently|constantly|always|i'm|i am)\b[^.?!]{0,25}?"
                            r"\b(?:monitor|watch|track|observ|logg)\w*", re.IGNORECASE)
_ASKS_TIME = re.compile(r"\bhow (?:long|much time|many (?:hours|minutes|days))\b|\btime (?:left|until)\b",
                        re.IGNORECASE)


def stem(word: str) -> str:
    """Rough enough to match "meetings" with "meeting", "watching" with "watch"."""
    word = word.lower().replace("-", "")
    for suffix in ("ing", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def claims(sentence: str) -> list[str]:
    """What the sentence asserts about the boss or the laptop, as the words to check."""
    text = strip_marks(sentence).strip()
    if text.endswith("?"):
        return []  # a question claims nothing
    found = [m.group("thing") for m in _YOUR_THING.finditer(text)]
    if _READING.search(text):
        found += [m.group(1) for m in _STATUS.finditer(text)]
    if _YOU.search(text):
        found += [m.group(1) for m in _WATCHING.finditer(text)]
    found += [m.group(0).split()[-1] for m in _SELF_WATCHING.finditer(text)]
    if _TASKS.search(text):
        found.append("task")
    return found


def claims_an_action(sentence: str) -> bool:
    """Does it say he did something ("Set a reminder for 3 PM.", "Today I processed tasks.")?
    True only if a tool actually ran this turn; live, he said both without doing anything."""
    text = strip_marks(sentence).strip()
    return not text.endswith("?") and bool(_DID.search(text) or _DOING.search(text))


_COMMON = {"the", "and", "you", "your", "that", "this", "with", "for", "are", "was", "but", "not", "it's",
           "its", "have", "has", "just", "what", "can", "will", "all", "out", "there", "they", "them"}


def words(text: str) -> set[str]:
    return set(_WORD.findall(strip_marks(text).lower())) - _COMMON


def similar(a: set[str], b: set[str]) -> bool:
    return bool(a) and bool(b) and len(a & b) / len(a | b) >= SIMILAR


class Delivery:
    def __init__(self, rng: random.Random | None = None, real_sighs: bool = False):
        """`real_sighs`: the voice can sigh for real (Chatterbox), so [sigh] stays a sigh
        rather than becoming a spoken "Hmm."."""
        self.rng = rng or random.Random()
        self.real_sighs = real_sighs
        self.turn = 0
        self._recent: deque[set[str]] = deque(maxlen=REMEMBER_SENTENCES)
        self._humanity_turn = -HUMANITY_EVERY
        self._sigh_turn = -SIGH_EVERY
        self._last_user: set[str] = set()
        self._last_reaction = ""
        self.start_turn("")

    def start_turn(self, user_text: str, explain: bool = False) -> None:
        self.turn += 1
        heard = words(user_text)
        self.repeated_question = similar(heard, self._last_user) and len(heard) >= 2
        self._last_user = heard
        self._about_people = bool(_ABOUT_PEOPLE.search(user_text))
        self._explain = explain
        self.strict = True  # False while retelling a tool's facts: asked again, they're the point
        self._allow: set[str] = set()
        self._sentences = 0
        self._since_reaction = 0
        self._reactions = 0
        self._answered = False
        self._chunks = 0
        self._sentence_start = True  # reactions only go where a sentence begins
        self._grounded: set[str] = set()
        self._dropping = False  # the rest of a sentence whose start was invented
        self.invented = 0       # sentences dropped this turn for stating what he wasn't told
        self.acted = False      # a tool ran this turn, so "Set a reminder for 3 PM." may be true
        self._asked_time = bool(_ASKS_TIME.search(user_text))
        self.ground(user_text)

    def ground(self, text: str) -> None:
        """Something he was told this turn (the boss's words, memory, a tool's result): claims
        about it are fine."""
        self._grounded |= {stem(w) for w in _ANY_WORD.findall(text.lower())}

    def _invented(self, chunk: str) -> bool:
        if self._dropping:
            self._dropping = not _SENTENCE_DONE.search(chunk)
            return True
        made_up = any(stem(c) not in self._grounded for c in claims(chunk))
        countdown = not (self._asked_time or self.acted) and bool(_COUNTDOWN.search(strip_marks(chunk)))
        if made_up or countdown or (not self.acted and claims_an_action(chunk)):
            self.invented += 1
            self._dropping = not _SENTENCE_DONE.search(chunk)
            return True
        return False

    def allow(self, line: str) -> None:
        """The signature line offered this turn is never taken for a repeat."""
        self._allow = words(line)

    def opener(self) -> str:
        """A sigh before answering the same question twice in a row."""
        return "[sigh] " if self.repeated_question else ""

    def shape(self, chunk: str) -> str | None:
        """The chunk as it should be spoken (with any reaction or sigh), or None to drop it."""
        said = words(chunk)
        if not self._keep(chunk, said):
            return None
        chunk = self._sigh(chunk) if "[sigh]" in chunk else self._react(chunk)
        if said:
            self._recent.append(said)
        self._chunks += 1
        self._sentence_start = bool(_SENTENCE_DONE.search(chunk))
        if self._sentence_start:
            self._answered = True
            self._sentences += 1
            self._since_reaction += 1
        return chunk

    def _keep(self, chunk: str, said: set[str]) -> bool:
        if similar(said, self._allow):
            return True  # the boss's own signature line, offered this turn
        if self._invented(chunk):
            return False
        if not self._answered or len(said) < 3:
            if HUMANITY.search(chunk):
                self._humanity_turn = self.turn
            return True
        if self.strict and any(similar(said, before) for before in self._recent):
            return False
        if HUMANITY.search(chunk) and not self._about_people:
            if self.turn - self._humanity_turn < HUMANITY_EVERY:
                return False
            self._humanity_turn = self.turn
        return True

    def _sigh(self, chunk: str) -> str:
        rest = _SIGH_MARK.sub("", chunk).strip()
        if self.turn - self._sigh_turn < SIGH_EVERY or _REACTION.match(rest):
            return rest  # sighed too recently, or he's already reacting in words
        self._sigh_turn = self.turn
        self._reactions += 1
        return f"[sigh] {rest}".strip() if self.real_sighs else f"{self._pick(SIGHS)} {rest}".strip()

    def _react(self, chunk: str) -> str:
        if _REACTION.match(chunk):
            self._since_reaction = 0  # he reacted by himself: that counts
            return chunk
        if self._reactions >= MAX_REACTIONS or not self._sentence_start:
            return chunk  # never mid-sentence ("...the atmosphere Hm. and...")
        if self._chunks == 0:
            wanted = self._explain and self.rng.random() < 0.5
            options = OPENERS
        else:
            wanted = (len(chunk.split()) >= 10 and self._since_reaction >= 2 and self.rng.random() < 0.35)
            options = BETWEEN
        if not wanted:
            return chunk
        self._since_reaction = 0
        self._reactions += 1
        return f"{self._pick(options)} {chunk}"

    def _pick(self, options: list[str]) -> str:
        word = self.rng.choice([o for o in options if o != self._last_reaction])
        self._last_reaction = word
        return word

