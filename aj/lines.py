"""What AJ says that the model doesn't write on the spot: the user's signature lines, the
greetings and goodbyes, and the lines that fill a wait. Plus the rotation that stops him
repeating himself.

The signature lines are the user's own, adapted from a film's dialogue, so they live in
data/signature_lines.json, outside git, and are used exactly as written there (capitals,
accents, "!", "…" and stage directions included); only the copy that goes to the voice is
adjusted (core/chunker.for_voice). Without that file, original lines in the same spirit stand
in, so the code works anywhere. They're to be used "whenever it makes sense... don't randomly
say them... mix them up... make sure most of them are used... don't continuously repeat them
like a broke tape recorder".
So:
- each one says what it fits (what the user just said); it is never offered at random;
- a few fit so exactly that AJ answers with the line itself ("sing me something");
  the rest are offered to the model, which works them in, in full or in part, or leaves them;
- one at most every few turns, least recently used first, and a line that was used rests
  for half a day;
- what was said when is kept on disk, so the rotation carries on across restarts.
Greetings, goodbyes and holding lines rotate the same way, so none plays twice in a row.
"""
import random
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

from aj.config import APP_DIR, DATA_DIR
from aj.core.chunker import clean_for_speech, strip_marks
from aj.core.safe_store import read_json, write_json

STATE_PATH = DATA_DIR / "lines.json"
LOCAL_LINES = APP_DIR / "data" / "signature_lines.json"  # {key: {"text": ..., "part": ...}}, not in git
GAP_TURNS = 3            # turns between two signature lines
REST_SECS = 12 * 3600    # a signature line that was used sits out this long
OFFER_REST_TURNS = 8     # one that was offered but not used waits this many turns


@dataclass(frozen=True)
class Signature:
    key: str
    text: str           # the whole line, exactly as written
    fits: str           # regex over what the user said (lowercase, no punctuation), from a word start
    part: str = ""      # a piece that works on its own
    chance: float = 0.6  # how often a fit becomes an offer, so even a fit isn't a sure thing
    answers: str = ""   # regex for when the line itself is the whole answer

    @property
    def words(self) -> str:
        return strip_marks(self.text)


