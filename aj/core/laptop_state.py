"""What the laptop is doing right now, kept up to date while AJ is on, so he knows instead of
guessing.

Live (2026-10-18) he told the user the battery was at 17% and unplugged when it was at 80% and
plugged in, and said he couldn't know which apps were open. The user asked for something that
updates itself and that he reads from. So a Monitor thread refreshes a Snapshot every few
seconds: the battery and charger, the open apps (which one is in front, which are minimised,
and what a browser is showing), volume, mute, brightness, the internet connection, CPU and RAM
load and free disk space. Every turn starts with a fresh look at the quick parts, and the
summary goes into the message as "[Laptop now: ...]", where the delivery filter treats it as a
reading (core/delivery.py), so a number he makes up gets caught.

Kept in memory, not in a file: always current, no disk writes every few seconds (the user asked
for his SSD to be looked after), and gone the moment AJ is off. The cost was measured at
startup: the quick refresh takes a few milliseconds, the window list about 2 ms. Brightness
needs WMI through PowerShell (about 1.5 s of a process starting), so it's read rarely.
"""
import ctypes
import logging
import re
import shutil
import string
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, replace

log = logging.getLogger(__name__)

QUICK_EVERY = 5.0       # seconds: power, windows, volume, internet, CPU and RAM
DISKS_EVERY = 60.0
BRIGHTNESS_EVERY = 300.0
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
TITLE_CHARS = 60


@dataclass(frozen=True)
class OpenApp:
    name: str
    windows: int = 1
    minimised: bool = False      # every one of its windows is minimised
    front: bool = False
    titles: tuple[str, ...] = ()  # browsers only: what each window shows


@dataclass(frozen=True)
class Snapshot:
    taken: float = 0.0
    battery: int | None = None
    plugged: bool | None = None
    saver: bool = False
    apps: tuple[OpenApp, ...] = ()
    volume: int | None = None
    muted: bool | None = None
    brightness: int | None = None
    online: bool | None = None
    cpu: int | None = None
    ram: int | None = None
    disks: tuple[tuple[str, float, float], ...] = ()  # (drive, free GB, total GB)
    # Where AJ's own model runs ("the Radeon 780M (on battery)"), set by the engine when it moves
    # (core/placement.py): "where are you running?" gets a reading, not a guess.
    ai_unit: str = ""

    def summary(self) -> str:
        """One line for the model, short enough to send every turn."""
        parts = []
        if self.battery is not None:
            power = "" if self.plugged is None else ", plugged in" if self.plugged else ", on battery power"
            parts.append(f"battery {self.battery}%{power}" + (", battery saver on" if self.saver else ""))
        elif self.plugged is not None:
            parts.append("plugged in" if self.plugged else "on battery power")
        front = [a for a in self.apps if a.front]
        others = [a for a in self.apps if not a.front]
        # "Running", not "open": asked which apps he could open, he read out the open ones
        # (live, 2026-10-23: "Google Chrome, Claude").
        if front:
            parts.append(f"in front: {_describe(front[0])}")
        if others:
            parts.append(("also running: " if front else "running: ") + ", ".join(_describe(a) for a in others))
        elif not front:
            parts.append("no apps running")
        if self.volume is not None:
            parts.append("sound muted" if self.muted else f"volume {self.volume}%")
        if self.brightness is not None:
            parts.append(f"brightness {self.brightness}%")
        if self.online is not None:
            parts.append("online" if self.online else "no internet connection")
        load = [f"CPU {self.cpu}%" if self.cpu is not None else "", f"RAM {self.ram}% used" if self.ram is not None else ""]
        if any(load):
            parts.append(", ".join(x for x in load if x))
        if self.disks:
            parts.append("free space " + ", ".join(f"{d} {free:.0f} GB of {total:.0f} GB" for d, free, total in self.disks))
        if self.ai_unit:
            parts.append(f"your own model running on {self.ai_unit}")
        return "; ".join(parts)


def _describe(app: OpenApp) -> str:
    text = app.name
    if app.titles:
        shown = ", ".join(f'"{t}"' for t in app.titles)
        text += f" ({app.windows} windows: {shown})" if app.windows > 1 else f" (showing {shown})"
    elif app.windows > 1:
        text += f" ({app.windows} windows)"
    if app.minimised:
        text += " (minimised)"
    return text


# ---- readings -------------------------------------------------------------------------------

class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
_kernel32.GetSystemTimes.restype = wintypes.BOOL
_kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
_kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
_kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetDriveTypeW.restype = wintypes.UINT
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_DRIVE_FIXED = 3


