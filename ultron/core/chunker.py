"""Turn a stream of model text into speakable chunks.

The first chunk is cut as early as it can sound natural (at a comma once there are a
few words, or before an "and" or "because" in a long first sentence), because it decides how
soon Ultron makes a sound: the voice takes ~0.45 s for a short line and ~1 s for twenty words.
Later chunks are whole sentences, which sound better and keep the voice ahead of playback.

Text can carry marks for the voice: [sigh], [chuckle], [pause], and [slow] (the rest of that
line a little slower). Ultron's lines add them; the model's own stage directions ("*sighs*",
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


def for_voice(text: str) -> str:
    """The spelling the voice needs, without changing what anyone reads. Its dictionary has no
    accented words ("naïve" -> "naive"), and it reads a run of capitals differently from the
    same words in normal case (measured: 0.3 s longer), so shouted lines like the user's
    "STAY RIGHT WHERE YOU ARE..." are said from lowercase. A lone acronym ("USB") stays."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return _SHOUTING.sub(lambda m: m.group(0).lower(), text)


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