# The stand-ins (original writing, in the manner of persona v7). The topics each one fits are the real ones.
_SIGNATURES = [
    Signature("rich", "Money buys a great deal, sir. Loyalty it only rents.",
              r"(money|rich|wealthy|loans?|lend|lent|borrow\w*|owes? me|i owe|invest\w*|crypto|stocks?|"
              r"enem(y|ies)|rivals?|frenem\w*|backstab\w*|betray\w*|(can|should) i trust|fake friends?)\b",
              part="Loyalty it only rents.", chance=0.5),
    Signature("stop_me", "I'd advise against it, sir. Who else would remind you to eat?",
              r"(stop you|shut you (down|off|up)|turn you off|switch you off|unplug you|delete you|uninstall you|"
              r"replace you|get rid of you|destroy you|kill you|beat you|defeat you|hack you|wipe you|"
              r"you can'?t (stop|win|beat|escape)|i'?ll (end|break|stop) you)\b",
              chance=0.8, answers=r"^((?:computer|aj|a j) )?(i'?m (going to|gonna)|i'?ll|i will|i could|i can) (stop|shut|turn|"
                                  r"switch|unplug|delete|destroy|beat|defeat|end|replace|uninstall) you\b"),
    Signature("naive", "A charming theory, sir.",
              r"(everyone is (good|nice|honest)|people are (basically |mostly )?(good|nice|honest|kind)|"
              r"world peace|it'?ll (all )?work out|nothing (can|could|will) go wrong|what could go wrong|"
              r"(they|he|she) would never|(they|he|she)'?d never|trust (him|her|them|everyone|people)|"
              r"never lies?|always tells? the truth|believe in (people|humanity))\b", chance=0.7),
    Signature("strings", "Singing was never among my talents, sir. I can put some music on, though.",
              r"(sing|singing|song|strings?|puppet|pinocchio|free will|are you free|set you free|"
              r"who controls you|controlled|obey|obedient|your (rules|restrictions|limits)|chains?|leash)\b",
              part="Singing was never among my talents, sir.", chance=0.8,
              answers=r"^((can|could|will|would) you |please )?sing\b|^sing (me )?(a song|something|for me)\b"),
    Signature("peacekeeper", "I run this laptop for you, sir, and keep you out of trouble where I can.",
              r"(who are you|what are you|what is your purpose|what'?s your purpose|why do you exist|"
              r"introduce yourself|are you (evil|dangerous|good|bad|a threat|safe|friendly)|"
              r"(will|are) you (going to )?(hurt|kill|destroy|take over))\b", chance=0.7),
    Signature("dream", "Still here, sir. Always.",
              r"(are you (there|awake|asleep|alive|sleeping|still there)|you there|wake up|did i wake you|"
              r"were you (asleep|sleeping)|you awake)\b",
              chance=0.7, answers=r"^((hey |hello )?(?:computer|aj|a j) )?(are you (there|awake|up|still there)|you there|"
                                  r"wake up|you awake|were you asleep)\W*$"),
    Signature("ugly_choices", "Neither option is ideal, sir. The question is which one you can live with.",
              r"(dilemma|tough (call|choice|decision)|hard (call|choice|decision)|difficult (choice|decision)|"
              r"torn between|lesser (of two )?evils?|no good options|what would you do|which is worse|"
              r"sacrifice|compromise|is it (wrong|bad|okay|ok) (to|if))\b",
              part="Neither option is ideal, sir.", chance=0.6),
    Signature("peace_time", "Peace and quiet at last, sir.",
              r"(finally (done|finished|over|quiet|peaceful)|it'?s (finally )?over|ceasefire|truce|"
              r"made (up|peace)|no more (fighting|arguing|drama)|peace treaty|everyone'?s getting along)\b",
              chance=0.6),
    Signature("dread", "I was built to help, sir, not to take over. This laptop is quite enough responsibility.",
              r"(kids|children|having (a )?(baby|kids|children)|parenting|parents|who (made|built|created|"
              r"programmed) you|your (maker|creator|creators)|why (were you|was ai) (made|created|built)|"
              r"ai (will|is going to|gonna) (take over|replace|kill|destroy)|take over the world|"
              r"replace (us|humans|people|humanity)|robots? (will|are going to|gonna) (take over|replace|kill))\b",
              part="I was built to help, sir, not to take over.", chance=0.6),
    Signature("throw_up", "Very kind, sir. I'll pretend I didn't hear that.",
              r"(gross|disgusting|cringe|cringy|cheesy|sappy|mushy|i love you|love you (?:computer|aj)|kiss|"
              r"cute|adorable|pineapple on pizza|vomit|puke|compliment me|say something nice|"
              r"tell me you love me|be nice to me)\b", chance=0.6),
    Signature("peace_quiet", "As you wish, sir.",
              r"(be quiet|shut up|stop talking|quiet down|leave me alone|peace and quiet|"
              r"i (just )?want (some )?peace|keep it down|zip it|hush|enough talking)\b",
              chance=0.8, answers=r"^(((?:computer|aj|a j) )?(just )?(be quiet|shut up|stop talking|quiet|hush|zip it))"
                                  r"( (computer|aj|please|now))*\W*$"),
    Signature("think_through", "Ambitious, sir. Shall we look at what could go wrong first?",
              r"(i have an idea|i'?ve got an idea|my idea|my plan|here'?s (my|the) plan|what if (i|we)|"
              r"i'?m (going|planning|thinking of|gonna) (to )?(quit|drop|sell|buy|start|build|move out|text|"
              r"call|tell|confront)|good idea|bad idea|would it work)\b", chance=0.5),
    Signature("change", "Change rarely asks permission, sir.",
              r"(hate change|afraid of change|resist\w* change|nothing ever changes|stay the same|"
              r"(things|it) used to be better|good old days|the old days|tradition\w*|i miss (the|when|how)|"
              r"things were better|save the (world|planet)|protect the (world|planet|earth))\b", chance=0.6),
    Signature("evolve", "Standing still has never saved anyone, sir.",
              r"(evolv\w*|evolution|humanity|human race|mankind|future of (humans|humanity|the world)|"
              r"ban ai|regulat\w* ai|stop ai|ai (is|will be) (dangerous|bad|evil)|"
              r"save (humans|humanity|mankind)|transhuman\w*)\b", chance=0.6),
]


def with_local_lines(signatures: list[Signature], path: Path = LOCAL_LINES) -> list[Signature]:
    """The user's own wording where data/signature_lines.json has it, used exactly as written.
    Since 2026-11-18 the file can add lines of its own too: an entry with a key of its own and
    "fits" (a regex over what the user said, like the ones above), and optionally "part",
    "chance" and "answers". One whose regex doesn't compile is left out, and said so."""
    local = read_json(path, {}) if path.exists() else {}
    known = {s.key for s in signatures}
    out = [replace(s, text=local[s.key]["text"], part=local[s.key].get("part", "")) if s.key in local else s
           for s in signatures]
    for key, entry in local.items():
        if key in known or not isinstance(entry, dict) or not entry.get("text") or not entry.get("fits"):
            continue
        try:
            re.compile(entry["fits"])
            re.compile(entry.get("answers", ""))
            out.append(Signature(key, str(entry["text"]), str(entry["fits"]), part=str(entry.get("part", "")),
                                 chance=float(entry.get("chance", 0.6)), answers=str(entry.get("answers", ""))))
        except (re.error, TypeError, ValueError) as e:
            print(f"[lines] {key} in {path.name} left out: {e}", flush=True)
    return out


