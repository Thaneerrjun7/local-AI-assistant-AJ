"""Phase 0 environment probe: Python, power, RAM, GPU, and whether Windows exposes
per-process GPU counters (needed so the resource guard can ignore AJ's own load).

Uses only the standard library, so it runs before anything is installed.
"""
import ctypes
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aj.core.power import read_power  # noqa: E402

NO_WINDOW = 0x08000000


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                             creationflags=NO_WINDOW)
        return (out.stdout or out.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable ({exc})"


def main() -> None:
    print(f"Python      {sys.version.split()[0]}  ({sys.executable})")

    power = read_power()
    print(f"Power       plugged_in={power.plugged_in} battery={power.battery_percent}% "
          f"saver={power.battery_saver}")

    mem = _MemoryStatusEx()
    mem.dwLength = ctypes.sizeof(mem)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
    print(f"RAM         {mem.ullTotalPhys / 2**30:.1f} GB total, "
          f"{mem.ullAvailPhys / 2**30:.1f} GB free, load {mem.dwMemoryLoad}%")

    print("GPU         " + run(["nvidia-smi",
                                "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
                                "--format=csv,noheader"]))
    apps = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader"])
    print("GPU apps    " + (apps or "none"))

    counters = run(["typeperf", "-qx", "GPU Engine"])
    n = sum(1 for line in counters.splitlines() if "Utilization Percentage" in line)
    print(f"GPU Engine  per-process utilization counters: {n} instances"
          + ("" if n else "  <-- resource guard needs another way to subtract its own load"))


if __name__ == "__main__":
    main()
