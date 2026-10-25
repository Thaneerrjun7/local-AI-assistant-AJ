"""The laptop's live status as a tool, for when the "[Laptop now: ...]" note isn't enough, or for
"what apps are open?" and "how's my laptop doing?" said as instant commands.

The engine sets `monitor` while AJ is on (core/laptop_state.py keeps it fresh); without
one, as in tests and scripts, the status is read on the spot.
"""
import re
import time
from collections.abc import Callable

from aj.core import laptop_state
from aj.tools.registry import Level, tool

monitor: laptop_state.Monitor | None = None


@tool(level=Level.SAFE, timeout=8.0, cloud=False)
def laptop_status() -> str:
    """The laptop's live status: battery and charger, open apps and which is in front, volume, brightness, internet, CPU, RAM and free disk space."""
    if monitor is not None:
        snap = monitor.snapshot(fresh=True)
    else:
        before = laptop_state.cpu_times()
        time.sleep(0.25)  # the CPU load is the change over a moment
        snap = laptop_state.read(cpu_before=before)
    return f"Laptop now: {snap.summary()}."


_OPEN = r"(?:open|running|up)"
INTENTS: list[tuple[str, str, Callable[[re.Match], dict]]] = [
    (r"(?:(?:what(?:s| s| is) )?(?:the |my )?(?:laptop|system|computer|pc)(?:s| s)? (?:status|state)"
     r"|how(?:s| s| is) (?:the |my |this )?(?:laptop|computer|pc|system)(?: doing| looking)?"
     rf"|what (?:apps|programs|windows|applications) (?:are|do i have|have i got|is) {_OPEN}(?: right(?: now)?| now)?"
     rf"|what(?:s| s| is) {_OPEN}(?: right(?: now)?| now| on my (?:laptop|screen|computer))?"
     r"|(?:check|give me) (?:the |my )?(?:laptop|system) (?:status|state)"
     # RAM and CPU (live, 2026-10-19, this went to an online check instead)
     r"|(?:how much|what(?:s| s| is)(?: the| my)?) (?:ram|memory|cpu|processor)(?: usage| use| load| percentage| percent)?"
     r"(?: (?:is )?(?:being )?(?:used|in use|going on|left|free))?(?: right(?: now)?| now)?"
     r"|(?:is )?(?:my |the )?(?:ram|memory|cpu|laptop|computer) (?:full|busy|usage|use|high|load|maxed out)"
     r"|how (?:full|busy|hot) is (?:my |the )?(?:ram|memory|cpu|laptop|computer))",
     "laptop_status", lambda m: {}),
]