def _filetime(ft: wintypes.FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def cpu_times() -> tuple[int, int] | None:
    """(idle, total) since boot, in 100 ns units; the CPU load is the change between two."""
    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not _kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return _filetime(idle), _filetime(kernel) + _filetime(user)  # kernel time includes idle


def cpu_percent(before: tuple[int, int] | None, after: tuple[int, int] | None) -> int | None:
    if not before or not after or after[1] <= before[1]:
        return None
    busy = 1 - (after[0] - before[0]) / (after[1] - before[1])
    return max(0, min(100, round(busy * 100)))


def ram_percent() -> int | None:
    status = _MemoryStatus(dwLength=ctypes.sizeof(_MemoryStatus))
    return int(status.dwMemoryLoad) if _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None


def disks() -> tuple[tuple[str, float, float], ...]:
    found = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if _kernel32.GetDriveTypeW(root) != _DRIVE_FIXED:
            continue
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        found.append((f"{letter}:", usage.free / 2**30, usage.total / 2**30))
    return tuple(found)


def online() -> bool | None:
    try:
        flags = wintypes.DWORD()
        return bool(ctypes.windll.wininet.InternetGetConnectedState(ctypes.byref(flags), 0))
    except (OSError, AttributeError):
        return None


def open_apps() -> tuple[OpenApp, ...]:
    """The apps with windows open, front-most first, named as the Start menu names them."""
    from aj.tools import apps  # here: tools import core modules, not the other way round
    windows = apps.list_windows()
    front = _user32.GetForegroundWindow() or 0
    grouped: dict[str, list] = {}
    for w in windows:
        grouped.setdefault(apps.name_of(w), []).append(w)
    found = []
    for name, wins in grouped.items():
        titles = tuple(_short(w.title, name) for w in wins if w.exes & BROWSERS)
        found.append(OpenApp(name, len(wins), all(_user32.IsIconic(w.hwnd) for w in wins),
                             any(w.hwnd == front for w in wins), titles))
    return tuple(found)


def _short(title: str, app_name: str) -> str:
    for tail in (f" - {app_name}", " - Google Chrome", " - Microsoft Edge", " — Mozilla Firefox", " - Brave"):
        title = title.removesuffix(tail)
    return title if len(title) <= TITLE_CHARS else title[:TITLE_CHARS - 1].rstrip() + "…"


def _power() -> dict:
    from aj.core.power import read_power
    state = read_power()
    return {"battery": state.battery_percent, "plugged": state.plugged_in, "saver": state.battery_saver}


def _sound() -> dict:
    from aj.tools import system
    return {"volume": system.get_volume(), "muted": system.is_muted()}


def _brightness() -> dict:
    from aj.tools import system
    return {"brightness": system.get_brightness()}


def _safely(read, what: str) -> dict:
    """One reading; a failure costs only that reading, logged once in a while, never the turn."""
    try:
        return read()
    except Exception as e:  # noqa: BLE001 - Windows refuses things now and then; the rest still counts
        log.debug("laptop state: couldn't read %s: %s", what, e)
        return {}


def read(previous: Snapshot | None = None, cpu_before: tuple[int, int] | None = None,
         slow: bool = True) -> Snapshot:
    """A fresh snapshot. `slow`: also read the disks and the brightness (else keep the old ones)."""
    old = previous or Snapshot()
    values = {}
    values.update(_safely(_power, "power"))
    values.update(_safely(lambda: {"apps": open_apps()}, "windows"))
    values.update(_safely(_sound, "volume"))
    values.update(_safely(lambda: {"online": online(), "ram": ram_percent()}, "internet and RAM"))
    if cpu_before is not None:
        values["cpu"] = cpu_percent(cpu_before, cpu_times())
    if slow:
        values.update(_safely(lambda: {"disks": disks()}, "disks"))
        values.update(_safely(_brightness, "brightness"))
    return replace(old, taken=time.time(), **values)


# ---- the monitor ----------------------------------------------------------------------------

class Monitor:
    """Keeps a Snapshot fresh on a background thread while AJ is on. start() and stop() are
    called by the engine; snapshot() is safe from any thread."""

    def __init__(self, quick_every: float = QUICK_EVERY):
        self.quick_every = quick_every
        self._snap = Snapshot()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cpu = cpu_times()
        self._last_disks = self._last_brightness = 0.0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="laptop-state")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self, fresh: bool = False) -> Snapshot:
        """The latest snapshot; `fresh` takes the quick readings again first (a turn starting)."""
        if fresh:
            self.refresh(slow=False)
        with self._lock:
            return self._snap

    def refresh(self, slow: bool = False) -> None:
        with self._lock:
            old = self._snap
        now = time.monotonic()
        cpu_now = cpu_times()
        new = read(old, cpu_before=None, slow=False)
        new = replace(new, cpu=cpu_percent(self._cpu, cpu_now) if self._cpu else old.cpu, ai_unit=old.ai_unit)
        self._cpu = cpu_now
        if slow or now - self._last_disks >= DISKS_EVERY:
            new = replace(new, **_safely(lambda: {"disks": disks()}, "disks"))
            self._last_disks = now
        if slow or now - self._last_brightness >= BRIGHTNESS_EVERY:
            new = replace(new, **_safely(_brightness, "brightness"))
            self._last_brightness = now
        with self._lock:
            self._snap = new

    def changed(self, **values) -> None:
        """A tool just changed something (the brightness, say): note it now, not in five minutes."""
        with self._lock:
            self._snap = replace(self._snap, **values)

    def after_tool(self, tool: str, result: str) -> None:
        """Brightness is read only every few minutes, so a change AJ made goes in at once."""
        if tool == "set_brightness":
            found = re.search(r"(\d+)\s*%", result)
            if found:
                self.changed(brightness=int(found.group(1)))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("laptop state refresh failed")
            self._stop.wait(self.quick_every)



