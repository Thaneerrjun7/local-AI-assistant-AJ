"""Where AJ's models run while he's on: the RTX 4050, the Radeon 780M or the CPU
(docs/HARDWARE_AWARE_INFERENCE_PLAN.md, Phase 2).

Why (measured 2026-10-26/27, data/bench/placement): unplugged, this laptop switches the RTX 4050 off
altogether. llama-server, built for CUDA, then found no GPU and ran Qwen on the CPU: AJ's first
prompt (the persona and every tool, ~7,600 tokens) took 577.7 s to read, he answered nothing in
30 minutes of trying, and he drew 24.2 W idle against the laptop's 10.7 W. On the Radeon 780M,
through the Vulkan build of the same llama.cpp, the same prompt took 24.4 s, replies came in about
a second, and the model idled at 14.5 W; its price is ~3.8 GB of system RAM.

So with [placement] mode = "auto":
- plugged in, with the RTX 4050 there: the RTX 4050, as always;
- on battery, or the RTX 4050 missing, or a GPU-heavy app open: the 780M (the CPU when the 780M
  isn't there, or [placement] battery_unit = "cpu").
"fixed" (the default) keeps [llm] backend and device, exactly as before.

How a move goes: the wanted unit must stay wanted for settle_secs (a loose cable doesn't restart
him). Then, off the conversation's thread, a second llama-server starts on the other port with the
new unit, reads the persona (the warm-up), and only then, between turns, AJ moves to it and the old
one stops. If the new one fails, he stays where he is and tries again later. If the running server
dies (a GPU that went away under it), the next turn waits for a new one on the right unit at once.
"""
import ctypes
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, replace

from aj.config import LlmSettings, PlacementSettings, Settings
from aj.core import power_source
from aj.core.llm import LlamaServer

NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class Unit:
    key: str      # "rtx4050", "780m", "cpu"
    backend: str  # llama-server build: "cuda", "vulkan", "cpu"
    label: str    # for the log, the window and the user


RTX = Unit("rtx4050", "cuda", "the RTX 4050")
IGPU = Unit("780m", "vulkan", "the Radeon 780M")
CPU = Unit("cpu", "cpu", "the CPU")
UNITS = {u.key: u for u in (RTX, IGPU, CPU)}


@dataclass(frozen=True)
class Seen:
    """What the decision looks at."""
    on_ac: bool | None       # None: Windows can't say (counted as battery: the frugal side)
    rtx: bool                # the RTX 4050 is in Windows' device list (unplugged it isn't)
    igpu: bool               # the 780M is (in Lenovo's discrete-only mode it isn't)
    heavy_app: str = ""      # a GPU-heavy app that is open, if any


def decide(seen: Seen, s: PlacementSettings) -> tuple[Unit, str]:
    """(the unit, why) for what is seen."""
    frugal = IGPU if seen.igpu and s.battery_unit != "cpu" else CPU
    if seen.on_ac is not True:
        return frugal, "on battery"
    if seen.heavy_app and seen.igpu:
        return IGPU, f"{seen.heavy_app} is open"
    if not seen.rtx:
        return (IGPU if seen.igpu else CPU), "the RTX 4050 isn't there"
    return RTX, "plugged in"


def unit_of(llm: LlmSettings) -> Unit:
    """The unit these settings run on (a Vulkan device other than the 780M counts as the RTX 4050)."""
    if llm.backend == "cpu":
        return CPU
    if llm.backend == "vulkan" and llm.device != "Vulkan1":
        return IGPU
    return RTX


class Steady:
    """Hysteresis: a wanted unit counts once it has been wanted for `settle` seconds in a row."""

    def __init__(self, current: Unit, settle: float, clock: Callable[[], float] = time.monotonic):
        self.current, self.settle, self.clock = current, settle, clock
        self._wanted: Unit | None = None
        self._since = 0.0

    def due(self, wanted: Unit) -> Unit | None:
        """The unit to move to now, or None."""
        now = self.clock()
        if wanted == self.current:
            self._wanted = None
            return None
        if wanted != self._wanted:
            self._wanted, self._since = wanted, now
        return wanted if now - self._since >= self.settle else None


