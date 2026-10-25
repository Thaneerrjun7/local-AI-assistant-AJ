"""One AJ (Speak) at a time: a named mutex, and named events for "show yourself" and "listen now".

The same two Windows objects core/switch.py uses for the assistant, with names of their own. The
desktop shortcut can be double-clicked twice, and a second window sitting on the first would be
confusing, so a second launch finds the first and asks it to show itself, then exits.

The first one waits on the event in the kernel (no polling, no timer), so it costs nothing while
idle. The name is a parameter only so the tests can use names of their own and never meet a real
AJ (Speak) running on the desktop.

"Listen now" (2026-10-22): the assistant starts AJ (Speak) with --listen when the boss says "AJ,
speak"; when one is already running, the second launch sets this event as well as "show yourself".
"""
import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.SetEvent.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

NAME = "AJ.Speak"
_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF


class Instance:
    def __init__(self, name: str = NAME):
        self._mutex_name = f"Local\\{name}.Instance"
        self._show_name = f"Local\\{name}.Show"
        self._listen_name = f"Local\\{name}.Listen"
        self._mutex = None
        self._event = None
        self._listen_event = None
        self._stopping = threading.Event()

    def claim(self) -> bool:
        """Call once at start-up. False means another AJ (Speak) is already running."""
        handle = _kernel32.CreateMutexW(None, False, self._mutex_name)
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            return False
        self._mutex = handle
        return True

    def request_show(self, listen: bool = False) -> bool:
        """Ask the running one to show itself (and, with `listen`, to start listening). False if
        none is waiting yet (it is still starting)."""
        names = [self._show_name, *([self._listen_name] if listen else [])]
        found = False
        for name in names:
            handle = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
            if handle:
                _kernel32.SetEvent(handle)
                _kernel32.CloseHandle(handle)
                found = True
        return found

    def watch_for_show(self, on_show: Callable[[], None], on_listen: Callable[[], None] | None = None) -> None:
        """Call `on_show` (on a thread of its own) each time a second launch asks for the window,
        and `on_listen` when it asks for listening too."""
        self._event = _kernel32.CreateEventW(None, False, False, self._show_name)  # auto-reset
        self._wait_on(self._event, on_show, "speak-show")
        if on_listen is not None:
            self._listen_event = _kernel32.CreateEventW(None, False, False, self._listen_name)
            self._wait_on(self._listen_event, on_listen, "speak-listen")

    def _wait_on(self, event, call: Callable[[], None], name: str) -> None:
        def wait() -> None:
            while _kernel32.WaitForSingleObject(event, _INFINITE) == _WAIT_OBJECT_0:
                if self._stopping.is_set():
                    return
                call()

        threading.Thread(target=wait, daemon=True, name=name).start()

    def release(self) -> None:
        """Let go of both names (the process is ending, or a test is done)."""
        self._stopping.set()
        for event in (self._event, self._listen_event):
            if event:
                _kernel32.SetEvent(event)  # wakes the waiting thread so it can end
        if self._mutex:
            _kernel32.CloseHandle(self._mutex)
            self._mutex = None
