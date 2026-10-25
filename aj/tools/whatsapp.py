"""WhatsApp by voice: "go to Appa" gets the boss into Appa's chat, and goes no further.

The boss (2026-10-22): open WhatsApp, search the name in WhatsApp's own search bar, press Enter
and get him into the chat, the way SivaSafe did it. He types and sends the message himself:
nothing here ever types into the message box or sends anything. And no contact list: "I don't
want the entire contact database to be on the AI". WhatsApp's search does the finding, and the
only trace of a name is the audit line the dispatcher writes for every call. He also warned that
WhatsApp spends a while loading its chats when it first opens (0 to 100%, slower on bad Wi-Fi),
so nothing is typed until it has finished.

SivaSafe told "ready" from "loading" by sending full-screen screenshots to Gemini, which put his
chat list in the cloud. Here UI Automation (core/uia.py) reads WhatsApp's window as text,
locally. What it shows was measured live on 2026-10-22 with scripts/probe_whatsapp.py:
- WhatsApp Desktop is a web page (WebView2) in a WinUI window. Scans start at the page
  (AutomationId "RootWebArea"): from the window, WinUI and Chromium both list the page and
  FindAll multiplied it (25,804 elements for 332 real ones).
- Loading: a splash with a progress bar going 0 to 100 and "End-to-end encrypted". Loaded: the
  "Search or start a new chat" box and the chat list grid, and no progress bar. That took 9 to
  11 s from launch that day; on slow Wi-Fi it can take minutes, hence the long wait.
- Ctrl+F did nothing straight after a launch (the keys were with WinUI's frame, not the page).
  UIA SetFocus on the search box moves the page's focus, but the keys typed after the first
  SetFocus were dropped; after a second one they landed. So the name is typed and read back from
  the box, focusing it again if it didn't arrive, up to three times. The characters go in as real
  key presses (core/keyboard.py): WhatsApp ignored Unicode key events altogether.
- A box holding text loses its name (it was its placeholder), so the box is followed by its
  UI Automation runtime id.
- Enter in the search box opens the top result and leaves the keyboard focus in the message box,
  named "Type a message to Appa". That name is how the open chat is read back, and the focus is
  left there for dictation.

Every key (each letter of the name, Ctrl+A, Backspace, Enter, Escape) goes in the same order (the
review of 2026-10-22): first any modifier the boss is holding gets its moment to be let go, and if
it is still held nothing is pressed (with Ctrl down, letters are shortcuts); then the focus is
checked to still be the search box; then the key. Checked the other way round, the focus could
move during the wait. And a call the dispatcher gave up on (its timeout) presses nothing more.
"No chat called X" is a failure (ToolError), not a result: a model told "No chat called X" as an
ok result could still say "Opened X's chat" (the review of 2026-10-22).
"""
import logging
import re
import time
from collections.abc import Callable

from aj.core import keyboard, uia
from aj.tools import apps
from aj.tools.registry import Level, ToolError, cancel_event, tool

log = logging.getLogger(__name__)

LOAD_WAIT = 120.0        # how long WhatsApp gets to open and load its chats (the tool has 150 s)
_POLL = 0.25
_READY_POLLS = 2         # the loaded look, seen this many times in a row
_NO_GRID_POLLS = 12      # a search box and no progress bar for 3 s counts too (an account with no chats)
_LINK_POLLS = 8          # the "link your phone" screen for 2 s: no point waiting longer
_SETTLE = 0.3            # after focusing or typing, before reading the box back
_TYPE_ATTEMPTS = 4
_RESULTS_WAIT = 4.0      # for the search results to appear and stop changing
_RESULTS_STABLE = 3      # polls with the same results
_OPEN_WAIT = 3.0         # for the chat to open after Enter
_MAX_NAME = 60

_now: Callable[[], float] = time.monotonic  # module-level so tests can run the clock
_sleep: Callable[[float], None] = time.sleep

