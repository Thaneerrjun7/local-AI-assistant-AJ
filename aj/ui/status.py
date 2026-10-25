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
}
ERROR = ("Stopped: something went wrong", theme.RED_BRIGHT)

# The mic button and text box work only once the conversation can actually start.
NOT_READY = {Status.LOADING, Status.TURNING_OFF}


def look(status: Status) -> tuple[str, str]:
    """(label, dot colour) for the status line."""
    return _LOOKS[status]


def mic_caption(listening: bool) -> str:
    return "Mic on · click to mute" if listening else "Mic off · click to listen"
