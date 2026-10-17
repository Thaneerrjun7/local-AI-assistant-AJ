"""The one registry every tool lives in.

SivaSafe declared each tool twice, once as a hand-written JSON list for the model and once as a
branch of a 200-line if/elif chain in main.py, and the two drifted apart. Here a tool is a single
plain function. Its type hints become the JSON schema the model sees, the first line of its
docstring becomes the description, and the decorator records how far it is trusted, how long it
may run and whether it talks to the cloud. Nothing else has to be kept in step.

    from typing import Literal
    from ultron.tools.registry import tool, Level, ToolError

    @tool(level=Level.SAFE, timeout=2.0)
    def set_volume(level: int, mode: Literal["set", "up", "down"] = "set") -> str:
        \"\"\"Change the speaker volume.

        level: percent, 0 to 100
        \"\"\"
        ...
        return "Volume 35."

Parameter hints: str, int, float, bool, Literal[...] (an enum) and Optional[X] / X | None (not
required). Lines like "level: percent, 0 to 100" in the docstring become argument descriptions.
Anything unsupported fails at import, when it is cheap to fix, never halfway through a call.
"""
import inspect
import re
import types
import typing
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, TypeVar


class Level(Enum):
    """How far a tool is trusted to act on its own."""
    SAFE = "safe"        # runs straight away
    CONFIRM = "confirm"  # the user says yes or no first, to the exact arguments
    BLOCKED = "blocked"  # never runs
    LOCKED = "locked"    # reserved for future password-protected power tools; never runs today

    @property
    def runnable(self) -> bool:
        return self in (Level.SAFE, Level.CONFIRM)


class ToolError(Exception):
    """A failure the user should hear about, e.g. ToolError("There's no app called Spotfy.")."""


_JSON_TYPES: dict[type, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}
_EMPTY = inspect.Parameter.empty
# "name: what it is" or "name (int): what it is" on its own docstring line.
_NOTE = re.compile(r"^\s*(\w+)\s*(?:\([^)]*\))?\s*:\s*(\S.*?)\s*$")


@dataclass(frozen=True)
class Param:
    name: str
    kind: type                    # str, int, float or bool
    choices: tuple | None         # the Literal values, if any
    nullable: bool                # hinted Optional[X] / X | None
    required: bool                # no default and not nullable
    default: Any = _EMPTY
    description: str = ""

    @property
    def has_default(self) -> bool:
        return self.default is not _EMPTY

    def schema(self) -> dict:
        out: dict[str, Any] = {"type": _JSON_TYPES[self.kind]}
        if self.choices is not None:
            out["enum"] = list(self.choices)
        if self.description:
            out["description"] = self.description
        return out

    def usage(self) -> str:
        shape = " | ".join(_show(c) for c in self.choices) if self.choices else _JSON_TYPES[self.kind]
        return f"{self.name}{'' if self.required else '?'}: {shape}"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    func: Callable[..., Any]
    level: Level
    timeout: float
    cloud: bool
    params: tuple[Param, ...]
    module: str

    @property
    def parameters(self) -> dict:
        """The JSON schema of the arguments (a fresh dict each time, so callers may mutate it)."""
        schema: dict[str, Any] = {"type": "object",
                                  "properties": {p.name: p.schema() for p in self.params}}
        required = [p.name for p in self.params if p.required]
        if required:
            schema["required"] = required
        return schema

    def openai(self) -> dict:
        """This tool in the OpenAI "tools" format that llama-server accepts."""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}

    def usage(self) -> str:
        """The call shape for error messages: set_volume(level: integer, mode?: "set" | "up")."""
        return f"{self.name}({', '.join(p.usage() for p in self.params)})"


_TOOLS: dict[str, ToolSpec] = {}
F = TypeVar("F", bound=Callable[..., Any])


