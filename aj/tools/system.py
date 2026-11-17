"""Laptop settings by voice: volume, mute, brightness, media keys, window snapping,
screenshots, battery and the time.

Ported from SivaSafe's computer_settings.py, keeping what worked (common commands are
settled locally from fixed phrase tables, never by asking a model) and fixing the rest:
- Volume goes straight to Windows Core Audio through ctypes COM, so "volume 30" lands on
  exactly the 30% the Windows slider shows. SivaSafe tapped the volume key five times for
  a step and needed pycaw/comtypes for an exact level, mapped through decibels that don't
  match the slider.
- Windows are moved with ShowWindow/SetWindowPos on the foreground window, not simulated
  Win+arrow keys, so no keystroke can ever land in the wrong app. The media keys are the
  only simulated input left, because Windows has no other way to reach whichever player
  is active.
- Screenshots are saved to a file by GDI, not by opening the Snipping Tool overlay.
- No cloud call to guess what a command meant. Lock, shutdown, restart, sleep, sign-out
  and the Wi-Fi toggle are deliberately absent (docs/PLAN.md, "Deliberately not included").
"""
import base64
import ctypes
import os
import struct
import subprocess
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Literal

from aj.core.power import PowerState, read_power
from aj.tools.registry import Level, ToolError, tool

NO_WINDOW = 0x08000000
STEP = 10  # percent per "up"/"down", for volume and brightness alike
SCREENSHOT_DIR = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Pictures" / "AJ Screenshots"


# ---------------------------------------------------------------- argument checks

def _percent(value, noun: str) -> int:
    """A 0-100 level from whatever the model sent: 30, 30.0 or "30%"."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ToolError(f"{noun} must be a number from 0 to 100.")
    try:
        number = float(value.strip().rstrip("%")) if isinstance(value, str) else float(value)
    except ValueError:
        raise ToolError(f"{noun} must be a number from 0 to 100.") from None
    if not 0 <= number <= 100:
        raise ToolError(f"{noun} goes from 0 to 100.")
    return round(number)


def _validated(level, change, noun: str, by=None) -> tuple[int | None, int]:
    """(absolute level or None, step). A level wins when both are given ("up to 50" is 50).
    `by`: how far a change goes, in percent points ("lower the brightness by 50", the user,
    2026-11-18); 10 when not given."""
    if level is not None:
        return _percent(level, noun), 0
    if change is None:
        raise ToolError(f"Give a {noun.lower()} level from 0 to 100, or up or down.")
    direction = change.lower() if isinstance(change, str) else change
    if direction not in ("up", "down"):
        raise ToolError(f"{noun} can only go up or down.")
    step = STEP if by is None else _percent(by, f"How far the {noun.lower()} changes")
    return None, step if direction == "up" else -step


def _target(current: int, level: int | None, step: int) -> int:
    return level if level is not None else max(0, min(100, current + step))


# ---------------------------------------------------------------- Core Audio (ctypes COM)

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


_CLSID_MMDeviceEnumerator = _GUID.parse("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_IMMDeviceEnumerator = _GUID.parse("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_IAudioEndpointVolume = _GUID.parse("5CDF2C82-841E-4546-9722-0CF74078229A")
_CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0x0
_E_NOTFOUND = 0x80070490  # no default output device
_RENDER, _MULTIMEDIA = 0, 1  # EDataFlow.eRender, ERole.eMultimedia

# vtable slots (IUnknown takes 0-2)
_RELEASE = 2
_GET_DEFAULT_AUDIO_ENDPOINT = 4  # IMMDeviceEnumerator
_ACTIVATE = 3                    # IMMDevice
_SET_SCALAR, _GET_SCALAR, _SET_MUTE, _GET_MUTE = 7, 9, 14, 15  # IAudioEndpointVolume

_ole32 = ctypes.WinDLL("ole32", use_last_error=True)
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None
_ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
_ole32.CoCreateInstance.restype = ctypes.HRESULT  # ctypes raises OSError on a failed HRESULT


def _vcall(obj: ctypes.c_void_p, slot: int, argtypes: tuple, *args, restype=ctypes.HRESULT):
    """Call method number `slot` of a COM interface pointer."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[slot])
    return method(obj, *args)


