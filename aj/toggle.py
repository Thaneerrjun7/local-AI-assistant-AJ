"""The AJ shortcut. If AJ is off, turn him on (his window opens); if he's already on,
bring his window to the front, waking him if he's asleep (sleep mode, 2026-10-22). It never
turns him off: that's the window's Close button, the tray's Turn off or "Computer, turn off", so
a stray click on the shortcut can't cut him off mid-sentence.

The desktop shortcut runs `pythonw -m aj.toggle`, so no console window appears. The wake
listener (aj/listener.py) can start him the same way with start(["--wake-command", wav]).
"""
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from aj.config import APP_DIR
from aj.core import switch

# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | NORMAL_PRIORITY_CLASS: started from the listener
# (which runs below normal), AJ would otherwise inherit its low priority.
_DETACHED = 0x00000008 | 0x00000200 | 0x00000020


def start(extra_args: Sequence[str] = (), env: Mapping[str, str] | None = None) -> subprocess.Popen:
    """Start AJ detached with the venv's pythonw (no console), so he outlives whoever started him.
    `extra_args` go after `-m aj` (the listener's `--wake-command <wav>`)."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return subprocess.Popen([str(pythonw if pythonw.exists() else exe), "-m", "aj", *extra_args], cwd=APP_DIR,
                            env=dict(env) if env is not None else None, creationflags=_DETACHED, close_fds=True)


def main() -> None:
    if switch.is_running():
        switch.request_show()  # no-op while he's still starting (the window is on its way)
        return
    start()


if __name__ == "__main__":
    main()
