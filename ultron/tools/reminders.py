"""Reminders and timers that go off even when Ultron is turned off.

Ported from SivaSafe's reminder.py, reminder_store.py and reminder_fire.py. What carried over,
because it worked: each reminder is its own Windows Task Scheduler job (schtasks /Create /XML)
that runs pythonw.exe on reminder_fire.py when it is due. pythonw has no console, so nothing
flashes on screen, and the job fires whether Ultron is running or not, after a reboot too.

What changed:
- Recurrence, which SivaSafe lacked. "every day/weekday/monday at 7am" becomes a calendar
  trigger in the same job, so Windows itself keeps it repeating: nothing has to re-arm it, and
  one failed run can't end the series.
- cancel_reminder is a registered tool (CONFIRM). SivaSafe wrote one and never wired it up.
- The JSON store is the source of truth. The fire script only announces a reminder that is
  still in it, so a cancel sticks even if deleting the job failed.
- Times are read from plain words here, with the standard library, instead of asking the model
  for YYYY-MM-DD and HH:MM. Anything past or ambiguous ("tomorrow" with no time) is refused with
  a ToolError that says what to add, and every reply states the exact time that was picked.
- Jobs live in their own "Ultron" folder in Task Scheduler. schtasks always gets an argument
  list (never a shell) and no console window, and the reminder text never goes on a command
  line as plain text, so quotes or %VARS% in it can't break or change the job.
- One-off jobs delete themselves once they expire, 12 hours after they were due (the window
  in which a reminder missed while the laptop was off still fires late), so spent jobs don't
  pile up in Task Scheduler.
"""
import base64
import logging
import msvcrt
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from ultron.config import APP_DIR, DATA_DIR
from ultron.core import safe_store
from ultron.tools.registry import Level, ToolError, tool

log = logging.getLogger(__name__)

STORE_PATH = DATA_DIR / "reminders.json"
FIRE_SCRIPT = Path(__file__).resolve().with_name("reminder_fire.py")
TASK_FOLDER = "Ultron"
NO_WINDOW = 0x08000000
_SCHTASKS = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe")

MAX_TEXT = 200                     # characters of reminder text kept
GRACE = timedelta(hours=12)        # a one-off missed while the laptop was off still fires this late
MAX_AHEAD = timedelta(days=366)
MAX_TIMER = 24 * 3600              # seconds; anything longer is a reminder
EVERY_DAY = (0, 1, 2, 3, 4, 5, 6)  # Monday is 0, as datetime.weekday() counts
WEEKDAYS = (0, 1, 2, 3, 4)
WEEKEND = (5, 6)


class NotATime(ToolError):
    """`when` isn't a time phrase at all, as opposed to a time that is past or incomplete."""


@dataclass(frozen=True)
class When:
    """When a reminder goes off: the first time, and the weekdays it repeats on (None: once)."""
    due: datetime
    days: tuple[int, ...] | None = None


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


# ---- reading a time from plain words --------------------------------------------------------

_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
           "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
           "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}
_PARTS = {"morning": 9, "afternoon": 15, "evening": 18, "night": 20}  # the hour a bare "tonight" means
_FILLER = {"at", "on", "by", "around", "about", "for", "the", "and", "of"}
_SMALL = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
          "fifteen sixteen seventeen eighteen nineteen").split()
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
             "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
             "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
             "nineteenth": 19, "twentieth": 20, "thirtieth": 30}
_UNIT_SECONDS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600,
                 "day": 86400, "week": 604800}

_HELP = "Try 'at 6pm', 'in 20 minutes', 'tomorrow at 9', 'on Friday at 3pm' or 'every weekday at 7am'."
_REPEAT_HELP = ("I can repeat a reminder every day, every weekday, or on chosen days of the week, "
                "like 'every Monday and Thursday at 6pm'.")


def _alts(words) -> str:
    return "|".join(sorted(words, key=len, reverse=True))  # longest first: "minute" before "min"


_DAY_RE = _alts(_DAY_NAMES)
_MONTH_RE = _alts(_MONTHS)
_PART_RE = "morning|afternoon|evening|night"

_PUNCTUATION = re.compile(r"(?!(?<=\d)[.:](?=\d))[^\w\s]|_")  # keeps "6:30" and "1.5" whole
_NUMBER_WORDS = re.compile(rf"\b(?:(?P<tens>{_alts(_TENS)})(?:[ -](?P<unit>{_alts(_SMALL[1:10])}))?"
                           rf"|(?P<small>{_alts(_SMALL)}))\b")
# Ordinal words only where they mean a date ("the second", "october first"), never "a second".
_ORDINAL_WORDS = re.compile(rf"\b(?P<lead>the|{_MONTH_RE}) (?:(?P<tens>twenty|thirty)[ -])?(?P<ord>{_alts(_ORDINALS)})\b")

_RELATIVE = re.compile(r"(?:in|after|within) (?P<a>.+)|(?P<b>.+) (?:from now|later)")
_SPAN_PART = re.compile(rf"(?:and )?(?P<n>\d+(?:\.\d+)?|an?)(?P<h1> and a half)? (?P<unit>{_alts(_UNIT_SECONDS)})s?"
                        rf"(?P<h2> and a half)?(?: |$)")
_UNSUPPORTED = re.compile(
    r"\b(?:every|each) (?:other|second|\d+(?:\.\d+)?|few|couple of) (?:seconds?|secs?|minutes?|mins?|hours?|hrs?"
    r"|days?|weeks?|months?|years?)\b"
    r"|\b(?:every|each) (?:second|sec|minute|min|hour|hr|week|fortnight|month|year)s?\b"
    r"|\b(?:hourly|weekly|fortnightly|monthly|yearly|annually)\b")
