"""Reading other apps' windows through Windows UI Automation, the interface screen readers use.

Two features need to see inside another app without a screenshot:
- WhatsApp chat search (tools/whatsapp.py) has to know when WhatsApp has finished loading its
  chats, find its search box and read the open chat's name. SivaSafe judged that by sending
  full-screen screenshots to Gemini, which sent the user's chat list to the cloud (his worry,
  2026-10-22). UI Automation reads the same window locally, as text, and nothing leaves the laptop.
- Dictation has to know whether the keyboard focus is in a text box before it types, what the box
  already holds, whether it takes line breaks and where its caret is: focused_editable(),
  focused_value(), multiline() and caret_at_end().

It goes through comtypes and UIAutomationCore.dll, loaded on first use. If either is missing,
every function here returns None (or [] / False) and the reason is logged once: AJ loses
these features, never anything else.

Threads: a COM object belongs to the thread that made it, so each thread gets its own
IUIAutomation, with short timeouts so a frozen app can't hang the caller. Don't ask about the
focus from the Qt main thread while AJ's own window has it: UI Automation would wait on that
same busy thread (Microsoft: UIA clients must not run on a UI thread), so focused_* say None then.

Privacy: Node.name is whatever the app shows, chat previews included. Nothing here logs, prints
or stores a name; callers keep them in memory for the moment they need them.
"""
import ctypes
import logging
import os
import threading
import time
import re
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

CONNECTION_TIMEOUT_MS = 1500   # IUIAutomation2: how long to wait for an app to answer at all
TRANSACTION_TIMEOUT_MS = 3000  # ... and for one request to finish
_MAX_TEXT = 20000              # focused_value reads at most this many characters

# Control types (UIA_*ControlTypeId) by the names used here.
CONTROL_TYPES: dict[int, str] = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
    50005: "Hyperlink", 50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu",
    50010: "MenuBar", 50011: "MenuItem", 50012: "ProgressBar", 50013: "RadioButton",
    50014: "ScrollBar", 50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb", 50028: "DataGrid",
    50029: "DataItem", 50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane",
    50034: "Header", 50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}
_TYPE_IDS = {name: tid for tid, name in CONTROL_TYPES.items()}

# Property and pattern ids (UIAutomationClient.h).
_P_CONTROL_TYPE, _P_NAME, _P_HAS_FOCUS, _P_FOCUSABLE, _P_ENABLED = 30003, 30005, 30008, 30009, 30010
_P_AUTOMATION_ID, _P_CLASS_NAME, _P_IS_PASSWORD, _P_OFFSCREEN = 30011, 30012, 30019, 30022
_P_HAS_TEXT, _P_HAS_VALUE, _P_VALUE_READ_ONLY, _P_RANGE_VALUE = 30040, 30043, 30046, 30047
_P_LEGACY_STATE, _P_ARIA_ROLE, _P_BOUNDS, _P_RUNTIME_ID = 30096, 30101, 30001, 30000
_P_NATIVE_WINDOW, _P_ARIA_PROPERTIES = 30020, 30102
_CACHED = (_P_RUNTIME_ID, _P_CONTROL_TYPE, _P_NAME, _P_HAS_FOCUS, _P_FOCUSABLE, _P_ENABLED, _P_AUTOMATION_ID,
           _P_CLASS_NAME, _P_IS_PASSWORD, _P_OFFSCREEN, _P_HAS_TEXT, _P_HAS_VALUE, _P_VALUE_READ_ONLY,
           _P_RANGE_VALUE, _P_LEGACY_STATE, _P_ARIA_ROLE, _P_BOUNDS, _P_NATIVE_WINDOW, _P_ARIA_PROPERTIES)
_VALUE_PATTERN, _TEXT_PATTERN = 10002, 10014
_RANGE_START, _RANGE_END = 0, 1       # TextPatternRangeEndpoint
_TREE_SCOPE_DESCENDANTS = 4
_IGNORE_CASE = 1                      # PropertyConditionFlags_IgnoreCase
_STATE_READONLY = 0x40                # MSAA STATE_SYSTEM_READONLY
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1