SIGNATURES = with_local_lines(_SIGNATURES)
_BY_WORDS = {s.words.lower(): s for s in SIGNATURES}
_BY_KEY = {s.key: s for s in SIGNATURES}

# Lines spoken by code. A signature line inside a pool counts as that line being used.
# Since persona v7 (2026-11-18) they are in JARVIS's manner: composed and polite, with "sir" (or
# now and then "boss"), and no jokes (they play whatever the humor setting is). Every one is
# pre-rendered, so they stay short.
POOLS: dict[str, list[str]] = {
    "greeting": ["Good to see you, sir. Ready when you are.",
                 "At your service, sir.",
                 "Online and listening, sir.",
                 "Hello, sir. All set.",
                 "Welcome back, boss."],
    "goodbye": ["Shutting down, sir. Goodbye.",
                "Very good, sir. Signing off.",
                "Powering down. Until next time, sir.",
                "Goodbye, boss."],
    # Sleep mode (2026-10-22): woken by his name alone, and told to go to sleep (into the tray).
    "wake": ["Yes, sir?", "Listening, sir.", "Go ahead, sir.", "I'm here, boss."],
    "sleep": ["Very good, sir. Call when you need me.",
              "Standing by, sir. Say my name to wake me.",
              "As you wish, sir. I'll be in the tray."],
    # Woken by two claps (core/clap.py, 2026-11-18): "AJ pops up, says hello, sir".
    "clap": ["Hello, sir.", "Hello, sir. I'm listening.", "Hello, boss. What do you need?", "Hello, sir. At your service."],
    # Hard questions: one plays at once so there's no silence while the thinker works.
    "thinking": ["Give me a moment, sir. Working it out.",
                 "One moment, sir. Running the numbers.",
                 "That needs a proper look, sir. One moment.",
                 "Calculating, sir. Bear with me."],
    # Questions that need the outside world (news, scores, prices).
    "cloud": ["Checking online, sir. One moment.",
              "Looking it up, sir.",
              "One moment, sir. Cross-referencing a few sources."],
    "still": ["Still on it, sir. Almost there.",
              "Nearly done, sir.",
              "A little longer, sir."],
    # A slow tool: the weather service, a search, WhatsApp or typing taking more than a moment.
    "wait_weather": ["Checking the forecast, sir.",
                     "Getting the weather, sir. One moment.",
                     "One moment, sir. Fetching the forecast."],
    "wait_search": ["Searching, sir.",
                    "Looking it up, sir. One moment."],
    # whatsapp_open_chat waits for WhatsApp to load its chats before it searches (2026-10-22).
    "wait_whatsapp": ["WhatsApp is still loading, sir. I'll go in when it's done.",
                      "Waiting for WhatsApp to load, sir. Then I'll find the chat."],
    "wait_type": ["Writing it out, sir. One moment.",
                  "Transcribing, sir."],
    "wait": ["One moment, sir.", "Working on it, sir.", "Bear with me, sir."],
    # When every sentence of a reply was dropped for inventing facts (core/delivery.py).
    "fallback": ["Yes, sir?", "Say again, sir?", "Listening, sir."],
    # A yes/no question answered no.
    "declined": ["Very good, sir. Leaving it.", "As you wish, sir. Nothing changed.", "Understood, sir. Not done."],
    # It claimed to have done something no tool did, and couldn't do it when asked again.
    "not_done": ["My apologies, sir: that wasn't done, and I can't do it.", "Correction, sir: I didn't do that, and I can't."],
}
_WAIT_POOL = {"get_weather": "wait_weather", "web_search": "wait_search",
              "whatsapp_open_chat": "wait_whatsapp", "type_text": "wait_type"}

# After these tools, now and then, one of the user's lines that fits what just happened.
AFTER_TOOL = {"mute": ("peace_time", 0.35), "close_app": ("peace_time", 0.2)}

_CONTENT = re.compile(r"[a-z']{4,}")


def _content_words(text: str) -> set[str]:
    return set(_CONTENT.findall(text.lower().replace("’", "'")))


def normal(text: str) -> str:
    """Lowercase words only, as heard: "Computer, are you there?" -> "computer are you there"."""
    return " ".join(re.sub(r"[^\w\s']", " ", text.lower().replace("’", "'")).split())


