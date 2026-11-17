"""Sleep mode and the wake words: the assistant can sleep, and wakes when it hears its name.

The user, 2026-10-22: the assistant answers to "AJ" and to "Computer" ("hey AJ or computer, then
it should respond either way"). Asleep, it hides in the tray and answers nothing until an
utterance starts with one of them; "Hey AJ, open WhatsApp" wakes it and runs the command in the
same breath. "Go back to sleep" (and the like) puts it to sleep again. Listening while asleep is
for when the laptop is plugged in only (his words: on battery "this feature should not be
available at all"), which `Gate.hears_wake_words` answers for the pipeline.

Asleep it still hears everything (the VAD and Parakeet keep running, and only work when
someone talks), so the rules below decide from the transcript alone. Three of them keep
ordinary talk in the room from waking it:
- A wake word must START the utterance. "The computer is slow" or "I'm 18 years of age" never
  wake it; nor does "...open WhatsApp, computer" (a name at the end proves nothing).
- After the name there must be the end of the utterance, a word that starts a command ("open",
  "what", "turn"), a question to it ("can you", "are you"), or a pause Parakeet marked (a comma
  or a full stop) that isn't followed by "the", "a" or "and". So "Computer, open WhatsApp" and
  "Computer open WhatsApp" wake it; "Computer science is hard", "Computer's slow", "AJ is
  coming over" and "computer, the phone and a tablet" don't. After "hey", "hi" or "hello" the
  name is clearly being called, so anything may follow.
- "Ajay", "Aja" and "age" are NOT spellings of AJ: Parakeet never wrote them for AJ (measured
  below), and "Ajay said..." is ordinary talk.

How Parakeet writes the names, measured by scripts/check_wake_words.py (2026-10-22: the Piper
voice saying each phrase at three speeds, two noise settings and three pitches, through the
real VAD, as the user's words go; 18 renderings of each phrase):
- "AJ": "AJ", "Aj", "A J", "A.J.", "A Jay"; after "hey" also "Jay" and "J" ("Hey, Jay.",
  "Hey J.", "Hey hey Jay"); now and then "OJ" ("OJ turn off.", "A OJ, open WhatsApp"),
  "AAJ" ("AAJ, Volume 30"), "I J" ("I J Sleep.") and, after "hey", "A G" ("A A G type.").
  Before a command often "Edge" ("Edge go to sleep." 6 times in 18, "Edge turn off."), so
  "Edge" counts only when a command word follows: "Edge is slow" is about the browser.
- "Hey": often "A" ("A AJ.", "A A J open WhatsApp", "A computer."), sometimes dropped.
- "Computer": "Computer". Said fast it came back "Peter", "Puta" or "Beautiful": not accepted.
- "OK": "Okay", "OK", "K".
- Never accepted, though measured: "As I speak.", "I sleep." and "I just sleep." for "AJ, speak."
  and "AJ, sleep." (too ordinary), "Age eight turn off." (age), "Aye.", "Ay." and "A." for
  "Hey A.J." (too short), "RJ" (it came back for "Ajay is coming for dinner").
The voice varies from run to run (Piper's delivery is random), so the numbers move a few
points between runs. Last two runs: wake phrases 87-90% (with the right command left over),
sleep phrases 83-88%, and 47 ordinary sentences woke it 0 times in 846 renderings each run.
Parakeet rarely puts a comma after the name ("Computer, open WhatsApp" came back "Computer open,
what's that?"), which is why a command word after the name counts as much as a comma. And it
drops a leading "a" ("A computer can do it faster" came back "Computer can do it faster" 7
times in 18), which is why "can" alone isn't enough: it takes "can you".

Accepted trade-off: "Hey Jay, how are you?" said to a friend named Jay wakes it too, because
Parakeet writes "Hey AJ" exactly that way about one time in five.

No Qt and no audio here: the pipeline asks about each transcript, and the tests feed it text.
"""
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import IntEnum, StrEnum


