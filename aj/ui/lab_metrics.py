"""This process's own CPU and GPU use, for the live readout in the lab window (aj/ui/lab.py).

The lab polls it once a second from the Qt thread, so every call has to be cheap and none may
raise: a readout that threw would take the lab down with it. A figure Windows can't give is None,
for the lab to show as a dash. There is no Qt here, so it is tested without a window.

CPU is this process's user + kernel time (time.process_time: every thread, Qt's render thread
included) over the wall-clock time since the previous call, as a percent of ONE core, so two busy
cores read 200. Task Manager divides by all 16 threads instead, which makes one busy thread look
like 6%.

GPU is the 3D engine's utilisation from the Windows performance counters (PDH), this process only:
    \\GPU Engine(pid_<PID>_*engtype_3D)\\Utilization Percentage
Each instance is one engine of one adapter, named like
    pid_1234_luid_0x00000000_0x0000D1A3_phys_0_eng_0_engtype_3D
The luid says which adapter, and that matters on this laptop: the window draws on the Radeon 780M
(Direct3D 11) while the model runs on the RTX 4050 in another process, so gpu_by_adapter() lets the
lab confirm its load lands on one luid. The figure is the share of the time between two
collections that the engine was busy with this process's work, so the first reading (and the
first after a new engine appears) has none yet.

Measured on this laptop (Windows 11 26200, 2026-10-23):
- Engines that appear after the counter was added ARE picked up: PDH expands the wildcard again at
  every collection. A process that only starts drawing later (the lab makes its meters before its
  window shows) appears one collection after it starts, so the counter is never re-added.
- A process with no GPU engines at all matches no instance (PDH_NO_DATA): it is using none of the
  GPU, so that reads 0.0, not None.
- The first PdhAddEnglishCounterW in a process takes ~230 ms, for any counter: PDH loading the
  English counter names. So the query is opened on a background thread and gpu_percent() is None
  until it is ready, instead of stalling the lab's first frame.
- After that, one collection plus one read takes under a millisecond.
"""
import ctypes
import os
import re
import threading
import time
from ctypes import wintypes

_ERROR_SUCCESS = 0
_PDH_FMT_DOUBLE = 0x00000200
_PDH_MORE_DATA = 0x800007D2   # the buffer is too small; PDH has written the size it needs
_PDH_NO_DATA = 0x800007D5     # no instance matches: this process has no GPU engines (yet)
_PDH_CSTATUS_VALID_DATA = 0x0
_PDH_CSTATUS_NEW_DATA = 0x1
_GOOD = (_PDH_CSTATUS_VALID_DATA, _PDH_CSTATUS_NEW_DATA)

# gpu_percent() and gpu_by_adapter() called together share one collection: a second collection a
# few microseconds after the first would be a utilisation over no time at all.
_MIN_INTERVAL = 0.25  # s

_LUID = re.compile(r"(?:^|_)(luid_0x[0-9a-f]+_0x[0-9a-f]+)(?:_|$)", re.IGNORECASE)


class _FmtCounterValueUnion(ctypes.Union):
    _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double),
                ("largeValue", ctypes.c_longlong), ("stringValue", ctypes.c_void_p)]


class _FmtCounterValue(ctypes.Structure):
    """PDH_FMT_COUNTERVALUE: a DWORD status, then a union of 8-byte members, so on x64 the value
    sits at offset 8 after 4 bytes of padding: 16 bytes in all."""
    _anonymous_ = ("value",)
    _fields_ = [("CStatus", wintypes.DWORD), ("value", _FmtCounterValueUnion)]


class _FmtCounterValueItem(ctypes.Structure):
    """PDH_FMT_COUNTERVALUE_ITEM_W: the instance's name (a pointer into the same buffer, where PDH
    puts the strings after the items), then its value at offset 8: 24 bytes on x64."""
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _FmtCounterValue)]