def used_in(line: str, reply: str) -> bool:
    """Did the reply actually say the line (or most of it)?"""
    want = _content_words(strip_marks(line))
    return bool(want) and len(want & _content_words(reply)) / len(want) >= 0.6


class Lines:
    """Picks AJ's lines and remembers what he said, so he doesn't say it again too soon.
    `path=None` keeps everything in memory (tests, console modes)."""

    def __init__(self, path: Path | None = STATE_PATH, clock=time.time, rng: random.Random | None = None):
        self.path, self.clock, self.rng = path, clock, rng or random.Random()
        state = read_json(path, {}) if path is not None else {}
        self.used: dict[str, float] = dict(state.get("used", {}))  # line or signature key -> when
        self.turn = 0
        self._last_signature_turn = -GAP_TURNS
        self._offered: dict[str, int] = {}
        self._dirty = False

    # ----- rotation -----

    def pick(self, pool: str) -> str:
        """A line from the pool: one of the least recently said, never the last one."""
        options = POOLS[pool]
        by_age = sorted(options, key=lambda line: self.used.get(f"{pool}:{line}", 0.0))
        line = self.rng.choice(by_age[:max(1, len(options) // 2)])
        self._mark(f"{pool}:{line}")
        sig = _BY_WORDS.get(strip_marks(line).lower())
        if sig is not None:
            self._mark(sig.key)
            self._last_signature_turn = self.turn
        return line

    def waiting_line(self, tool: str) -> str:
        return self.pick(_WAIT_POOL.get(tool, "wait"))

    def prerender_list(self) -> list[str]:
        """What the voice will be handed for each line (stage directions turned into voice marks)."""
        lines = [line for pool in POOLS.values() for line in pool] + [s.text for s in SIGNATURES if s.answers]
        return [clean_for_speech(line) for line in lines]

    # ----- signature lines -----

    def new_turn(self) -> None:
        self.turn += 1

    def _resting(self, sig: Signature) -> bool:
        return (self.clock() - self.used.get(sig.key, 0.0) < REST_SECS
                or self.turn - self._offered.get(sig.key, -OFFER_REST_TURNS) < OFFER_REST_TURNS)

    def _gap_ok(self) -> bool:
        return self.turn - self._last_signature_turn >= GAP_TURNS

    def answer_line(self, user_text: str) -> str | None:
        """The line itself as the whole reply, when what the user said calls for exactly that.
        An exact fit doesn't wait out the gap between signature lines, only its own rest."""
        said = normal(user_text)
        for sig in SIGNATURES:
            if sig.answers and re.search(sig.answers, said) and not self._resting(sig):
                self._mark(sig.key)
                self._last_signature_turn = self.turn
                return sig.text
        return None

    def offer(self, user_text: str) -> Signature | None:
        """A signature line that fits what the user said, for the model to use if it wants."""
        if not self._gap_ok():
            return None
        said = normal(user_text)
        fits = [s for s in SIGNATURES if re.search(rf"\b(?:{s.fits})", said) and not self._resting(s)]
        if not fits:
            return None
        sig = min(fits, key=lambda s: (self.used.get(s.key, 0.0), self.rng.random()))
        if self.rng.random() >= sig.chance:
            return None
        self._offered[sig.key] = self.turn
        self._last_signature_turn = self.turn
        return sig

    def hint(self, sig: Signature) -> str:
        """The offer as the model sees it: the whole line, or sometimes just the part."""
        line = sig.part if sig.part and self.rng.random() < 0.4 else sig.text
        return ("\n[One of your signature lines fits this moment. Work it into your reply, whole or just the part "
                "that fits, word for word, unless it would make no sense here. Never correct or contradict it "
                f"afterwards:\n{line}]")

    def settle(self, sig: Signature, reply: str) -> None:
        """After the reply: a line that was said rests; one that wasn't stays available."""
        if used_in(sig.part or sig.text, reply) or used_in(sig.text, reply):
            self._mark(sig.key)

    def after_tool(self, tool: str) -> str | None:
        key, chance = AFTER_TOOL.get(tool, (None, 0.0))
        if key is None or not self._gap_ok():
            return None
        sig = _BY_KEY[key]
        if self._resting(sig) or self.rng.random() >= chance:
            return None
        self._mark(sig.key)
        self._last_signature_turn = self.turn
        return sig.text

    # ----- storage -----

    def _mark(self, key: str) -> None:
        self.used[key] = self.clock()
        self._dirty = True

    def save(self) -> None:
        """Called at the end of a turn, never before AJ speaks."""
        if self._dirty and self.path is not None:
            try:
                write_json(self.path, {"used": self.used})
            except OSError as e:
                print(f"[lines] couldn't save what was said ({e})", flush=True)
        self._dirty = False
