"""Who has the boss's words right now: AJ (Speak), or the assistant's own dictation.

The boss can open AJ (Speak) by voice ("AJ, speak") or by hand while the assistant is on and
listening. Both hear the same microphone, so what he says for AJ (Speak) to type would also reach
the assistant as a command (the review of 2026-10-22). So:
- AJ (Speak) raises a named Windows event, Local\\AJ.Speak.Listening, for as long as a dictation of
  its own is under way (listening, writing it down, typing). The assistant's pipeline and the wake
  listener drop every utterance that ends while it is raised: not shown, answered or logged.
- The assistant raises Local\\AJ.Dictating while its own dictation waits for his words
  ("write this down"). AJ (Speak) won't start a dictation then, and the assistant won't arm one
  while AJ (Speak) is listening: each says the other is busy.

The events are manual-reset and belong to the process that made them: if it ends, the event goes
with it, so a crash never leaves the other side deaf. Reading one opens it by name and waits zero
milliseconds: a few microseconds, done once per utterance.

The names start with AJ_SHARE_NAMESPACE ("AJ" unless set): the test suite sets a namespace of its
own (tests/__init__.py), so a test can never make the boss's running assistant deaf.
"""
import ctypes
import os
import threading
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.SetEvent.restype = wintypes.BOOL
_kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
_kernel32.ResetEvent.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0


def _name(what: str) -> str:
    return f"Local\\{os.environ.get('AJ_SHARE_NAMESPACE') or 'AJ'}.{what}"


SPEAK_LISTENING = "Speak.Listening"
AJ_DICTATING = "Dictating"


class Flag:
    """A named event this process raises while it has the boss's words. Made on first use."""

    def __init__(self, what: str):
        self.what = what
        self._handle = None
        self._lock = threading.Lock()

    def set(self, on: bool = True) -> None:
        with self._lock:
            if self._handle is None:
                if not on:
                    return
                self._handle = _kernel32.CreateEventW(None, True, False, _name(self.what))  # manual reset
                if not self._handle:
                    raise OSError(f"couldn't make the event {_name(self.what)} (error {ctypes.get_last_error()})")
            (_kernel32.SetEvent if on else _kernel32.ResetEvent)(self._handle)

    def clear(self) -> None:
        self.set(False)

    def close(self) -> None:
        with self._lock:
            if self._handle:
                _kernel32.CloseHandle(self._handle)
                self._handle = None


def is_set(what: str) -> bool:
    """Is that event raised by some process right now? False when nobody made it."""
    handle = _kernel32.OpenEventW(_SYNCHRONIZE, False, _name(what))
    if not handle:
        return False
    try:
        return _kernel32.WaitForSingleObject(handle, 0) == _WAIT_OBJECT_0
    finally:
        _kernel32.CloseHandle(handle)


def speak_listening() -> bool:
    """Is AJ (Speak) taking a dictation right now? Then the boss's words are for it."""
    return is_set(SPEAK_LISTENING)


def aj_dictating() -> bool:
    """Is the assistant waiting for words to type ("write this down")?"""
    return is_set(AJ_DICTATING)


# Each process raises only its own: the assistant AJ_DICTATING, AJ (Speak) SPEAK_LISTENING.
dictating_flag = Flag(AJ_DICTATING)
speak_flag = Flag(SPEAK_LISTENING)