class Mode(StrEnum):
    AWAKE = "awake"    # answers what it hears (the behaviour before sleep mode existed)
    ASLEEP = "asleep"  # window in the tray; answers only an utterance that starts with a wake word


# ---- the wake words ----

class _Call(IntEnum):
    """How clearly the words before the name call it; also what a spelling needs before it."""
    NONE = 0     # nothing before the name: "AJ, open WhatsApp", "Computer."
    RUSHED = 1   # "a", "eh": how Parakeet writes a rushed "hey", but usually the article
    OKAY = 2     # "okay", "ok": often only a start ("Okay, computer stuff later")
    HEY = 3      # "hey", "hi", "hello": someone is being called


_GREETINGS = {"hey": _Call.HEY, "hi": _Call.HEY, "hello": _Call.HEY, "hiya": _Call.HEY, "yo": _Call.HEY,
              "oi": _Call.HEY, "ok": _Call.OKAY, "okay": _Call.OKAY, "k": _Call.OKAY,
              "a": _Call.RUSHED, "ay": _Call.RUSHED, "eh": _Call.RUSHED}
_FILLERS = {"uh", "uhh", "um", "umm", "er", "erm", "hmm", "ah", "oh", "so", "and", "well", "yeah", "right",
            "alright", "now"}

# How Parakeet writes each name, and what must come before that spelling (see the module
# docstring): "Jay, pass the salt" is talking to Jay, and "A G" only ever came after a "hey".
# Any other name (Settings.name or an alias) is matched as written, word for word.
SPELLINGS: dict[str, list[tuple[tuple[str, ...], _Call]]] = {
    "aj": [(("aj",), _Call.NONE), (("a", "j"), _Call.NONE), (("a", "jay"), _Call.NONE), (("oj",), _Call.NONE),
           (("o", "j"), _Call.NONE), (("aaj",), _Call.NONE), (("i", "j"), _Call.NONE), (("ij",), _Call.NONE),
           (("edge",), _Call.NONE), (("a", "g"), _Call.RUSHED), (("jay",), _Call.HEY), (("j",), _Call.HEY)],
}
_ONLY_BEFORE_A_COMMAND = {("edge",)}  # a real word: "Edge turn off" wakes it, "Edge." or "Edge, is it" don't
# Spellings that are other words or names too ("OJ" is orange juice). After one of them, without
# a "hey" before it, a polite word or "what" is not a command: "OJ, please" at breakfast, "OJ,
# thanks" and "Edge, please" woke it before the review of 2026-10-22 (scripts/check_wake_words.py
# had "Edge, please." wake it 5 times in 18). "Please" is looked past, so "OJ, please open
# WhatsApp" still wakes it.
_WEAK = {("oj",), ("o", "j"), ("i", "j"), ("ij",), ("aaj",), ("edge",), ("a", "g"), ("jay",), ("j",)}
_SOFT = {"please", "thanks", "thank", "good", "what", "whats", "what's"}

# Words that start a command or a question. One of these straight after the name ("Computer
# open WhatsApp") counts like a comma. A question to it counts when "you", "I" or "we" follows
# ("AJ can you hear me", not "computer can do it faster"). After a rushed "hey" written "a" only
# a command word counts: "a computer, a phone and a tablet" is ordinary talk.
_DOING = set("""
open close launch start stop play pause resume skip next previous mute unmute turn switch set put go
lock unlock take find search look google show tell give read write type speak dictate send message text
call remind check make help say repeat explain define translate calculate convert add create increase
decrease raise lower dim brighten maximize maximise minimize minimise snap move copy rename delete undo
cancel sleep wake shut restart reboot activate deactivate enable disable volume brightness screenshot
weather bring empty please thanks thank goodbye goodnight good
what whats what's who whos who's when where wheres where's why how hows how's which
""".split())
_ASKING = re.compile(r"(?:can|could|would|will|do|does|did|should|shall|may|are|have)\s+(?:you|u|i|we)\b")
_PAUSE_AFTER = re.compile(r"^\s*[,.!?;:]")  # a pause Parakeet marked straight after the name
_LISTING = {"the", "a", "an", "and", "or", "but", "plus"}  # "computer, the phone and a tablet": a list
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_BETWEEN_LETTERS = re.compile(r"^[\s.]*$")  # "a.j.", "a j" and "a. j." are one name
_LOOKS_AHEAD = 5  # greetings and fillers allowed before the name ("uh, okay, hey AJ")


