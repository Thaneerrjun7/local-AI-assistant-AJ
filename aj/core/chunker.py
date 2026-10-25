"""Turn a stream of model text into speakable chunks.

The first chunk is cut as early as it can sound natural (at a comma once there are a
few words, or before an "and" or "because" in a long first sentence), because it decides how
soon AJ makes a sound: the voice takes ~0.45 s for a short line and ~1 s for twenty words.
Later chunks are whole sentences, which sound better and keep the voice ahead of playback.

Text can carry marks for the voice: [sigh], [chuckle], [pause], and [slow] (the rest of that
line a little slower). AJ's lines add them; the model's own stage directions ("*sighs*",
"(chuckles)") are turned into them, and any other ("[singing]", "*breathes*") is dropped
rather than read out. Breaths were tried and dropped: synthesized, they sounded fake (user,
2026-10-06); spoken reactions ("Hm.", "Right.") do that job now (core/delivery.py).
"""
import re
import unicodedata

_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s")
_CLAUSE_END = re.compile(r"[,;:—–]\s")
_JOINT = re.compile(r"\s(?=(?:and|but|because|which|while|since|although|though|unless|until|when)\s)")
JOINT_MIN_WORDS = 6
_NOT_SPOKEN = re.compile(r"[*#_`~>|]|[\U0001F300-\U0001FAFF☀-➿]")

MARK = re.compile(r"\[(sigh|chuckle|pause|slow)\]\s*", re.IGNORECASE)
_DIRECTION = r"[\*\(\[]\s*(?:{})\b[^\*\)\]]{{0,24}}[\*\)\]]"
_STAGE = [(re.compile(_DIRECTION.format(r"sigh|sighs|sighing|exhales?"), re.IGNORECASE), "[sigh] "),
          (re.compile(_DIRECTION.format(r"chuckles?|chuckling|laughs?|laughing|snickers?|heh"), re.IGNORECASE),
           "[chuckle] "),
          (re.compile(_DIRECTION.format(r"breath|breathes|inhales?|takes a breath|deep breath"), re.IGNORECASE), ""),
          # Kokoro can't sing: a sung line is delivered slower, which is the closest it gets.
          (re.compile(_DIRECTION.format(r"sings?|singing"), re.IGNORECASE), "[slow] "),
          (re.compile(r"[\*\(]\s*pauses?\b[^\*\)]{0,24}[\*\)]", re.IGNORECASE), "[pause] "),
          (re.compile(r"\[(?!(?:sigh|chuckle|pause|slow)\])[^\]]{1,30}\]", re.IGNORECASE), "")]

# Symbols the voice would skip or mangle, written out the way a person says them. Curly quotes
# become straight ones (the lexicon knows "it's", not "it’s"); a dash between numbers is "to"
# ("5–10 minutes") and between words a comma.
_SPOKEN_FORMS = [(re.compile("[‘’]"), "'"), (re.compile("[“”]"), '"'),
                 (re.compile(r"(?<=\d)\s*[—–]\s*(?=\d)"), " to "), (re.compile(r"\s*[—–]\s*(?=\S)"), ", "),
                 (re.compile(r"\s*°\s*C\b"), " degrees Celsius"), (re.compile(r"\s*°\s*F\b"), " degrees Fahrenheit"),
                 (re.compile(r"\s*°"), " degrees"), (re.compile(r"(\d)\s*%"), r"\1 percent")]

# A spoken "..." should be a real pause: Kokoro barely pauses on it (measured 2026-10-06), so
# the voice renders the words either side separately with a gap between.
_TRAIL_OFF = re.compile(r"(?<=\.\.\.)\s+|(?<=…)\s+")
# Three or more capitalised words in a row (apostrophes and punctuation between them allowed).
_SHOUTING = re.compile(r"\b[A-Z][A-Z']*[A-Z]?(?:[\s,.?!;:…-]+[A-Z][A-Z']*){2,}\b")