_MESSAGE_BOX = re.compile(r"^type a message(?: to (?P<name>.+?))?\s*$", re.IGNORECASE)
_LOADING = re.compile(r"loading\b", re.IGNORECASE)  # "Loading your chats", "Loading..."
_LINK = re.compile(r"\b(?:scan (?:the )?qr code|link with phone|link a device|log ?in with (?:your )?phone"
                   r"|use whatsapp on your computer)\b", re.IGNORECASE)
_NO_RESULTS = re.compile(r"\bno (?:chats|contacts|messages|results)\b|\bnothing found\b", re.IGNORECASE)
_GRIDS = {"DataGrid", "List", "Table"}
_GRID_ROLES = {"grid", "list", "listbox", "treegrid"}


# ---- names ---------------------------------------------------------------------------------

_POSSESSIVE = r"(?:['’]s)"
_TAIL = re.compile(rf"(?:{_POSSESSIVE}?\s+(?:chat|chats|number|contact|conversation|whatsapp)"
                   r"|\s+(?:on|in) whatsapp)\s*$", re.IGNORECASE)
_HEAD = re.compile(r"^(?:(?:the|my)\s+)?(?:(?:chat|conversation)\s+with\s+)?", re.IGNORECASE)


def clean_name(text: str) -> str:
    """"Suhinish number", "Suhinish's chat", "Appa on WhatsApp", "chat with Appa" -> the name."""
    name = " ".join(str(text).split()).strip(" .,!?\"'")
    for _ in range(3):  # "Appa's chat on WhatsApp" has two tails
        shorter = _TAIL.sub("", _HEAD.sub("", name)).strip(" .,!?\"'")
        if shorter == name:
            break
        name = shorter
    return re.sub(rf"{_POSSESSIVE}$", "", name).strip()


def _spoken(name: str) -> str:
    """'appa' from the fast path (lowercase) -> 'Appa'; a name with capitals stays as it is."""
    return name.title() if name.islower() else name


def _possessive(name: str) -> str:
    return f"{name}'s"


# ---- reading WhatsApp's page (pure: tests feed these lists of uia.Node) --------------------

def _texts(nodes: list[uia.Node], kinds: tuple[str, ...] = ("Text", "StatusBar")) -> list[str]:
    """Short interface texts. Not chat rows or groups, whose names are message previews."""
    return [n.name.strip() for n in nodes if n.name and len(n.name) <= 80 and n.control_type in kinds
            and n.aria_role not in ("row", "gridcell")]


def _grid(nodes: list[uia.Node]) -> uia.Node | None:
    return next((n for n in nodes if n.control_type in _GRIDS or n.aria_role in _GRID_ROLES), None)


def search_box(nodes: list[uia.Node]) -> uia.Node | None:
    """WhatsApp's search box: the edit box named like "Search or start a new chat". Once it holds
    text it loses that name, and then only a plain text input sitting above the chat list counts,
    so the message box (a rich editor at the bottom, "Type a message to ...") can never be taken
    for it: Enter there would send."""
    edits = [n for n in nodes if (n.control_type == "Edit" or n.aria_role in ("textbox", "searchbox"))
             and not _MESSAGE_BOX.match(n.name.strip())]
    named = next((n for n in edits if "search" in n.name.lower()), None)
    if named is not None:
        return named
    grid = _grid(nodes)
    if grid is None or grid.bounds is None:
        return None
    for n in edits:
        plain_input = "html-input" in n.class_name or n.aria_role == "searchbox"
        if plain_input and n.bounds is not None and n.bounds[1] + n.bounds[3] <= grid.bounds[1] + 1:
            return n
    return None


def screen_state(nodes: list[uia.Node] | None) -> str:
    """'loading' (the splash or a loading text), 'link' (it wants a phone linked), 'ready'
    (search box and chat list, nothing loading), 'nogrid' (search box but no chat list yet) or
    'starting' (nothing to go on yet)."""
    if not nodes:
        return "starting"
    if any(_LOADING.match(t) for t in _texts(nodes)):
        return "loading"
    box = search_box(nodes)
    if box is None:
        # The splash's progress bar. Once the search box is there, a progress bar is a photo or
        # voice note downloading in the open chat, not the load.
        if any(n.control_type == "ProgressBar" or n.aria_role == "progressbar" for n in nodes):
            return "loading"
        return "link" if any(_LINK.search(t) for t in _texts(nodes, ("Text", "Button", "Header"))) else "starting"
    if not box.enabled or box.offscreen:
        return "starting"
    return "ready" if _grid(nodes) is not None else "nogrid"


