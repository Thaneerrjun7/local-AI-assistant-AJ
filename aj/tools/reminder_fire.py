"""What Windows Task Scheduler runs when a reminder or timer is due, whether AJ is on or off.

    pythonw.exe aj\\tools\\reminder_fire.py --id 3f9a1c2b7d --text <the text, base64url>

Ported from SivaSafe's reminder_fire.py and core/win_notify.py. pythonw.exe has no console, so a
reminder arriving is just the notification appearing.

- The reminder list decides. One that is no longer in it was cancelled: nothing is shown and the
  job deletes itself, so a cancel sticks even if AJ couldn't delete the job. Only when the
  list itself is gone or unreadable is the copy of the text on the command line shown instead.
- The notification is a Windows toast sent through PowerShell's built-in WinRT bridge (SivaSafe
  needed the `winrt` package for it; AJ adds no packages). It appears under AJ's own name,
  claimed per user under HKCU\\Software\\Classes\\AppUserModelId with no admin rights. If the
  toast fails, a plain message box takes its place: a reminder that passes in silence is the
  worst bug this feature can have.
- Nothing printed here is ever seen, so every run leaves a line in %APPDATA%\\AJ\\logs\\reminders.log.
"""
import argparse
import base64
import binascii
import ctypes
import os
import subprocess
import sys
import traceback
import winreg
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

if __package__ in (None, ""):  # started as a script by Task Scheduler: make "aj" importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from aj.tools import reminders
    _IMPORT_ERROR = ""
except Exception:  # noqa: BLE001 - a broken install still announces the reminder from its own copy
    reminders = None
    _IMPORT_ERROR = traceback.format_exc()

# Worked out here, not imported from aj.config, so it still works if that import is what broke.
LOG_PATH = Path(os.environ.get("APPDATA", str(Path.home()))) / "AJ" / "logs" / "reminders.log"
NO_WINDOW = 0x08000000
_POWERSHELL = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                  / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")

APP_ID = "AJ.Assistant"
APP_NAME = "AJ"
_APP_ID_KEY = rf"Software\Classes\AppUserModelId\{APP_ID}"
# PowerShell's own toast identity. Always registered, so it is the fallback if AJ's can't be.
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# The toast and its sender arrive through environment variables, so no quoting can go wrong.
# Show() hands the toast over and returns at once; the pause keeps PowerShell alive until
# Windows has taken delivery (SivaSafe found exiting straight away can lose it silently).
_TOAST_SCRIPT = """
$ErrorActionPreference = 'Stop'
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($env:AJ_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:AJ_TOAST_APP).Show($toast)
Start-Sleep -Seconds 3
"""

_MB_ICONINFORMATION, _MB_SYSTEMMODAL, _MB_SETFOREGROUND, _MB_TOPMOST = 0x40, 0x1000, 0x10000, 0x40000


def _log(line: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")
    except OSError:
        pass


def ensure_app_id() -> str:
    """Claim AJ's toast identity for this user (idempotent, no admin) and return the id to use."""
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _APP_ID_KEY, 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current = winreg.QueryValueEx(key, "DisplayName")[0]
            except FileNotFoundError:
                current = None
            if current != APP_NAME:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        return APP_ID
    except OSError as exc:
        _log(f"couldn't register the {APP_ID} app id ({exc}); sending as PowerShell")
        return POWERSHELL_APP_ID


def toast_xml(title: str, text: str) -> str:
    """A toast that stays up about 25 seconds (duration="long") with the reminder sound."""
    return ('<toast duration="long"><visual><binding template="ToastGeneric">'
            f"<text>{escape(title)}</text><text>{escape(text)}</text>"
            '</binding></visual><audio src="ms-winsoundevent:Notification.Reminder"/></toast>')


def _run_powershell(script: str, env: dict[str, str]) -> tuple[int, str]:
    """(exit code, error text) of a PowerShell script run with no window. Tests replace this."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        done = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                              capture_output=True, timeout=30, creationflags=NO_WINDOW, env={**os.environ, **env})
    except subprocess.TimeoutExpired:
        return -1, "PowerShell took too long"
    except OSError as exc:
        return -1, f"couldn't start PowerShell ({exc})"
    return done.returncode, done.stderr.decode("oem", "replace").strip()


def _message_box(title: str, text: str) -> None:
    """A plain Windows message box on top of everything. Tests replace this."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user32.MessageBoxW.restype = ctypes.c_int
    user32.MessageBoxW(None, text, f"{APP_NAME}: {title}",
                       _MB_ICONINFORMATION | _MB_SYSTEMMODAL | _MB_SETFOREGROUND | _MB_TOPMOST)


def notify(title: str, text: str) -> str:
    """Show a toast as AJ, or a message box if Windows won't. Returns which one was shown."""
    clean = text.replace("\0", "")
    code, error = _run_powershell(_TOAST_SCRIPT, {"AJ_TOAST_XML": toast_xml(title, clean),
                                                  "AJ_TOAST_APP": ensure_app_id()})
    if code == 0:
        return "toast"
    _log(f"the toast failed ({error or f'exit code {code}'}); showing a message box")
    _message_box(title, clean)
    return "message box"


def unpack_text(packed: str) -> str:
    try:
        return base64.urlsafe_b64decode(packed.encode("ascii")).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeError):
        return ""


def fire(item_id: str, fallback: str) -> int:
    """Announce one due reminder or timer. Returns the process exit code."""
    if reminders is None:
        _log(f"{item_id}: AJ's code didn't import, so showing the job's own copy:\n{_IMPORT_ERROR}")
        notify("Reminder", fallback or "A reminder is due.")
        return 0
    entry, have_store = reminders.claim(item_id, datetime.now().replace(microsecond=0))
    if entry is None:
        if have_store:  # cancelled, or a one-off that was already shown: say nothing, tidy up
            _log(f"{item_id}: no longer set; deleting its job")
            reminders.unschedule(item_id)
            return 0
        _log(f"{item_id}: the reminder list is missing or unreadable; showing the job's own copy")
        notify("Reminder", fallback or "A reminder is due.")
        return 0
    title, text = reminders.announcement(entry)
    _log(f"{item_id}: shown as a {notify(title, text)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Announce a due AJ reminder.")
    parser.add_argument("--id", required=True, help="the reminder's id in reminders.json")
    parser.add_argument("--text", default="", help="its text as base64url, shown if the list is gone")
    args = parser.parse_args(argv)
    try:
        return fire(args.id, unpack_text(args.text))
    except Exception:  # noqa: BLE001 - the last resort below must still happen
        _log(f"{args.id}: FAILED\n{traceback.format_exc()}")
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except (ImportError, RuntimeError):
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