# Where typing goes in: edit boxes and documents, and the rich editors (WhatsApp's message box,
# Google Docs) that Chromium shows as a group or custom control with a text pattern.
_TEXT_BOXES = {"Edit", "Document", "ComboBox"}
_RICH_EDITORS = {"Edit", "Document", "Custom", "Group", "Pane"}


@dataclass(frozen=True)
class Node:
    """One element of another app's window, read in a single round trip."""
    control_type: str = ""        # "Edit", "Button", ... ("" if unknown)
    name: str = ""                # what the app calls it: a label, a chat's name, a preview
    automation_id: str = ""
    class_name: str = ""
    enabled: bool = True
    focused: bool = False         # has the keyboard focus
    focusable: bool = False
    offscreen: bool = False
    password: bool = False
    has_value: bool = False       # supports the Value pattern
    read_only: bool = False       # ... and says it is read-only
    has_text: bool = False        # supports the Text pattern
    range_value: float | None = None  # a progress bar's or slider's value
    legacy_state: int = 0         # MSAA state bits
    aria_role: str = ""           # web apps: the element's ARIA role ("textbox", "progressbar", ...)
    bounds: tuple[float, float, float, float] | None = None  # left, top, width, height
    runtime_id: tuple[int, ...] = ()  # UI Automation's id for the element while it exists
    native_hwnd: int = 0          # its own window, for classic Win32 controls (0 for web content)
    aria_properties: str = ""     # web apps: "multiline=true;readonly=false;..." (Chromium)
    element: Any = field(default=None, compare=False, repr=False)  # the live element, for set_focus


# ---- loading comtypes, once ----------------------------------------------------------------

_load_lock = threading.Lock()
_module: Any = None          # comtypes.gen.UIAutomationClient, or False once it has failed
_local = threading.local()   # this thread's IUIAutomation
_ole32 = ctypes.WinDLL("ole32")
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = ctypes.c_long
_user32 = ctypes.WinDLL("user32")
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD


def _import_comtypes() -> Any:
    import comtypes.client  # noqa: F401  (initialises COM on the importing thread)
    comtypes.client.GetModule("UIAutomationCore.dll")  # writes comtypes.gen.UIAutomationClient the first time
    from comtypes.gen import UIAutomationClient
    return UIAutomationClient


def _load() -> Any:
    """The generated UIAutomationClient module, or None when UI Automation can't be had."""
    global _module
    if _module is None:
        with _load_lock:
            if _module is None:
                # On a thread of its own: importing comtypes initialises COM on the importing
                # thread, and would fail on one that already chose the other threading model.
                box: dict[str, Any] = {}

                def work() -> None:
                    try:
                        box["module"] = _import_comtypes()
                    except BaseException as exc:  # ImportError, OSError, COMError ...
                        box["error"] = exc

                worker = threading.Thread(target=work, name="uia-load", daemon=True)
                worker.start()
                worker.join(60.0)
                if "module" in box:
                    _module = box["module"]
                else:
                    _module = False
                    log.warning("UI Automation is unavailable (%s); WhatsApp search and the dictation "
                                "focus check are off", box.get("error", "loading comtypes timed out"))
    return _module or None


def available() -> bool:
    return _client() is not None


def _client() -> Any:
    """This thread's IUIAutomation, made on first use."""
    made = getattr(_local, "client", None)
    if made is not None:
        return made or None
    module = _load()
    if module is None:
        return None
    _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)  # S_FALSE / changed-mode: COM is on anyway
    try:
        import comtypes
        try:
            client = comtypes.CoCreateInstance(module.CUIAutomation8._reg_clsid_, interface=module.IUIAutomation2,
                                               clsctx=_CLSCTX_INPROC_SERVER)
            client.ConnectionTimeout = CONNECTION_TIMEOUT_MS
            client.TransactionTimeout = TRANSACTION_TIMEOUT_MS
        except Exception:  # before Windows 8: no timeouts to set
            client = comtypes.CoCreateInstance(module.CUIAutomation._reg_clsid_, interface=module.IUIAutomation,
                                               clsctx=_CLSCTX_INPROC_SERVER)
        request = client.CreateCacheRequest()
        for prop in _CACHED:
            request.AddProperty(prop)
    except Exception as exc:
        log.warning("couldn't start UI Automation on this thread: %s", exc)
        _local.client = False
        return None
    _local.client, _local.request = client, request
    return client