def tool(level: Level = Level.SAFE, timeout: float = 5.0, cloud: bool = False) -> Callable[[F], F]:
    """Register a function as a tool. The function itself is returned unchanged."""
    if callable(level) and not isinstance(level, Level):  # written as a bare @tool
        return tool()(level)

    def register(func: F) -> F:
        spec = build_spec(func, level, timeout, cloud)
        old = _TOOLS.get(spec.name)
        if old and (old.module, old.func.__qualname__) != (spec.module, func.__qualname__):
            raise ValueError(f"tool name {spec.name!r} is already taken by {old.module}")
        _TOOLS[spec.name] = spec
        return func
    return register


def get(name: str) -> ToolSpec | None:
    return _TOOLS.get(name)


def specs() -> list[ToolSpec]:
    """Every registered tool, in registration order."""
    return list(_TOOLS.values())


def openai_tools(names: Iterable[str] | None = None) -> list[dict]:
    """The tools list for llama-server.

    Pass the names the router picked (most relevant first) to keep the list short: a 4B model
    chooses far better from a handful of tools than from all of them. Unknown names are skipped,
    and BLOCKED/LOCKED tools are never offered, since the model would only be refused.
    """
    chosen = _TOOLS.values() if names is None else [_TOOLS[n] for n in dict.fromkeys(names) if n in _TOOLS]
    return [spec.openai() for spec in chosen if spec.level.runnable]


def unregister(name: str) -> bool:
    """Remove a tool (used when its module fails to load, and by tests)."""
    return _TOOLS.pop(name, None) is not None


def build_spec(func: Callable[..., Any], level: Level, timeout: float, cloud: bool) -> ToolSpec:
    """Read a function's signature, hints and docstring into a ToolSpec, or raise TypeError."""
    name = func.__name__
    if not isinstance(level, Level):
        raise TypeError(f"{name}: level must be a Level, got {level!r}")
    if not timeout > 0:
        raise ValueError(f"{name}: timeout must be positive, got {timeout!r}")
    lines = (inspect.getdoc(func) or "").splitlines()
    description = lines[0].strip() if lines else ""
    if not description:
        raise TypeError(f"{name}: a tool needs a docstring; its first line is what the model reads")
    notes = {m[1]: m[2] for m in map(_NOTE.match, lines[1:]) if m}
    hints = typing.get_type_hints(func)
    params = tuple(_param(name, p, hints, notes.get(p.name, ""))
                   for p in inspect.signature(func).parameters.values())
    return ToolSpec(name, description, func, level, float(timeout), bool(cloud), params, func.__module__)


def _param(tool_name: str, p: inspect.Parameter, hints: dict, note: str) -> Param:
    where = f"{tool_name}({p.name})"
    if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD, p.POSITIONAL_ONLY):
        raise TypeError(f"{where}: tools are called with named arguments only")
    if p.name not in hints:
        raise TypeError(f"{where}: needs a type hint")
    hint, nullable = _unwrap_optional(hints[p.name], where)
    kind, choices = _kind(hint, where)
    return Param(p.name, kind, choices, nullable, required=p.default is _EMPTY and not nullable,
                 default=p.default, description=note)


def _unwrap_optional(hint: Any, where: str) -> tuple[Any, bool]:
    if typing.get_origin(hint) not in (typing.Union, types.UnionType):
        return hint, False
    members = typing.get_args(hint)
    rest = [m for m in members if m is not type(None)]
    if len(rest) != 1 or len(members) != 2:
        raise TypeError(f"{where}: only X | None unions are supported, got {hint!r}")
    return rest[0], True


def _kind(hint: Any, where: str) -> tuple[type, tuple | None]:
    if hint in _JSON_TYPES:
        return hint, None
    if typing.get_origin(hint) is Literal:
        values = typing.get_args(hint)
        kinds = {bool if isinstance(v, bool) else type(v) for v in values}
        if len(kinds) == 1 and next(iter(kinds)) in _JSON_TYPES:
            return kinds.pop(), values
        raise TypeError(f"{where}: Literal values must all be str, int, float or bool, got {values!r}")
    raise TypeError(f"{where}: unsupported type {hint!r} (use str, int, float, bool, Literal or X | None)")


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return f'"{value}"' if isinstance(value, str) else str(value)