def result_rows(nodes: list[uia.Node]) -> list[uia.Node]:
    return [n for n in nodes if n.aria_role == "row" or (n.control_type == "DataItem" and not n.aria_role)]


def no_results(nodes: list[uia.Node]) -> bool:
    return not result_rows(nodes) or any(_NO_RESULTS.search(t) for t in _texts(nodes))


def chat_name(focused: uia.Node | None) -> str | None:
    """The open chat's name, read from its message box ("Type a message to Appa"), else None."""
    if focused is None or focused.control_type != "Edit":
        return None
    found = _MESSAGE_BOX.match(focused.name.strip())
    return found["name"].strip() if found and found["name"] else None


def _is_message_box(found: uia.Node | None) -> bool:
    return found is not None and found.control_type == "Edit" and bool(_MESSAGE_BOX.match(found.name.strip()))


def _same(a: uia.Node | None, b: uia.Node | None) -> bool:
    if a is None or b is None:
        return False
    if a.runtime_id and b.runtime_id:
        return a.runtime_id == b.runtime_id
    return (a.control_type, a.automation_id, a.name) == (b.control_type, b.automation_id, b.name)


# ---- driving WhatsApp ----------------------------------------------------------------------

def _app() -> apps.App:
    app = apps.resolve("whatsapp", strict=True)
    if app is None:
        raise ToolError("I can't find WhatsApp on this laptop.")
    return app


def _window(app: apps.App) -> int:
    mine = apps.find_windows(app)
    return mine[0].hwnd if mine else 0


def _page(hwnd: int) -> list[uia.Node] | None:
    root = uia.find(hwnd, automation_id="RootWebArea") if hwnd else None
    return uia.scan(root) if root is not None else None


def _wait_until_loaded(app: apps.App, deadline: float) -> tuple[int, list[uia.Node]]:
    """WhatsApp's window and page once its chats have loaded. Raises ToolError at the deadline."""
    started, hwnd, state, streak, last = _now(), 0, "starting", 0, ""
    while True:
        hwnd = _window(app)
        nodes = _page(hwnd) if hwnd else None
        state = screen_state(nodes) if hwnd else "nowindow"
        streak = streak + 1 if state == last else 1
        last = state
        if (state == "ready" and streak >= _READY_POLLS) or (state == "nogrid" and streak >= _NO_GRID_POLLS):
            log.info("WhatsApp ready after %.1f s", _now() - started)
            return hwnd, nodes or []
        if state == "link" and streak >= _LINK_POLLS:
            raise ToolError("WhatsApp wants you to link your phone first, so I can't search it yet.")
        if _now() >= deadline:
            break
        _sleep(_POLL)
    log.info("WhatsApp not ready after %.1f s (%s)", _now() - started, state)
    if state == "nowindow":
        raise ToolError("WhatsApp didn't open.")
    raise ToolError("WhatsApp is still loading your chats, probably the Wi-Fi. Ask me again in a minute.")


_LOCKED = "The laptop is locked, so I can't get into WhatsApp."


def _ensure_front(hwnd: int) -> None:
    if keyboard.screen_locked():  # live (2026-10-22): keys meant for WhatsApp would reach the lock screen
        raise ToolError(_LOCKED)
    if keyboard.front_window() != hwnd and not keyboard.focus(hwnd):
        raise ToolError("Windows wouldn't let me bring WhatsApp to the front.")


_KEYS_HELD = "A key was still held down, so I didn't type into WhatsApp. Let go of it and ask again."
_GAVE_UP = "That took too long, so I stopped and pressed nothing more in WhatsApp."


def _ready_to_press(box: uia.Node) -> bool:
    """The last check before any key: held modifiers first (refused if still held after the
    wait), then the focus, which must still be the search box. False when the focus has moved."""
    stop = cancel_event()
    if stop is not None and stop.is_set():
        raise ToolError(_GAVE_UP)
    try:
        keyboard.wait_for_modifiers()
    except keyboard.ModifiersHeld:
        raise ToolError(_KEYS_HELD) from None
    return _same(uia.focused_node(), box)