def _spellings(name: str) -> list[tuple[tuple[str, ...], _Call]]:
    words = tuple(_WORD.findall(name.lower()))
    if not words:
        return []
    return SPELLINGS.get("".join(words)) or [(words, _Call.NONE)]


class WakeWords:
    """Finds a wake word at the start of what was heard. `names`: what the assistant answers to
    (Settings.name and Settings.aliases)."""

    def __init__(self, names: Iterable[str] = ("AJ", "Computer")):
        self.names = [n.strip() for n in names if n and n.strip()]
        spellings = [s for name in self.names for s in _spellings(name)]
        self._spellings = sorted(spellings, key=lambda s: -len(s[0]))  # "a jay" before "a"
        self.vocatives = {" ".join(words) for words, need in self._spellings
                          if need == _Call.NONE and words not in _ONLY_BEFORE_A_COMMAND}

    def split(self, transcript: str) -> tuple[bool, str]:
        """(True, the command after the wake word) when the utterance starts with one, the command
        possibly empty ("Hey AJ."); (False, the whole utterance) when it doesn't."""
        text = transcript.strip()
        lower = text.lower().replace("’", "'").replace("‘", "'")
        if len(lower) != len(text):  # a character whose lowercase is longer: positions would drift
            text = lower
        words = [(m.group(), m.start(), m.end()) for m in _WORD.finditer(lower)]
        call = _Call.NONE
        for start in range(min(len(words), _LOOKS_AHEAD + 1)):
            if start:  # the word before this start must be a greeting or a filler
                word = words[start - 1][0]
                if word in _GREETINGS:
                    call = _GREETINGS[word]  # the last one decides: in "hey, a computer..." it's "a"
                elif word not in _FILLERS:
                    break
            found = self._name_at(words, start, lower, call)
            if found is not None:
                end, spelling = found
                rest = text[end:].lstrip(" \t,.!?;:-")
                weak = spelling in _WEAK and call < _Call.HEY
                if spelling in _ONLY_BEFORE_A_COMMAND:
                    called = _hard_command(rest) if weak else _first_word(rest) in _DOING
                else:
                    called = self._called(call, lower[end:], rest, weak)
                if called:
                    return True, rest
        return False, text

    def _name_at(self, words: list[tuple[str, int, int]], start: int, lower: str,
                 call: _Call) -> tuple[int, tuple[str, ...]] | None:
        """Where the name starting at words[start] ends in the text, and how it was spelt."""
        for spelling, need in self._spellings:
            found = words[start:start + len(spelling)]
            if call < need or tuple(w for w, _, _ in found) != spelling:
                continue
            if all(_BETWEEN_LETTERS.match(lower[a[2]:b[1]]) for a, b in zip(found, found[1:])):
                return found[-1][2], spelling
        return None

    @staticmethod
    def _called(call: _Call, after: str, rest: str, weak: bool = False) -> bool:
        """Was the name called, judging by what follows it? `weak`: a spelling that is another
        word too, with no "hey" before it, so a polite word or "what" after it proves nothing."""
        if call == _Call.HEY or not rest:
            return True
        first = _first_word(rest)
        if weak and first in _SOFT:
            return _hard_command(rest)
        if first in _DOING:
            return True
        if call == _Call.RUSHED:
            return False
        return bool(_ASKING.match(rest.lower())) or bool(_PAUSE_AFTER.match(after)) and first not in _LISTING


