"""Readings of this laptop's hardware for the placement work (docs/HARDWARE_AWARE_INFERENCE_PLAN.md),
shared by scripts/check_devices.py and scripts/bench_baseline.py. Standard library and ctypes only.

Only nvidia_smi() talks to the RTX 4050, and asking the card anything can wake it from its powered-
down state (D3): exactly what the battery runs measure. So the card's power state is read from
Windows' Plug and Play records instead (power_state), which never touches the device. Checked
2026-10-26: plugged in and idle, the card read D3 five times in a row while being asked.

The battery is read from its driver (IOCTL_BATTERY_QUERY_STATUS), not WMI: no process to start,
so sampling it every few seconds costs nothing measurable.
"""
import ctypes
import json
import re
import subprocess
import uuid
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

NO_WINDOW = 0x08000000
ROOT = Path(__file__).resolve().parent.parent
VULKAN_SERVER = ROOT / "models" / "llama.cpp-vulkan" / "llama-server.exe"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_cfgmgr = ctypes.WinDLL("cfgmgr32")
_psapi = ctypes.WinDLL("psapi", use_last_error=True)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> "_GUID":
        u = uuid.UUID(text)
        g = cls(u.fields[0], u.fields[1], u.fields[2])
        g.Data4[:] = u.bytes[8:]
        return g


# ---- the battery ---------------------------------------------------------------------------

class _InterfaceData(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", _GUID), ("Flags", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p)]


class _WaitStatus(ctypes.Structure):
    _fields_ = [("BatteryTag", wintypes.ULONG), ("Timeout", wintypes.ULONG), ("PowerState", wintypes.ULONG),
                ("LowCapacity", wintypes.ULONG), ("HighCapacity", wintypes.ULONG)]


class _BatteryStatus(ctypes.Structure):
    _fields_ = [("PowerState", wintypes.ULONG), ("Capacity", wintypes.ULONG), ("Voltage", wintypes.ULONG),
                ("Rate", ctypes.c_long)]


_BATTERY_CLASS = _GUID.of("72631e54-78a4-11d0-bcf7-00aa00b7b32a")
_DIGCF_PRESENT, _DIGCF_DEVICEINTERFACE = 0x2, 0x10
_IOCTL_BATTERY_QUERY_TAG, _IOCTL_BATTERY_QUERY_STATUS = 0x294040, 0x29404C
_BATTERY_POWER_ON_LINE = 0x1
_INVALID_HANDLE = wintypes.HANDLE(-1).value

_setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
_setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
                                                  wintypes.DWORD, ctypes.POINTER(_InterfaceData)]
_setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_InterfaceData), ctypes.c_void_p,
                                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.HANDLE]
_kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


@dataclass(frozen=True)
class Battery:
    on_ac: bool
    watts: float          # drawn from the battery; 0.0 while plugged in (the driver reports charging as positive)
    capacity_mwh: int


def _battery_paths() -> list[str]:
    devs = _setupapi.SetupDiGetClassDevsW(ctypes.byref(_BATTERY_CLASS), None, None, _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
    paths: list[str] = []
    try:
        i = 0
        while True:
            data = _InterfaceData(cbSize=ctypes.sizeof(_InterfaceData))
            if not _setupapi.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(_BATTERY_CLASS), i, ctypes.byref(data)):
                return paths
            need = wintypes.DWORD()
            _setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(data), None, 0, ctypes.byref(need), None)
            detail = ctypes.create_string_buffer(need.value)
            ctypes.cast(detail, ctypes.POINTER(wintypes.DWORD))[0] = 8  # cbSize of the 64-bit detail struct
            if _setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(data), detail, need, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(detail) + 4))
            i += 1
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(devs)


