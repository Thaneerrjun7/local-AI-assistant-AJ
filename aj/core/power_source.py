"""Is the laptop plugged in? One question, asked every few seconds by the wake listener
(aj/listener.py) and by sleep mode.

The user, 2026-10-22: the wake words and sleep mode "should only be available when my laptop is
plugged in. If it's not plugged in, this feature should not be available at all." So the
answer has to be cheap (GetSystemPowerStatus is a plain kernel call, microseconds) and it must
never raise: a listener that crashed on a failed reading would be off for good.

How the answers come out:
- Windows says on mains (ACLineStatus 1): True. Says on battery (0): False.
- Windows can't say (ACLineStatus 255) but the machine has no battery at all (BatteryFlag 128,
  "no system battery": a desktop, or this laptop with its battery taken out): True. Without a
  battery the mains is the only thing it can be running on, so it counts as plugged in.
- Windows can't say and there is a battery, or the call itself fails: None. The callers treat
  None as battery: the safe side is "not listening" (core/power.py decides the same way).

This is separate from core/power.py's read_power on purpose: that one drops BatteryFlag (needed
for the no-battery case above) and raises when the call fails, which is right for the battery
tool that tells the user what went wrong, and wrong for a background check.
"""
import ctypes
from ctypes import wintypes


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

_AC_OFFLINE, _AC_ONLINE = 0, 1
_NO_SYSTEM_BATTERY = 128
_UNKNOWN = 255  # both ACLineStatus and BatteryFlag use 255 for "unknown"


def _read() -> tuple[int, int] | None:
    """(ACLineStatus, BatteryFlag) from Windows, or None when the call fails."""
    status = _SystemPowerStatus()
    if not _kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    return status.ACLineStatus, status.BatteryFlag


def ac_from(ac_line: int, battery_flag: int) -> bool | None:
    """The answer for one reading (see the module docstring)."""
    if ac_line == _AC_ONLINE:
        return True
    if ac_line == _AC_OFFLINE:
        return False
    if battery_flag != _UNKNOWN and battery_flag & _NO_SYSTEM_BATTERY:
        return True
    return None


def on_ac_power() -> bool | None:
    """True plugged in (or no battery at all), False on battery, None when Windows can't say.
    Never raises."""
    try:
        reading = _read()
    except OSError:
        return None
    return None if reading is None else ac_from(*reading)
