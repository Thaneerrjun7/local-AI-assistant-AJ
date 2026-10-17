"""AC/battery state through GetSystemPowerStatus. No dependencies.

The wake word only listens while plugged in, so an unknown AC state counts as
battery: the safe side is "not listening".
"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SystemPowerStatus)]
_kernel32.GetSystemPowerStatus.restype = wintypes.BOOL


@dataclass(frozen=True)
class PowerState:
    plugged_in: bool | None  # None when Windows reports the AC state as unknown
    battery_percent: int | None
    battery_saver: bool

    @property
    def on_ac(self) -> bool:
        return self.plugged_in is True


def read_power() -> PowerState:
    status = _SystemPowerStatus()
    if not _kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    plugged = {0: False, 1: True}.get(status.ACLineStatus)
    percent = None if status.BatteryLifePercent == 255 else status.BatteryLifePercent
    return PowerState(plugged, percent, bool(status.SystemStatusFlag & 1))
