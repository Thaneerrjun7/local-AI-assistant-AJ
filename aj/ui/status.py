"""What each engine Status looks like on the status line: its words and its dot colour.
No Qt here, so the mapping is testable without a display."""
from aj.core.pipeline import Status
from aj.ui import theme

_LOOKS: dict[Status, tuple[str, str]] = {
    Status.LOADING: ("Loading...", theme.MUTED),
    Status.LISTENING: ("Listening", theme.RED_BRIGHT),
    Status.HEARING: ("Hearing you", "#ff8a8a"),
    Status.THINKING: ("Thinking", theme.RED),
    Status.SPEAKING: ("Speaking", theme.RED_BRIGHT),
    Status.MIC_OFF: ("Mic off", theme.MUTED),
    Status.TURNING_OFF: ("Turning off...", theme.MUTED),
    # "Write this down": what he says next is typed where his cursor is, not answered (2026-10-22).
    Status.DICTATING: ("Listening for dictation", "#ff8a8a"),
    # Sleep mode (2026-10-22): the window is in the tray then, but shows this when brought back.
    Status.ASLEEP: ("Asleep · say its name to wake it", theme.MUTED),
    Status.ASLEEP_ON_BATTERY: ("Asleep · not listening on battery", theme.MUTED),
    Status.ASLEEP_MIC_OFF: ("Asleep · mic off", theme.MUTED),
    # The mic wouldn't open (2026-10-22): the window comes up saying so, and it is tried again.
    Status.MIC_FAILED: ("Microphone not working · check it, then click the mic", theme.RED_BRIGHT),
    # An answer failed (2026-11-18): shown for a few seconds, then he listens again.
    Status.ERROR: ("Error · that answer failed, ask again", theme.RED_BRIGHT),
}
ERROR = ("Stopped: something went wrong", theme.RED_BRIGHT)

# The mic button and text box work only once the conversation can actually start.
NOT_READY = {Status.LOADING, Status.TURNING_OFF}

# The orb has a look for each Status, named by its value (qml/OrbStates.js), and one for an
# engine that died. The status words take the alarm's colour too (the user, 2026-10-23).
ERROR_MODE = "error"
ERROR_TONE = "#ff5a4e"
_TONES = {Status.MIC_FAILED: "#ffa25c", Status.ERROR: ERROR_TONE}


def look(status: Status) -> tuple[str, str]:
    """(label, dot colour) for the status line."""
    return _LOOKS[status]


def orb_mode(status: Status) -> str:
    """The orb's look for a status."""
    return status.value


# A tool running (Phase 2, 2026-10-23): "Working · weather" and the orb's Working look, while
# AJ would otherwise show Thinking. A tool not named here shows its own name, spaced out.
WORKING_MODE = "working"
_TOOL_WORDS = {
    "get_weather": "weather", "calculate": "calculating", "web_search": "searching the web",
    "current_time": "the time", "play_youtube": "YouTube", "open_website": "a website",
    "open_app": "apps", "switch_to_app": "apps", "close_app": "apps", "list_apps": "apps", "snap_window": "windows",
    "find_files": "files", "list_folder": "files", "open_file": "files", "open_folder": "files",
    "copy_file": "files", "move_file": "files", "rename_file": "files", "delete_file": "files",
    "read_text_file": "files", "undo_last": "undoing",
    "create_reminder": "reminders", "list_reminders": "reminders", "cancel_reminder": "reminders",
    "start_timer": "a timer", "whatsapp_open_chat": "WhatsApp",
    "set_volume": "sound", "mute": "sound", "media": "media", "set_brightness": "brightness",
    "battery_status": "the laptop", "laptop_status": "the laptop", "disk_space": "the laptop",
    "type_text": "typing", "type_that": "typing", "polish_typed": "typing", "start_dictation": "dictation",
    "what_do_you_know": "memory", "forget_fact": "memory",
}


def working_text(tool: str) -> str:
    return f"Working · {_TOOL_WORDS.get(tool, tool.replace('_', ' '))}"


# The statuses the orb moves with a voice in: the window reads the levels only then.
LEVEL_STATUSES = frozenset({Status.HEARING, Status.SPEAKING, Status.DICTATING})


def tone(status: Status) -> str:
    """The status words' colour: "" for the usual, or the alarm's."""
    return _TONES.get(status, "")


def mic_caption(listening: bool) -> str:
    return "Mic on · click to mute" if listening else "Mic off · click to listen"