# ---- reading elements ----------------------------------------------------------------------

def _cached(element: Any, prop: int, kind: type, default: Any) -> Any:
    try:
        value = element.GetCachedPropertyValue(prop)
    except Exception:
        return default
    if kind is float:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    if kind is tuple:
        return tuple(float(v) for v in value) if isinstance(value, (tuple, list)) and len(value) == 4 else default
    if kind is list:  # an int array
        return tuple(int(v) for v in value) if isinstance(value, (tuple, list)) else default
    return value if isinstance(value, kind) else default  # "not supported" comes back as a COM object


def _node(element: Any) -> Node:
    return Node(
        control_type=CONTROL_TYPES.get(_cached(element, _P_CONTROL_TYPE, int, 0), ""),
        name=_cached(element, _P_NAME, str, ""),
        automation_id=_cached(element, _P_AUTOMATION_ID, str, ""),
        class_name=_cached(element, _P_CLASS_NAME, str, ""),
        enabled=_cached(element, _P_ENABLED, bool, True),
        focused=_cached(element, _P_HAS_FOCUS, bool, False),
        focusable=_cached(element, _P_FOCUSABLE, bool, False),
        offscreen=_cached(element, _P_OFFSCREEN, bool, False),
        password=_cached(element, _P_IS_PASSWORD, bool, False),
        has_value=_cached(element, _P_HAS_VALUE, bool, False),
        read_only=_cached(element, _P_VALUE_READ_ONLY, bool, False),
        has_text=_cached(element, _P_HAS_TEXT, bool, False),
        range_value=_cached(element, _P_RANGE_VALUE, float, None),
        legacy_state=_cached(element, _P_LEGACY_STATE, int, 0),
        aria_role=_cached(element, _P_ARIA_ROLE, str, ""),
        bounds=_cached(element, _P_BOUNDS, tuple, None),
        runtime_id=_cached(element, _P_RUNTIME_ID, list, ()),
        native_hwnd=_cached(element, _P_NATIVE_WINDOW, int, 0),
        aria_properties=_cached(element, _P_ARIA_PROPERTIES, str, ""),
        element=element,
    )


def _root(target: Any) -> Any:
    """A window handle, a Node or a live element, as a live element."""
    client = _client()
    if client is None or target is None:
        return None
    if isinstance(target, Node):
        return target.element
    if isinstance(target, int):
        return client.ElementFromHandle(target) if target else None
    return target


def _condition(client: Any, control_type: str | None, name: str | None, automation_id: str | None,
               class_name: str | None) -> Any:
    parts = []
    if control_type is not None:
        if control_type not in _TYPE_IDS:
            raise ValueError(f"unknown control type {control_type!r}")
        parts.append(client.CreatePropertyCondition(_P_CONTROL_TYPE, _TYPE_IDS[control_type]))
    if name is not None:
        parts.append(client.CreatePropertyConditionEx(_P_NAME, name, _IGNORE_CASE))
    if automation_id is not None:
        parts.append(client.CreatePropertyCondition(_P_AUTOMATION_ID, automation_id))
    if class_name is not None:
        parts.append(client.CreatePropertyCondition(_P_CLASS_NAME, class_name))
    if not parts:
        return client.CreateTrueCondition()
    condition = parts[0]
    for part in parts[1:]:
        condition = client.CreateAndCondition(condition, part)
    return condition