def clean_for_speech(text: str) -> str:
    for pattern, replacement in _STAGE:
        text = pattern.sub(replacement, text)
    for pattern, spoken in _SPOKEN_FORMS:
        text = pattern.sub(spoken, text)
    return re.sub(r"\s+", " ", _NOT_SPOKEN.sub("", text)).strip()


# Said as words, for the voice only (what's shown stays as written). Checked by having the voice
# say them and speech-to-text write them down (scripts/check_pronunciation.py, 2026-10-21): the
# Piper voice turned "USB" into "UMISB", "RAM" into "our AN", "C: 120 GB" into "See 120, GB3"
# and "$799" into "7.99".
# Two vowels and a "d" in the northern voice (the boss, 2026-10-22: "good" sounds like "guv",
# "RAM" like "rum"). Its espeak spelling is right (gˈʊd, ɹˈæm); the voice's own sounds aren't:
# - a "good" that ends a phrase loses its "d" (no release), so it trails into a "v": speech-to-
#   text wrote "Good. Opening it now." as "Gov opening it now"/"Go of opening it now" 11 times in
#   20 and a lone "Good." as "Cove", "Cold", "Code". With "goodd" (two d's: a longer hold, then a
#   release) phrase-final, it heard "good" 240 times in 240 over 12 phrasings (145 in 160 before);
#   noise, timing and speed settings didn't help (39-42 in 60). Mid-phrase it was already clear.
# - its "a" in "ram" is the northern open one (measured F1 690, F2 1160 Hz), about where American
#   ears put "rum" (F1 620, F2 1200), and speech-to-text wrote "rum", "run", "room" or "ROM" 30
#   times in 160. "rairm" (ɹˈeəm, the American "ram" before an m) moves it forward (F2 1560) and
#   cut those to 11, but speech-to-text, which knows northern voices, then writes "RAM" less
#   often (95 in 160, against 118; the rest "REM", "rare"); "rem" was heard as "REM" nearly
#   always. The boss's ear decides: samples of each are in data/pronunciation_samples.
_SAY_AS = [(re.compile(p, flags), r) for p, r, flags in [
    (r"\b(g)ood(?=[\"')\]]*(?:[.,!?;:…]|$))", r"\1oodd", re.IGNORECASE),
    (r"\be\.g\.,?", "for example", re.IGNORECASE), (r"\bi\.e\.,?", "that is", re.IGNORECASE),
    (r"\betc\.", "et cetera", re.IGNORECASE), (r"\bvs\.?(?=\s)", "versus", re.IGNORECASE),
    (r"\s&\s", " and ", 0), (r"\bMr\.", "Mister", 0), (r"\bMrs\.", "Missus", 0), (r"\bDr\.", "Doctor", 0),
    (r"\$(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)", r"\1 dollars", 0),
    (r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?USD\b", r"\1 dollars", 0),
    (r"(?:(?<=\s)|^)([A-Z]):(?=\s)", r"\1 drive", 0),
    (r"(?<=\d)\s?TB\b", " terabytes", 0), (r"(?<=\d)\s?GB\b", " gigabytes", 0), (r"(?<=\d)\s?MB\b", " megabytes", 0),
    (r"(?<=\d)\s?KB\b", " kilobytes", 0), (r"(?<=\d)\s?GHz\b", " gigahertz", 0), (r"(?<=\d)\s?MHz\b", " megahertz", 0),
    (r"(?<=\d)\s?kg\b", " kilograms", 0), (r"(?<=\d)\s?km/h\b", " kilometres per hour", 0),
    (r"(?<=\d)\s?km\b", " kilometres", 0), (r"(?<=\d)\s?cm\b", " centimetres", 0), (r"(?<=\d)\s?mm\b", " millimetres", 0),
    (r"(?<=\d)\s?ms\b", " milliseconds", 0), (r"(?<=\d)\s?mph\b", " miles per hour", 0),
    (r"\bWi-?Fi\b", "wifi", re.IGNORECASE), (r"\bRAM\b", "rairm", 0), (r"\bOK\b", "okay", 0),
    (r"\bUSB\b", "U S B", 0), (r"\bCPU\b", "C P U", 0), (r"\bGPU\b", "G P U", 0), (r"\bSSD\b", "S S D", 0),
    (r"\bHDD\b", "H D D", 0), (r"\bVPN\b", "V P N", 0), (r"\bURL\b", "U R L", 0), (r"\bPDF\b", "P D F", 0),
    (r"\bPC\b", "P C", 0), (r"\bTV\b", "T V", 0), (r"\bAI\b", "A.I.", 0), (r"\bAM\b", "A M", 0), (r"\bPM\b", "P M", 0),
]]


def for_voice(text: str) -> str:
    """The spelling the voice needs, without changing what anyone reads. Its dictionary has no
    accented words ("naïve" -> "naive"), and it reads a run of capitals differently from the
    same words in normal case (measured: 0.3 s longer), so shouted lines like the user's
    "STAY RIGHT WHERE YOU ARE..." are said from lowercase. Units, short forms and acronyms are
    said as words (_SAY_AS)."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = _SHOUTING.sub(lambda m: m.group(0).lower(), text)
    for pattern, said in _SAY_AS:  # after: spelled-out letters ("U S B") aren't shouting
        text = pattern.sub(said, text)
    return text


def drop_notes(pieces):
    """The model's text without any bracketed note it writes itself: live, v4 sometimes echoed
    the prompt's own notes ("[A line of yours fits this moment...]") or wrote asides in
    brackets, and the voice read them out. Voice marks ([sigh], [pause]...) pass through.
    Works on the stream, so a note split across pieces is still caught; one left open at the
    end is dropped."""
    held = ""
    for piece in pieces:
        out = ""
        for ch in piece:
            if held:
                held += ch
                if ch == "]":
                    if MARK.fullmatch(held):
                        out += held
                    held = ""
            elif ch == "[":
                held = ch
            else:
                out += ch
        if out:
            yield out


def strip_marks(text: str) -> str:
    """The words alone: what the transcript shows and the model sees in its history."""
    return re.sub(r"\s+", " ", MARK.sub("", text)).strip()


def speech_parts(text: str) -> list[tuple[str, str]]:
    """What the voice does with a line, in order: ("say", words), ("sound", "sigh" | "chuckle"),
    ("pause", "long" | "short") or ("slow", "")."""
    parts: list[tuple[str, str]] = []

    def words(chunk: str) -> None:
        for i, piece in enumerate(p.strip() for p in _TRAIL_OFF.split(chunk)):
            if not piece:
                continue
            if i and parts and parts[-1][0] == "say":
                parts.append(("pause", "short"))
            parts.append(("say", piece))

    pos = 0
    for m in MARK.finditer(text):
        words(text[pos:m.start()])
        name = m.group(1).lower()
        parts.append(("pause", "long") if name == "pause" else ("slow", "") if name == "slow" else ("sound", name))
        pos = m.end()
    words(text[pos:])
    return parts


class SentenceChunker:
    def __init__(self, first_min_words: int = 3):
        self.first_min_words = first_min_words
        self._buf = ""
        self._first = True

    def feed(self, piece: str) -> list[str]:
        self._buf += piece
        out = []
        while True:
            cut = self._find_cut()
            if cut is None:
                return out
            chunk, self._buf = self._buf[:cut], self._buf[cut:]
            chunk = clean_for_speech(chunk)
            if chunk:
                out.append(chunk)
                self._first = False

    def flush(self) -> list[str]:
        chunk, self._buf = clean_for_speech(self._buf), ""
        return [chunk] if chunk else []

    def _find_cut(self) -> int | None:
        m = _SENTENCE_END.search(self._buf)
        if self._first:
            end = m.start() if m else len(self._buf)
            cuts = [c.end() for c in _CLAUSE_END.finditer(self._buf, 0, end)
                    if len(self._buf[:c.start()].split()) >= self.first_min_words]
            cuts += [j.start() + 1 for j in _JOINT.finditer(self._buf, 0, end)
                     if len(self._buf[:j.start()].split()) >= JOINT_MIN_WORDS]
            if cuts:
                return min(cuts)
        return m.end() if m else None
