"""The global hotkey that starts and stops AJ (Speak) without clicking it.

The user, 2026-10-22: "anywhere where I can type anything, I should be able to activate speech to
text." The window is always there, but a click means moving the mouse away from what he is writing,
so Ctrl+Alt+S does the same as the mic button, from any app.

It is Windows' own RegisterHotKey, not a keyboard hook: Windows hands this thread one message per
press, nothing watches his other keystrokes, and the thread sleeps in GetMessageW the rest of the
time. If another program already holds Ctrl+Alt+S the next choice is Ctrl+Alt+D, and the window's
tooltip says which one is live. The assistant's own hotkey (Settings.hotkey, Ctrl+Alt+U) is never
taken, whatever it is set to.

`keys` is the part that talks to Windows; the tests give it a fake, so no test ever registers a
real hotkey on the desktop.
"""
import ctypes
import logging
import threading
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000  # no repeats while the keys are held down
_WM_HOTKEY, _WM_QUIT = 0x0312, 0x0012
_ID = 0x5350  # the hotkey's id on this thread ("SP")


@dataclass(frozen=True)
class Chord:
    label: str
    mods: int
    vk: int


CHORDS = (Chord("Ctrl+Alt+S", MOD_CONTROL | MOD_ALT, ord("S")),
          Chord("Ctrl+Alt+D", MOD_CONTROL | MOD_ALT, ord("D")))


class Win32Keys:
    """RegisterHotKey on the calling thread's message queue (so register(), wait() and
    unregister() must all be called from the same thread)."""

    def __init__(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._user32, self._kernel32 = user32, kernel32
        self._thread_id = 0

    def register(self, key_id: int, chord: Chord) -> bool:
        self._thread_id = self._kernel32.GetCurrentThreadId()
        ok = self._user32.RegisterHotKey(None, key_id, chord.mods | MOD_NOREPEAT, chord.vk)
        if not ok:
            log.warning("couldn't register %s (error %s)", chord.label, ctypes.get_last_error())
        return bool(ok)

    def unregister(self, key_id: int) -> None:
        self._user32.UnregisterHotKey(None, key_id)

    def wait(self) -> int | None:
        """Sleep until a registered hotkey is pressed and return its id; None once wake() is called."""
        msg = wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == _WM_HOTKEY:
                return int(msg.wParam)
        return None

    def wake(self) -> None:
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)


class Hotkey:
    def __init__(self, on_press: Callable[[], None], chords: Sequence[Chord] = CHORDS, keys=None,
                 avoid: Sequence[str] = ()):
        """`on_press` runs on the hotkey's own thread (the window turns it into a Qt signal).
        `avoid`: combinations in use by something else ("ctrl+alt+u"), never taken."""
        self.on_press = on_press
        skip = {a.replace(" ", "").lower() for a in avoid}
        self.chords = [c for c in chords if c.label.lower() not in skip]
        self.keys = keys
        self.chord: Chord | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, timeout: float = 3.0) -> Chord | None:
        """Register the first free combination. Returns it, or None when all are taken (the window
        then works by clicking only)."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="speak-hotkey")
        self._thread.start()
        self._ready.wait(timeout)
        return self.chord

    def stop(self) -> None:
        if self._thread is not None and self.keys is not None:
            self.keys.wake()
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        try:
            self.keys = self.keys or Win32Keys()
            for chord in self.chords:
                if self.keys.register(_ID, chord):
                    self.chord = chord
                    break
        except Exception:  # noqa: BLE001 - no hotkey is not a reason to lose the window
            log.exception("the hotkey could not be set up")
        finally:
            self._ready.set()
        if self.chord is None:
            return
        try:
            while (hit := self.keys.wait()) is not None:
                if hit != _ID:
                    continue
                try:
                    self.on_press()
                except Exception:  # noqa: BLE001 - one bad press must not end the hotkey
                    log.exception("the hotkey's action failed")
        finally:
            self.keys.unregister(_ID)


def describe(chord: Chord | None, chords: Sequence[Chord] = CHORDS) -> str:
    """What the tooltip says about the hotkey, including when the first choice was taken."""
    if chord is None:
        return f"no hotkey ({' and '.join(c.label for c in chords)} are both taken)"
    if chord != chords[0]:
        return f"{chord.label} ({chords[0].label} is taken by another program)"
    return chord.label
