"""The zero-LLM fast path: a transcript straight to a tool call, in well under a millisecond.

"Open WhatsApp" should not wait for the model to work out that it means open_app(name="whatsapp").
Each tool module lists the commands it can recognise on its own:

    INTENTS = [
        (r"(open|launch|start) (?P<name>.+)", "open_app", lambda m: {"name": m["name"]}),
    ]

How the rules work:
- A pattern must match the WHOLE normalized transcript (re.fullmatch), so "how do I open a jar"
  never fires open_app. ^ and $ are allowed but not needed. Matching ignores case.
- Patterns see normalized text: lowercase, single spaces, no punctuation ("what's" -> "whats",
  "35%" -> "35", but "5:30" and "2.5" keep their digits together), with leading "hey", "ultron",
  "please", "can you" and trailing "please", "now", "ultron", "for me" removed.
- The builder turns the match into the tool's arguments. It may return None to decline, and the
  next rule is tried. None values are dropped. The dispatcher still validates and coerces
  everything, so {"level": "35"} is fine for an int.
- First match wins: modules in tools.MODULES order, then list order. Put specific rules first.
Anything no rule claims goes to the LLM as usual. A broken rule is logged and skipped.
"""
import logging
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import ModuleType

from ultron.tools import registry

log = logging.getLogger(__name__)

_APOSTROPHES = re.compile(r"['‘’`]")
_PUNCTUATION = re.compile(r"(?!(?<=\d)[.:](?=\d))[^\w\s]|_")  # keeps "5:30" and "2.5" whole
_SPACES = re.compile(r"\s+")
_LEADING = re.compile(r"^(?:(?:hey|hi|ok|okay|ultron|please|(?:can|could|would) you)\s+)+")
_TRAILING = re.compile(r"(?:\s+(?:please|now|ultron|for me))+$")


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    tool: str
    build: Callable[[re.Match], dict | None]
    source: str  # the module it came from, for log messages


_rules: list[Rule] | None = None  # built on first use, rebuilt by every load_all()


def normalize(transcript: str) -> str:
    text = _APOSTROPHES.sub("", transcript.lower())
    text = _SPACES.sub(" ", _PUNCTUATION.sub(" ", text)).strip()
    return _TRAILING.sub("", _LEADING.sub("", text))


def match(transcript: str) -> tuple[str, dict] | None:
    """(tool name, arguments) for a command the fast path recognises, otherwise None."""
    rules = _rules if _rules is not None else _collect_now()
    text = normalize(transcript)
    if not text:
        return None
    for rule in rules:
        found = rule.pattern.fullmatch(text)
        if found is None:
            continue
        try:
            args = rule.build(found)
        except Exception:
            log.exception("intent for %s from %s failed on %r; skipping it", rule.tool, rule.source, text)
            continue
        if args is None:
            continue
        if not isinstance(args, dict):
            log.warning("intent for %s from %s returned %r, not a dict; skipping it", rule.tool, rule.source, args)
            continue
        return rule.tool, {k: v for k, v in args.items() if v is not None}
    return None


def collect(modules: Iterable[ModuleType] | None = None) -> int:
    """(Re)build the rule table from the modules' INTENTS. Default: every loaded tool module.

    Returns how many rules were accepted.
    """
    global _rules
    if modules is None:
        modules = _loaded_tool_modules()
    rules = []
    for module in modules:
        for entry in getattr(module, "INTENTS", None) or ():
            rule = _compile(module.__name__, entry)
            if rule is not None:
                rules.append(rule)
    _rules = rules
    return len(rules)


def _collect_now() -> list[Rule]:
    collect()
    return _rules or []


def _loaded_tool_modules() -> list[ModuleType]:
    from ultron import tools  # here, not at the top: ultron.tools imports this module
    names = (f"{tools.__name__}.{name}" for name in tools.MODULES)
    return [sys.modules[n] for n in names if n in sys.modules]


def _compile(source: str, entry: object) -> Rule | None:
    try:
        pattern, tool_name, build = entry  # type: ignore[misc]
        compiled = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern, re.IGNORECASE)
    except (TypeError, ValueError, re.error) as e:
        log.warning("%s: skipping a malformed intent %r (%s)", source, entry, e)
        return None
    if not callable(build):
        log.warning("%s: skipping the intent for %s: its builder isn't callable", source, tool_name)
        return None
    if registry.get(tool_name) is None:
        log.warning("%s: skipping an intent for unknown tool %r", source, tool_name)
        return None
    return Rule(compiled, tool_name, build, source)