def _first_word(text: str) -> str:
    found = _WORD.match(text.lower().replace("’", "'"))
    return found.group() if found else ""


def _hard_command(rest: str) -> bool:
    """After a weak spelling: does a real command word follow, past a leading "please"? ("OJ,
    please open WhatsApp" yes; "OJ, please", "OJ, thanks", "OJ, what?" no.)"""
    words = _WORD.findall(rest.lower().replace("’", "'"))
    while words and words[0] == "please":
        words = words[1:]
    return bool(words) and words[0] in _DOING and words[0] not in _SOFT


DEFAULT = WakeWords()


def split_wake(transcript: str, words: WakeWords = DEFAULT) -> tuple[bool, str]:
    """(did it start with a wake word, the command after it). Without a wake word the second
    item is the whole utterance, so it is always what to answer."""
    return words.split(transcript)


# ---- going to sleep ----

# A greeting or goodbye said with it counts too (live, 2026-11-18: "Go to sleep. Hello." went to
# the model, which said "Sleeping well, sir. Goodnight." and didn't sleep).
_POLITE_START = re.compile(
    r"^(?:(?:please|ok|okay|alright|all right|right|so|now|just|and|uh+|um+|hey|then|well|yeah|hello|hi|"
    r"(?:can|could|would|will) you|you can|you may|you should|why dont you|why not|i want you to|"
    r"i need you to|go ahead and|its time to|it is time to|time to)\s+)+")
_POLITE_END = re.compile(
    r"(?:\s+(?:please|now|right now|then|for now|for a bit|for a while|for the night|for tonight|"
    r"again|thanks|thank you|buddy|friend|mate|ok|okay|hello|hi|bye|bye bye|goodbye|good bye|good ?night|"
    r"see you|see you later|see ya|cheers|sir))+$")
_SLEEP = re.compile(
    r"(?:go|get|head)(?: back)?(?: to| into)? sleep(?: mode| mod)?"
    r"|(?:back )?to sleep(?: mode)?"
    r"|(?:the )?sleep(?: mode| mod)?(?: on| now| activated?| engaged| time)?"
    r"|(?:activate|enable|start|enter|engage|initiate|begin|turn on|switch on|switch to|switch into|change to|"
    r"go into|get into|go back into|back into|return to|put yourself (?:in|into|on|to)|put yourself back"
    r"(?: in| into| to)?) (?:the |your )?sleep(?: mode)?"
    r"|(?:take|have|go take|go have) a nap|nap(?: time)?"
    r"|(?:go (?:to |into )?|enter |switch to )?stand ?by(?: mode)?"
    r"|good ?night"
    r"|(?:bed|nap) ?time|go to bed"
    # 2026-11-18, the user: going to sleep means going into the tray, so the tray itself works too.
    r"|(?:go|get|head|move)(?: back)? (?:to|into|in) (?:the |your )?(?:system )?tray"
    r"|hide(?: yourself| away)?|minimi[sz]e yourself")


def _plain(text: str) -> str:
    text = text.lower().replace("’", "").replace("'", "")
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def is_sleep_command(text: str, words: WakeWords = DEFAULT) -> bool:
    """"Go back to sleep", "activate sleep mode", "AJ, sleep", "sleep now, computer": the whole
    utterance must be the command, so "I didn't get any sleep" or "how do I go to sleep faster"
    never are. Putting the LAPTOP to sleep is a different thing (tools/power.py, with a yes): a
    command that names it ("put the laptop to sleep", "sleep the computer") is not this one."""
    _, rest = words.split(text)
    plain = _POLITE_END.sub("", _POLITE_START.sub("", _plain(rest)))
    # "Go to sleep, computer": the name said at the end, with no "the" or "my" before it.
    for vocative in sorted(words.vocatives, key=len, reverse=True):
        if plain.endswith(" " + vocative) and not re.search(r"\b(?:the|my|this|your|that)$",
                                                            plain[:-len(vocative) - 1]):
            plain = _POLITE_END.sub("", plain[:-len(vocative) - 1])
            break
    return bool(_SLEEP.fullmatch(plain))


