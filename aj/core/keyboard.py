"""Typing and key presses into the window in front, through SendInput.

Two features need to type into another app: WhatsApp chat search (tools/whatsapp.py) and
dictation. SivaSafe pasted its text through the clipboard (pyperclip, then Ctrl+V), which threw
away whatever the user had copied. Here the clipboard is never touched: text is typed as keys.

Two kinds of key event, because neither reaches every app:
- A character the keyboard layout has a key for (with Shift at most) is typed as that key,
  exactly as the user would type it. Live (2026-10-22) WhatsApp's search box ignored Unicode key
  events completely and took these. Caps Lock is allowed for, so "Appa" stays "Appa".
- Anything else (emoji, characters this layout can't type, AltGr ones, which would mean holding
  Ctrl+Alt) goes as a Unicode key event (KEYEVENTF_UNICODE): a character outside the Basic
  Multilingual Plane as its two UTF-16 halves, back to back in one batch. Most apps take these
  (a test window did, emoji included); WhatsApp may drop them.

Enter is the dangerous key: WhatsApp, Teams and Discord send the message on Enter. So type_text
never sends one. A trailing newline is dropped, a carriage return is folded into the newline, and
other control characters are left out, since a typed "\\r" reaches a text box as Enter. A newline
inside the text becomes Shift+Enter only when the caller says the box is multi-line (`multiline`:
WhatsApp's message box, a document, an edit box UI Automation calls multi-line); everywhere else
it becomes a space, because Shift+Enter in a single-line Chrome input or the Run box submits it,
as Enter would (the review of 2026-10-22). Only press("enter") sends Enter, and only a caller that
means it uses it (whatsapp.py opening the top search result).

Long text goes in small batches with a short pause between them, so a busy WebView app
(WhatsApp, Teams) can keep up. Before anything is sent, a modifier the user is still holding (the
Ctrl of a shortcut, say) gets a moment to be let go, so a typed "a" can't turn into Ctrl+A. If one
is still held after that, nothing is sent at all (ModifiersHeld): until 2026-10-22 it typed anyway,
and letters typed with Ctrl down are shortcuts, not text (the review of 2026-10-22).

Every event carries _EXTRA_INFO in dwExtraInfo, so a keyboard hook can tell AJ's keys from
the user's. The SendInput call goes through the module-level _send_input so tests can replace it
and never touch the desktop (system.py does the same with _keybd_event).
"""
import ctypes
import os
import re
import time
from ctypes import wintypes


CHUNK_CHARS = 16      # characters per SendInput batch
CHUNK_PAUSE = 0.015   # seconds between batches, for WebView apps
MODIFIER_WAIT = 1.0   # how long a held Ctrl/Alt/Shift/Win gets to be released before typing
_POLL = 0.02

_INPUT_KEYBOARD = 1
_KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
_MAPVK_VK_TO_VSC = 0
_EXTRA_INFO = 0x414A4B59  # "AJKY": marks AJ's own injected keys

_VK_SHIFT, _VK_CONTROL, _VK_MENU, _VK_LWIN, _VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
_VK_RETURN = 0x0D
_VK_UNASSIGNED = 0xE8  # no key: pressing it only tells Windows this process had the last input

_KEYS: dict[str, int] = {
    "enter": 0x0D, "return": 0x0D, "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "tab": 0x09,
    "space": 0x20, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
    **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)},  # "a" -> VK 0x41
    **{str(d): 0x30 + d for d in range(10)},
    "/": 0xBF, ",": 0xBC, ".": 0xBE, "-": 0xBD, "=": 0xBB,
}
_MODIFIERS: dict[str, int] = {"ctrl": _VK_CONTROL, "control": _VK_CONTROL, "shift": _VK_SHIFT,
                              "alt": _VK_MENU, "win": _VK_LWIN}
# Keys Windows marks as "extended" (the grey cluster): without the flag, Home can arrive as numpad 7.
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, _VK_LWIN, _VK_RWIN}