def _press_in(box: uia.Node, keys: str) -> bool:
    """Press `keys` only if the focus is still in the search box. True if pressed."""
    if not _ready_to_press(box):
        return False
    keyboard.press(keys)
    return True


def _clear(box: uia.Node) -> bool:
    """Empty the search box: Ctrl+A, then Backspace, each only while the box has the focus."""
    if not _press_in(box, "ctrl+a") or not _press_in(box, "backspace"):
        return False
    _sleep(_SETTLE)
    return True


def _type_search(hwnd: int, box: uia.Node, name: str) -> None:
    """Get `name` into the search box, checked by reading the box back. Keys are sent only while
    UI Automation says the focus is in the search box, so the message box never gets any."""
    for attempt in range(_TYPE_ATTEMPTS):
        _ensure_front(hwnd)
        uia.set_focus(box)
        _sleep(_SETTLE)
        if not _same(uia.focused_node(), box):
            continue
        if uia.value(box):            # an earlier search is still there
            if not _clear(box) or uia.value(box):  # the focus moved, or those keys were dropped: again
                continue
        if not _ready_to_press(box):
            continue
        keyboard.type_text(name)
        _sleep(_SETTLE)
        typed = uia.value(box)
        if typed is not None and typed.strip().casefold() == name.casefold():
            return
        log.info("WhatsApp search: the name didn't arrive (attempt %d)", attempt + 1)
        if typed:
            _clear(box)
    raise ToolError("I couldn't type into WhatsApp's search box.")


def _shape(nodes: list[uia.Node]) -> tuple:
    """What the results look like, to see them change and settle (kept in memory only)."""
    rows = result_rows(nodes)
    return tuple(r.runtime_id or r.name for r in rows[:6]), len(rows), no_results(nodes)


def _results(hwnd: int, before: tuple) -> list[uia.Node]:
    """The page once the search results have appeared and stopped changing."""
    deadline = _now() + _RESULTS_WAIT
    last, steady = None, 0
    while True:
        nodes = _page(hwnd) or []
        shape = _shape(nodes)
        steady = steady + 1 if shape == last else 1
        last = shape
        if shape != before and steady >= _RESULTS_STABLE:
            return nodes
        if _now() >= deadline:
            return nodes
        _sleep(_POLL)


def _open_top(hwnd: int, box: uia.Node) -> tuple[bool, str | None]:
    """Enter in the search box opens the top result. (opened, the chat's name if it could be read)."""
    _ensure_front(hwnd)
    if not _same(uia.focused_node(), box):
        uia.set_focus(box)
        _sleep(_SETTLE)
    if not _press_in(box, "enter"):  # modifiers let go, then the focus checked, then Enter
        raise ToolError("WhatsApp's search box lost the focus, so I didn't open anything.")
    deadline = _now() + _OPEN_WAIT
    while True:
        _sleep(_POLL)
        focused = uia.focused_node()
        if _is_message_box(focused):
            return True, chat_name(focused)
        if focused is not None and not _same(focused, box) and _now() >= deadline:
            return True, None  # the focus moved, but not to a message box it could read
        if _now() >= deadline:
            return False, None


