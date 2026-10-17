"""The Ultron shortcut. If Ultron is off, turn him on (his window opens); if he's already on,
bring his window to the front. It never turns him off: that's the window's Close button or
"Ultron, turn off", so a stray click on the shortcut can't cut him off mid-sentence.

The desktop shortcut runs `pythonw -m ultron.toggle`, so no console window appears.
"""
import subprocess
import sys
from pathlib import Path

from ultron.config import APP_DIR
from ultron.core import switch

_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def main() -> None:
    if switch.is_running():
        switch.request_show()  # no-op while he's still starting (the window is on its way)
        return
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    subprocess.Popen([str(pythonw if pythonw.exists() else exe), "-m", "ultron"], cwd=APP_DIR,
                     creationflags=_DETACHED, close_fds=True)


if __name__ == "__main__":
    main()