class _KeybdInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):  # only here so the union, and so INPUT, has its real size
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeybdInput), ("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
_user32.MapVirtualKeyW.restype = wintypes.UINT
_user32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.HKL]
_user32.MapVirtualKeyExW.restype = wintypes.UINT
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
for _name in ("IsWindow", "IsIconic", "SetForegroundWindow", "BringWindowToTop"):
    getattr(_user32, _name).argtypes = [wintypes.HWND]
    getattr(_user32, _name).restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
_user32.SwitchToThisWindow.restype = None
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
_user32.EnumChildWindows.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

_user32.VkKeyScanExW.argtypes = [wintypes.WCHAR, wintypes.HKL]
_user32.VkKeyScanExW.restype = ctypes.c_short
_user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
_user32.GetKeyboardLayout.restype = wintypes.HKL
_user32.GetKeyState.argtypes = [ctypes.c_int]
_user32.GetKeyState.restype = ctypes.c_short

_send_input = _user32.SendInput  # module-level so tests can replace it


def _key_held(vk: int) -> bool:
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def _caps_lock_on() -> bool:
    return bool(_user32.GetKeyState(0x14) & 1)


def _layout() -> int:
    """The keyboard layout of the window in front (each thread can have its own)."""
    front = front_window()
    return _user32.GetKeyboardLayout(_user32.GetWindowThreadProcessId(front, None) if front else 0) or 0


def _key_for(char: str, layout: int) -> tuple[int, bool] | None:
    """(virtual key, needs Shift) for a character this layout types with one key, else None."""
    if len(char) != 1 or ord(char) > 0xFFFF:
        return None
    found = _user32.VkKeyScanExW(char, layout)
    if found == -1:
        return None
    vk, shifts = found & 0xFF, (found >> 8) & 0xFF
    if shifts & ~0x1 or vk in (0, 0xFF, _VK_RETURN):  # Ctrl/Alt/AltGr or odd states: not a plain key
        return None
    return vk, bool(shifts & 0x1)


_SW_RESTORE = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_FRAME_HOST = "applicationframehost.exe"


# ---- windows -------------------------------------------------------------------------------

def front_window() -> int:
    """The window in front (the one that gets the keys), or 0 when there is none."""
    return _user32.GetForegroundWindow() or 0


_user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_user32.OpenInputDesktop.restype = wintypes.HANDLE
_user32.CloseDesktop.argtypes = [wintypes.HANDLE]
_user32.CloseDesktop.restype = wintypes.BOOL
_user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
_user32.GetUserObjectInformationW.restype = wintypes.BOOL
_DESKTOP_READOBJECTS, _UOI_NAME = 0x0001, 2