@tool(level=Level.SAFE, timeout=150.0, cloud=False)
def whatsapp_open_chat(name: str) -> str:
    """Open someone's WhatsApp chat: opens WhatsApp, waits for it to load, searches the name and opens the top match. Never types or sends a message.

    name: the person's or group's name as it is in WhatsApp, e.g. Appa
    """
    wanted = clean_name(name)
    if not wanted:
        raise ToolError("Whose chat should I open?")
    if len(wanted) > _MAX_NAME:
        raise ToolError("That name is too long for a WhatsApp search.")
    shown = _spoken(wanted)
    deadline = _now() + LOAD_WAIT
    if not uia.available():
        raise ToolError("I can't read WhatsApp's window on this laptop, so I can't search it.")
    if keyboard.screen_locked():
        raise ToolError(_LOCKED)
    app = _app()
    apps.open_app(name=app.name)  # brings it to the front, or starts it (ToolError if it can't)
    hwnd, nodes = _wait_until_loaded(app, deadline)
    box = search_box(nodes)
    if box is None:
        raise ToolError("I can't find WhatsApp's search box.")
    before = _shape(nodes)
    _type_search(hwnd, box, wanted)
    nodes = _results(hwnd, before)
    if no_results(nodes):
        if keyboard.front_window() == hwnd:
            try:
                _press_in(box, "escape")  # clears the search, only while the box has the focus
            except ToolError:
                pass  # a key held down: the search stays, and the answer is the same
        raise ToolError(f"No chat called {shown} in WhatsApp.")
    opened, header = _open_top(hwnd, box)
    if not opened:
        raise ToolError(f"I found {shown} in WhatsApp, but the chat didn't open.")
    if header is None:
        return f"The top match for {shown} should be open, but I couldn't confirm which chat it is."
    if header.casefold() in (wanted.casefold(), wanted.casefold() + "s"):
        return f"{_possessive(header)} chat is open."
    return f"Opened {_possessive(header)} chat, the top match for {shown}."


# ---- fast-path intents ---------------------------------------------------------------------
# Matched in full against the normalized transcript (intents.py): lowercase, no punctuation, so
# "Appa's chat" arrives as "appas chat". whatsapp is first in tools.MODULES, so every rule here
# must decline (return None) what belongs to apps.py or web.py: "go to youtube", "go to gmail
# com", "go to settings", "go to chrome".

# Words that end a "go to ..." but aren't anyone's name.
_NOT_NAMES = {
    "the", "a", "an", "my", "your", "our", "his", "her", "their", "this", "that", "it", "me", "him",
    "them", "us", "you", "there", "here", "sleep", "bed", "school", "work", "class", "home", "back",
    "top", "bottom", "end", "start", "beginning", "next", "previous", "last", "first", "page", "line",
    "group", "chat", "chats", "whatsapp", "everyone", "someone", "somebody", "anyone", "nobody",
    "dinner", "lunch", "breakfast", "church", "temple", "office", "town", "bathroom", "toilet", "gym",
}
# Words that make it a place, a file or a web page rather than a person.
_PLACE_WORDS = {
    "downloads", "download", "desktop", "documents", "document", "pictures", "photos", "music", "videos",
    "video", "folder", "file", "files", "drive", "website", "site", "page", "tab", "window", "link", "url",
    "settings", "setting", "channel", "playlist", "song", "dot", "www", "http", "https", "recycle", "bin",
    "mode", "volume", "brightness", "app", "application", "program",
}
_TLDS = {"com", "org", "net", "io", "co", "ai", "dev", "app", "gov", "edu", "me", "tv", "uk", "my", "sg", "in", "au"}
_MESSAGE_TAIL = re.compile(r"\s+(?:saying|that|to say|and say|and tell (?:him|her|them)|telling (?:him|her|them)"
                           r"|asking (?:him|her|them)|about|to ask)\b.*$")


def _name_like(name: str) -> bool:
    """One to three words of letters, none of them a command word, a place or a web address."""
    words = name.split()
    if not 1 <= len(words) <= 3 or not all(w.isalpha() for w in words):
        return False
    if words[0] in _NOT_NAMES or name in _NOT_NAMES or set(words) & _PLACE_WORDS or words[-1] in _TLDS:
        return False
    return True


def _is_app_or_site(name: str) -> bool:
    from aj.tools import web  # here: web is only needed for its site names
    return name in web.SITES or name.replace(" ", "") in web.SITES or apps.resolve(name, strict=True) is not None


def _whatsapp_on_screen() -> bool:
    """Is WhatsApp's window open? A bare "go to X" means a chat only then."""
    try:
        app = apps.resolve("whatsapp", strict=True)
        return app is not None and bool(apps.find_windows(app))
    except Exception:
        log.exception("checking for WhatsApp's window failed")
        return False


