"""What each engine Status looks like on the status line: its words and its dot colour.
No Qt here, so the mapping is testable without a display."""
from ultron.core.pipeline import Status
from ultron.ui import theme

_LOOKS: dict[Status, tuple[str, str]] = {
    Status.LOADING: ("Loading...", theme.MUTED),
    Status.LISTENING: ("Listening", theme.RED_BRIGHT),
    Status.HEARING: ("Hearing you", "#ff8a8a"),
    Status.THINKING: ("Thinking", theme.RED),
    Status.SPEAKING: ("Speaking", theme.RED_BRIGHT),
    Status.MIC_OFF: ("Mic off", theme.MUTED),
    Status.TURNING_OFF: ("Turning off...", theme.MUTED),
}
ERROR = ("Stopped: something went wrong", theme.RED_BRIGHT)

# The mic button and text box work only once the conversation can actually start.
NOT_READY = {Status.LOADING, Status.TURNING_OFF}


def look(status: Status) -> tuple[str, str]:
    """(label, dot colour) for the status line."""
    return _LOOKS[status]


def mic_caption(listening: bool) -> str:
    return "Mic on · click to mute" if listening else "Mic off · click to listen"
