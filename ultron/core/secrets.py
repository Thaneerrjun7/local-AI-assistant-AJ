"""API keys live in Windows Credential Manager (encrypted with your Windows login),
never in a project file. SivaSafe kept every key in one plaintext JSON; Ultron doesn't.
"""
import ctypes
from ctypes import wintypes

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class _Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


_advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.POINTER(ctypes.POINTER(_Credential))]
_advapi32.CredReadW.restype = wintypes.BOOL
_advapi32.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
_advapi32.CredWriteW.restype = wintypes.BOOL
_advapi32.CredFree.argtypes = [ctypes.c_void_p]

GEMINI = "Ultron/Gemini"


def read_secret(target: str) -> str | None:
    cred = ctypes.POINTER(_Credential)()
    if not _advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(cred)):
        if ctypes.get_last_error() == _ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        blob = ctypes.string_at(cred.contents.CredentialBlob, cred.contents.CredentialBlobSize)
        return blob.decode("utf-16-le")
    finally:
        _advapi32.CredFree(cred)


def write_secret(target: str, secret: str, user: str = "ultron") -> None:
    blob = secret.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = _Credential(Type=_CRED_TYPE_GENERIC, TargetName=target, CredentialBlobSize=len(blob),
                       CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)),
                       Persist=_CRED_PERSIST_LOCAL_MACHINE, UserName=user)
    if not _advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())