_PAST_WORDS = re.compile(rf"\b(?:yesterday|ago|last (?:night|week|month|year|{_DAY_RE}))\b")
_RECUR = re.compile(
    rf"\b(?:(?P<daily>(?:every|each) (?:single )?day|daily|everyday)"
    rf"|(?P<weekdays>(?:(?:every|each|on) )?(?:week ?days|work ?days)|(?:every|each) (?:week ?day|work ?day))"
    rf"|(?P<weekend>(?:(?:every|each|on) )?weekends|(?:every|each) weekend)"
    rf"|(?:every|each) (?P<part>{_PART_RE})"
    rf"|(?P<lead>every |each |on )?(?P<list>(?:{_DAY_RE})s?(?:(?: and| or)? (?:{_DAY_RE})s?)*)"
    rf")(?: (?P<part2>{_PART_RE})s?)?\b")
_DAY = re.compile(
    rf"\b(?:(?P<after>(?:the )?day after tomorrow)"
    rf"|(?P<tomorrow>tomorrow)(?: (?P<tpart>{_PART_RE}))?"
    rf"|(?P<today>later today|today)"
    rf"|(?P<tonight>tonight)"
    rf"|this (?P<thispart>morning|afternoon|evening)"
    rf"|in (?P<n>\d+|an?) (?P<unit>days?|weeks?)"
    rf"|(?:on )?(?:(?P<mod>this coming|coming|this|next) )?(?P<wday>{_DAY_RE})(?: (?P<wpart>{_PART_RE}))?"
    rf"|(?:on )?(?:the )?(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?(?: of)? (?P<mon1>{_MONTH_RE})(?: (?P<y1>20\d\d))?"
    rf"|(?:on )?(?P<mon2>{_MONTH_RE}) (?:the )?(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?(?: (?P<y2>20\d\d))?"
    rf"|(?:on )?the (?P<d3>\d{{1,2}})(?:st|nd|rd|th)"
    rf")\b")
_PART_ALONE = re.compile(r"\b(?:in the (?P<p>morning|afternoon|evening|night)|at (?P<n>night))\b")
_TIME = re.compile(
    r"\b(?:(?P<noon>(?:12 )?(?:noon|midday))|(?P<midnight>(?:12 )?midnight)"
    r"|(?P<qm>half|quarter|\d{1,2}(?: minutes?)?) (?P<dir>past|after|to|before|till) (?P<qh>\d{1,2})(?: (?P<qap>am|pm))?"
    r"|(?P<h>\d{1,2})(?:[:.](?P<m>\d{2})| (?P<m2>[0-5]\d))?(?: (?P<ap>am|pm))?(?P<oc> oclock)?"
    r")\b")
_PREPOSITION = re.compile(r"\b(?:at|by|around|about|for)\s*$")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")


def _number_word(m: re.Match) -> str:
    if m["small"]:
        return str(_SMALL.index(m["small"]))
    return str(_TENS[m["tens"]] + (_SMALL.index(m["unit"]) if m["unit"] else 0))


def _clean(text: str) -> str:
    """Lowercase, no punctuation, numbers as digits: "Six-thirty P.M." -> "6 30 pm"."""
    s = re.sub(r"['‘’`]", "", str(text).lower())
    s = re.sub(r"\bo ?clock\b", " oclock", s)  # "o'clock" lost its apostrophe just above
    s = re.sub(r"\b([ap])\.? ?m\b\.?", r"\1m", s)  # "p.m." and "p m" -> "pm"
    s = _PUNCTUATION.sub(" ", s)
    s = _ORDINAL_WORDS.sub(lambda m: f"{m['lead']} {_TENS.get(m['tens'] or '', 0) + _ORDINALS[m['ord']]}th", s)
    s = _NUMBER_WORDS.sub(_number_word, s)
    s = re.sub(r"\b(\d{1,2}) oh (\d)\b", r"\1:0\2", s)  # "six oh five"
    s = re.sub(r"(\d)(am|pm)\b", r"\1 \2", s)
    return " ".join(s.split())


def parse_span(text: str) -> int | None:
    """Seconds in a spoken length of time ("20 minutes", "an hour and a half", "1 hour 30 min")."""
    s = re.sub(r"\b(?:half an?|a half) (hour|minute|day|week)\b", r"0.5 \1", _clean(text))
    pos, total = 0, 0.0
    while pos < len(s):
        m = _SPAN_PART.match(s, pos)
        if m is None:
            return None
        count = 1.0 if m["n"] in ("a", "an") else float(m["n"])
        total += (count + (0.5 if m["h1"] or m["h2"] else 0)) * _UNIT_SECONDS[m["unit"]]
        pos = m.end()
    return round(total) if total > 0 else None


@dataclass
class _Day:
    """The day part of a phrase: a fixed date, or a weekday still to be placed."""
    label: str                # for questions: "What time tomorrow?"
    on: date | None = None     # a fixed date (named "on": "date" would shadow the type)
    weekday: int | None = None
    skip_today: bool = False  # "next friday" said on a Friday
    hint: str | None = None   # "tomorrow morning" -> "morning"


def _take(pattern: re.Pattern, s: str, accept: Callable[[re.Match], bool] = lambda m: True):
    """The first match `accept` likes, and `s` with it cut out."""
    for m in pattern.finditer(s):
        if accept(m):
            return m, f"{s[:m.start()]} {s[m.end():]}"
    return None, s


