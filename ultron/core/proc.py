"""Tie child processes to Ultron's lifetime.

Children are put in a Windows job object with KILL_ON_JOB_CLOSE, so if Ultron exits
or crashes, the OS closes the job handle and kills them: an orphaned llama-server
would otherwise keep ~3 GB of VRAM while you play a game.
"""
import ctypes
import subprocess
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_EXTENDED_LIMIT_INFO = 9
_KILL_ON_JOB_CLOSE = 0x2000
_job = None


def _get_job():
    global _job
    if _job is None:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(job, _EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        _job = job
    return _job


def bind_to_ultron(proc: subprocess.Popen) -> None:
    """Kill `proc` automatically when Ultron's process ends, however it ends."""
    if not _kernel32.AssignProcessToJobObject(_get_job(), wintypes.HANDLE(int(proc._handle))):
        raise ctypes.WinError(ctypes.get_last_error())