# ---- what's there --------------------------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _DevInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", _GUID), ("DevInst", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p)]


def _guid(text: str) -> _GUID:
    u = uuid.UUID(text)
    g = _GUID(u.fields[0], u.fields[1], u.fields[2])
    g.Data4[:] = u.bytes[8:]
    return g


_DISPLAY = _guid("4d36e968-e325-11ce-bfc1-08002be10318")  # GUID_DEVCLASS_DISPLAY
_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
_setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DevInfo)]
_setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DevInfo), wintypes.DWORD,
                                                        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
                                                        ctypes.POINTER(wintypes.DWORD)]
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]


def adapters() -> list[str]:
    """The graphics adapters Windows has right now ("AMD Radeon(TM) 780M", "NVIDIA GeForce RTX 4050
    Laptop GPU"); a removed one isn't listed. A few milliseconds, no process started."""
    devs = _setupapi.SetupDiGetClassDevsW(ctypes.byref(_DISPLAY), None, None, 0x2)  # DIGCF_PRESENT
    names: list[str] = []
    try:
        i = 0
        while True:
            info = _DevInfo(cbSize=ctypes.sizeof(_DevInfo))
            if not _setupapi.SetupDiEnumDeviceInfo(devs, i, ctypes.byref(info)):
                return names
            buf = ctypes.create_unicode_buffer(256)
            if _setupapi.SetupDiGetDeviceRegistryPropertyW(devs, ctypes.byref(info), 0x0, None, buf,  # SPDRP_DEVICEDESC
                                                           ctypes.sizeof(buf), None):
                names.append(buf.value)
            i += 1
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(devs)


def look(s: PlacementSettings, open_apps: list[str] = ()) -> Seen:
    """What the decision needs, read now."""
    found = adapters()
    heavy = next((app for app in open_apps if any(h.lower() in app.lower() for h in s.gpu_heavy_apps if h)), "")
    return Seen(power_source.on_ac_power(), any("RTX" in a or "NVIDIA" in a for a in found),
                any("Radeon" in a for a in found), heavy)


_VULKAN = re.compile(r"^\s*(Vulkan\d+):\s*(.+?)\s*\(", re.MULTILINE)


