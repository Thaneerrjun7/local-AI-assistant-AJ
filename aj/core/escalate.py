"""Hard questions: the local thinker first, Gemini 2.5 Flash if it's slow or fails.

The user's rule (2026-10-04): local first; Gemini takes over when the thinker hasn't answered
in ~8 s, when it fails, or when the question needs live information (news, scores, prices),
which no local model can know. If there's no Gemini key or no network, the thinker gets
longer instead of AJ going silent. Qwen then voices the answer in character, so the
persona stays consistent whichever model actually solved it.
"""
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass

from aj.core import gemini

THINKER_BUDGET_SECS = 8.0
THINKER_PATIENCE_SECS = 60.0  # when Gemini isn't available, wait this long for the thinker

_LIVE = re.compile(r"\b(news|latest|currently|right now|this (week|month|year)|today'?s|tonight'?s|score|scores|"
                   r"price of|stock|exchange rate|who won|released|election|weather)\b", re.IGNORECASE)
_HARD = re.compile(r"\b(calculate|work out|figure out|solve|step by step|how long (until|will|would)|"
                   r"how many .* (if|when|per|left)|percent(age)? of|split|convert|compare|plan (my|a|the)|"
                   r"schedule|what('s| is) the best way|prove|logic|puzzle|riddle)\b|\d+\s*[-+*/x×÷%]\s*\d+",
                   re.IGNORECASE)
GEMINI_SYSTEM = ("Answer accurately and concisely for a voice assistant: one to four plain sentences that can "
                 "be read aloud, no markdown. If you are not sure, say so.")


def needs_live_info(text: str) -> bool:
    return bool(_LIVE.search(text))


def looks_hard(text: str) -> bool:
    """Cheap first-pass router: maths, planning and multi-step reasoning go to the thinker."""
    return bool(_HARD.search(text)) or needs_live_info(text)


@dataclass
class HardAnswer:
    text: str
    source: str  # "thinker", "gemini" or "none"


class Escalator:
    def __init__(self, thinker, gemini_ready: Callable[[], bool] = gemini.has_key):
        self.thinker, self.gemini_ready = thinker, gemini_ready
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="escalate")

    def can_help(self) -> bool:
        """Is there anyone to escalate to? Without a thinker (no room for it next to the
        Chatterbox voice) or a Gemini key, Qwen answers hard questions itself."""
        return (self.thinker is not None and self.thinker.available) or self.gemini_ready()

    def _ask_gemini(self, question: str, web: bool) -> str | None:
        if not self.gemini_ready():
            return None
        try:
            text = "".join(gemini.stream_reply([{"role": "system", "content": GEMINI_SYSTEM},
                                                {"role": "user", "content": question}], web=web)).strip()
            return text or None
        except gemini.GeminiUnavailable:
            return None

    def answer(self, question: str, context: list[dict] | None = None,
               on_slow: Callable[[], None] | None = None) -> HardAnswer:
        """Best available answer. `on_slow` fires once if the thinker misses its budget, so
        AJ can say a second holding line while Gemini (or the thinker) finishes."""
        if needs_live_info(question):
            text = self._ask_gemini(question, web=True)
            if text:
                return HardAnswer(text, "gemini")
        if self.thinker is None or not self.thinker.available:
            text = self._ask_gemini(question, web=False)
            return HardAnswer(text, "gemini") if text else HardAnswer("", "none")
        job = self._pool.submit(self.thinker.solve, question, context)
        try:
            return HardAnswer(job.result(timeout=THINKER_BUDGET_SECS), "thinker")
        except FutureTimeout:
            if on_slow:
                on_slow()
        except Exception:  # thinker crashed or returned nothing: escalate below
            job = None
        text = self._ask_gemini(question, web=False)
        if text:
            return HardAnswer(text, "gemini")
        if job is not None:
            try:
                return HardAnswer(job.result(timeout=THINKER_PATIENCE_SECS), "thinker")
            except Exception:
                pass
        return HardAnswer("", "none")


def relay_prompt(question: str, answer: HardAnswer) -> str:
    """What Qwen is asked to say, so the answer comes out in AJ's voice with the facts intact."""
    if not answer.text:
        return (f"The user asked: {question}\nYou could not work this out reliably right now. Say so honestly "
                f"in one short sentence, in character. Do not guess.")
    return (f"The user asked: {question}\nThe correct answer, already worked out: {answer.text}\n"
            f"Tell the user this answer in your own way of talking. Keep every fact, number and name exactly as given; "
            f"do not add anything new.")
