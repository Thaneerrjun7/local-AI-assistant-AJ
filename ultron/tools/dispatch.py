"""Runs one tool call, safely, and writes it down.

Every call goes through Dispatcher.run, whether it came from the zero-LLM fast path or from the
model. The order of the checks is the design:

1. Unknown tool: refused. SivaSafe's permission gate answered "allowed" for any name it didn't
   recognise (it failed open). Here a name that isn't in the registry never runs.
2. BLOCKED or LOCKED: refused, with a message that says why, so the model stops asking.
3. Arguments are checked against the tool's schema and gently coerced ("35" -> 35, "Up" -> "up").
   Bad ones are rejected with a message naming the problem and the right call shape, so the model
   can fix it in one retry.
4. CONFIRM: the user is asked about these exact arguments. Anything but a clear yes is a no.
5. The tool runs on a daemon thread under its own timeout. A hung tool costs one stuck thread,
   never a frozen Ultron (SivaSafe's weather call once hung the whole session), and a daemon
   thread can't keep Ultron alive after he is turned off.

Every call, allowed or not, becomes one JSON line in the audit log.
"""
import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from ultron.config import DATA_DIR
from ultron.tools import registry
from ultron.tools.registry import Level, Param, ToolError, ToolSpec

AUDIT_PATH = DATA_DIR / "audit.jsonl"

log = logging.getLogger(__name__)
_AUDIT_LOCK = threading.Lock()
_TRUE = {"true", "yes", "y", "on", "1"}
_FALSE = {"false", "no", "n", "off", "0"}
_KIND_NAMES = {str: "text", int: "a whole number", float: "a number", bool: "true or false"}


class ToolResult(str):
    """What to tell the model or the user, plus how the call ended.

    It is a plain str to anyone who only wants the text. outcome is one of: ok, error (the tool
    raised ToolError), crash, timeout, declined, blocked, locked, unknown, bad_args.
    """
    outcome: str

    def __new__(cls, text: str, outcome: str) -> "ToolResult":
        self = super().__new__(cls, text)
        self.outcome = outcome
        return self

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


class BadArgs(ValueError):
    """Arguments that don't fit the tool's schema. The message is written for the model."""


class Dispatcher:
    def __init__(self, confirm: Callable[[str], bool], audit_path: Path = AUDIT_PATH):
        self.confirm = confirm
        self.audit_path = Path(audit_path)

    def run(self, name: str, args: Mapping | str | None = None) -> ToolResult:
        """Run one tool call. args may be a dict or a JSON string (llama-server builds differ)."""
        t0 = time.perf_counter()
        spec = registry.get(name)
        result, logged_args = self._run(spec, name, args)
        self._audit(name, logged_args, spec, result, t0)
        return result

    def _run(self, spec: ToolSpec | None, name: str, raw: Any) -> tuple[ToolResult, Any]:
        if spec is None:
            known = ", ".join(s.name for s in registry.specs() if s.level.runnable) or "none"
            return ToolResult(f"Refused: there is no tool called {name!r}. Tools: {known}.", "unknown"), raw
        if spec.level is Level.BLOCKED:
            return ToolResult(f"Refused: {name} is blocked. Ultron is not allowed to do that.", "blocked"), raw
        if spec.level is Level.LOCKED:
            return ToolResult(f"Refused: {name} is locked. It needs a password, and none is set up.",
                              "locked"), raw
        try:
            given = parse_args(raw)
            kwargs = validate(spec, given)
        except BadArgs as e:
            return ToolResult(f"Bad arguments for {name}: {e}. Call it as {spec.usage()}.", "bad_args"), raw
        if spec.level is Level.CONFIRM and not self._ask(spec, kwargs):
            return ToolResult(f"Not done: the user said no to {name}. Don't retry unless they ask again.",
                              "declined"), kwargs
        return execute(spec, kwargs), kwargs

    def _ask(self, spec: ToolSpec, kwargs: dict) -> bool:
        try:
            return self.confirm(confirm_question(spec, kwargs)) is True
        except Exception:
            log.exception("asking to confirm %s failed; treating it as no", spec.name)
            return False

    def _audit(self, name: str, args: Any, spec: ToolSpec | None, result: ToolResult, t0: float) -> None:
        entry = {"time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                 "tool": name, "args": args, "level": spec.level.name if spec else None,
                 "outcome": result.outcome, "ms": round((time.perf_counter() - t0) * 1000, 1),
                 "cloud": bool(spec and spec.cloud)}
        if not result.ok:
            entry["detail"] = str(result)
        line = json.dumps(entry, ensure_ascii=False, default=repr)
        try:
            with _AUDIT_LOCK:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            log.exception("could not write to the audit log %s", self.audit_path)


