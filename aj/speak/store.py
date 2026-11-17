"""What AJ (Speak) remembers between runs: where its window sits and whether Polite was on.

One small JSON file in the data folder. It is only a convenience, so every read and write fails
soft: a missing, unreadable or hand-edited file means the defaults, and a file that can't be
written means the position is forgotten, never a crash and never a window stuck off-screen.

The user, 2026-10-22: the window is draggable and its position is remembered. A screen can go away
(a monitor unplugged, a different resolution), so a saved position is used only if enough of the
window would still be on a screen; otherwise it goes back to the corner.
"""
import json
import logging
import os
from pathlib import Path

from aj.config import DATA_DIR

log = logging.getLogger(__name__)

PATH = DATA_DIR / "speak_window.json"
_LIMIT = 100_000  # no screen is this big: a larger number is a corrupt file
Rect = tuple[int, int, int, int]  # left, top, width, height
CORNER_MARGIN = 24
MIN_VISIBLE = 48  # how much of the window must be on a screen for a saved position to count


def _number(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and -_LIMIT < value < _LIMIT


def load(path: Path | None = None) -> dict:
    """{"x": int, "y": int, "polite": bool}, with only the keys that were saved and make sense."""
    try:
        data = json.loads(Path(PATH if path is None else path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # first run, or a file nobody can read: start fresh
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    if _number(data.get("x")) and _number(data.get("y")):
        out.update(x=data["x"], y=data["y"])
    if isinstance(data.get("polite"), bool):
        out["polite"] = data["polite"]
    return out


def save(data: dict, path: Path | None = None) -> bool:
    """Write it (to a temporary file first, so a crash can't leave half a file). False if it couldn't."""
    target = Path(PATH if path is None else path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, target)
        return True
    except (OSError, TypeError, ValueError) as exc:
        log.error("couldn't save the window position: %s", exc)
        return False


def corner(screen: Rect, width: int, height: int) -> tuple[int, int]:
    """Bottom right of a screen's usable area (the part the taskbar leaves), where it starts."""
    left, top, screen_w, screen_h = screen
    return left + screen_w - width - CORNER_MARGIN, top + screen_h - height - CORNER_MARGIN


def on_a_screen(x: int, y: int, width: int, height: int, screens: list[Rect]) -> bool:
    """Is enough of a window at (x, y) on some screen for him to find and grab it?"""
    need_w, need_h = min(width, MIN_VISIBLE), min(height, MIN_VISIBLE)
    for left, top, screen_w, screen_h in screens:
        across = min(x + width, left + screen_w) - max(x, left)
        down = min(y + height, top + screen_h) - max(y, top)
        if across >= need_w and down >= need_h:
            return True
    return False


def place(saved: dict, width: int, height: int, screens: list[Rect]) -> tuple[int, int]:
    """Where the window goes: where it was left, if that is still on a screen, else the corner of
    the first screen."""
    if "x" in saved and "y" in saved and on_a_screen(saved["x"], saved["y"], width, height, screens):
        return saved["x"], saved["y"]
    if not screens:
        return saved.get("x", 0), saved.get("y", 0)
    return corner(screens[0], width, height)