def _take_recurrence(s: str) -> tuple[tuple[int, ...] | None, str | None, str]:
    def repeats(m: re.Match) -> bool:
        if m["list"] is None:
            return True
        names = [w for w in m["list"].split() if w not in ("and", "or")]
        return (m["lead"] or "").strip() in ("every", "each") or any(n.endswith("s") for n in names)

    m, s = _take(_RECUR, s, repeats)
    if m is None:
        return None, None, s
    if m["daily"] or m["part"]:
        days = EVERY_DAY
    elif m["weekdays"]:
        days = WEEKDAYS
    elif m["weekend"]:
        days = WEEKEND
    else:
        names = [w.removesuffix("s") for w in m["list"].split() if w not in ("and", "or")]
        days = tuple(sorted({_DAY_NAMES.index(n) for n in names}))
    return days, m["part"] or m["part2"], s


def _month_day(year: int | None, month: int, day: int, today: date) -> date:
    try:
        found = date(year or today.year, month, day)
    except ValueError:
        raise ToolError(f"There's no {date(2000, month, 1):%B} {day}.") from None
    if year is None and (today - found).days > 60:  # "January 5th", said in December
        try:
            found = found.replace(year=found.year + 1)
        except ValueError:  # February 29th
            raise ToolError(f"There's no {found:%B} {day} next year.") from None
    return found