def confirm_question(spec: ToolSpec, kwargs: dict) -> str:
    """The yes/no question for a CONFIRM tool, naming the exact action and arguments."""
    if not kwargs:
        return f"About to run {spec.name}. Go ahead?"
    shown = ", ".join(f"{k} {_say(v)}" for k, v in kwargs.items())
    return f"About to run {spec.name}: {shown}. Go ahead?"


def execute(spec: ToolSpec, kwargs: dict) -> ToolResult:
    """Call the tool on a daemon thread and wait at most spec.timeout seconds for it."""
    box: dict[str, Any] = {}
    done = threading.Event()

    def work() -> None:
        try:
            box["value"] = spec.func(**kwargs)
        except BaseException as e:  # noqa: BLE001 - reported to the caller below
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=work, name=f"tool-{spec.name}", daemon=True).start()
    if not done.wait(spec.timeout):
        return ToolResult(f"{spec.name} took too long (over {spec.timeout:g} s) and was abandoned. "
                          "It may still finish in the background.", "timeout")
    error = box.get("error")
    if isinstance(error, ToolError):
        return ToolResult(str(error) or f"{spec.name} failed.", "error")
    if error is not None:
        log.error("tool %s crashed", spec.name, exc_info=error)
        return ToolResult(f"{spec.name} failed unexpectedly ({type(error).__name__}: {error}).", "crash")
    value = box.get("value")
    return ToolResult("Done." if value is None else str(value), "ok")


def parse_args(raw: Any) -> dict:
    """Arguments as a dict, from a dict, a JSON string, or nothing at all."""
    if raw is None or (isinstance(raw, (str, bytes)) and not raw.strip()):
        return {}
    for _ in range(2):  # some servers double-encode: a JSON string holding a JSON object
        if not isinstance(raw, (str, bytes)):
            break
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise BadArgs(f"the arguments are not valid JSON ({e})") from None
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise BadArgs(f"the arguments must be a JSON object, got {type(raw).__name__}")
    return dict(raw)


def validate(spec: ToolSpec, given: dict) -> dict:
    """Check and coerce arguments against the schema. Returns the keyword arguments to call with.

    A missing or null argument falls back to the function's default (models often send null for
    "not given"); a nullable one with no default gets None.
    """
    params = {p.name: p for p in spec.params}
    problems = [f"unknown argument {k!r}" for k in given if k not in params]
    kwargs: dict[str, Any] = {}
    for p in spec.params:
        value = given.get(p.name)
        if value is None:
            if p.required:
                problems.append(f"{p.name} is required")
            elif not p.has_default:  # nullable with no default
                kwargs[p.name] = None
            continue
        try:
            kwargs[p.name] = coerce(p, value)
        except BadArgs as e:
            problems.append(str(e))
    if problems:
        raise BadArgs("; ".join(problems))
    return kwargs


def coerce(p: Param, value: Any) -> Any:
    """One value converted to the parameter's type and checked against its enum."""
    if isinstance(value, (Mapping, list, tuple)):
        raise BadArgs(f"{p.name} must be {_KIND_NAMES[p.kind]}, not a {type(value).__name__}")
    if p.kind is str:
        value = value if isinstance(value, str) else str(value)
    elif p.kind is bool:
        value = _to_bool(p, value)
    else:
        value = _to_number(p, value)
    return _pick_choice(p, value) if p.choices is not None else value


def _to_bool(p: Param, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise BadArgs(f"{p.name} must be true or false, got {value!r}")


def _to_number(p: Param, value: Any) -> int | float:
    number: int | float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = value
    elif isinstance(value, str):
        text = value.strip().removesuffix("%").strip()
        try:
            number = int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                pass
    if number is not None and math.isfinite(number):
        if p.kind is float:
            return float(number)
        if float(number).is_integer():
            return int(number)
    raise BadArgs(f"{p.name} must be {_KIND_NAMES[p.kind]}, got {value!r}")


def _pick_choice(p: Param, value: Any) -> Any:
    if value in p.choices:  # already the right type: coerce() converted it first
        return value
    if isinstance(value, str):  # "Next Track" / "next-track" -> "next_track"
        wanted = _fold(value)
        for choice in p.choices:
            if _fold(choice) == wanted:
                return choice
    shown = ", ".join(f'"{c}"' if isinstance(c, str) else str(c) for c in p.choices)
    raise BadArgs(f"{p.name} must be one of {shown}, got {value!r}")


def _fold(text: str) -> str:
    return re.sub(r"[\s_-]+", "_", text.strip().lower())


def _say(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f'"{value}"' if isinstance(value, str) else str(value)
