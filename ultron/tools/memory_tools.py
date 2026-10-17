"""Voice access to Ultron's memory: "what do you know about me" and "forget X".

Forgetting is CONFIRM: it's the one memory action that loses something you told him, so he
asks first. The fact's old value still goes into fact_history, so a mistaken forget can be
found again.
"""
import re

from ultron import memory
from ultron.tools.registry import Level, tool


@tool(level=Level.SAFE, timeout=5.0)
def what_do_you_know() -> str:
    """Say what Ultron remembers about the user."""
    return memory.what_do_you_know()


@tool(level=Level.CONFIRM, timeout=5.0)
def forget_fact(what: str) -> str:
    """Forget something Ultron remembers about the user.

    what: the fact to forget, in the user's words, e.g. my old address
    """
    return memory.forget(what)


INTENTS = [
    (r"what (do you|did you) (know|remember) about me|what do you remember", "what_do_you_know", lambda m: {}),
    (r"forget (?:that |about )?(?P<what>.+)", "forget_fact",
     lambda m: None if re.fullmatch(r"it|everything|all of it", m["what"]) else {"what": m["what"]}),
]