def _take_day(s: str, today: date) -> tuple[_Day | None, str]:
    m, s = _take(_DAY, s)
    if m is None:
        return None, s
    if m["after"]:
        when = today + timedelta(days=2)
        return _Day(_day_phrase(when, today), when), s
    if m["tomorrow"]:
        return _Day("tomorrow", today + timedelta(days=1), hint=m["tpart"]), s
    if m["today"]:
        return _Day("today", today), s
    if m["tonight"]:
        return _Day("tonight", today, hint="night"), s
    if m["thispart"]:
        return _Day(f"this {m['thispart']}", today, hint=m["thispart"]), s
    if m["unit"]:
        count = 1 if m["n"] in ("a", "an") else int(m["n"])
        when = today + timedelta(days=count * (7 if m["unit"].startswith("week") else 1))
        return _Day(_day_phrase(when, today), when), s
    if m["wday"]:
        weekday = _DAY_NAMES.index(m["wday"])
        return _Day(f"on {m['wday'].capitalize()}", weekday=weekday, skip_today=m["mod"] == "next",
                    hint=m["wpart"]), s
    if m["d3"]:
        day = int(m["d3"])
        year, month = (today.year, today.month) if day >= today.day else \
            (today.year + today.month // 12, today.month % 12 + 1)
        try:
            when = date(year, month, day)
        except ValueError:
            raise ToolError(f"There's no {date(2000, month, 1):%B} {day}.") from None
        return _Day(_day_phrase(when, today), when), s
    month = _MONTHS[m["mon1"] or m["mon2"]]
    year = m["y1"] or m["y2"]
    when = _month_day(int(year) if year else None, month, int(m["d1"] or m["d2"]), today)
    return _Day(_day_phrase(when, today), when), s


def _take_time(s: str) -> tuple[tuple | None, str]:
    """((hour, minute, "am"/"pm"/None, "noon"/"midnight"/None), rest) for the clock time in `s`."""
    def is_clock(m: re.Match) -> bool:
        if m["h"] is None or m["m"] or m["m2"] or m["ap"] or m["oc"]:
            return True
        # A bare number is a time only after "at" ("at 6"), or when nothing else is left ("tomorrow 9").
        return bool(_PREPOSITION.search(s[:m.start()])) or not (s[:m.start()] + s[m.end():]).strip()

    m, rest = _take(_TIME, s, is_clock)
    if m is None:
        return None, s
    if m["noon"]:
        return (12, 0, None, "noon"), rest
    if m["midnight"]:
        return (0, 0, None, "midnight"), rest
    if m["qm"]:
        minutes = {"half": 30, "quarter": 15}.get(m["qm"]) or int(m["qm"].split()[0])
        hour = int(m["qh"])
        if minutes > 59 or not 0 <= hour <= 23:
            raise ToolError(f"'{m[0]}' isn't a time I can read.")
        if m["dir"] in ("to", "before", "till"):  # "quarter to 7" is 6:45, "quarter to 1" is 12:45
            hour, minutes = (12 if hour == 1 else hour - 1) if hour else 23, 60 - minutes
        return (hour, minutes, m["qap"], None), rest
    return (int(m["h"]), int(m["m"] or m["m2"] or 0), m["ap"], None), rest


def _readings(clock: tuple | None, hint: str | None) -> list[tuple[int, int, int]]:
    """(days later, hour, minute) for what a clock time can mean, earlier in the day first."""
    if clock is None:
        return [(0, _PARTS[hint], 0)]
    hour, minute, half, special = clock
    if special == "noon":
        return [(0, 12, 0)]
    if special == "midnight":
        return [(1, 0, 0)]  # the end of the day it's on
    if minute > 59:
        raise ToolError(f"{hour}:{minute:02d} isn't a time.")
    if half:
        if not 1 <= hour <= 12:
            raise ToolError(f"{hour} {half} isn't a time.")
        return [(0, hour % 12 + (12 if half == "pm" else 0), minute)]
    if hour > 23:
        raise ToolError(f"{hour}:{minute:02d} isn't a time.")
    if hour == 0 or hour > 12:  # the 24-hour clock
        return [(0, hour, minute)]
    if hint == "morning":
        return [(0, hour % 12, minute)]
    if hint in ("afternoon", "evening"):
        return [(0, hour % 12 + 12, minute)]
    if hint == "night":  # "tonight at 11" is 11 pm; "tonight at 1" is 1 am, after midnight
        return [(1, hour % 12, minute)] if hour == 12 or hour <= 4 else [(0, hour + 12, minute)]
    return [(0, hour % 12, minute), (0, hour % 12 + 12, minute)]


def _likely(readings: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    """On a named day with no am or pm: 7 to 11 are the morning, 12 to 6 the afternoon and evening."""
    if len(readings) == 1:
        return readings[0]
    return readings[0] if 7 <= readings[0][1] <= 11 else readings[1]


def _at(day: date, reading: tuple[int, int, int]) -> datetime:
    later, hour, minute = reading
    return datetime.combine(day, time(hour, minute)) + timedelta(days=later)


def _next_on(days: tuple[int, ...], at: time, after: datetime) -> datetime:
    """The first time-of-day `at` on one of `days` that comes after `after`."""
    for ahead in range(8):
        day = after.date() + timedelta(days=ahead)
        candidate = datetime.combine(day, at)
        if day.weekday() in days and candidate > after:
            return candidate
    raise ValueError(f"no weekday in {days!r}")


def _soon_enough(due: datetime, now: datetime) -> datetime:
    if due - now > MAX_AHEAD:
        raise ToolError("That's more than a year away. I keep reminders for up to a year.")
    return due


def _past(due: datetime, today: date) -> ToolError:
    return ToolError(f"{_clock(due)} {_day_phrase(due.date(), today)} has already passed.")


def parse_when(text: str, now: datetime) -> When:
    """Read a spoken time: "at 6pm", "in 20 minutes", "tomorrow at 9", "every weekday at 7am".

    Raises NotATime when `text` isn't a time phrase at all, and ToolError when it is one that is
    past ("today at 9am" at noon), incomplete ("tomorrow") or a repeat it can't do ("every hour").
    A clock time with no day is the next one to come round: "at 7am" said at 10pm is tomorrow.
    """
    raw = " ".join(str(text).split())
    today = now.date()
    if _ISO.fullmatch(raw):  # the model sometimes sends 2026-10-07 18:00
        try:
            exact = datetime.fromisoformat(raw)
        except ValueError:
            raise NotATime(f"'{raw}' isn't a real date. {_HELP}") from None
        if len(raw) == 10:
            raise ToolError(f"What time {_day_phrase(exact.date(), today)}?")
        if exact <= now:
            raise _past(exact, today)
        return When(_soon_enough(exact, now))
    s = _clean(raw)
    if not s:
        raise NotATime(f"When should it go off? {_HELP}")
    relative = _RELATIVE.fullmatch(s)
    if relative:
        seconds = parse_span(relative["a"] or relative["b"])
        if seconds is not None:
            return When(_soon_enough(now + timedelta(seconds=seconds), now))
    if _UNSUPPORTED.search(s):
        raise ToolError(_REPEAT_HELP)
    if _PAST_WORDS.search(s):
        raise ToolError("That's in the past. When should it go off?")
    days, hint, s = _take_recurrence(s)
    day, s = _take_day(s, today) if days is None else (None, s)
    if day and day.hint:
        hint = day.hint
    if hint is None:
        part, s = _take(_PART_ALONE, s)
        hint = part and (part["p"] or part["n"])
    clock, s = _take_time(s)
    if any(word not in _FILLER for word in s.split()):
        raise NotATime(f"I couldn't tell when '{raw}' is. {_HELP}")
    if clock is None and hint is None:
        if days:
            raise ToolError(f"What time {_days_phrase(days)}?")
        if day:
            raise ToolError(f"What time {day.label}?")
        raise NotATime(f"I couldn't tell when '{raw}' is. {_HELP}")
    readings = _readings(clock, hint)

    if days:
        _, hour, minute = _likely(readings)
        return When(_next_on(days, time(hour, minute), now), days)
    if day is None:  # "at 6pm", "in the morning": the next time it comes round
        for base in (today, today + timedelta(days=1)):
            for reading in readings:
                if (due := _at(base, reading)) > now:
                    return When(_soon_enough(due, now))
    if day.weekday is not None:
        ahead = (day.weekday - today.weekday()) % 7 or (7 if day.skip_today else 0)
        due = _at(today + timedelta(days=ahead), _likely(readings))
        return When(_soon_enough(due if due > now else due + timedelta(days=7), now))
    if day.on == today and len(readings) > 1:  # "today at 6": whichever 6 o'clock is still to come
        due = next((d for r in readings if (d := _at(today, r)) > now), _at(today, readings[-1]))
    else:
        due = _at(day.on, _likely(readings))
    if due <= now:
        raise _past(due, today)
    return When(_soon_enough(due, now))


# ---- saying times out loud ------------------------------------------------------------------

def _ordinal(day: int) -> str:
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _clock(moment: datetime) -> str:
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'am' if moment.hour < 12 else 'pm'}"


def _day_phrase(day: date, today: date) -> str:
    ahead = (day - today).days
    if ahead == 0:
        return "today"
    if ahead == 1:
        return "tomorrow"
    if 1 < ahead < 7:
        return f"on {day:%A}"
    phrase = f"on {day:%A}, {day:%B} {_ordinal(day.day)}"
    return phrase if day.year == today.year else f"{phrase}, {day.year}"


def _days_phrase(days) -> str:
    days = tuple(sorted(days))
    if days == EVERY_DAY:
        return "every day"
    if days == WEEKDAYS:
        return "every weekday"
    names = [_DAY_NAMES[d].capitalize() for d in days]
    return "every " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])


