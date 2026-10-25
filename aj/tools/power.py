"""The laptop's own power: lock, sleep, restart, shut down, sign out, and calling a shutdown off.

Left out at first (docs/PLAN.md, "Deliberately not included"); the boss asked for them on
2026-10-18, after AJ had claimed "Screen is now locked." and "System is now powered down."
with no way to do either. Now he can, honestly:
- Locking is harmless (a sign-in brings everything back), so it runs at once.
- Everything else can lose unsaved work, so the boss says yes in the window first.
- Restart and shut down go through Windows' own shutdown.exe with a 30-second countdown,
  which Windows shows on screen; "cancel the shutdown" stops it. Sleep and sign-out wait a
  few seconds first, so he can say so before the laptop goes.
- No shell, no command-line text from the model: fixed argument lists only.
"""
import ctypes
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from aj.tools.registry import Level, ToolError, tool

NO_WINDOW = 0x08000000
COUNTDOWN_SECS = 30  # restart and shut down: Windows counts this down on screen
SOON_SECS = 4.0      # sleep and sign-out: long enough to say so first
_SHUTDOWN = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "shutdown.exe")
_NOTHING_PENDING = 1116  # shutdown /a with no shutdown in progress
_ALREADY_PENDING = 1190  # shutdown /s or /r with one already on its way

_pending: threading.Timer | None = None  # a sleep or sign-out about to happen
_lock = threading.Lock()


def _shutdown_exe(args: list[str]) -> int:
    try:
        done = subprocess.run([_SHUTDOWN, *args], capture_output=True, text=True, timeout=10,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolError(f"Windows wouldn't run its shutdown command ({exc}).") from None
    return done.returncode


def _start_countdown(args: list[str], what: str) -> None:
    code = _shutdown_exe(args)
    if code == _ALREADY_PENDING:
        raise ToolError("A shutdown or restart is already counting down. Say cancel the shutdown to stop it.")
    if code != 0:
        raise ToolError(f"Windows wouldn't start the {what}.")


def _soon(action: Callable[[], None]) -> None:
    global _pending
    with _lock:
        if _pending is not None:
            _pending.cancel()
        _pending = threading.Timer(SOON_SECS, action)
        _pending.daemon = True
        _pending.start()


def _suspend() -> None:
    ctypes.WinDLL("powrprof").SetSuspendState(False, False, False)


def _sign_out_now() -> None:
    _shutdown_exe(["/l"])


@tool(level=Level.SAFE, timeout=3.0, cloud=False)
def lock_screen() -> str:
    """Lock the laptop: the sign-in screen comes up, and everything stays open behind it."""
    if not ctypes.WinDLL("user32").LockWorkStation():
        raise ToolError("Windows wouldn't lock the screen.")
    return "Locked."


@tool(level=Level.CONFIRM, timeout=5.0, cloud=False, ask="Put the laptop to sleep?")
def sleep_laptop() -> str:
    """Put the laptop to sleep, a few seconds from now."""
    _soon(_suspend)
    return "Going to sleep in a few seconds."


@tool(level=Level.CONFIRM, timeout=12.0, cloud=False,
      ask=f"Restart the laptop in {COUNTDOWN_SECS} seconds? Anything unsaved could be lost.")
def restart_laptop() -> str:
    """Restart the laptop after a 30-second countdown that cancel_shutdown can stop."""
    _start_countdown(["/r", "/t", str(COUNTDOWN_SECS), "/c", "AJ is restarting the laptop."], "restart")
    return f"Restarting in {COUNTDOWN_SECS} seconds. Say cancel the shutdown to stop it."


@tool(level=Level.CONFIRM, timeout=12.0, cloud=False,
      ask=f"Shut the laptop down in {COUNTDOWN_SECS} seconds? Anything unsaved could be lost.")
def shutdown_laptop() -> str:
    """Shut the laptop down after a 30-second countdown that cancel_shutdown can stop."""
    _start_countdown(["/s", "/t", str(COUNTDOWN_SECS), "/c", "AJ is shutting the laptop down."], "shutdown")
    return f"Shutting down in {COUNTDOWN_SECS} seconds. Say cancel the shutdown to stop it."


@tool(level=Level.CONFIRM, timeout=5.0, cloud=False, ask="Sign out of Windows? Your apps will close.")
def sign_out() -> str:
    """Sign out of Windows, a few seconds from now. Open apps close."""
    _soon(_sign_out_now)
    return "Signing you out in a few seconds."


@tool(level=Level.SAFE, timeout=12.0, cloud=False)
def cancel_shutdown() -> str:
    """Stop a pending shutdown, restart, sleep or sign-out."""
    global _pending
    with _lock:
        timer, _pending = _pending, None
    if timer is not None and timer.is_alive():
        timer.cancel()
        return "Cancelled. The laptop stays on."
    code = _shutdown_exe(["/a"])
    if code == 0:
        return "Cancelled. The laptop stays on."
    if code == _NOTHING_PENDING:
        raise ToolError("There's no shutdown or restart to cancel.")
    raise ToolError("Windows wouldn't cancel the shutdown.")


_DEVICE = r"(?:the |my |this )?(?:laptop|computer|pc|machine|system|device)"
# The assistant answers to "Computer" too, and has a sleep mode of its own (core/wake.py,
# 2026-10-22): "sleep, computer" said to it means that, so after "sleep" a bare "computer" isn't
# the laptop here. "Sleep the computer" and "put the computer to sleep" still are (with a yes).
_SLEEPER = r"(?:(?:the |my |this )?(?:laptop|pc|machine|system|device)|(?:the |my |this )computer)"
INTENTS: list[tuple[str, str, Callable]] = [
    (r"lock(?: up)?(?: (?:the |my |this )?(?:screen|laptop|computer|pc|machine|it))?", "lock_screen", lambda m: {}),
    # "Do me a favor and close the screen" (the boss, 2026-10-19) means lock it.
    (r"(?:close|shut|turn off|switch off|black out) (?:the |my |this )?screen", "lock_screen", lambda m: {}),
    (rf"(?:put|send) {_DEVICE} (?:to sleep|in(?:to)? sleep(?: mode)?)|sleep {_SLEEPER}|{_SLEEPER}(?: go)? to sleep",
     "sleep_laptop", lambda m: {}),
    (rf"(?:restart|reboot) {_DEVICE}|reboot", "restart_laptop", lambda m: {}),
    (rf"(?:shut ?down|turn off|switch off|power (?:off|down)) {_DEVICE}", "shutdown_laptop", lambda m: {}),
    (r"(?:sign|log) (?:me )?out(?: of (?:the |my )?(?:laptop|computer|pc|windows))?", "sign_out", lambda m: {}),
    (r"(?:cancel|abort|stop) (?:the )?(?:shut ?down|restart|reboot|sign ?out|log ?out|sleep)", "cancel_shutdown",
     lambda m: {}),
]