def _load_pdh():
    """pdh.dll with the five calls declared, or None where there is no PDH (not Windows)."""
    try:
        pdh = ctypes.WinDLL("pdh")
    except (AttributeError, OSError):
        return None
    handle_out = ctypes.POINTER(wintypes.HANDLE)
    dword_out = ctypes.POINTER(wintypes.DWORD)
    for name, args in (
            ("PdhOpenQueryW", [wintypes.LPCWSTR, ctypes.c_size_t, handle_out]),
            ("PdhAddEnglishCounterW", [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, handle_out]),
            ("PdhCollectQueryData", [wintypes.HANDLE]),
            ("PdhGetFormattedCounterArrayW", [wintypes.HANDLE, wintypes.DWORD, dword_out, dword_out,
                                              ctypes.c_void_p]),
            ("PdhCloseQuery", [wintypes.HANDLE])):
        call = getattr(pdh, name)
        call.argtypes = args
        call.restype = wintypes.DWORD  # PDH_STATUS is a LONG; unsigned compares with 0x8.../0xC... as written
    return pdh


_pdh = _load_pdh()


def adapter_of(instance: str) -> str | None:
    """'luid_0x00000000_0x0000D1A3' for 'pid_1234_luid_0x00000000_0x0000D1A3_phys_0_eng_0_engtype_3D',
    or None for a name with no luid in it. The case is kept as given (PDH writes the hex in capitals,
    PowerShell's Get-Counter in small letters)."""
    match = _LUID.search(instance)
    return match.group(1) if match else None


def by_adapter(readings) -> dict[str, float]:
    """(instance name, percent) readings summed per adapter. A process can have several 3D engines
    on one adapter (Microsoft's software renderer has 17) and engines on more than one adapter.
    Names without a luid are left out."""
    totals: dict[str, float] = {}
    for name, percent in readings:
        adapter = adapter_of(name)
        if adapter is not None:
            totals[adapter] = totals.get(adapter, 0.0) + percent
    return totals