def screen_locked() -> bool:
    """Is the laptop locked (or a secure screen like Ctrl+Alt+Del or an admin prompt up)? Keys
    must not go anywhere then: live (2026-10-22) the lock screen was in front during a test, and
    typed text would have gone to its password box."""
    front = front_window()
    if front and window_exe(front) == "lockapp.exe":
        return True
    desktop = _user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not desktop:
        return True  # the secure desktop won't open for us
    try:
        name = ctypes.create_unicode_buffer(64)
        if not _user32.GetUserObjectInformationW(desktop, _UOI_NAME, name, ctypes.sizeof(name), None):
            return False
        return name.value.lower() != "default"
    finally:
        _user32.CloseDesktop(desktop)


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _exe_of_pid(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else None
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        path = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return ""
        return os.path.basename(path.value).lower()
    finally:
        _kernel32.CloseHandle(handle)


def window_exe(hwnd: int) -> str:
    """The program a window belongs to, lowercase ("whatsapp.root.exe"), or "" if Windows won't
    say. A Store app's frame belongs to ApplicationFrameHost.exe, so its app is named instead."""
    if not hwnd or not _user32.IsWindow(hwnd):
        return ""
    own = _pid_of(hwnd)
    exe = _exe_of_pid(own)
    if exe != _FRAME_HOST:
        return exe
    hosted: list[int] = []

    @_WNDENUMPROC
    def visit(child, _):
        pid = _pid_of(child)
        if pid and pid != own and pid not in hosted:
            hosted.append(pid)
        return True

    _user32.EnumChildWindows(hwnd, visit, 0)
    return next((e for e in map(_exe_of_pid, hosted) if e), exe)


def focus(hwnd: int, timeout: float = 2.0) -> bool:
    """Bring a window to the front, restoring it if minimised. True only once it really is in
    front, checked until `timeout`.

    Windows only lets the app the user is using move the focus, so this borrows the front
    window's input state for the switch (as apps.py does), then falls back on SwitchToThisWindow,
    then on pressing a key that doesn't exist, which counts as this process having had the last
    input and so earns it the right to take the foreground.
    """
    if not hwnd or not _user32.IsWindow(hwnd):
        return False
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
    deadline = time.monotonic() + max(0.0, timeout)
    for attempt in range(3):
        if front_window() == hwnd:
            return True
        if attempt == 0:
            _attached_raise(hwnd)
        elif attempt == 1:
            _user32.SwitchToThisWindow(hwnd, True)
        else:
            _send([(_VK_UNASSIGNED, 0, 0), (_VK_UNASSIGNED, 0, _KEYEVENTF_KEYUP)])
            _attached_raise(hwnd)
        if _wait_front(hwnd, min(deadline, time.monotonic() + 0.3)):
            return True
    return _wait_front(hwnd, deadline)


def _attached_raise(hwnd: int) -> None:
    front = front_window()
    me = _kernel32.GetCurrentThreadId()
    theirs = _user32.GetWindowThreadProcessId(front, None) if front else 0
    joined = bool(theirs) and theirs != me and bool(_user32.AttachThreadInput(me, theirs, True))
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if joined:
            _user32.AttachThreadInput(me, theirs, False)


def _wait_front(hwnd: int, until: float) -> bool:
    while True:
        if front_window() == hwnd:
            return True
        if time.monotonic() >= until:
            return False
        time.sleep(_POLL)


# ---- keys ----------------------------------------------------------------------------------

def _event(vk: int, scan: int, flags: int) -> _Input:
    item = _Input(type=_INPUT_KEYBOARD)
    item.ki = _KeybdInput(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=_EXTRA_INFO)
    return item


def _send(events: list[tuple[int, int, int]]) -> None:
    """One SendInput call for (virtual key, scan code, flags) events, all or nothing."""
    if not events:
        return
    batch = (_Input * len(events))(*(_event(*e) for e in events))
    sent = _send_input(len(events), batch, ctypes.sizeof(_Input))
    if sent != len(events):
        # Windows blocks injected input into a window running as administrator (UIPI).
        raise OSError(f"Windows accepted {sent} of {len(events)} key events "
                      f"(error {ctypes.get_last_error()}); the window in front may be running as administrator")


def _vk_events(vk: int, up: bool, layout: int = 0) -> tuple[int, int, int]:
    flags = (_KEYEVENTF_KEYUP if up else 0) | (_KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
    scan = _user32.MapVirtualKeyExW(vk, _MAPVK_VK_TO_VSC, layout) if layout else _user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)
    return vk, scan & 0xFFFF, flags


class ModifiersHeld(OSError):
    """Ctrl, Alt, Shift or Win was still held after MODIFIER_WAIT, so nothing was sent."""


_MODIFIER_NAMES = {_VK_CONTROL: "Ctrl", _VK_MENU: "Alt", _VK_SHIFT: "Shift", _VK_LWIN: "Windows",
                   _VK_RWIN: "Windows"}


def wait_for_modifiers() -> None:
    """Give a held Ctrl, Alt, Shift or Win up to MODIFIER_WAIT to be let go. Raises ModifiersHeld
    if one still is: until 2026-10-22 it typed anyway, and a letter typed with Ctrl down is a
    shortcut (Ctrl+A selects all, Ctrl+W closes a tab), not text (the review of 2026-10-22).
    whatsapp.py calls it before checking the focus, so that check is the last thing before a key."""
    deadline = time.monotonic() + MODIFIER_WAIT
    held = [vk for vk in (_VK_CONTROL, _VK_MENU, _VK_SHIFT, _VK_LWIN, _VK_RWIN) if _key_held(vk)]
    while held:
        if time.monotonic() >= deadline:
            names = sorted({_MODIFIER_NAMES[vk] for vk in held})
            raise ModifiersHeld(f"{' and '.join(names)} {'is' if len(names) == 1 else 'are'} still held down, "
                                "so no keys were sent")
        time.sleep(_POLL)
        held = [vk for vk in held if _key_held(vk)]


def _combo(keys: str) -> list[tuple[int, int, int]]:
    """'ctrl+shift+f' -> the events for Ctrl down, Shift down, F down, F up, Shift up, Ctrl up."""
    parts = [p.strip().lower() for p in str(keys).split("+")]
    if not parts or any(not p for p in parts):
        raise ValueError(f"not a key combination: {keys!r}")
    *mods, last = parts
    unknown = [m for m in mods if m not in _MODIFIERS]
    if unknown or last not in _KEYS:
        raise ValueError(f"unknown key in {keys!r}")
    vks = [_MODIFIERS[m] for m in mods] + [_KEYS[last]]
    return [_vk_events(vk, False) for vk in vks] + [_vk_events(vk, True) for vk in reversed(vks)]


def press(keys: str) -> None:
    """Press one key or combination: "enter", "escape", "ctrl+f", "backspace", "shift+enter".
    Raises ValueError for a key it doesn't know, OSError if the laptop is locked, a modifier is
    still held (ModifiersHeld) or Windows refuses the input."""
    events = _combo(keys)
    _refuse_if_locked()
    wait_for_modifiers()
    _send(events)


def _refuse_if_locked() -> None:
    if screen_locked():
        raise OSError("the laptop is locked, so no keys were sent")


def _char_events(char: str, layout: int = 0, caps: bool = False) -> list[tuple[int, int, int]]:
    """The events for one typed character: its key (with Shift if needed) when the layout has
    one, else its UTF-16 units as Unicode events, each pressed and released."""
    if char == "\n":
        return [_vk_events(_VK_SHIFT, False), _vk_events(_VK_RETURN, False),
                _vk_events(_VK_RETURN, True), _vk_events(_VK_SHIFT, True)]
    key = _key_for(char, layout)
    if key is not None:
        vk, shift = key
        if caps and 0x41 <= vk <= 0x5A:
            shift = not shift  # Caps Lock flips letters; Shift flips them back
        press_key = [_vk_events(vk, False, layout), _vk_events(vk, True, layout)]
        if not shift:
            return press_key
        return [_vk_events(_VK_SHIFT, False, layout), *press_key, _vk_events(_VK_SHIFT, True, layout)]
    data = char.encode("utf-16-le", "surrogatepass")
    events = []
    for i in range(0, len(data), 2):
        unit = int.from_bytes(data[i:i + 2], "little")
        events += [(0, unit, _KEYEVENTF_UNICODE), (0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)]
    return events


_LINE_BREAKS = re.compile(r" *\n+ *")


def _clean(text: str, multiline: bool = False) -> str:
    """Line breaks as "\\n" (outside a multi-line box, one space for each run of them), tabs as
    spaces, no other control characters, no trailing newline."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = "".join(c for c in text if c == "\n" or (ord(c) >= 0x20 and not 0x7F <= ord(c) <= 0x9F))
    text = text.rstrip("\n")
    return text if multiline else _LINE_BREAKS.sub(" ", text)


def type_text(text: str, multiline: bool = False) -> None:
    """Type text into the window in front, as if on the keyboard, without the clipboard. Enter is
    never sent. `multiline`: the caller has checked that the focus is in a multi-line box, so "\\n"
    becomes Shift+Enter there; otherwise a newline is typed as a space, since Shift+Enter submits a
    single-line box (the review of 2026-10-22). Raises OSError if the laptop is locked, a modifier
    is still held (ModifiersHeld) or Windows refuses the input."""
    text = _clean(text, multiline)
    if not text:
        return
    _refuse_if_locked()
    wait_for_modifiers()
    layout, caps = _layout(), _caps_lock_on()
    for start in range(0, len(text), CHUNK_CHARS):
        if start:
            time.sleep(CHUNK_PAUSE)
        events = [e for char in text[start:start + CHUNK_CHARS] for e in _char_events(char, layout, caps)]
        _send(events)