def _from_spoken(rest: str) -> tuple[str, str]:
    """(the name, the name as said) from what followed the verb. WhatsApp words and message
    content go, and so does the "s" of a lost apostrophe: "appas chat" was "Appa's chat". A real
    "Suhas chat" becomes "suha", which WhatsApp's search still matches to Suhas."""
    rest = _MESSAGE_TAIL.sub("", rest.strip())
    rest = re.sub(r"\s+(?:on|in) whatsapp$", "", rest)
    tail = re.search(r"\s+(?:chat|chats|number|contact|conversation|whatsapp)$", rest)
    if tail:
        rest = rest[:tail.start()]
    rest = re.sub(r"^(?:the |my )?(?:chat|conversation) with ", "", rest)
    said = re.sub(r"^the ", "", rest).strip()
    if tail and len(said) > 3 and said.endswith("s") and not said.endswith("ss"):
        return said[:-1], said
    return said, said


def _explicit(m: re.Match) -> dict | None:
    """The request names WhatsApp, a chat, a number or a message: a name is enough."""
    name, said = _from_spoken(m["rest"])
    if not _name_like(name):
        return None
    if m.groupdict().get("whatsapp") is None and (_is_app_or_site(said) or _is_app_or_site(name)):
        return None  # "open the teams chat" is the Teams app's
    return {"name": name}


def _bare_go_to(m: re.Match) -> dict | None:
    """'go to X' is said of sites, folders and apps too: a chat only while WhatsApp is open and X
    is nothing else."""
    name = m["rest"].strip()
    if not _name_like(name) or _is_app_or_site(name) or not _whatsapp_on_screen():
        return None
    return {"name": name}


_GO = r"(?:go to|open(?: up)?|show(?: me)?|pull up|bring up|take me to|switch to|jump to|get me into|find|search for)"

# "I want to talk to Suhinish on WhatsApp" (the acceptance run, 2026-10-22): left to the model, it
# called switch_to_app and said the chat was open. A phrase that names WhatsApp and a person is
# the fast path's, whether or not WhatsApp is open.
_WANT = r"(?:(?:i (?:want|need|would like|wanna)|id like|i have|lets|let me|can i|could i|i will|ill|im going)(?: to)? )?"
_TALK = r"(?:talk|chat|speak|text|message|write) (?:to|with)|chat|talk|reach|contact|ping|text|message|whatsapp"

INTENTS: list[tuple[str, str, Callable[[re.Match], dict | None]]] = [
    # "Open WhatsApp and go to Appa": the boss's own phrasing (2026-10-22).
    (rf"open whatsapp and (?:{_GO}|message|text|talk to|chat with|speak to) (?P<rest>.+)(?P<whatsapp>)",
     "whatsapp_open_chat", _explicit),
    # "I want to talk to Suhinish on WhatsApp", "chat with Appa on WhatsApp", "on WhatsApp, talk to Amma".
    (rf"{_WANT}(?:{_TALK}) (?P<rest>.+?)(?P<whatsapp> (?:on|in|over|via|through|using) whatsapp)",
     "whatsapp_open_chat", _explicit),
    (rf"(?P<whatsapp>(?:on|in|over) whatsapp) {_WANT}(?:{_TALK}|{_GO}) (?P<rest>.+)", "whatsapp_open_chat",
     _explicit),
    # "Message Appa", "text Appa saying I'm late": the chat only; he types the message himself.
    (r"(?:message|text|whatsapp|send a message to|send a text to) (?P<rest>.+?)(?P<whatsapp> on whatsapp)?",
     "whatsapp_open_chat", _explicit),
    (rf"{_GO} (?P<rest>.+?)(?P<whatsapp> (?:on|in) whatsapp)", "whatsapp_open_chat", _explicit),
    (rf"{_GO} (?P<rest>(?:the )?(?:chat|conversation) with .+)", "whatsapp_open_chat", _explicit),
    (rf"{_GO} (?P<rest>.+ (?:chat|number|contact|conversation))", "whatsapp_open_chat", _explicit),
    (r"go to (?P<rest>.+)", "whatsapp_open_chat", _bare_go_to),
]