def _unique(found: Any) -> list[Node]:
    """An element array as Nodes, each element once. Chromium's page can be reached by two paths
    (live in WhatsApp, 2026-10-22: FindAll from the window gave 25,804 elements for 332)."""
    if not found:
        return []
    seen: set[tuple[int, ...]] = set()
    out = []
    for i in range(found.Length):
        item = _node(found.GetElement(i))
        if item.runtime_id:
            if item.runtime_id in seen:
                continue
            seen.add(item.runtime_id)
        out.append(item)
    return out


def node(target: Any) -> Node | None:
    """A window (by handle) or element, read fresh."""
    try:
        element = _root(target)
        return _node(element.BuildUpdatedCache(_local.request)) if element is not None else None
    except Exception as exc:
        log.debug("reading an element failed: %s", exc)
        return None


def scan(target: Any) -> list[Node] | None:
    """Every element under a window (by handle) or element, in tree order, in one round trip.
    None when UI Automation can't read it (the window has gone, the app isn't answering)."""
    try:
        root = _root(target)
        if root is None:
            return None
        return _unique(root.FindAllBuildCache(_TREE_SCOPE_DESCENDANTS, _client().CreateTrueCondition(),
                                              _local.request))
    except Exception as exc:
        log.debug("scanning a window failed: %s", exc)
        return None


def find_all(target: Any, control_type: str | None = None, name: str | None = None,
             automation_id: str | None = None, class_name: str | None = None) -> list[Node]:
    """The elements under `target` that match every given property (names ignore case), in tree order."""
    try:
        root, client = _root(target), _client()
        if root is None:
            return []
        return _unique(root.FindAllBuildCache(_TREE_SCOPE_DESCENDANTS,
                                              _condition(client, control_type, name, automation_id, class_name),
                                              _local.request))
    except ValueError:
        raise
    except Exception as exc:
        log.debug("finding elements failed: %s", exc)
        return []


def find(target: Any, control_type: str | None = None, name: str | None = None,
         automation_id: str | None = None, class_name: str | None = None,
         timeout: float = 0.0, poll: float = 0.1) -> Node | None:
    """The first element under `target` matching every given property, waiting up to `timeout`."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            root, client = _root(target), _client()
            if root is not None:
                found = root.FindFirstBuildCache(_TREE_SCOPE_DESCENDANTS,
                                                 _condition(client, control_type, name, automation_id, class_name),
                                                 _local.request)
                if found:
                    return _node(found)
        except ValueError:
            raise
        except Exception as exc:
            log.debug("finding an element failed: %s", exc)
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def set_focus(target: Node | Any) -> bool:
    """Give an element the keyboard focus (it comes to the front with its window). True if UIA took it."""
    element = target.element if isinstance(target, Node) else target
    if element is None or _client() is None:
        return False
    try:
        element.SetFocus()
        return True
    except Exception as exc:
        log.debug("SetFocus failed: %s", exc)
        return False


def value(target: Node | Any) -> str | None:
    """An element's current text through its Value pattern (an edit box's contents), or None."""
    element = target.element if isinstance(target, Node) else target
    module = _load()
    if element is None or module is None or (isinstance(target, Node) and target.password):
        return None
    try:
        pattern = element.GetCurrentPattern(_VALUE_PATTERN)
        if not pattern:
            return None
        return str(pattern.QueryInterface(module.IUIAutomationValuePattern).CurrentValue or "")
    except Exception as exc:
        log.debug("reading a value failed: %s", exc)
        return None


# ---- the keyboard focus --------------------------------------------------------------------

def _own_window_in_front() -> bool:
    pid = wintypes.DWORD()
    front = _user32.GetForegroundWindow()
    if front:
        _user32.GetWindowThreadProcessId(front, ctypes.byref(pid))
    return bool(front) and pid.value == os.getpid()


def focused_node() -> Node | None:
    """The element with the keyboard focus, or None when UI Automation can't tell."""
    if threading.current_thread() is threading.main_thread() and _own_window_in_front():
        return None  # see the module docstring: this would wait on our own busy UI thread
    client = _client()
    if client is None:
        return None
    try:
        return _node(client.GetFocusedElementBuildCache(_local.request))
    except Exception as exc:
        log.debug("reading the focused element failed: %s", exc)
        return None


