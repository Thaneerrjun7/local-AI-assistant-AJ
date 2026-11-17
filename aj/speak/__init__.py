"""AJ (Speak): a small floating window that types what the user says, wherever his cursor is.

The user, 2026-10-22: "The speech to text tool should be available for me to use anywhere, meaning
anywhere where I can type anything, I should be able to activate speech to text. So I want a
separate small window for speech to text. That can be used as a tool on its own... I want it created
and put on my desktop. It should be called AJ (speak)." And: "This tool can use Gemini. It can take
longer, but ... I want it to be accurate."

It is its own program (`pythonw -m aj.speak`, the desktop shortcut), not part of the assistant:
no brain, no voice, no local model, so it starts in a moment and costs nothing while it waits. The
words come from the same dictation core the assistant uses (core/dictation.py): Gemini writes them
down, Parakeet stands in only when Gemini can't, and type_into() types them with real key presses
where his cursor is. It never presses Enter, never touches the clipboard and never sends: he sends
it himself.

Modules: speaker.py is the logic with no Qt (so it can be tested with fakes), listen.py the
microphone and the voice-activity detector that decides when he has stopped, window.py the floating
window, hotkey.py the global hotkey, instance.py the single-instance guard, store.py the saved
position, icon.py the icon, app.py the start-up.
"""
