"""His humor setting: a dial from 0 to 100 percent that the user turns by voice, the way Cooper
turns TARS's in Interstellar. Persona v6 (2026-10-22), the user: "TARS from Interstellar for high
efficiency paired with adjustable humor". docs/TARS_RESEARCH.md has the film's settings and where
each comes from.

- The level is kept in personality.json in the data folder, so it survives a restart.
- persona.system_prompt() turns the level into the humor part of the personality prompt; the
  brain rebuilds its cached prompt when current_humor() changes.
- The default is 60: Cooper's last word on it in the film, after 75 brought a mock self-destruct
  countdown. One step ("turn your humor down") is 15, Cooper's own last cut (75 to 60); from the
  default, one step up is 75, the setting TARS flew the whole mission on.
- Honesty is not a dial here. TARS ran at 90 percent; Computer runs at 100, fixed, because the
  core rules (no invented facts, nothing said to be done that wasn't) came from the user's own
  live findings. Asked about it, or asked to lower it, he says so.
- A tool result is spoken as it is, so the answers are short and in character ("Humor at 45
  percent."), and the intents below catch the usual ways of saying it, so the dial turns with no
  model call at all.
"""
import re
from typing import Literal

from aj.config import DATA_DIR
from aj.core.safe_store import read_json, write_json
from aj.tools.registry import Level, ToolError, tool

PATH = DATA_DIR / "personality.json"  # {"humor": 60}
DEFAULT_HUMOR = 60
STEP = 15
HONESTY = 100  # fixed: see the module docstring
_HONESTY_LINE = (f"Honesty at {HONESTY} percent, and it stays there. You get the truth, including the parts "
                 "you won't like.")


def current_humor() -> int:
    """The humor setting, 0 to 100: the saved one, or the default if there's none (or it's bad)."""
    saved = read_json(PATH, {})
    level = saved.get("humor") if isinstance(saved, dict) else None
    if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 <= level <= 100:
        return DEFAULT_HUMOR
    return round(level)


def _save(level: int) -> None:
    saved = read_json(PATH, {})
    write_json(PATH, {**(saved if isinstance(saved, dict) else {}), "humor": level})


def _confirmation(level: int) -> str:
    if level == 0:
        return "Humor at 0 percent. Jokes off."
    if level == 100:
        return "Humor at 100 percent. You asked for it."
    return f"Humor at {level} percent."


@tool(level=Level.SAFE, timeout=2.0, cloud=False)
def set_humor(level: int | None = None, change: Literal["up", "down"] | None = None) -> str:
    """Set your own humor setting to 0-100 percent, or turn it up or down a step.

    level: percent, 0 (no jokes) to 100
    change: up or down by 15, instead of a level
    """
    now = current_humor()
    if level is not None:
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise ToolError("Humor needs a number from 0 to 100 percent.")
        if not 0 <= level <= 100:  # said honestly rather than quietly clamped
            raise ToolError(f"Humor goes from 0 to 100 percent. It stays at {now} percent.")
        target = round(level)
    elif change in ("up", "down"):
        target = max(0, min(100, now + (STEP if change == "up" else -STEP)))
    else:
        raise ToolError("Give a humor level from 0 to 100 percent, or up or down.")
    if target == now:
        edge = {100: " That's the top.", 0: " That's the bottom."}.get(now, "") if level is None else ""
        return f"Humor is already at {now} percent.{edge}"
    try:
        _save(target)
    except OSError:
        raise ToolError(f"I couldn't save the new humor setting, so it stays at {now} percent.") from None
    return _confirmation(target)


@tool(level=Level.SAFE, timeout=1.0, cloud=False)
def personality_setting(setting: Literal["humor", "honesty"] = "humor") -> str:
    """Tell your own humor or honesty setting.

    setting: humor or honesty
    """
    if setting == "honesty":
        return _HONESTY_LINE
    return f"Humor at {current_humor()} percent."


# ---------------------------------------------------------------- fast-path intents
#
# Normalized text (aj/tools/intents.py): lowercase, no punctuation, "60%" -> "60". It does
# nothing to number words, and speech-to-text writes both "60" and "sixty", so both are read here.
# Out-of-range numbers go through: the tool says the range rather than quietly clamping.

_UNITS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
          "fifteen sixteen seventeen eighteen nineteen").split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_SPECIAL = {"max": 100, "maximum": 100, "full": 100, "half": 50, "hundred": 100, "a hundred": 100,
            "one hundred": 100}


def _alternatives(words) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


_N = (rf"(?P<n>\d{{1,3}}|(?:{_alternatives(_TENS)})(?: (?:{_alternatives(_UNITS[1:10])}))?|(?:a |one )?hundred"
      rf"|{_alternatives(_UNITS)}|max(?:imum)?|full|half)(?: ?percent| per cent)?")