class ProcessMeters:
    """CPU and GPU meters for this process. Make one when the lab starts, read it from one thread
    (the Qt thread), and close() it when the lab closes."""

    def __init__(self):
        self._pid = os.getpid()
        self._cpu_then: tuple[float, float] | None = None  # (process_time, perf_counter) last call
        self._lock = threading.Lock()  # between the opening thread and close()
        self._closed = False
        self._handles = None  # (query, counter), set once by the opening thread
        self._buffer = None   # the array buffer, kept and grown when PDH asks for more
        self._sample: dict[str, float] | None = None
        self._sampled_at = float("-inf")
        self.gpu_supported: bool | None = None  # None while opening; False: no GPU Engine counters
        self._opener = threading.Thread(target=self._open_gpu, daemon=True, name="lab-metrics-pdh")
        self._opener.start()

    # --- CPU ---

    def cpu_percent(self) -> float:
        """This process's CPU since the previous call, as a percent of one core (2 busy cores =
        200). The first call has nothing to compare with and returns 0.0."""
        try:
            now = (time.process_time(), time.perf_counter())
        except OSError:  # never seen: Windows refusing the process its own times
            return 0.0
        then, self._cpu_then = self._cpu_then, now
        if then is None or now[1] <= then[1]:
            return 0.0
        return 100.0 * (now[0] - then[0]) / (now[1] - then[1])

    # --- GPU ---

    def gpu_percent(self) -> float | None:
        """This process's 3D-engine utilisation, summed over its engines (on every adapter), since
        the previous collection. None while the query opens, on the first collection, or when PDH
        can't say; 0.0 when the process has no GPU engines."""
        sample = self._read_gpu()
        return None if sample is None else float(sum(sample.values()))

    def gpu_by_adapter(self) -> dict[str, float]:
        """The same utilisation per adapter, keyed by luid ('luid_0x00000000_0x0000D1A3'). Empty
        when there is no figure, or no engines."""
        sample = self._read_gpu()
        return dict(sample) if sample else {}

    def wait_for_gpu(self, timeout: float = 5.0) -> bool:
        """Waits (at most timeout seconds) for the background open, and says whether there is a GPU
        figure to be had. For tests and scripts: the lab needn't call it."""
        self._opener.join(timeout)
        return bool(self.gpu_supported)

    def close(self) -> None:
        """Frees the query. Safe to call twice, and while the query is still being opened."""
        with self._lock:
            self._closed = True
            handles, self._handles = self._handles, None
        self._sample = None
        if handles is not None:
            try:
                _pdh.PdhCloseQuery(handles[0])  # closes its counter too
            except Exception:  # noqa: BLE001 - closing is best effort, never a crash on the way out
                pass

    def _open_gpu(self) -> None:
        """On the opening thread: the query, the counter, and the first of the two collections a
        utilisation needs, so the lab's first poll already has a figure for any engine that exists."""
        query, counter = wintypes.HANDLE(), wintypes.HANDLE()
        opened = added = False
        try:
            if _pdh is not None:
                opened = _pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) == _ERROR_SUCCESS
                path = rf"\GPU Engine(pid_{self._pid}_*engtype_3D)\Utilization Percentage"
                added = opened and _pdh.PdhAddEnglishCounterW(
                    query, path, 0, ctypes.byref(counter)) == _ERROR_SUCCESS
                if added:
                    _pdh.PdhCollectQueryData(query)
        except Exception:  # noqa: BLE001 - a thread that raised would only print; no figure instead
            added = False
        with self._lock:
            if added and not self._closed:
                self._sampled_at = time.perf_counter()
                self._handles = (query, counter)
                self.gpu_supported = True
                return
            self.gpu_supported = False
        if opened:
            try:
                _pdh.PdhCloseQuery(query)
            except Exception:  # noqa: BLE001
                pass

    def _read_gpu(self) -> dict[str, float] | None:
        """Per-adapter figures from a fresh collection, or from the last one if it was under
        _MIN_INTERVAL ago. Never raises."""
        handles = self._handles
        if handles is None:
            return None
        now = time.perf_counter()
        if now - self._sampled_at < _MIN_INTERVAL:
            return self._sample
        self._sampled_at = now
        query, counter = handles
        try:
            status = _pdh.PdhCollectQueryData(query)
            if status == _PDH_NO_DATA:  # the only counter matched nothing: no engines
                self._sample = {}
            elif status != _ERROR_SUCCESS:
                self._sample = None
            else:
                self._sample = self._read_array(counter)
        except Exception:  # noqa: BLE001 - a failed reading is a dash in the lab, never a crash
            self._sample = None
        return self._sample

    def _read_array(self, counter) -> dict[str, float] | None:
        """The counter's instances from the last collection, summed per adapter. The buffer is sized
        by PDH: a call with too small a buffer (none, the first time) says how many bytes it needs."""
        size, count = wintypes.DWORD(len(self._buffer) if self._buffer else 0), wintypes.DWORD(0)
        for _ in range(3):  # the size can't change between two reads of one collection; 3 is plenty
            status = _pdh.PdhGetFormattedCounterArrayW(counter, _PDH_FMT_DOUBLE, ctypes.byref(size),
                                                       ctypes.byref(count), self._buffer)
            if status != _PDH_MORE_DATA:
                break
            self._buffer = ctypes.create_string_buffer(size.value)
        if status == _PDH_NO_DATA:
            return {}
        if status != _ERROR_SUCCESS:
            return None  # among them PDH_CSTATUS_INVALID_DATA: the first collection, no figure yet
        if not count.value:
            return {}
        if self._buffer is None or len(self._buffer) < count.value * ctypes.sizeof(_FmtCounterValueItem):
            return None  # PDH said success without the room for its items: never seen, but never read past
        items = (_FmtCounterValueItem * count.value).from_buffer(self._buffer)
        readings = [(item.szName or "", item.FmtValue.doubleValue) for item in items
                    if item.FmtValue.CStatus in _GOOD]
        if not readings:
            return None  # engines that have only been seen once: no figure yet
        return by_adapter(readings)