def vulkan_device(server: str, name: str = "Radeon") -> str | None:
    """The Vulkan build's id for the GPU whose name contains `name` ("Vulkan0" for the 780M here):
    the numbering follows what Windows has, so it is asked each time."""
    try:
        out = subprocess.run([server, "--list-devices"], capture_output=True, text=True, timeout=30,
                             creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return next((m[1] for m in _VULKAN.finditer(out.stdout + out.stderr) if name in m[2]), None)


def settings_for(unit: Unit, llm: LlmSettings, port: int | None = None,
                 find_device: Callable[[str], str | None] | None = None) -> LlmSettings | None:
    """[llm] moved to `unit` (and `port`), or None when the unit can't be reached."""
    if unit == IGPU:
        device = (find_device or vulkan_device)(llm.vulkan_server)
        if device is None:
            return None
        return replace(llm, backend="vulkan", device=device, port=port or llm.port)
    if unit == CPU:
        return replace(llm, backend="cpu", device="", port=port or llm.port)
    return replace(llm, backend="cuda", device="", port=port or llm.port)


def start_settings(settings: Settings, seen: Seen | None = None) -> LlmSettings:
    """The settings AJ's model starts with: [llm] as it is, or with mode "auto" the unit for now
    (on battery, the 780M from the start)."""
    if settings.placement.mode != "auto":
        return settings.llm
    unit, _ = decide(seen or look(settings.placement), settings.placement)
    for candidate in (unit, CPU):
        if (moved := settings_for(candidate, settings.llm)) is not None:
            return moved
    return settings.llm


# ---- moving while he runs ------------------------------------------------------------------

class Placer:
    """Watches, decides and moves AJ's model between turns (mode "auto" only).

    `server` is the one running now. The engine supplies the rest:
      open_apps()       the names of the open apps (core/laptop_state.py)
      idle()            no turn is being answered: the moment to switch
      warm_up(base)     the persona read by the server at `base`, before it takes over
      adopt(server, unit, why)  point everything at the new server; the old one is stopped here after
    """

    def __init__(self, settings: Settings, server: LlamaServer, *, open_apps: Callable[[], list[str]],
                 idle: Callable[[], bool], warm_up: Callable[[str], None],
                 adopt: Callable[[LlamaServer, Unit, str], None], stop: threading.Event,
                 look_now: Callable[[], Seen] | None = None, make: Callable[[LlmSettings], LlamaServer] = LlamaServer,
                 clock: Callable[[], float] = time.monotonic, find_device: Callable[[str], str | None] | None = None):
        self.settings, self.server = settings, server
        self.open_apps, self.idle, self.warm_up, self.adopt, self.stop = open_apps, idle, warm_up, adopt, stop
        self.look = look_now or (lambda: look(settings.placement, self.open_apps()))
        self.make, self.clock, self.find_device = make, clock, find_device
        self.steady = Steady(unit_of(server.settings), settings.placement.settle_secs, clock)
        self.why = "as configured"
        self._lock = threading.RLock()  # one move at a time: the watcher's, or a recovery's
        self._retry_at = 0.0
        self._thread: threading.Thread | None = None

    @property
    def unit(self) -> Unit:
        return self.steady.current

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="placement")
        self._thread.start()

    def _run(self) -> None:
        while not self.stop.wait(self.settings.placement.check_secs):
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - a failed look or move costs a move, never AJ
                print(f"(placement: {exc})", flush=True)

    def step(self) -> bool:
        """One look; a move if one is due. Returns whether AJ moved."""
        if self.server.proc is None:
            return False  # asleep with the model let go (Settings.sleep.free_gpu): nothing to move
        if self.server.proc.poll() is not None:
            return self.recover()
        wanted, why = decide(self.look(), self.settings.placement)
        unit = self.steady.due(wanted)
        if unit is None or self.clock() < self._retry_at:
            return False
        return self._move(unit, why)

    def recover(self) -> bool:
        """The running server died: a new one on the unit for now, at once. Called before a turn
        (the engine's model_ready) and by the watcher. False when nothing needed doing."""
        if self.server.proc is None or self._alive(self.server):
            return False
        with self._lock:
            if self._alive(self.server):
                return True  # a move that was under way has just replaced it
            wanted, why = decide(self.look(), self.settings.placement)
            for unit in (wanted, CPU):
                if self._move(unit, f"{why}; the server on {self.unit.label} had stopped", wait_idle=False):
                    return True
        return False

    @staticmethod
    def _alive(server: LlamaServer) -> bool:
        return server.proc is not None and server.proc.poll() is None

    def _port(self) -> int:
        p = self.settings.placement
        return p.alternate_port if self.server.settings.port != p.alternate_port else self.settings.llm.port

    def _move(self, unit: Unit, why: str, wait_idle: bool = True) -> bool:
        with self._lock:
            llm = settings_for(unit, self.settings.llm, self._port(), self.find_device)
            if llm is None:
                print(f"(placement: {unit.label} can't be reached; staying on {self.unit.label})", flush=True)
                self._retry_at = self.clock() + 60
                return False
            new = self.make(llm)
            t0 = self.clock()
            try:
                new.start()
                self.warm_up(new.base)
            except Exception as exc:  # noqa: BLE001 - the old server keeps going
                new.stop()
                print(f"(placement: {unit.label} didn't start ({exc}); staying on {self.unit.label})", flush=True)
                self._retry_at = self.clock() + 60
                return False
            # Between turns only; but a server that died meanwhile is replaced at once (a turn may
            # be waiting for exactly this, in recover()).
            while wait_idle and not self.idle() and not self.stop.is_set() and self._alive(self.server):
                time.sleep(0.2)
            old, self.server = self.server, new
            self.steady.current, self.why = unit, why
            self.adopt(new, unit, why)
            old.stop()
            print(f"(AJ's model moved to {unit.label}: {why}, ready in {self.clock() - t0:.1f} s.)", flush=True)
            return True