def _number(text: str) -> int:
    """'60', 'sixty', 'seventy five', 'a hundred', 'max' -> int."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text in _SPECIAL:
        return _SPECIAL[text]
    if text in _UNITS:
        return _UNITS.index(text)
    tens, _, unit = text.partition(" ")
    return 20 + 10 * _TENS.index(tens) + (_UNITS.index(unit) if unit else 0)


_HUMOR = r"(?:your |the |ur )?humou?r(?: setting| level| parameter)?"
_HONESTY = r"(?:your |the |ur )?honesty(?: setting| level| parameter)?"
# Asking needs "your" or "setting": "what is humor" and "what is honesty" are questions for the model.
_HUMOR_ASK = r"(?:(?:your|ur) humou?r(?: setting| level| parameter)?|(?:the )?humou?r (?:setting|level|parameter))"
_HONESTY_ASK = r"(?:(?:your|ur) honesty(?: setting| level| parameter)?|(?:the )?honesty (?:setting|level|parameter))"
_WHATS = r"(?:what(?:s| s| is) |tell me |how high is |check )?"
_NOW = r"(?: (?:at|set to|now|right now|at the moment))*"
_BIT = r"(?: (?:a bit|a little|a little bit|a notch|a touch|slightly|some))?"


def _whole(body: str) -> re.Pattern:
    return re.compile(rf"(?:{body})", re.IGNORECASE)


def _level(m) -> dict:
    return {"level": _number(m["n"])}


INTENTS = [
    # "set humor to sixty", "humour 75 percent", "turn your humor down to 40", "max humor"
    (_whole(rf"(?:(?:set|put|change|make|turn|bring|dial|adjust|switch|drop|lower|raise) )?{_HUMOR}"
            rf"(?: (?:to|at|on|down to|up to|all the way up to|all the way down to))? {_N}"), "set_humor", _level),
    (_whole(rf"{_N} humou?r"), "set_humor", _level),
    # off and back on
    (_whole(rf"(?:turn|switch|shut) off {_HUMOR}|(?:turn|switch|shut) {_HUMOR} off|humou?r off"
            r"|(?:no|stop(?: the)?|enough(?: with the)?|quit(?: the)?|cut(?: out)? the) (?:more )?(?:jokes|joking)"
            r"(?: around)?(?: (?:from now on|for now|for today|today|please))?|(?:stop|quit) joking(?: around)?"),
     "set_humor", lambda m: {"level": 0}),
    (_whole(rf"(?:turn|switch) on {_HUMOR}|(?:turn|switch) {_HUMOR} (?:back )?on|humou?r (?:back )?on"),
     "set_humor", lambda m: {"level": DEFAULT_HUMOR}),
    # a step down / up
    (_whole(rf"(?:turn|bring|dial|tone|take|knock) {_HUMOR} down{_BIT}|(?:turn|bring|dial|tone|take|knock) down "
            rf"{_HUMOR}{_BIT}|(?:lower|reduce|decrease|drop) {_HUMOR}{_BIT}|humou?r down"
            r"|(?:a bit |a little )?(?:less|fewer|not so many) (?:jokes?|joking|humou?r)|be less funny"
            # "tone down the jokes a bit" (scripts/check_tool_picks.py, 2026-11-18: the model said
            # "Humor at 45 percent" without changing it)
            r"|(?:tone|dial|turn|bring|knock|cut|take) (?:down|back) (?:the |your |on the )?(?:jokes?|joking|humou?r|sarcasm)"
            r"(?: (?:a bit|a little|a tad|slightly|a notch))?"
            r"|(?:tone|dial|turn|bring|knock|cut|take) (?:the |your )?(?:jokes?|joking|humou?r|sarcasm) (?:down|back)"
            r"(?: (?:a bit|a little|a tad|slightly|a notch))?|be (?:a bit |a little )?more serious|less sarcasm"),
     "set_humor", lambda m: {"change": "down"}),
    (_whole(rf"(?:turn|bring|dial|crank|kick|take) {_HUMOR} up{_BIT}|(?:turn|bring|dial|crank|kick) up "
            rf"{_HUMOR}{_BIT}|(?:raise|increase|boost) {_HUMOR}{_BIT}|humou?r up"
            r"|be (?:a bit |a little )?(?:funnier|more funny)|(?:tell |make )?more (?:jokes|humou?r)"),
     "set_humor", lambda m: {"change": "up"}),
    # asking
    (_whole(rf"{_WHATS}{_HUMOR_ASK}{_NOW}|how funny are you(?: set to)?{_NOW}"),
     "personality_setting", lambda m: {"setting": "humor"}),
    # honesty: asked, or asked to change (it doesn't)
    (_whole(rf"{_WHATS}{_HONESTY_ASK}{_NOW}|how honest are you(?: set to)?{_NOW}"
            rf"|(?:(?:set|put|change|make|turn|bring|dial|adjust|lower|drop|reduce|raise) )?{_HONESTY}"
            rf"(?: (?:down|up))?(?: (?:to|at|down to|up to))? {_N}"
            rf"|(?:turn|bring|dial|lower|reduce|drop|raise|increase) (?:down |up )?{_HONESTY}(?: (?:down|up))?{_BIT}"),
     "personality_setting", lambda m: {"setting": "honesty"}),
]
