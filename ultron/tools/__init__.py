"""Ultron's tools: one module per area, each a handful of @tool functions plus optional INTENTS.

registry.py  the @tool decorator, Level, ToolError, and the JSON schemas the model sees
dispatch.py  Dispatcher.run: the one gate every call goes through (and the audit log)
intents.py   the zero-LLM fast path from transcript to tool call

load_all() imports the modules in MODULES. One that is missing or fails to import is logged and
skipped, and any tools it registered before failing are removed again: one broken file costs its
own tools, never Ultron, and never leaves half a module callable.
"""
import importlib
import importlib.util
import logging

from ultron.tools import registry

MODULES = ["apps", "system", "files", "reminders", "weather", "web", "memory_tools"]

log = logging.getLogger(__name__)


def load_all() -> list[str]:
    """Import every tool module that exists and works, rebuild the fast-path rules, and return
    the names of the modules that loaded."""
    from ultron.tools import intents  # here, not at the top: intents reads MODULES from this package
    loaded = [name for name in MODULES if _load(name)]
    intents.collect()
    return loaded


def _load(name: str) -> bool:
    full = f"{__name__}.{name}"
    try:
        if importlib.util.find_spec(full) is None:
            log.warning("tool module %s is missing; skipping it", name)
            return False
        importlib.import_module(full)
        return True
    except Exception:
        log.exception("tool module %s failed to load; skipping it", name)
        for spec in registry.specs():
            if spec.module == full:
                registry.unregister(spec.name)
        return False