def battery() -> Battery | None:
    """The first battery's state, or None when there's none or it can't be read."""
    for path in _battery_paths():
        handle = _kernel32.CreateFileW(path, 0xC0000000, 3, None, 3, 0x80, None)
        if handle == _INVALID_HANDLE:
            continue
        try:
            tag, wait, got = wintypes.ULONG(), wintypes.ULONG(0), wintypes.DWORD()
            if not _kernel32.DeviceIoControl(handle, _IOCTL_BATTERY_QUERY_TAG, ctypes.byref(wait), 4, ctypes.byref(tag), 4,
                                             ctypes.byref(got), None):
                continue
            ask, status = _WaitStatus(BatteryTag=tag.value), _BatteryStatus()
            if not _kernel32.DeviceIoControl(handle, _IOCTL_BATTERY_QUERY_STATUS, ctypes.byref(ask), ctypes.sizeof(ask),
                                             ctypes.byref(status), ctypes.sizeof(status), ctypes.byref(got), None):
                continue
            return battery_from(status.PowerState, status.Rate, status.Capacity)
        finally:
            _kernel32.CloseHandle(handle)
    return None


def battery_from(power_state: int, rate_mw: int, capacity_mwh: int) -> Battery:
    """The driver's numbers as a Battery: a negative rate is power leaving the battery."""
    return Battery(bool(power_state & _BATTERY_POWER_ON_LINE), round(max(0, -rate_mw) / 1000, 2), capacity_mwh)


# ---- a device's power state, from Windows' own records ------------------------------------

class _PropKey(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.ULONG)]


_POWER_DATA = _PropKey(_GUID.of("a45c254e-df1c-4efd-8020-67d146a850e0"), 32)  # DEVPKEY_Device_PowerData


def power_state(instance_id: str) -> str | None:
    """"D0" (on) to "D3" (powered down) for a device, "absent" when Windows has removed it, or None
    when it can't be read. Measured 2026-10-26: unplugged, this laptop removes the RTX 4050
    altogether (Device Manager: not present), and llama-server then finds no CUDA device."""
    node = wintypes.DWORD()
    if _cfgmgr.CM_Locate_DevNodeW(ctypes.byref(node), instance_id, 0) != 0:  # CM_LOCATE_DEVNODE_NORMAL: present only
        return "absent"
    kind, size = wintypes.ULONG(), wintypes.ULONG(64)
    raw = (ctypes.c_ubyte * 64)()
    if _cfgmgr.CM_Get_DevNode_PropertyW(node, ctypes.byref(_POWER_DATA), ctypes.byref(kind), raw, ctypes.byref(size), 0):
        return None
    return decode_power_data(bytes(raw[:size.value]))


def decode_power_data(raw: bytes) -> str | None:
    """CM_POWER_DATA's PD_MostRecentPowerState (1 = D0 ... 4 = D3) as "D0".."D3"."""
    if len(raw) < 8:
        return None
    state = int.from_bytes(raw[4:8], "little")
    return f"D{state - 1}" if 1 <= state <= 4 else None


# ---- processes ----------------------------------------------------------------------------

class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


class _MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MemoryCounters), wintypes.DWORD]
_PROCESS_QUERY_LIMITED_INFORMATION, _PROCESS_VM_READ = 0x1000, 0x0010


def processes() -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) for every process."""
    snap = _kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    out: list[tuple[int, int, str]] = []
    try:
        entry = _ProcessEntry(dwSize=ctypes.sizeof(_ProcessEntry))
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return out


def tree(root: int, table: list[tuple[int, int, str]] | None = None) -> list[tuple[int, str]]:
    """The process and everything it started, as (pid, exe name)."""
    table = processes() if table is None else table
    names = {pid: name for pid, _, name in table}
    found, todo = [], [root]
    while todo:
        pid = todo.pop()
        if pid in names and pid not in (p for p, _ in found):
            found.append((pid, names[pid]))
            todo += [child for child, parent, _ in table if parent == pid and child != pid]
    return found


@dataclass(frozen=True)
class Usage:
    working_set_mb: float
    private_mb: float
    cpu_seconds: float


def usage(pid: int) -> Usage | None:
    """A process's memory (what Task Manager shows is close to its working set) and CPU time so far."""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    try:
        mem = _MemoryCounters(cb=ctypes.sizeof(_MemoryCounters))
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _psapi.GetProcessMemoryInfo(handle, ctypes.byref(mem), mem.cb) or \
                not _kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        ticks = sum((t.dwHighDateTime << 32) | t.dwLowDateTime for t in times[2:])  # kernel + user, 100 ns units
        mb = 1024 * 1024
        return Usage(round(mem.WorkingSetSize / mb, 1), round(mem.PagefileUsage / mb, 1), ticks / 1e7)
    finally:
        _kernel32.CloseHandle(handle)


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def ram_mb() -> tuple[int, int]:
    """(total, free) system RAM in MB."""
    status = _MemoryStatus(dwLength=ctypes.sizeof(_MemoryStatus))
    _kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullTotalPhys // 2**20, status.ullAvailPhys // 2**20