# ---- the state ----

@dataclass(frozen=True)
class Heard:
    """What to do with one utterance (or one typed line)."""
    action: str     # "answer", "wake" (the name alone: a short line, then listen), "sleep", "ignore"
    text: str = ""  # for "answer": what to answer, the wake word taken off

    ANSWER = "answer"
    WAKE = "wake"
    SLEEP = "sleep"
    IGNORE = "ignore"


class Gate:
    """Awake or asleep, and what each utterance means in that state. The pipeline asks it about
    everything heard before anything is answered; asleep, nothing without a wake word gets past
    it, so talk in the room is never shown, answered, or remembered.

    route() changes the mode as it goes ("Hey AJ" wakes it, "go to sleep" puts it to sleep);
    wake() and sleep() are for the tray, the shortcut and the idle timer. `on_change` is told
    every change of mode (on the caller's thread). `idle_mins` > 0: fall asleep after that long
    with nothing answered (off by default)."""

    def __init__(self, words: WakeWords = DEFAULT, asleep: bool = False, idle_mins: float = 0.0,
                 on_change: Callable[[Mode], None] | None = None, clock: Callable[[], float] = time.monotonic):
        self.words, self.idle_mins, self.on_change, self.clock = words, idle_mins, on_change, clock
        self.mode = Mode.ASLEEP if asleep else Mode.AWAKE
        self._last_active = clock()

    @property
    def asleep(self) -> bool:
        return self.mode == Mode.ASLEEP

    def route(self, text: str, typed: bool = False) -> Heard:
        """`typed`: words typed into the window, which is deliberate enough to wake it without its
        name (the window can only be typed into once it's shown)."""
        woke, rest = self.words.split(text)
        if typed and self.asleep and not is_sleep_command(text, self.words):
            self.wake()
        if self.asleep:
            if not woke:
                return Heard(Heard.IGNORE)
            if is_sleep_command(rest, self.words):  # "AJ, go back to sleep" while asleep: stay asleep
                return Heard(Heard.SLEEP)
            self.wake()
        elif is_sleep_command(text, self.words):
            self.sleep()
            return Heard(Heard.SLEEP)
        self.touch()
        if woke and not rest:
            return Heard(Heard.WAKE)
        return Heard(Heard.ANSWER, rest)

    def only_name(self, text: str) -> bool:
        """"Hey AJ." and nothing else (yet): the command may be on its way after a breath."""
        woke, rest = self.words.split(text)
        return woke and not rest

    def wake(self) -> bool:
        """True if it was asleep."""
        self.touch()
        return self._set(Mode.AWAKE)

    def sleep(self) -> bool:
        """True if it was awake."""
        return self._set(Mode.ASLEEP)

    def touch(self) -> None:
        """Something was answered: the idle timer starts again."""
        self._last_active = self.clock()

    def idle_due(self) -> bool:
        """Awake, and nothing answered for `idle_mins` (never when idle_mins is 0)."""
        return (not self.asleep and self.idle_mins > 0
                and self.clock() - self._last_active >= self.idle_mins * 60)

    @staticmethod
    def hears_wake_words(on_ac: bool | None) -> bool:
        """Listening for the wake words while asleep is for mains power only (the user,
        2026-10-22: on battery "this feature should not be available at all"). `on_ac` is
        core/power_source.on_ac_power(): a machine with no battery at all already reads True,
        so None means Windows couldn't say, and then it doesn't listen: his rule, kept even
        when unsure. The pipeline asks again a few seconds later."""
        return on_ac is True

    def _set(self, mode: Mode) -> bool:
        if mode == self.mode:
            return False
        self.mode = mode
        if self.on_change is not None:
            self.on_change(mode)
        return True
