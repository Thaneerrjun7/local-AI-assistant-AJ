"""AJ's manual on/off switch.

Exactly one AJ runs at a time (a named mutex). "Off" is a named event the running
AJ waits on: setting it makes AJ exit cleanly, and the job object in proc.py
takes llama-server down with it, so off really frees everything.

"Show" is a second named event: clicking the shortcut while AJ is on brings his
window to the front instead of turning him off (off is the window's Close button).
"""
import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_user32.AllowSetForegroundWindow.restype = wintypes.BOOL
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenMutexW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_MUTEX_NAME = "Local\\AJ.Instance"
_STOP_NAME = "Local\\AJ.Stop"
_SHOW_NAME = "Local\\AJ.Show"
_ERROR_ALREADY_EXISTS = 183
_SYNCHRONIZE = 0x00100000
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF
_ASFW_ANY = 0xFFFFFFFF  # (DWORD)-1: any process may take the foreground
_held = []  # keep handles alive for the life of the process


def is_running() -> bool:
    handle = _kernel32.OpenMutexW(_SYNCHRONIZE, False, _MUTEX_NAME)
    if handle:
        _kernel32.CloseHandle(handle)
        return True
    return False


def claim_instance() -> bool:
    """Call once at startup. False means another AJ is already on."""
    handle = _kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _held.append(handle)
    return True


def request_off() -> bool:
    """Ask the running AJ to turn off. False if none is running."""
    handle = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, _STOP_NAME)
    if not handle:
        return False
    _kernel32.SetEvent(handle)
    _kernel32.CloseHandle(handle)
    return True


def watch_for_off(stop: threading.Event) -> None:
    """Start a thread that sets `stop` when someone flips the switch off."""
    event = _kernel32.CreateEventW(None, True, False, _STOP_NAME)
    _kernel32.ResetEvent(event)
    _held.append(event)

    def wait() -> None:
        while not stop.is_set():
            if _kernel32.WaitForSingleObject(event, 250) == _WAIT_OBJECT_0:
                stop.set()

    threading.Thread(target=wait, daemon=True, name="off-switch").start()


def request_show() -> bool:
    """Ask the running AJ to bring his window to the front. False if no window is up yet."""
    handle = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, _SHOW_NAME)
    if not handle:
        return False
    # Windows lets only the process the user just clicked take the foreground, so hand that
    # right on; without it the window would just flash in the taskbar.
    _user32.AllowSetForegroundWindow(_ASFW_ANY)
    _kernel32.SetEvent(handle)
    _kernel32.CloseHandle(handle)
    return True


def watch_for_show(on_show: Callable[[], None]) -> None:
    """Start a thread that calls `on_show` (on that thread) each time someone asks for the
    window. It blocks in the kernel between requests, so it costs nothing while idle."""
    event = _kernel32.CreateEventW(None, False, False, _SHOW_NAME)  # auto-reset: one request, one call
    _held.append(event)

    def wait() -> None:
        while _kernel32.WaitForSingleObject(event, _INFINITE) == _WAIT_OBJECT_0:
            on_show()

    threading.Thread(target=wait, daemon=True, name="show-switch").start()