# ---- devices ------------------------------------------------------------------------------

def run(cmd: list[str], timeout: float = 60) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                             creationflags=NO_WINDOW)
        return (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable ({exc})"


def powershell_json(script: str) -> list[dict]:
    """A PowerShell pipeline's objects (ending in ConvertTo-Json), as a list of dicts."""
    out = run(["powershell", "-NoProfile", "-Command", f"[Console]::OutputEncoding = [Text.Encoding]::UTF8; {script}"])
    try:
        data = json.loads(out)
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def pnp_devices() -> list[dict]:
    """Every device Windows knows, present or not: InstanceId, FriendlyName, Class, Status, Present."""
    return powershell_json("Get-PnpDevice | Select-Object InstanceId,FriendlyName,Class,Status,Present | ConvertTo-Json -Compress")


def display_drivers() -> list[dict]:
    """The graphics adapters' driver versions: DeviceName, DriverVersion, DriverDate."""
    return powershell_json("Get-CimInstance Win32_PnPSignedDriver | Where-Object DeviceClass -eq 'DISPLAY' | "
                           "Select-Object DeviceName,DriverVersion,@{n='DriverDate';e={$_.DriverDate.ToString('yyyy-MM-dd')}} | "
                           "ConvertTo-Json -Compress")


def nvidia_smi() -> dict:
    """The RTX 4050's name, driver and memory, and which processes hold its memory. Wakes the card."""
    gpu = run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used", "--format=csv,noheader,nounits"])
    apps = run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"])
    return parse_nvidia(gpu, apps)


def parse_nvidia(gpu: str, apps: str) -> dict:
    fields = [f.strip() for f in gpu.strip().splitlines()[0].split(",")] if gpu.strip() else []
    if len(fields) != 4 or not fields[2].isdigit():
        return {}
    used_by = {}
    for line in apps.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            used_by[int(parts[0])] = int(parts[1])
    return {"name": fields[0], "driver": fields[1], "total_mb": int(fields[2]), "used_mb": int(fields[3]),
            "used_by_pid_mb": used_by}


_VULKAN_DEVICE = re.compile(r"^\s*(Vulkan\d+):\s*(.+?)\s*\((\d+) MiB, (\d+) MiB free\)", re.MULTILINE)


def vulkan_devices(server: Path = VULKAN_SERVER) -> list[dict]:
    """What the Vulkan build of llama-server can run on: [{id, name, total_mb, free_mb}]."""
    if not server.exists():
        return []
    return parse_vulkan(run([str(server), "--list-devices"]))


def parse_vulkan(text: str) -> list[dict]:
    return [{"id": m[1], "name": m[2], "total_mb": int(m[3]), "free_mb": int(m[4])} for m in _VULKAN_DEVICE.finditer(text)]


def cpu_name() -> str:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
        return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()


def bios_version() -> str:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as key:
        return str(winreg.QueryValueEx(key, "BIOSVersion")[0]).strip()


# ---- numbers ------------------------------------------------------------------------------

def pct(values: list[float], q: float) -> float | None:
    """The q-th percentile (0-100), interpolated; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    at = (len(ordered) - 1) * q / 100
    low = int(at)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (at - low), 1)