def _span(seconds: int, adjective: bool = False) -> str:
    """'1 hour and 30 minutes', or as an adjective '90 minute' ("a 90 minute timer")."""
    if adjective:
        if seconds % 3600 == 0:
            return f"{seconds // 3600} hour"
        if seconds % 60 == 0:
            return f"{seconds // 60} minute"
        return f"{seconds} second" if seconds < 60 else f"{seconds // 60} minute {seconds % 60} second"
    parts = [(seconds // 3600, "hour"), (seconds % 3600 // 60, "minute"), (seconds % 60, "second")]
    said = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in parts if n]
    return " and ".join(said) or "0 seconds"


def _left(seconds: float) -> str:
    """Time left on a timer: whole minutes, or seconds in the last minute."""
    seconds = max(0, round(seconds))
    return _span(seconds) if seconds < 60 else _span(round(seconds / 60) * 60)


# ---- the store (the source of truth) --------------------------------------------------------
# A list of entries:
#   {"id": "3f9a1c2b7d", "kind": "reminder" | "timer", "text": "call mom",
#    "due": "2026-10-07T18:00:00",        the first time it goes off
#    "days": null | [0, 1, 2, 3, 4],      weekdays it repeats on (Monday = 0)
#    "seconds": 600,                      timers only
#    "created": "...", "task": "Ultron\\Reminder-3f9a1c2b7d",
#    "fired": "...",                      one-offs, once shown (then dropped)
#    "last_fired": "..."}                 repeating ones

_THREAD_LOCK = threading.Lock()


@contextmanager
def _locked() -> Iterator[None]:
    """Hold the store for one read-change-write, against Ultron's other threads and the fire script."""
    path = STORE_PATH.with_name(STORE_PATH.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries for about 10 s, then raises
            except OSError as exc:
                raise ToolError("The reminder list is busy. Try again in a moment.") from exc
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


def _valid(entry: object) -> bool:
    try:
        datetime.fromisoformat(entry["due"])  # type: ignore[index]
        return (isinstance(entry["id"], str) and entry["kind"] in ("reminder", "timer")  # type: ignore[index]
                and isinstance(entry["text"], str))  # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        return False


def _read() -> list[dict] | None:
    """Every well-formed entry, or None if there is no readable store at all."""
    data = safe_store.read_json(STORE_PATH, None)
    if not isinstance(data, list):
        if data is not None:
            log.warning("%s doesn't hold a list; ignoring it", STORE_PATH)
        return None
    good = [e for e in data if _valid(e)]
    if len(good) != len(data):
        log.warning("dropping %d malformed entries from %s", len(data) - len(good), STORE_PATH)
    return good


def _save(items: list[dict]) -> None:
    try:
        safe_store.write_json(STORE_PATH, items)
    except OSError as exc:
        raise ToolError(f"Couldn't save the reminder list ({exc.strerror or exc}).") from exc


def _due(entry: dict) -> datetime:
    return datetime.fromisoformat(entry["due"])


def _next(entry: dict, now: datetime) -> datetime:
    """When an entry goes off next (a repeating one keeps coming round)."""
    due = _due(entry)
    if not entry.get("days") or due > now:
        return due
    return _next_on(tuple(entry["days"]), due.time(), now)


def _spent(entry: dict, now: datetime) -> bool:
    return not entry.get("days") and bool(entry.get("fired") or _due(entry) < now - GRACE)


def _load(now: datetime) -> list[dict]:
    """The live entries, soonest first. Shown and long-missed one-offs are dropped. Call under _locked()."""
    items = _read() or []
    live = [e for e in items if not _spent(e, now)]
    if len(live) != len(items):
        _save(live)
    return sorted(live, key=lambda e: _next(e, now))


def claim(item_id: str, now: datetime) -> tuple[dict | None, bool]:
    """For the fire script: (the entry going off now, marked as shown; whether the store exists).

    (None, True) means it was cancelled or was already shown, so nothing should appear.
    (None, False) means there is no readable store, so the fire script falls back on its own copy.
    """
    with _locked():
        items = _read()
        if items is None:
            return None, False
        for entry in items:
            if entry["id"] != item_id:
                continue
            if entry.get("days"):
                entry["last_fired"] = now.isoformat(timespec="seconds")
            elif entry.get("fired"):
                return None, True  # a second start of a one-off: it was already shown
            else:
                entry["fired"] = now.isoformat(timespec="seconds")
            _save(items)
            return dict(entry), True
        return None, True


def announcement(entry: dict) -> tuple[str, str]:
    """(title, text) of the notification for an entry."""
    if entry["kind"] == "timer":
        return "Timer", f"Your {entry['text']} is done."
    return "Reminder", entry["text"]


# ---- Windows Task Scheduler -----------------------------------------------------------------

def task_name(item_id: str) -> str:
    return f"{TASK_FOLDER}\\Reminder-{item_id}"


def pythonw() -> str:
    """The windowless interpreter the job runs: the project's venv first."""
    for candidate in (APP_DIR / ".venv" / "Scripts" / "pythonw.exe", Path(sys.executable).with_name("pythonw.exe")):
        if candidate.is_file():
            return str(candidate)
    log.warning("no pythonw.exe found; reminders will flash a console window")
    return sys.executable


def pack_text(text: str) -> str:
    """The text as base64url: nothing in it can be read as a quote, a switch or a %VARIABLE%."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _trigger(entry: dict) -> str:
    due = _due(entry)
    start = f"      <StartBoundary>{due.isoformat(timespec='seconds')}</StartBoundary>\n"
    days = sorted(entry.get("days") or ())
    if not days:
        end = (due + GRACE).isoformat(timespec="seconds")
        return ("    <TimeTrigger>\n" + start + f"      <EndBoundary>{end}</EndBoundary>\n"
                "      <Enabled>true</Enabled>\n    </TimeTrigger>\n")
    if tuple(days) == EVERY_DAY:
        repeat = "      <ScheduleByDay>\n        <DaysInterval>1</DaysInterval>\n      </ScheduleByDay>\n"
    else:
        names = "".join(f"          <{_DAY_NAMES[d].capitalize()} />\n" for d in days)
        repeat = ("      <ScheduleByWeek>\n        <DaysOfWeek>\n" + names + "        </DaysOfWeek>\n"
                  "        <WeeksInterval>1</WeeksInterval>\n      </ScheduleByWeek>\n")
    return "    <CalendarTrigger>\n" + start + "      <Enabled>true</Enabled>\n" + repeat + "    </CalendarTrigger>\n"


def task_xml(entry: dict) -> str:
    """The Task Scheduler job for an entry: run pythonw on the fire script at the due time(s).

    Kept close to SivaSafe's job, which worked: it runs only while you're logged on (a toast
    needs your desktop), without admin rights, on battery too, catches up after the laptop was
    off (StartWhenAvailable), and never wakes a sleeping laptop.
    """
    arguments = subprocess.list2cmdline([str(FIRE_SCRIPT), "--id", entry["id"], "--text", pack_text(entry["text"])])
    expire = "" if entry.get("days") else "    <DeleteExpiredTaskAfter>PT0S</DeleteExpiredTaskAfter>\n"
    kind = "timer" if entry["kind"] == "timer" else "reminder"
    return ('<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            "  <RegistrationInfo>\n"
            f"    <Description>{escape(f'Ultron {kind}: ' + entry['text'])}</Description>\n"
            "  </RegistrationInfo>\n"
            "  <Triggers>\n" + _trigger(entry) + "  </Triggers>\n"
            "  <Principals>\n    <Principal>\n      <LogonType>InteractiveToken</LogonType>\n"
            "      <RunLevel>LeastPrivilege</RunLevel>\n    </Principal>\n  </Principals>\n"
            "  <Settings>\n"
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
            "    <StartWhenAvailable>true</StartWhenAvailable>\n"
            "    <Enabled>true</Enabled>\n"
            "    <Hidden>false</Hidden>\n"
            "    <WakeToRun>false</WakeToRun>\n"
            "    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>\n" + expire +
            "  </Settings>\n"
            "  <Actions>\n    <Exec>\n"
            f"      <Command>{escape(pythonw())}</Command>\n"
            f"      <Arguments>{escape(arguments)}</Arguments>\n"
            f"      <WorkingDirectory>{escape(str(APP_DIR))}</WorkingDirectory>\n"
            "    </Exec>\n  </Actions>\n"
            "</Task>\n")


def schtasks(args: list[str]) -> tuple[int, str]:
    """Run schtasks.exe with an argument list (no shell, no console window): (exit code, its message).

    Tests replace this function, so nothing they do reaches the real Task Scheduler.
    """
    try:
        done = subprocess.run([_SCHTASKS, *args], capture_output=True, timeout=10, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return -1, "schtasks took too long"
    except OSError as exc:
        return -1, f"couldn't start schtasks ({exc.strerror or exc})"
    message = (done.stderr or done.stdout).decode("oem", "replace").strip()
    return done.returncode, message.removeprefix("ERROR:").strip()


def schedule(entry: dict) -> None:
    """Register (or replace) the entry's job. Raises ToolError if Windows refuses it."""
    fd, path = tempfile.mkstemp(prefix="ultron-task-", suffix=".xml")
    try:
        with open(fd, "w", encoding="utf-16") as fh:  # the encoding the XML declares
            fh.write(task_xml(entry))
        code, message = schtasks(["/Create", "/TN", entry["task"], "/XML", path, "/F"])
    finally:
        Path(path).unlink(missing_ok=True)
    if code != 0:
        raise ToolError(f"Windows Task Scheduler wouldn't take the reminder ({message or f'error {code}'}).")


def unschedule(item_id: str) -> bool:
    """Delete an entry's job. A failure is only logged: the fire script ignores entries not in the store."""
    code, message = schtasks(["/Delete", "/TN", task_name(item_id), "/F"])
    if code != 0:
        log.info("couldn't delete the job for %s: %s", item_id, message)
    return code == 0


# ---- the tools ------------------------------------------------------------------------------

def _new_entry(kind: str, text: str, plan: When, now: datetime, seconds: int | None = None) -> dict:
    item_id = uuid.uuid4().hex[:10]
    entry = {"id": item_id, "kind": kind, "text": text, "due": plan.due.isoformat(timespec="seconds"),
             "days": list(plan.days) if plan.days else None, "created": now.isoformat(timespec="seconds"),
             "task": task_name(item_id)}
    if seconds is not None:
        entry["seconds"] = seconds
    return entry


def _add(entry: dict, now: datetime) -> None:
    """Save first, then schedule: the fire script ignores jobs whose entry isn't saved."""
    with _locked():
        _save(_load(now) + [entry])
    try:
        schedule(entry)
    except BaseException:
        try:
            with _locked():
                _save([e for e in _read() or [] if e["id"] != entry["id"]])
        except ToolError:
            log.exception("couldn't take %s back out of the store", entry["id"])
        raise


def _reminder_text(text: str) -> str:
    what = " ".join(str(text).split()).strip(" .,;:!?")
    what = re.sub(r"^(?:to|about)\s+", "", what, flags=re.IGNORECASE)
    if not what:
        raise ToolError("What should I remind you about?")
    if len(what) > MAX_TEXT:
        what = what[:MAX_TEXT].rsplit(" ", 1)[0] + "..."
    return what


def _when_said(entry: dict, now: datetime) -> str:
    due = _next(entry, now)
    if entry.get("days"):
        return f"{_clock(due)} {_days_phrase(entry['days'])}"
    return f"{_clock(due)} {_day_phrase(due.date(), now.date())}"


def _line(entry: dict, now: datetime) -> str:
    if entry["kind"] == "timer":
        due = _next(entry, now)
        return f"{entry['text']}, {_left((due - now).total_seconds())} left (goes off at {_clock(due)})"
    return f"{entry['text']}, {_when_said(entry, now)}"


def _numbered(items: list[dict], now: datetime) -> str:
    return "\n".join(f"{i}. {_line(e, now)}" for i, e in enumerate(items, 1))


@tool(level=Level.SAFE, timeout=15.0, cloud=False)
def create_reminder(text: str, when: str) -> str:
    """Set a reminder that pops up on screen at a given time, even if Ultron is closed.

    text: what to remind about, e.g. "call mom"
    when: in plain words, e.g. "at 6pm", "in 20 minutes", "tomorrow at 9", "on friday at 3pm", "every weekday at 7am"
    """
    what = _reminder_text(text)
    now = _now()
    plan = parse_when(when, now)
    entry = _new_entry("reminder", what, plan, now)
    _add(entry, now)
    if plan.days:
        first = _day_phrase(plan.due.date(), now.date()).removeprefix("on ")
        return f"Reminder set for {_when_said(entry, now)}, starting {first}: {what}."
    return f"Reminder set for {_when_said(entry, now)}: {what}."


@tool(level=Level.SAFE, timeout=15.0, cloud=False)
def start_timer(minutes: int | None = None, seconds: int | None = None) -> str:
    """Start a countdown timer that pops up when it ends, even if Ultron is closed.

    minutes: whole minutes (90 for an hour and a half)
    seconds: extra seconds, or the whole length of a short timer
    """
    if (minutes or 0) < 0 or (seconds or 0) < 0:
        raise ToolError("A timer can't run backwards.")
    total = (minutes or 0) * 60 + (seconds or 0)
    if total <= 0:
        raise ToolError("How long should the timer run?")
    if total > MAX_TIMER:
        raise ToolError("Timers go up to 24 hours. Set a reminder for anything longer.")
    now = _now()
    due = now + timedelta(seconds=total)
    _add(_new_entry("timer", f"{_span(total, adjective=True)} timer", When(due), now, seconds=total), now)
    day = "" if due.date() == now.date() else " tomorrow"
    return f"Timer set for {_span(total)}. It goes off at {_clock(due)}{day}."


@tool(level=Level.SAFE, timeout=15.0, cloud=False)
def list_reminders() -> str:
    """List the reminders and timers that are set, soonest first."""
    now = _now()
    with _locked():
        items = _load(now)
    if not items:
        return "No reminders or timers are set."
    timers = sum(e["kind"] == "timer" for e in items)
    counts = [f"{n} {noun}{'' if n == 1 else 's'}" for n, noun in ((len(items) - timers, "reminder"), (timers, "timer")) if n]
    return f"You have {' and '.join(counts)}:\n{_numbered(items, now)}"


_WHICH_NOISE = {"the", "my", "a", "an", "reminder", "reminders", "number", "no", "one", "about", "to", "for",
                "called", "that", "says", "saying", "set", "please", "of", "them"}
_WHICH_ALL = {"all", "everything", "every", "both"}


def _pick(items: list[dict], which: str, now: datetime) -> list[dict]:
    """The entries `which` means: words from the text, a list number, "timer", or "all"."""
    if not items:
        raise ToolError("There are no reminders or timers to cancel.")
    words = _clean(which).split()
    core = [w for w in words if w not in _WHICH_NOISE]
    timers = [e for e in items if e["kind"] == "timer"]
    if any(w in _WHICH_ALL for w in words) and set(core) <= _WHICH_ALL | {"timer", "timers", "and"}:
        return timers if "timers" in core and "reminders" not in words else items
    if len(core) == 1:
        index = core[0]
        number = _ORDINALS.get(index) or (-1 if index == "last" else None)
        if number is None and (m := re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", index)):
            number = int(m[1])
        if number is not None:
            if number == -1 or 1 <= number <= len(items):
                return [items[number - 1 if number > 0 else -1]]
            raise ToolError(f"There's no number {number}. You have {len(items)}:\n{_numbered(items, now)}")
        if index in ("timer", "timers"):
            if len(timers) == 1 or (timers and index == "timers"):
                return timers
            if not timers:
                raise ToolError("No timer is running.")
            raise ToolError(f"Which timer?\n{_numbered(timers, now)}")
    if not core:  # "cancel my reminder": fine if there's only one
        if len(items) == 1:
            return items
        raise ToolError(f"Which one?\n{_numbered(items, now)}")

    def matches(entry: dict) -> bool:
        hay = _clean(entry["text"]).split() + [entry["kind"]]
        return all(any(h.startswith(w) or (len(w) > 3 and h.startswith(w.removesuffix("s"))) for h in hay)
                   for w in core)

    found = [e for e in items if matches(e)]
    exact = [e for e in found if _clean(e["text"]) == " ".join(core)]
    if len(found) == 1 or len(exact) == 1:
        return exact if len(exact) == 1 else found
    if not found:
        raise ToolError(f"No reminder matches '{which}'. You have:\n{_numbered(items, now)}")
    raise ToolError(f"More than one matches '{which}'. Which number?\n{_numbered(found, now)}")


@tool(level=Level.CONFIRM, timeout=30.0, cloud=False)
def cancel_reminder(which: str) -> str:
    """Cancel a reminder or timer so it never goes off.

    which: words from its text (e.g. "dentist"), its number from list_reminders, "timer", or "all"
    """
    now = _now()
    with _locked():
        items = _load(now)
        chosen = _pick(items, which, now)
        gone = {e["id"] for e in chosen}
        _save([e for e in items if e["id"] not in gone])
    for entry in chosen:
        unschedule(entry["id"])
    if len(chosen) == 1:
        entry = chosen[0]
        if entry["kind"] == "timer":
            return f"Cancelled the {entry['text']}."
        return f"Cancelled the reminder: {entry['text']}, {_when_said(entry, now)}."
    return f"Cancelled all {len(chosen)}."


# ---- fast-path intents ----------------------------------------------------------------------
# Matched in full against the normalized transcript (see intents.py): lowercase, no punctuation
# ("6 p.m." arrives as "6 p m"), and "hey ultron", "please", "can you" ... already trimmed.

def _is_time(text: str, now: datetime) -> bool:
    try:
        parse_when(text, now)
    except NotATime:
        return False
    except ToolError:
        return True  # a time, but past or missing a part: the tool says so out loud
    return True


def _reminder_args(m: re.Match) -> dict | None:
    """Split "call mom at 6pm" / "in 20 minutes to stretch" into text and when, or decline."""
    rest = m["rest"].strip()
    if rest.startswith("that "):
        return None  # "remind me that the meeting moved": the model reads that better
    body = re.sub(r"^(?:to|about|of|for) ", "", rest)
    now = _now()
    for cut in re.finditer(r" (?:to|about) ", body):  # the time first: "at 6 to call mom"
        when, text = body[:cut.start()], body[cut.end():]
        if text and _is_time(when, now):
            return {"text": text, "when": when}
    words = body.split()
    for i in range(1, len(words)):  # the text first: "call mom at 6pm", longest time phrase wins
        when = " ".join(words[i:])
        if _is_time(when, now):
            return {"text": " ".join(words[:i]), "when": when}
    return None


def _timer_args(m: re.Match) -> dict | None:
    total = parse_span(re.sub(r"^(?:for|of) ", "", m["d"]))
    if total is None:
        return None
    return {"minutes": total // 60 or None, "seconds": total % 60 or None}


def _cancel_args(m: re.Match) -> dict:
    which = m["w"].strip()
    return {"which": f"{which} timer" if m[0].endswith("timer") else which}


_REMIND = r"(?:remind me|(?:set|add|create|make|schedule|put in) (?:me )?(?:a |an )?reminder)"
_CANCEL = r"(?:cancel|delete|remove|clear|get rid of)"
_LISTED = r"(?:reminders and timers|reminders|timers)"
_ORDINAL_SAID = r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|\d{1,2}(?:st|nd|rd|th)"

INTENTS: list[tuple[str, str, Callable[[re.Match], dict | None]]] = [
    # cancelling comes first, so "cancel the 10 minute timer" never starts one
    (rf"{_CANCEL} (?:all (?:of )?)?(?:my |the )?(?P<what>{_LISTED})", "cancel_reminder",
     lambda m: {"which": "all timers" if m["what"] == "timers" else "all"}),
    (r"(?:cancel|delete|remove|clear|get rid of|stop|turn off|kill) (?:the |my |that )?timer", "cancel_reminder",
     lambda m: {"which": "timer"}),
    (rf"{_CANCEL} (?:the |my )?reminder (?:number |no )?(?P<n>\d{{1,2}})", "cancel_reminder",
     lambda m: {"which": m["n"]}),
    (rf"{_CANCEL} (?:the |my )?(?P<n>{_ORDINAL_SAID}) (?:reminder|timer|one)", "cancel_reminder",
     lambda m: {"which": m["n"]}),
    (rf"{_CANCEL} (?:the |my )?reminder (?:about|for|to|called|saying|that says) (?P<w>.+)", "cancel_reminder",
     _cancel_args),
    (rf"{_CANCEL} (?:the |my )?(?P<w>.+?) (?:reminder|timer)", "cancel_reminder", _cancel_args),
    # listing
    (rf"(?:what|which) {_LISTED} (?:do i have|have i got|have i set|are (?:set|there|on|running))"
     rf"|what(?:s| s| is| are) (?:my |the )?{_LISTED}"
     rf"|(?:list|show|read|tell me|give me|check)(?: me)? (?:all )?(?:of )?(?:my |the )?{_LISTED}"
     rf"|(?:do i have|have i got|have i set|are there) any {_LISTED}(?: set| running| on| today)?"
     rf"|any {_LISTED}|my {_LISTED}"
     r"|(?:how much time|how long) (?:is )?(?:left|remaining) on (?:the |my )?timer"
     r"|what(?:s| s| is) left on (?:the |my )?timer|(?:check|how is|hows) (?:the |my )?timer",
     "list_reminders", lambda m: {}),
    # timers
    (r"(?:(?:set|start|create|make|put on|begin) (?:a |an |me a )?)?timer (?P<d>.+)", "start_timer", _timer_args),
    (r"(?:(?:set|start|create|make|put on) (?:a |an |me a )?)?(?P<d>.+?) (?:timer|countdown)", "start_timer",
     _timer_args),
    (r"count ?down (?P<d>.+)", "start_timer", _timer_args),
    # reminders
    (rf"{_REMIND} (?P<rest>.+)", "create_reminder", _reminder_args),
]