def editable(found: Node) -> bool:
    """Can typing go into this element? Edit boxes and documents that aren't read-only, and the
    rich editors web apps build (a focusable element with a text pattern that isn't read-only)."""
    if found.has_value and not found.read_only and (found.control_type in _TEXT_BOXES or found.has_text):
        return True
    if found.has_value:
        return False  # it says it's read-only (a web page itself, a log view)
    return (found.has_text and found.focusable and found.control_type in _RICH_EDITORS
            and not found.legacy_state & _STATE_READONLY)


def focused_editable() -> bool | None:
    """Is the keyboard focus in a text field typing would go into? None when UIA can't tell."""
    found = focused_node()
    return None if found is None else editable(found)


def focused_value() -> str | None:
    """The focused field's current text, when it can be read. Never a password field's."""
    found = focused_node()
    module = _load()
    if found is None or module is None or found.password:
        return None
    try:
        if found.has_value:
            pattern = found.element.GetCurrentPattern(_VALUE_PATTERN)
            return str(pattern.QueryInterface(module.IUIAutomationValuePattern).CurrentValue or "")
        if found.has_text:
            pattern = found.element.GetCurrentPattern(_TEXT_PATTERN)
            text = pattern.QueryInterface(module.IUIAutomationTextPattern).DocumentRange.GetText(_MAX_TEXT)
            return str(text or "")
    except Exception as exc:
        log.debug("reading the focused field failed: %s", exc)
    return None


# ---- line breaks and the caret (the review of 2026-10-22) -----------------------------------

_ARIA_MULTILINE = re.compile(r"(?:^|;)\s*multiline\s*=\s*true\b", re.IGNORECASE)
_ES_MULTILINE = 0x0004   # a Win32 edit control's style bit
_GWL_STYLE = -16
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long


def _win32_multiline(found: Node) -> bool:
    """A classic edit or rich edit control with the ES_MULTILINE style (the bit means something
    else for other window classes, so only those are asked)."""
    if not found.native_hwnd or "edit" not in found.class_name.lower():
        return False
    return bool(_user32.GetWindowLongW(found.native_hwnd, _GWL_STYLE) & _ES_MULTILINE)


def multiline(found: Node | None) -> bool:
    """Does this box take line breaks? A document (Word, Google Docs, Notepad's text), a web text
    area (Chromium says "multiline=true"), or a Win32 edit box with the multi-line style. False
    when it can't be told: a newline is then typed as a space, because Shift+Enter submits a
    single-line box (a Chrome input, the Run box), as Enter would."""
    if found is None:
        return False
    if found.control_type == "Document":
        return True
    if _ARIA_MULTILINE.search(found.aria_properties or ""):
        return True
    try:
        return _win32_multiline(found)
    except Exception as exc:  # noqa: BLE001 - a window that went away: not known to be multi-line
        log.debug("reading an edit box's style failed: %s", exc)
        return False


def caret_at_end(found: Node | None) -> bool | None:
    """Is the caret at the very end of this box's text, with nothing selected? None when UI
    Automation can't tell (no text pattern, no selection reported). Backspaces delete what is
    before the caret, or a selection, so "make it nicer" deletes only after this says True."""
    module = _load()
    if found is None or found.element is None or module is None or found.password:
        return None
    try:
        pattern = found.element.GetCurrentPattern(_TEXT_PATTERN)
        if not pattern:
            return None
        text = pattern.QueryInterface(module.IUIAutomationTextPattern)
        selection = text.GetSelection()
        if not selection or selection.Length != 1:
            return None
        caret = selection.GetElement(0)
        if caret.CompareEndpoints(_RANGE_START, caret, _RANGE_END) != 0:
            return False  # some text is selected: a Backspace would delete all of it
        return caret.CompareEndpoints(_RANGE_END, text.DocumentRange, _RANGE_END) == 0
    except Exception as exc:  # noqa: BLE001 - an app that doesn't answer: it can't be told
        log.debug("reading the caret failed: %s", exc)
        return None