@contextmanager
def _speakers() -> Iterator[ctypes.c_void_p]:
    """The default output device's IAudioEndpointVolume, with COM set up around it.

    Tools run on worker threads, so COM is initialised per call. If the thread is already
    in another apartment (RPC_E_CHANGED_MODE) COM still works and isn't ours to tear down.
    """
    ours = _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED) in (0, 1)  # S_OK, S_FALSE
    held: list[ctypes.c_void_p] = []
    try:
        enumerator = ctypes.c_void_p()
        _ole32.CoCreateInstance(ctypes.byref(_CLSID_MMDeviceEnumerator), None, _CLSCTX_ALL,
                                ctypes.byref(_IID_IMMDeviceEnumerator), ctypes.byref(enumerator))
        held.append(enumerator)
        device = ctypes.c_void_p()
        _vcall(enumerator, _GET_DEFAULT_AUDIO_ENDPOINT,
               (ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
               _RENDER, _MULTIMEDIA, ctypes.byref(device))
        held.append(device)
        volume = ctypes.c_void_p()
        _vcall(device, _ACTIVATE,
               (ctypes.POINTER(_GUID), wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
               ctypes.byref(_IID_IAudioEndpointVolume), _CLSCTX_ALL, None, ctypes.byref(volume))
        held.append(volume)
        yield volume
    except OSError as e:
        code = (e.winerror or 0) & 0xFFFFFFFF
        if code == _E_NOTFOUND:
            raise ToolError("No speakers or headphones are active.") from e
        raise ToolError(f"Windows audio refused the request (error {code:#010x}).") from e
    finally:
        for obj in reversed(held):
            if obj.value:
                _vcall(obj, _RELEASE, (), restype=ctypes.c_ulong)
        if ours:
            _ole32.CoUninitialize()


def _get_scalar(volume: ctypes.c_void_p) -> float:
    level = ctypes.c_float()
    _vcall(volume, _GET_SCALAR, (ctypes.POINTER(ctypes.c_float),), ctypes.byref(level))
    return level.value


def _set_scalar(volume: ctypes.c_void_p, level: float) -> None:
    # The "scalar" level is the audio-tapered 0.0-1.0 the Windows volume slider shows.
    _vcall(volume, _SET_SCALAR, (ctypes.c_float, ctypes.c_void_p), level, None)


def _get_mute(volume: ctypes.c_void_p) -> bool:
    muted = wintypes.BOOL()
    _vcall(volume, _GET_MUTE, (ctypes.POINTER(wintypes.BOOL),), ctypes.byref(muted))
    return bool(muted.value)


def _set_mute(volume: ctypes.c_void_p, on: bool) -> None:
    _vcall(volume, _SET_MUTE, (wintypes.BOOL, ctypes.c_void_p), on, None)


def get_volume() -> int:
    """Current master volume, 0-100, as the Windows slider shows it."""
    with _speakers() as speakers:
        return round(_get_scalar(speakers) * 100)


def is_muted() -> bool:
    with _speakers() as speakers:
        return _get_mute(speakers)


@tool(level=Level.SAFE, timeout=5.0, cloud=False)
def set_volume(level: int | None = None, change: Literal["up", "down"] | None = None, by: int | None = None) -> str:
    """Set the system volume to 0-100, or change it up or down.

    level: percent, 0 to 100
    change: up or down, instead of a level
    by: how many percent a change goes up or down (10 if left out)
    """
    absolute, step = _validated(level, change, "Volume", by)
    with _speakers() as speakers:
        current = round(_get_scalar(speakers) * 100)
        target = _target(current, absolute, step)
        unmute = target > 0 and _get_mute(speakers)  # like the volume keys: changing it unmutes
        if target == current and not unmute:
            return f"Volume is already at {current}%."
        _set_scalar(speakers, target / 100)
        if unmute:
            _set_mute(speakers, False)
            return f"Volume at {target}%, unmuted."
    return f"Volume at {target}%."


@tool(level=Level.SAFE, timeout=5.0, cloud=False)
def mute(on: bool | None = None) -> str:
    """Mute or unmute the sound; leave 'on' out to toggle.

    on: true to mute, false to unmute
    """
    if on is not None and not isinstance(on, bool):
        raise ToolError("'on' must be true or false.")
    with _speakers() as speakers:
        muted = _get_mute(speakers)
        want = not muted if on is None else on
        if want == muted:
            return "Already muted." if muted else "Sound is already on."
        _set_mute(speakers, want)
    return "Muted." if want else "Unmuted."


# ---------------------------------------------------------------- brightness (WMI)

_POWERSHELL = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                  / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
_NO_BRIGHTNESS = "This display doesn't let Windows change its brightness. External monitors usually don't."

# One PowerShell run reads and (if asked) sets, because starting PowerShell is the slow part.
# $level -1 means "no absolute level"; $step 0 with no level means "just read".
# Failures are printed as "error <message>" on stdout: with -EncodedCommand, PowerShell writes
# its own errors to stderr as CLIXML, which is no use to read out.
_BRIGHTNESS_SCRIPT = """
$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'
try {
  $level = {level}; $step = {step}
  $now = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $now) { 'unsupported'; exit }
  $current = [int]$now.CurrentBrightness
  if ($level -ge 0) { $target = $level } else { $target = [Math]::Max(0, [Math]::Min(100, $current + $step)) }
  if ($target -ne $current) {
    $methods = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods | Select-Object -First 1
    Invoke-CimMethod -InputObject $methods -MethodName WmiSetBrightness -Arguments @{Timeout = [uint32]0; Brightness = [byte]$target} | Out-Null
  }
  "$current $target"
} catch { "error $($_.Exception.Message)" }
"""


def _run_brightness(level: int, step: int) -> tuple[int, int]:
    """(brightness before, brightness asked for). Raises ToolError if the panel can't do it."""
    script = _BRIGHTNESS_SCRIPT.replace("{level}", str(int(level))).replace("{step}", str(int(step)))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")  # no quoting to get wrong
    try:
        done = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                              capture_output=True, text=True, timeout=7, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise ToolError("Windows took too long to answer about the brightness.") from None
    except OSError as e:
        raise ToolError(f"Couldn't start PowerShell for the brightness: {e.strerror}.") from None
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    if lines and lines[-1] == "unsupported":
        raise ToolError(_NO_BRIGHTNESS)
    if lines and lines[-1].startswith("error "):
        raise ToolError(f"Windows wouldn't change the brightness: {lines[-1][6:].rstrip('. ')}.")
    try:
        current, target = (int(n) for n in lines[-1].split())
    except (IndexError, ValueError):
        raise ToolError("Brightness change failed: PowerShell gave no usable answer.") from None
    return current, target


def get_brightness() -> int:
    return _run_brightness(-1, 0)[0]


@tool(level=Level.SAFE, timeout=8.0, cloud=False)
def set_brightness(level: int | None = None, change: Literal["up", "down"] | None = None,
                   by: int | None = None) -> str:
    """Set the screen brightness to 0-100, or change it up or down.

    level: percent, 0 to 100
    change: up or down, instead of a level
    by: how many percent a change goes up or down (10 if left out)
    """
    absolute, step = _validated(level, change, "Brightness", by)
    current, target = _run_brightness(-1 if absolute is None else absolute, step)
    if target == current:
        return f"Brightness is already at {current}%."
    return f"Brightness at {target}%."


# ---------------------------------------------------------------- media keys

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_size_t]  # wintypes.BYTE is signed
_user32.keybd_event.restype = None
_keybd_event = _user32.keybd_event  # module-level so tests can replace it

_KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP = 0x1, 0x2
_MEDIA = {
    "play_pause": (0xB3, "Toggled play/pause."),
    "next": (0xB0, "Skipped to the next track."),
    "previous": (0xB1, "Back to the previous track."),
    "stop": (0xB2, "Stopped playback."),
}


@tool(level=Level.SAFE, timeout=2.0, cloud=False)
def media(action: Literal["play_pause", "next", "previous", "stop"]) -> str:
    """Control whatever is playing: play/pause, next, previous or stop."""
    if action not in _MEDIA:
        raise ToolError("Media can only play_pause, next, previous or stop.")
    key, done = _MEDIA[action]
    _keybd_event(key, 0, _KEYEVENTF_EXTENDEDKEY, 0)
    _keybd_event(key, 0, _KEYEVENTF_EXTENDEDKEY | _KEYEVENTF_KEYUP, 0)
    return done


# ---------------------------------------------------------------- windows

class _Rect(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    def edges(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _Rect), ("rcWork", _Rect), ("dwFlags", wintypes.DWORD)]


_HWND = wintypes.HWND
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = _HWND
_user32.GetClassNameW.argtypes = [_HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
for _name in ("IsWindow", "IsWindowVisible", "IsIconic", "IsZoomed"):
    getattr(_user32, _name).argtypes = [_HWND]
    getattr(_user32, _name).restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [_HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [_HWND, _HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [_HWND, ctypes.POINTER(_Rect)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.MonitorFromWindow.argtypes = [_HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HMONITOR
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
_user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.GetDC.argtypes = [_HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [_HWND, wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int

_dwmapi = ctypes.WinDLL("dwmapi")
_dwmapi.DwmGetWindowAttribute.argtypes = [_HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

_SW_MAXIMIZE, _SW_MINIMIZE, _SW_RESTORE = 3, 6, 9
_SWP_NOZORDER, _SWP_NOACTIVATE, _SWP_NOOWNERZORDER = 0x4, 0x10, 0x200
_MONITOR_DEFAULTTONEAREST = 2
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DPI_PER_MONITOR_AWARE_V2 = -4
_ERROR_ACCESS_DENIED = 5
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
_POSITIONS = ("left", "right", "maximize", "minimize", "restore")
_last_minimized: int | None = None  # so "restore" can bring back the window "minimize" hid


@contextmanager
def _physical_pixels() -> Iterator[None]:
    """Work in real pixels on this thread. Python isn't DPI-aware, so at this laptop's 150%
    scaling Windows hands it a 1707x1067 make-believe screen instead of 2560x1600, and snaps
    and screenshots would come out the wrong size."""
    previous = _user32.SetThreadDpiAwarenessContext(_DPI_PER_MONITOR_AWARE_V2)
    try:
        yield
    finally:
        if previous:
            _user32.SetThreadDpiAwarenessContext(previous)


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _foreground_window() -> int:
    hwnd = _user32.GetForegroundWindow()
    if not hwnd or not _user32.IsWindowVisible(hwnd) or _class_name(hwnd) in _SHELL_CLASSES:
        raise ToolError("There's no app window in front to move.")
    return hwnd


def _half(work: tuple[int, int, int, int], side: str,
          margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> tuple[int, int, int, int]:
    """(x, y, width, height) filling the left or right half of a work area. `margins` are the
    invisible resize borders Windows 10/11 draws outside a window, added back so the visible
    edge meets the screen edge with no gap."""
    left, top, right, bottom = work
    middle = left + (right - left) // 2
    x0, x1 = (left, middle) if side == "left" else (middle, right)
    m_left, m_top, m_right, m_bottom = margins
    return x0 - m_left, top - m_top, x1 - x0 + m_left + m_right, bottom - top + m_top + m_bottom


def _frame_margins(hwnd: int) -> tuple[int, int, int, int]:
    window, frame = _Rect(), _Rect()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(window)) or _dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(frame), ctypes.sizeof(frame)) != 0:
        return 0, 0, 0, 0
    return frame.left - window.left, frame.top - window.top, window.right - frame.right, window.bottom - frame.bottom


def _snap_half(hwnd: int, side: str) -> None:
    with _physical_pixels():
        if _user32.IsIconic(hwnd) or _user32.IsZoomed(hwnd):
            _user32.ShowWindow(hwnd, _SW_RESTORE)
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        monitor = _user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
        if not monitor or not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ToolError("Couldn't work out which screen that window is on.")
        x, y, width, height = _half(info.rcWork.edges(), side, _frame_margins(hwnd))
        flags = _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_NOOWNERZORDER
        if not _user32.SetWindowPos(hwnd, None, x, y, width, height, flags):
            if ctypes.get_last_error() == _ERROR_ACCESS_DENIED:
                raise ToolError("That window runs as administrator, so Windows won't let me move it.")
            raise ToolError("Windows wouldn't move that window.")


@tool(level=Level.SAFE, timeout=3.0, cloud=False)
def snap_window(position: Literal["left", "right", "maximize", "minimize", "restore"]) -> str:
    """Snap the front window to the left or right half, or maximize, minimize or restore it."""
    global _last_minimized
    if position not in _POSITIONS:
        raise ToolError("Windows can go left, right, maximize, minimize or restore.")
    hidden = _last_minimized
    if position == "restore" and hidden and _user32.IsWindow(hidden) and _user32.IsIconic(hidden):
        _user32.ShowWindow(hidden, _SW_RESTORE)
        _last_minimized = None
        return "Restored the window."
    hwnd = _foreground_window()
    if position == "minimize":
        _user32.ShowWindow(hwnd, _SW_MINIMIZE)
        _last_minimized = hwnd
        return "Minimized the window."
    if position == "maximize":
        _user32.ShowWindow(hwnd, _SW_MAXIMIZE)
        return "Maximized the window."
    if position == "restore":
        _user32.ShowWindow(hwnd, _SW_RESTORE)
        return "Restored the window."
    _snap_half(hwnd, position)
    return f"Snapped the window to the {position}."


# ---------------------------------------------------------------- screenshot (GDI + stdlib PNG)

class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
                                    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
_gdi32.BitBlt.restype = wintypes.BOOL
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.DeleteDC.restype = wintypes.BOOL
_gdi32.GdiFlush.argtypes = []
_gdi32.GdiFlush.restype = wintypes.BOOL

_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 76, 77, 78, 79
_SRCCOPY, _CAPTUREBLT = 0x00CC0020, 0x40000000


def _grab_screens() -> tuple[int, int, bytes]:
    """(width, height, top-down BGRA pixels) of every monitor together.
    GDI rather than Qt: it is safe on any thread and needs no QApplication."""
    with _physical_pixels():
        metric = _user32.GetSystemMetrics
        x, y = metric(_SM_XVIRTUALSCREEN), metric(_SM_YVIRTUALSCREEN)
        width, height = metric(_SM_CXVIRTUALSCREEN), metric(_SM_CYVIRTUALSCREEN)
        screen = _user32.GetDC(None)
        if not screen:
            raise ToolError("Windows wouldn't let me read the screen.")
        memory = _gdi32.CreateCompatibleDC(screen)
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, -height  # negative: rows top-down
        info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(screen, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        try:
            if not bitmap or not memory:
                raise ToolError("Not enough memory to capture the screen.")
            previous = _gdi32.SelectObject(memory, bitmap)
            copied = _gdi32.BitBlt(memory, 0, 0, width, height, screen, x, y, _SRCCOPY | _CAPTUREBLT)
            _gdi32.SelectObject(memory, previous)
            if not copied:
                raise ToolError("Windows wouldn't let me read the screen (is it locked?).")
            _gdi32.GdiFlush()
            return width, height, ctypes.string_at(bits.value, width * height * 4)
        finally:
            if bitmap:
                _gdi32.DeleteObject(bitmap)
            if memory:
                _gdi32.DeleteDC(memory)
            _user32.ReleaseDC(None, screen)


def _bgra_to_rgb(bgra: bytes) -> bytes:
    rgb = bytearray(len(bgra) // 4 * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return bytes(rgb)


def _png(width: int, height: int, rgb: bytes) -> bytes:
    """A minimal 8-bit RGB PNG: stdlib only, no image library needed."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    stride = width * 3
    rows = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))  # filter 0 per row
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def _screenshot_path(folder: Path, when: datetime) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"Screenshot {when:%Y-%m-%d %H-%M-%S}"
    path, n = folder / f"{stem}.png", 2
    while path.exists():
        path, n = folder / f"{stem} ({n}).png", n + 1
    return path


@tool(level=Level.SAFE, timeout=10.0, cloud=False)
def screenshot() -> str:
    """Save a picture of all screens to Pictures\\AJ Screenshots and give its path."""
    width, height, bgra = _grab_screens()
    path = _screenshot_path(SCREENSHOT_DIR, datetime.now())
    try:
        path.write_bytes(_png(width, height, _bgra_to_rgb(bgra)))
    except OSError as e:
        raise ToolError(f"Couldn't save the screenshot: {e.strerror}.") from None
    return f"Screenshot saved to {path}"


# ---------------------------------------------------------------- battery and time

def _describe_power(state: PowerState) -> str:
    if state.battery_percent is None:
        return "Windows isn't reporting a battery level" + (", but it's plugged in." if state.plugged_in else ".")
    parts = [f"Battery at {state.battery_percent}%"]
    if state.plugged_in is not None:
        parts.append("plugged in" if state.plugged_in else "on battery power")
    if state.battery_saver:
        parts.append("battery saver on")
    return ", ".join(parts) + "."


@tool(level=Level.SAFE, timeout=2.0, cloud=False)
def battery_status() -> str:
    """Report the battery level and whether the laptop is plugged in."""
    try:
        return _describe_power(read_power())
    except OSError as e:
        raise ToolError(f"Windows didn't report the power state: {e.strerror}.") from None


def _ordinal(day: int) -> str:
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _describe_time(now: datetime) -> str:
    hour = now.hour % 12 or 12
    half = "AM" if now.hour < 12 else "PM"
    return f"It's {hour}:{now.minute:02d} {half} on {now:%A}, {now:%B} {_ordinal(now.day)}, {now.year}."


@tool(level=Level.SAFE, timeout=1.0, cloud=False)
def current_time() -> str:
    """Tell the current local time and date."""
    return _describe_time(datetime.now())


# ---------------------------------------------------------------- fast-path intents
#
# Matched against a lowercased, punctuation-stripped transcript with "hey", "computer" and
# "please" removed. Every pattern is anchored to the whole utterance, so "play despacito"
# or "what does mute mean" never trip them: a miss just goes to the model, a false hit
# would do the wrong thing instantly. Apostrophes may vanish or become spaces depending on
# the stripper, so "what's" is written to match "whats", "what s" and "what is".

_UNITS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
          "fifteen sixteen seventeen eighteen nineteen").split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_SPECIAL = {"max": 100, "maximum": 100, "full": 100, "half": 50,
            "hundred": 100, "a hundred": 100, "one hundred": 100}


def _alternatives(words) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


_NUM = (rf"(?P<n>\d{{1,3}}|(?:{_alternatives(_TENS)})(?: ?(?:{_alternatives(_UNITS[1:10])}))?"
        rf"|(?:a |one )?hundred|{_alternatives(_UNITS)}|max(?:imum)?|full|half)(?: ?percent| per cent|%)?")


def _number(text: str) -> int:
    """'30', 'thirty', 'thirty five', 'max', 'half' -> int. Speech-to-text writes both forms."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text in _SPECIAL:
        return _SPECIAL[text]
    if text in _UNITS:
        return _UNITS.index(text)
    for i, tens in enumerate(_TENS):
        if text.startswith(tens):
            rest = text[len(tens):].strip()
            return 20 + 10 * i + (_UNITS.index(rest) if rest else 0)
    raise ValueError(f"not a number: {text!r}")


_DIRECTION = {
    "up": "up", "louder": "up", "increase": "up", "raise": "up", "boost": "up", "brighter": "up", "brighten": "up",
    "down": "down", "quieter": "down", "softer": "down", "decrease": "down", "lower": "down", "reduce": "down",
    "dimmer": "down", "darker": "down", "dim": "down",
}
_WINDOW = {"left": "left", "right": "right", "maximize": "maximize", "maximise": "maximize",
           "minimize": "minimize", "minimise": "minimize"}

_ASK = r"(?:(?:can|could|would|will) you |(?:i want you to|i need you to|go ahead and|just) )?"
_TRAIL = r"(?: (?:now|right now|today|for me|more|a bit|a little|a little bit|a tad|slightly|thanks|thank you))*"
_A_BIT = r"(?:(?:a (?:bit|little|little bit|tad)|slightly|bit|little) )?"
_SET = r"(?:(?:set|turn|change|put|make|bring|adjust) )?"
_WHATS = r"what(?:s| s| is)"
_SCREEN = r"(?:the |my )?(?:screen |display )?"
# "battery" as speech-to-text writes it ("battery's" is "batterys" once normalized). It also
# writes "better" and "batter", real words, so those count only with a battery word next to them.
_BATTERY = r"(?:battery|batteries|batterys|battery s|battry|batery)"
_ANY_BATTERY = rf"(?:{_BATTERY}|better|batter)"
_BATTERY_NOUN = (rf"(?:{_BATTERY} (?:percent|per cent|percentage|percentages|presentation|level|status|life|charge)"
                 r"|(?:better|batter) (?:percent|per cent|percentage|percentages|level|charge))")
_HAVE = r"(?: (?:do i have|have i got|do we have|i have|is there|are there|there|is it|is|are))?"
_ON_LAPTOP = r"(?: (?:on|in|of|for) (?:the|my|this) (?:laptop|computer|pc|machine))?"


def _whole(body: str) -> str:
    return rf"^{_ASK}(?:{body}){_TRAIL}$"


def _level(m) -> dict:
    return {"level": _number(m["n"])}


def _change(m) -> dict:
    return {"change": _DIRECTION[m["d"]]}


def _nothing(m) -> dict:
    return {}


def _by(m) -> dict:
    """"lower the brightness by 50": down, 50 points."""
    return {"change": _DIRECTION[m["d"]], "by": _number(m["n"])}


_UP_DOWN = r"(?P<d>increase|raise|boost|decrease|lower|reduce|up|down)"
_TURN = r"(?:turn|bring|put|crank|drop|take) "


INTENTS = [
    # volume
    (_whole(rf"{_SET}(?:the |my )?(?:volume|sound)(?: level)?(?: (?:to|at|on|up to|down to))? {_NUM}"),
     "set_volume", _level),
    (_whole(rf"(?:turn|crank|bring) (?:it|the volume|the sound|the music) (?:up|down) to {_NUM}"), "set_volume", _level),
    (_whole(r"(?P<n>max(?:imum)?|full) volume"), "set_volume", _level),
    (_whole(r"turn (?:it|that|the volume|the sound|the music) (?P<d>up|down)"), "set_volume", _change),
    (_whole(r"turn (?P<d>up|down) (?:the )?(?:volume|sound|music)"), "set_volume", _change),
    (_whole(r"(?:the )?(?:volume|sound) (?P<d>up|down)"), "set_volume", _change),
    (_whole(rf"(?:make it |make the music )?{_A_BIT}(?P<d>louder|quieter|softer)"), "set_volume", _change),
    # "increase the volume to 100", "lower the volume by 20" (the user, 2026-11-18)
    (_whole(rf"(?:increase|raise|boost|decrease|lower|reduce|drop|change) (?:the |my )?(?:volume|sound)(?: level)? to {_NUM}"),
     "set_volume", _level),
    (_whole(rf"{_UP_DOWN} (?:the |my )?(?:volume|sound)(?: level)? by {_NUM}"), "set_volume", _by),
    (_whole(rf"{_TURN}(?:the |my )?(?:volume|sound) {_UP_DOWN} by {_NUM}"), "set_volume", _by),
    (_whole(r"(?P<d>increase|raise|boost|decrease|lower|reduce) (?:the )?(?:volume|sound)"), "set_volume", _change),
    # mute
    (_whole(r"(?P<d>mute|unmute)(?: (?:it|everything|(?:the )?(?:sound|volume|audio|speakers|music|laptop|computer)))?"),
     "mute", lambda m: {"on": m["d"] == "mute"}),
    (_whole(r"turn (?:the )?(?:sound|audio)(?: back)? (?P<d>off|on)|turn (?P<e>off|on) the (?:sound|audio)"),
     "mute", lambda m: {"on": (m["d"] or m["e"]) == "off"}),
    (_whole(r"toggle (?:the )?mute"), "mute", _nothing),
    # brightness
    (_whole(rf"{_SET}{_SCREEN}brightness(?: level)?(?: (?:to|at|on|up to|down to))? {_NUM}"),
     "set_brightness", _level),
    (_whole(rf"turn {_SCREEN}brightness (?:up|down) to {_NUM}"), "set_brightness", _level),
    (_whole(r"(?P<n>max(?:imum)?|full) brightness"), "set_brightness", _level),
    (_whole(rf"(?:make (?:it|the screen|the display|my screen) )?{_A_BIT}(?P<d>brighter|dimmer|darker)"),
     "set_brightness", _change),
    (_whole(rf"(?:turn )?{_SCREEN}brightness (?P<d>up|down)"), "set_brightness", _change),
    (_whole(rf"turn (?P<d>up|down) {_SCREEN}brightness"), "set_brightness", _change),
    (_whole(rf"(?:increase|raise|boost|decrease|lower|reduce|drop|change) {_SCREEN}brightness(?: level)? to {_NUM}"),
     "set_brightness", _level),
    (_whole(rf"{_UP_DOWN} {_SCREEN}brightness(?: level)? by {_NUM}"), "set_brightness", _by),
    (_whole(rf"{_TURN}{_SCREEN}brightness {_UP_DOWN} by {_NUM}"), "set_brightness", _by),
    (_whole(rf"(?P<d>increase|raise|boost|decrease|lower|reduce) {_SCREEN}brightness"), "set_brightness", _change),
    (_whole(r"(?P<d>dim|brighten)(?: (?:the|my) (?:screen|display))?"), "set_brightness", _change),
    # media
    (_whole(r"(?:pause|play|resume|unpause)(?: (?:the |my )?(?:music|song|track|video|playback|media|audio|podcast|it|this))?"),
     "media", lambda m: {"action": "play_pause"}),
    (_whole(r"(?:(?:play |go to |skip to )?(?:the )?next (?:song|track)|skip(?: (?:this|the|that) (?:song|track|one)| song| track| it| this)?)"),
     "media", lambda m: {"action": "next"}),
    (_whole(r"(?:(?:play |go to |go back to )?(?:the )?(?:previous|last) (?:song|track)|previous|(?:go )?back a (?:song|track))"),
     "media", lambda m: {"action": "previous"}),
    (_whole(r"stop (?:the |my )?(?:music|playback|song|video|media|audio|podcast)"),
     "media", lambda m: {"action": "stop"}),
    # windows
    (_whole(r"(?:snap|move|put|send)(?: (?:the|this|my) window| it| this| window)?(?: to)?(?: the)? (?P<p>left|right)"
            r"(?: side| half)?(?: of (?:the|my) screen)?"), "snap_window", lambda m: {"position": _WINDOW[m["p"]]}),
    (_whole(r"(?:the |this )?window (?:to the )?(?P<p>left|right)"), "snap_window", lambda m: {"position": _WINDOW[m["p"]]}),
    (_whole(r"(?P<p>maximi[sz]e|minimi[sz]e)(?: (?:the|this|my) window| it| this| that| window)?"),
     "snap_window", lambda m: {"position": _WINDOW[m["p"]]}),
    (_whole(r"restore (?:it|(?:the |this |my )?window)|unminimi[sz]e(?: (?:the|this|my) window| it)?"),
     "snap_window", lambda m: {"position": "restore"}),
    # screenshot
    (_whole(r"(?:(?:take|grab|capture|get|make|snap) (?:a |an |me a )?)?(?:screenshot|screen ?shot|screen grab|screen capture)"
            r"(?: of (?:the|my) (?:screen|desktop))?(?: (?:this|it|that))?|(?:capture|grab) (?:the |my )?(?:screen|desktop)"),
     "screenshot", _nothing),
    # time and date
    # "(what) time is it": speech-to-text sometimes drops the leading "what" (heard live as
    # "Time is it."), and a miss here sends the question to the model, which once invented a date.
    (_whole(rf"{_WHATS}? the (?:current )?(?:time|date|day)|(?:what )?time is it(?: right)?|what time it is|(?:tell|give) me the (?:time|date)"
            rf"|(?:the |current )?(?:time|date)|do you (?:know|have) (?:the time|what time it is)"
            rf"|what (?:day|date) is (?:it|today)|{_WHATS} today(?:s| s)? date|{_WHATS} today|today(?:s| s)? date"),
     "current_time", _nothing),
    # battery
    # "How much charge does the laptop have?" and "What is the battery status of the laptop?"
    # missed this live (2026-10-18), and the model made up "17 percent, unplugged".
    (_whole(r"how much (?:battery|charge)(?: (?:do i have|have i got|do we have|is left|is there|left|remaining))?(?: left)?"
            r"(?: (?:on|in|of) (?:the|my|this) (?:laptop|computer|pc|machine))?"
            r"|how much (?:battery|charge) (?:does|has) (?:the|my|this) (?:laptop|computer|pc|machine) (?:have|got)(?: left)?"
            rf"|(?:{_WHATS} )?(?:the |my )?(?:battery|charge)(?: (?:level|status|percentage|percent|life|left))?"
            r"(?: (?:of|on|for) (?:the|my|this) (?:laptop|computer|pc|machine))?(?: at| like)?"
            rf"|how(?:s| s| is) (?:the |my )?battery(?: doing| looking)?|(?:check|show) (?:the |my )?battery"
            r"|am i (?:plugged in|charging|on battery)|is (?:it|the laptop|my laptop|the battery) (?:charging|plugged in)"),
     "battery_status", _nothing),
    # Speech-to-text often writes "battery" as "better" for the user: "How much better percentages
    # there left?" missed this live (2026-10-22) and the model read out the RAM instead; so did
    # "my battery presentation right now" and the correction "No, I'm talking about battery
    # percentage." "better" and "batter" count only next to a battery word (percent, percentage,
    # charge, level) or as "how much ... left", so "which is better" and "how much better is it"
    # stay with the model.
    (_whole(rf"(?:how (?:much|many)(?: is)?(?: (?:the|my))? |{_WHATS}(?: (?:the|my))? |(?:the |my ))?{_BATTERY_NOUN}"
            rf"{_HAVE}(?: (?:left|remaining))?{_ON_LAPTOP}(?: at| like| now)?(?: right)?"
            rf"|how (?:much|many) {_ANY_BATTERY}{_HAVE} (?:left|remaining){_ON_LAPTOP}(?: right)?"
            rf"|how (?:much|many) (?:percent|percentage|per cent)(?: of)?(?: (?:the|my))? {_BATTERY}{_HAVE}"
            rf"(?: (?:left|remaining))?(?: right)?"
            rf"|how much is (?:the|my) {_BATTERY}|{_WHATS} my {_ANY_BATTERY} at(?: right)?"
            rf"|what (?:percent|percentage|level) is (?:the|my) {_ANY_BATTERY}(?: at)?(?: right)?"
            rf"|(?:no )?(?:i ?m |i am |i was )?(?:talking about|asking about|asking for|asking|i mean|i meant|i said)"
            rf"(?: (?:the|my))? (?:{_BATTERY}|{_BATTERY_NOUN})"),
     "battery_status", _nothing),
]
