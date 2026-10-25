"""AJ's brain: decides how each thing you say is handled, then speaks the result.

1. Instant commands ("open spotify", "volume 30") match a tool intent and run with no model
   call at all, so they feel immediate. A slow one (the weather service, a search) gets a short
   waiting line so there's no dead air. A question about facts a tool fetched ("does it rain
   today?") goes through Qwen with the exact question, so it gets a direct answer ("No, I
   don't think so...") instead of the same report read out again.
2. A few things you can say call for one of AJ's signature lines as the whole answer
   ("sing me something"); see aj/lines.py.
3. Hard questions (maths, planning, multi-step, live information) get a holding line at once,
   so there's never silence. Meanwhile the thinker works on it, with Gemini as backup.
   Qwen then voices the answer in character with the facts kept intact.
4. Everything else streams from Qwen, which may call laptop tools itself mid-turn. Tool
   results go back to Qwen for one spoken follow-up. When one of the signature lines fits what
   you said, Qwen is offered it to use, in full or in part, or not at all.

Every sentence passes through Delivery on its way to the voice: it adds a spoken reaction now
and then ("Hm.", "Right.") and drops an aside he has already made recently. Memory, when present, adds what AJ knows about
you to each turn. It goes after the persona prefix and never into it, so the model's prompt
cache stays valid.

Dictation (core/dictation.py, the boss's ask of 2026-10-22): after "write this down", or after a
WhatsApp chat opens ("What would you like to say?"), his next words are not a turn for the model:
dictate() has them typed where he asked, and he presses Send himself. A one-go "type <words>"
claimed by the fast path hands type_text this turn's own audio, so Gemini writes the words down
rather than the command transcript.

The words of a dictation stay out of the logs (the review of 2026-10-22): after a dictation turn,
`typed_turn` holds how many characters were dictated, and the pipeline logs "(dictation, N
characters)" instead of them. They stay out of memory too, as before. And "stop", "cancel" or
"never mind" stops any typing still in flight (Dictation.abort), such as a type_text the
dispatcher gave up on.
"""
import inspect
import json
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Iterable
from datetime import datetime

from aj import persona
from aj.config import Settings
from aj.core import dictation as dictating
from aj.core.chunker import SentenceChunker, clean_for_speech, drop_notes, strip_marks
from aj.core.delivery import Delivery
from aj.core.escalate import looks_hard, needs_live_info, relay_prompt
from aj.core.llm import Cancelled, stream_events
from aj.lines import Lines

# The model sees one fixed tool list, the same on every request: the list is part of the prompt
# prefix the model caches, so a request with a different list (or none) re-reads ~3,000 tokens,
# about 0.7 s (measured). Unregistered names are skipped. Since v4 he gets every tool he has, as
# SivaSafe's Gemini did: with only some of them, he claimed to do the rest in words.
LLM_TOOLS = ["open_app", "switch_to_app", "close_app", "open_website", "play_youtube",
             "set_volume", "mute", "set_brightness", "media", "snap_window", "screenshot",
             "battery_status", "laptop_status", "current_time", "disk_space",
             "create_reminder", "start_timer", "list_reminders", "cancel_reminder",
             "get_weather", "web_search",
             "find_files", "open_file", "open_folder", "list_folder", "read_text_file",
             "move_file", "copy_file", "rename_file", "delete_file", "undo_last",
             "lock_screen", "sleep_laptop", "restart_laptop", "shutdown_laptop", "sign_out", "cancel_shutdown",
             "calculate", "what_do_you_know", "forget_fact",
             # 2026-10-22: WhatsApp chats (tools/whatsapp.py), dictation (tools/dictation.py) and his
             # humor dial (tools/personality.py). type_that and polish_typed are fast-path only.
             "whatsapp_open_chat", "start_dictation", "type_text", "set_humor", "personality_setting",
             # 2026-10-22: the AJ (Speak) window by voice (tools/dictation.py).
             "open_speak"]
MAX_TOOL_ROUNDS = 3  # find a file, then open it: tool calls may follow tool results this many times
# Results from these are raw material (search hits, file listings) for Qwen to summarise,
# not sentences to read out word for word.
_SUMMARISE = {"web_search", "find_files", "list_folder", "read_text_file", "list_reminders"}
# Facts the boss asks questions about: Qwen answers the question from them.
_ANSWER_FROM = {"get_weather"}
# Tools that can take a second or two: a waiting line plays if they haven't finished by then.
# type_text waits on Gemini's transcript; opening a chat waits for WhatsApp to load.
_SLOW = {"get_weather", "web_search", "find_files", "whatsapp_open_chat", "type_text", "type_that", "polish_typed"}
# Results written to be said as they are, however long ("Gemini wasn't available, so I used the
# offline ears. Check it before you send. Typed it in WhatsApp. Press send when you're ready.").
_SAY_AS_IS = {"start_dictation", "type_text", "type_that", "polish_typed", "whatsapp_open_chat"}
# Tools that put words where the cursor is: after one of them, a chat that just opened needs no dictation.
_TYPES = {"start_dictation", "type_text", "type_that", "polish_typed"}
# What the model is told when it opened a chat itself: the question and the listening are done in code.
_CHAT_OPEN_HOW = ("[The chat is open. If the user already said the words to write, call type_text with them now. "
                  "Otherwise say only that the chat is open, in a few words: the question about what to say is "
                  "asked after your reply, and the user's next words are typed in for them.]")
# whatsapp_open_chat succeeds only with a chat in front ("Appa's chat is open.", "Opened Appa's chat,
# the top match...", "The top match ... should be open"). Since 2026-10-22 "No chat called Bob in
# WhatsApp." is a failure (ToolError), so the outcome alone says a chat opened: the result's
# wording is no longer read (the review of 2026-10-22).
_ASKED_WHAT = re.compile(r"\bwhat (?:would|do|did|will) you (?:like|want|wanna) to (?:say|write|send|type|tell)\b",
                         re.IGNORECASE)
# Names the model echoes as speaker labels ("Computer: I am your friend."): its own two names
# and the prompt's own example labels.
_LABELS = ("AJ", "Computer", "User", "Assistant")
# Readings: safe to run again by themselves when the boss says the answer was wrong.
_READINGS = {"battery_status", "laptop_status", "current_time", "disk_space", "get_weather", "list_reminders",
             "web_search", "find_files", "list_folder", "read_text_file", "what_do_you_know"}
# "That's not true.", "Are you sure?", "Check again.", "Make a new accurate reading.": live
# (2026-10-18) he answered these by repeating the made-up battery level ("That was the last
# accurate reading"). Now the readings from his last answer are taken again first.
_CHALLENGE = re.compile(
    r"\b(?:that|it|this|you)(?:'?s|'?re| is| are)? (?:not (?:true|right|correct|accurate)|wrong|incorrect|lying|a lie)\b"
    r"|\b(?:check|look|try|read) (?:it |that |again|once more)|\bare you sure\b|\bdouble[- ]?check\b"
    r"|\b(?:new|fresh|another|accurate|proper|real) reading\b|\bre-?check\b|\bthat can'?t be right\b"
    r"|\bthat'?s (?:not|wrong)\b|^\W*wrong\W*$"
    r"|\bis (?:it|that|this|the [\w ]{1,30}?) (?:really )?(?:true|right|correct|accurate)\b|^\W*really\W*$",
    re.IGNORECASE)
_NOTHING_DONE = ("[Check: part of your reply said something was done, but no tool ran, so that part wasn't said. "
                 "If an action was asked for and a tool can do it, call the tool now. If none can, say plainly "
                 "that you can't. Otherwise just carry on naturally. Don't mention this note.]")
_RECHECK_HOW = ("[You doubted yourself, so you just took that reading again; it's above. Answer from this fresh "
                "result only. If your last answer was wrong, admit it in a few words; if it was right, say so "
                "plainly, without arguing.]")
_SPEAK_DIRECTLY_MAX = 160
WAIT_LINE_AFTER_SECS = 0.6
SAME_PLACE_TURNS = 3  # a weather follow-up without a place keeps the last one for this many turns
RELAY_TEMPERATURE = 0.3  # retelling facts: a little variety in the wording, none in the facts
RECENT_ACTIONS_SECS = 10 * 60  # what he did is mentioned to him for this long
# The live status and his recent actions go into a message only when it's about the laptop or
# something on it: sent every turn, he answered "how's it going" and "relax" with what was open
# and how the battery was (the boss: "completely jumping left and right", 2026-10-19).
_ABOUT_LAPTOP = re.compile(
    r"\b(laptop|computer|pc|machine|system|battery|charg\w*|plugged|power\w*|apps?|programs?|windows?|"
    r"open\w*|clos\w*|running|front|minimi[sz]\w*|tabs?|chrome|browser|whatsapp|spotify|discord|explorer|"
    r"settings|youtube|website|volume|sound|mute\w*|bright\w*|screen|display|wi-?fi|internet|online|offline|"
    r"network|cpu|ram|memory|disk|storage|space|drive|status|did|didn'?t|done|work(?:ed|ing)?)\b",
    re.IGNORECASE)
# The clock goes in only when the message is about time: sent every turn, he volunteered "The
# time is 2:40 AM" in answer to "okay, relax". Time questions themselves take the fast path.
_ABOUT_TIME = re.compile(
    r"\b(time|date|day|days|today|tonight|tomorrow|yesterday|week|weekend|month|year|morning|afternoon|"
    r"evening|night|noon|midnight|o'?clock|am|pm|now|later|soon|ago|until|till|since|when|hours?|minutes?|"
    r"clock|schedule|remind\w*|timer|alarm|late|early|monday|tuesday|wednesday|thursday|friday|saturday|"
    r"sunday|birthday|christmas|holiday|deadline|exam|meeting|dinner|lunch|breakfast)\b", re.IGNORECASE)
# "And tomorrow?" right after the weather: the forecast for that day, fetched, not made up (live,
# 2026-10-19, the model invented tomorrow's forecast).
_WEATHER_MORE = re.compile(
    r"^\W*(?:and|so|what about|how about|and what about|and how about)?\W*(?:the weather\W+)?(?:for\W+)?"
    r"(?P<day>tomorrow|today|tonight|this week|the week|next week|the weekend|this weekend|later|now)"
    r"(?:\W+then)?\W*$", re.IGNORECASE)
_WEATHER_DAY = {"tomorrow": "tomorrow", "today": "today", "tonight": "today", "later": "today", "now": "now"}

_WEATHER_HOW = ("[That's the forecast. Answer exactly what the user asked, the way a person would, not like a "
                "report. If it's a yes-or-no question, answer it first (\"No, I don't think so.\") and add only "
                "the detail that backs it up. If they asked about the weather in general, describe it in two or "
                "three natural spoken sentences: how it is now, how the day goes, anything worth knowing. Use "
                "only what the forecast says: no timings, causes or details it doesn't give, and don't repeat what you "
                "already told them.]")


def _humor() -> int | None:
    """The boss's humor setting (tools/personality.py), or None when that module isn't loaded or
    can't say. Only a module that loaded is asked: importing a broken one here could leave half
    its tools registered."""
    module = sys.modules.get("aj.tools.personality")
    try:
        return int(module.current_humor()) if module is not None else None
    except Exception:  # noqa: BLE001 - no dial costs the humor line, never the turn
        return None


class Brain:
    def __init__(self, base: str, settings: Settings, dispatcher=None, escalator=None, memory=None,
                 lines: Lines | None = None, real_sighs: bool = False, state=None, dictation=None):
        """`state`: a core.laptop_state.Monitor (or anything with snapshot() and after_tool()),
        so every turn starts with what the laptop is actually doing. `dictation`: the engine's
        core.dictation.Dictation, shared with the dictation tools and the pipeline."""
        self.base, self.settings = base, settings.llm
        self.dispatcher, self.escalator, self.memory = dispatcher, escalator, memory
        self.state = state
        self.dictation = dictation
        self._laptop = ""  # this turn's "[Laptop now: ...]" summary
        self._show_laptop = False  # this turn is about the laptop, so the summary goes in
        self._show_time = True  # this turn is about time, so the clock goes in
        self._weather_turn: int | None = None  # when the forecast was last fetched
        self._actions: deque[tuple[float, str]] = deque(maxlen=4)  # what he did lately, for himself
        self.lines = lines if lines is not None else Lines(path=None)
        # No added "Hm."/"Right." since v5: the character says little (core/delivery.py).
        self.delivery = Delivery(real_sighs=real_sighs, react=False)
        self.name = getattr(settings, "name", persona.NAME)
        names = dict.fromkeys([self.name, getattr(persona, "ALIAS", ""), *_LABELS])
        self._labels = re.compile(rf"(?:^|(?<=\s))(?:{'|'.join(re.escape(n) for n in names if n)}):\s*",
                                  re.IGNORECASE)
        self.history: list[dict] = []
        self.typed_turn: int | None = None  # this turn was a dictation: how many characters (for the log)
        self.cancel = threading.Event()
        self._tools: list[dict] | None = None
        self._system: str | None = None
        self._system_humor: int | None = None  # the humor setting the cached prompt was built with
        self._place: tuple[str, int] | None = None  # the last place asked about, and on which turn
        self._last_readings: list[tuple[str, object]] = []  # readings his last answer was based on
        self._readings: list[tuple[str, object]] = []       # this turn's
        self._notes: list[str] = []  # what happened since the last turn (an app that didn't open)
        self._budget: int | None = None  # this turn's max_tokens (persona.reply_budget)
        self._cut_off = False  # the model's last reply hit the budget mid-sentence
        self._notes_lock = threading.Lock()
        self._warm = threading.Event()  # the prompt is in the model's cache (set when warm_up ends)
        self._warm.set()

    # ----- prompt building -----

    def _tool_list(self) -> list[dict] | None:
        if self.dispatcher is None:
            return None
        if self._tools is None:
            from aj.tools import registry
            known = {s.name for s in registry.specs()}
            self._tools = registry.openai_tools([n for n in LLM_TOOLS if n in known])
        return self._tools or None

    def _system_prompt(self) -> str:
        """The persona, with the tool rules for the tools he actually has. Built once and again
        only when the boss turns his humor dial (tools/personality.py): it heads every request,
        and the model caches it, so a rebuild costs one re-read of the prompt (~0.7 s)."""
        humor = _humor()
        if self._system is None or humor != self._system_humor:
            names = {t["function"]["name"] for t in self._tool_list() or []}
            takes_humor = humor is not None and "humor" in inspect.signature(persona.system_prompt).parameters
            self._system = (persona.system_prompt(names, self.name, humor=humor) if takes_humor
                            else persona.system_prompt(names, self.name))
            self._system_humor = humor
        return self._system

    def _user_message(self, user_text: str) -> str:
        """What is stored in history: just your words. (Until v4 a length reminder was added
        here; it stopped the model calling its tools.)"""
        return user_text

    def _messages(self, user_text: str, content: str | None = None) -> list[dict]:
        recent = self.history[-2 * self.settings.history_turns:]
        content = content if content is not None else self._user_message(user_text)
        if self.memory is not None:
            known = self.memory.context_for(user_text)
            if known:
                self.delivery.ground(known, fact=True)
                content = (f"[Background you remember about the user. Use it only if this message is about it; "
                           f"never bring it up otherwise: {known}]\n{content}")
        # The real clock, every turn: asked the time without it, the model once answered
        # "Thursday, April 4" on Sunday 4 October. It goes in the user turn, not the system
        # prompt, so the cached persona prefix stays valid.
        with self._notes_lock:
            notes, self._notes = self._notes, []
        if notes:
            told = " ".join(notes)
            self.delivery.ground(told, fact=True)
            content = f"[Since your last reply: {told}]\n{content}"
        if self._show_laptop:
            lately = [f"{datetime.fromtimestamp(t):%I:%M %p} {what}" for t, what in list(self._actions)[-3:]
                      if time.time() - t < RECENT_ACTIONS_SECS]
            if lately:
                content = f"[What you did lately: {' | '.join(lately)}]\n{content}"
            if self._laptop:
                content = f"[Laptop now: {self._laptop}]\n{content}"
        if self._show_time:
            content = f"[Now: {datetime.now().strftime('%A %d %B %Y, %I:%M %p')}]\n{content}"
        return [{"role": "system", "content": self._system_prompt()}, *recent, {"role": "user", "content": content}]

    def warm_up(self) -> None:
        """Load the persona (and tool list) into the model's cache so the first real turn is fast.
        One token is enough: it's the prompt that has to be read, not a reply (a whole reply cost
        a few tenths of a second more at startup)."""
        try:
            for _ in stream_events(self.base, self._messages("hi"), self.settings, tools=self._tool_list(),
                                   max_tokens=1):
                pass
        except (OSError, ValueError):
            pass  # switched off before it finished, or the server hiccuped: the first turn reads the prompt itself
        finally:
            self._warm.set()

    def warm_up_in_background(self) -> None:
        """The warm-up (about 2 s) while the greeting plays, instead of before it (2026-10-21).
        A model request that comes first waits for it; instant commands never do."""
        self._warm.clear()
        threading.Thread(target=self.warm_up, daemon=True, name="warm-up").start()

    # ----- speaking -----

    def cut_off(self, heard: str) -> None:
        """The boss talked over the last reply: it stays in the conversation only as far as they
        heard it, marked as cut off, so the next answer takes both into account."""
        if not self.history or self.history[-1]["role"] != "assistant":
            return
        heard = strip_marks(heard).strip()
        self.history[-1] = {"role": "assistant",
                            "content": f"{heard} [cut off: the user started talking here]" if heard
                            else "[the user started talking before you said anything]"}

    def note(self, event: str) -> None:
        """Something that happened outside a turn, for him to know about on the next one. Safe
        from any thread."""
        with self._notes_lock:
            self._notes.append(event)
        self._actions.append((time.time(), event))

    def _look_at_laptop(self) -> None:
        """The live status for this turn, taken fresh as the turn starts."""
        self._laptop = ""
        if self.state is None:
            return
        try:
            self._laptop = self.state.snapshot(fresh=True).summary()
        except Exception:  # noqa: BLE001 - a failed look costs the note, never the turn
            return
        self.delivery.ground(self._laptop, fact=True)

    def _ran(self, tool: str, args, result) -> None:
        """A tool ran: what it returned is a reading he may repeat, and if it worked, what it
        does may be said to be done."""
        self.delivery.ground(str(result), fact=True)
        outcome = getattr(result, "outcome", "ok")
        if tool == "get_weather":
            self._weather_turn = self.delivery.turn
        if outcome == "ok":
            self.delivery.did(tool)
            if tool in _READINGS:
                self._readings.append((tool, args))
            if self.state is not None:
                self.state.after_tool(tool, str(result))
        if tool not in _READINGS:
            shown = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
            said = str(result).replace("\n", " ")[:90]
            self._actions.append((time.time(), f"{tool} {shown[:60]}: {said}" + ("" if outcome == "ok" else f" ({outcome})")))

    def _speak(self, pieces: Iterable[str], mouth, times, opener: str = "") -> str:
        """Stream the model's words to the voice sentence by sentence; returns what was said."""
        chunker, spoken = SentenceChunker(), []

        def out(chunks: list[str]) -> None:
            nonlocal opener
            for chunk in chunks:
                chunk, opener = opener + chunk, ""
                # "Computer: I am your friend.": the prompt's example labels, echoed (2026-10-21);
                # since the rename to AJ (2026-10-22) any of his names, old or new.
                chunk = self._labels.sub("", chunk).strip()
                if not chunk:
                    continue
                chunk = self.delivery.shape(chunk)
                if chunk is None:
                    continue  # an aside he made too recently
                if not times.first_chunk:
                    times.first_chunk = time.perf_counter()
                spoken.append(chunk)
                if mouth is not None:
                    mouth.say(chunk, times)

        try:
            self._cut_off = False
            for piece in drop_notes(pieces):
                if not times.first_token:
                    times.first_token = time.perf_counter()
                out(chunker.feed(piece))
            tail = chunker.flush()
            if self._cut_off:  # the budget ran out: no half sentences
                tail = [c for c in tail if re.search(r"[.!?…][\"')\]]*$", c)]
            out(tail)
        except Cancelled:
            spoken.append("...")
        return " ".join(spoken)

    def _say_line(self, line: str, mouth, times) -> str:
        """A fixed line (holding line, tool result) goes to the voice whole, not through the
        sentence chunker: whole lines are what the phrase cache pre-renders, so they play at once."""
        line = clean_for_speech(line)  # "10°C" -> "10 degrees Celsius": the voice can't say symbols
        now = time.perf_counter()
        times.first_token = times.first_token or now
        times.first_chunk = times.first_chunk or now
        if mouth is not None and line:
            mouth.say(line, times)
        return line

    # ----- the routes -----

    def reply(self, user_text: str, mouth, times) -> str:
        self.typed_turn = None
        if self.dictation is not None and self.dictation.armed is not None:
            # "Write this down", then typed words (the window's box, --text): those are the message.
            return self.dictate(None, mouth, times, typed=user_text)[1]
        if self.dictation is not None:
            self.dictation.new_turn()
            if dictating.is_cancel(user_text):
                self.dictation.abort()  # "stop": typing still in flight (a timed-out type_text) stops too
        self.cancel.clear()
        times.asked = time.perf_counter()
        self.lines.new_turn()
        self.delivery.start_turn(user_text, explain=persona.wants_explanation(user_text))
        self._budget = persona.reply_budget(user_text)
        self._look_at_laptop()
        self._show_laptop = bool(_ABOUT_LAPTOP.search(user_text) or _CHALLENGE.search(user_text))
        self._show_time = bool(self._show_laptop or _ABOUT_TIME.search(user_text))
        for earlier in [m["content"] for m in self.history if m["role"] == "user"][-2:]:
            self.delivery.ground(earlier)  # "my meeting's at 3" two turns ago still counts
        self._readings = []
        spoken = None
        if self._last_readings and self.dispatcher is not None and _CHALLENGE.search(user_text):
            spoken = self._recheck(user_text, mouth, times)
        if spoken is None:
            spoken = self._instant_command(user_text, mouth, times)
        if spoken is None:
            spoken = self._signature_answer(user_text, mouth, times)
        # Not about the laptop: "how much RAM is in use, is it full?" went to the online check
        # live (2026-10-19); the live status already has the answer.
        if spoken is None and self.escalator is not None and self.escalator.can_help() and looks_hard(user_text) \
                and not self._show_laptop:
            spoken = self._hard_question(user_text, mouth, times)
        if spoken is None:
            spoken = self._conversation(user_text, mouth, times)
        spoken = strip_marks(spoken)  # voice marks ([chuckle], [pause]) are for the ears, not the transcript
        self.history += [{"role": "user", "content": self._user_message(user_text)},
                         {"role": "assistant", "content": spoken}]
        if self.memory is not None and self.typed_turn is None:  # "type <words>": his message, not a fact
            self.memory.after_turn(user_text, spoken)
        if self.dictation is not None:
            self.dictation.saw_reply(spoken)  # a message he wrote out can be typed with "type that"
        self.lines.save()
        self._last_readings = self._readings
        return spoken

    def dictate(self, pieces, mouth, times, typed: str = "", capped: bool = False) -> tuple[str, str]:
        """The boss's words after "write this down" (start_dictation) or after a WhatsApp chat
        opened: typed where he asked, never sent. `pieces`: his speech, as the VAD cut it (None
        when he typed instead). Returns (his words, what was said back).

        Not given to memory: the words are his message to someone else, not something he told
        the assistant, and learning "facts" from them would be wrong."""
        self.cancel.clear()
        times.asked = time.perf_counter()
        self.lines.new_turn()
        self.delivery.start_turn("")
        desk = self.dictation
        request = desk.disarm() if desk is not None else None
        heard, line = typed.strip(), None
        self.typed_turn = len(heard)
        if request is None:
            line = dictating.NOTHING_READY
        elif not pieces and not heard:
            line = dictating.NOTHING_HEARD
        elif dictating.is_cancel(heard or desk.quick_listen(pieces)):
            line = dictating.CANCELLED
        if line is None:
            desk.lend(pieces, typed, command=False, request=request)
            args = {"text": typed, "polish": request.polish}
            result = self._run_tool("type_text", args, mouth, times)
            self._ran("type_text", args, result)
            outcome = getattr(result, "outcome", "ok")
            line = str(result) if outcome in ("ok", "error") else self._failure_line("type_text", outcome)
            if capped and outcome == "ok":
                line = f"{line} {dictating.CAPPED}"
            heard = desk.last_heard or heard
        self.typed_turn = len(heard)
        said = self._say_line(line, mouth, times)
        self.history += [{"role": "user", "content": f"[Dictated, to be typed, not said to you: {heard}]" if heard
                          else "[Dictation: nothing was said]"},
                         {"role": "assistant", "content": said}]
        self.lines.save()
        return heard, said

    def _listen_for_message(self) -> str:
        """A WhatsApp chat just opened: his next words are the message ("it'll say, what would you
        like to say? I would say it. And then ... write it in", the boss, 2026-10-22). Returns the
        question to ask, or "" when there's no dictation to arm."""
        if self.dictation is None:
            return ""
        try:
            self.dictation.arm(app="whatsapp", check=False)  # the chat just opened: its box is in front
        except dictating.NotDone:
            return ""
        return dictating.ASK_MESSAGE

    def _recheck(self, user_text: str, mouth, times) -> str:
        """Told he's wrong: take the readings his last answer came from again, then answer from
        the fresh ones."""
        fresh = []
        for tool, args in self._last_readings:
            result = self._run_tool(tool, args, mouth, times)
            self._ran(tool, args, result)
            fresh.append(f"[{tool} returned just now: {result}]")
        return self._relay(f"{user_text}\n" + "\n".join(fresh) + f"\n{_RECHECK_HOW}", user_text, mouth, times)

    def _instant_command(self, user_text: str, mouth, times) -> str | None:
        if self.dispatcher is None:
            return None
        from aj.tools import intents
        hit = intents.match(user_text) or self._weather_follow_up(user_text)
        if hit is None:
            return None
        tool, args = hit
        if tool == "get_weather":
            args = self._same_place(args)
        if tool == "type_text" and self.dictation is not None:
            # "Type I'll be late": Gemini hears this turn's own audio, and the command words come off
            # its transcript; Parakeet's words (user_text) are the fallback.
            self.dictation.lend(self.dictation.turn_audio, user_text, command=True)
        if tool == "type_text":  # the words are a message: logged as their length only (2026-10-22)
            self.typed_turn = len(str(args.get("text", "")) or user_text)
        result = self._run_tool(tool, args, mouth, times)
        self._ran(tool, args, result)
        if tool == "type_text" and self.dictation is not None and self.dictation.last_heard:
            self.typed_turn = len(self.dictation.last_heard)
        outcome = getattr(result, "outcome", "ok")
        failed = outcome != "ok"
        if outcome not in ("ok", "error"):  # the rest are worded for the model, not to be read out
            return self._say_line(self._failure_line(tool, outcome), mouth, times)
        if tool == "whatsapp_open_chat" and not failed:
            ask = self._listen_for_message()
            return self._say_line(f"{result} {ask}".strip(), mouth, times)
        if tool in _SAY_AS_IS:
            return self._say_line(str(result), mouth, times)
        if tool in _ANSWER_FROM and not failed:
            return self._answer_from(user_text, tool, result, mouth, times)
        if tool in _SUMMARISE or len(result) > _SPEAK_DIRECTLY_MAX:
            return self._relay_tool_result(user_text, tool, result, mouth, times)
        extra = None if failed else self.lines.after_tool(tool)
        return self._say_line(f"{result} {extra}" if extra else str(result), mouth, times)

    def _weather_follow_up(self, user_text: str) -> tuple[str, dict] | None:
        """"And tomorrow?" within two turns of a forecast: the forecast for that day."""
        if self._weather_turn is None or self.delivery.turn - self._weather_turn > 2:
            return None
        found = _WEATHER_MORE.match(user_text)
        if found is None:
            return None
        day = found["day"].lower()
        return "get_weather", {"day": _WEATHER_DAY.get(day, "week")}

    def _failure_line(self, tool: str, outcome: str) -> str:
        if outcome == "declined":
            return self.lines.pick("declined")
        if outcome == "timeout":
            return "That took too long, so I gave up on it."
        return "That didn't work. Something went wrong on my side."

    def _same_place(self, args: dict) -> dict:
        """"Weather in Toronto?" then "does it rain today?" means Toronto, not home, for a few
        turns. A place named again replaces it."""
        if args.get("location"):
            self._place = (args["location"], self.delivery.turn)
        elif self._place and self.delivery.turn - self._place[1] <= SAME_PLACE_TURNS:
            args = {**args, "location": self._place[0]}
            self._place = (self._place[0], self.delivery.turn)  # still talking about it
        return args

    def _run_tool(self, tool: str, args, mouth, times):
        """Run the tool; if it's one that can be slow and it hasn't finished in a moment, say a
        waiting line meanwhile, so the wait doesn't sound like AJ froze."""
        if tool not in _SLOW:
            return self.dispatcher.run(tool, args)
        done, box = threading.Event(), {}

        def work() -> None:
            try:
                box["result"] = self.dispatcher.run(tool, args)
            except BaseException as e:  # handed back to the caller below
                box["error"] = e
            finally:
                done.set()

        threading.Thread(target=work, daemon=True, name=f"tool-{tool}").start()
        if not done.wait(WAIT_LINE_AFTER_SECS):
            self._say_line(self.lines.waiting_line(tool), mouth, times)
            done.wait()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _stream(self, messages: list[dict], calls: list | None = None, temperature: float | None = None,
                raw: list[str] | None = None):
        """The model's words for these messages. Every request carries the same tool list, so the
        cached prompt prefix always fits. With `calls` None, tools are offered but not taken.
        `raw` collects the words as written, before anything is dropped."""
        self._warm.wait(30)  # the warm-up is still reading the prompt: one request at a time
        for kind, value in stream_events(self.base, messages, self.settings, tools=self._tool_list(),
                                         cancel=self.cancel, temperature=temperature,
                                         tool_choice="auto" if calls is not None else "none",
                                         max_tokens=self._budget):
            if kind == "text":
                if raw is not None:
                    raw.append(value)
                yield value
            elif kind == "finish":
                self._cut_off = value == "length"
            elif calls is not None:
                calls.extend(value)

    def _relay(self, content: str, user_text: str, mouth, times) -> str:
        self.delivery.strict = False  # the facts asked for again are the point, not a repeat
        return self._speak(self._stream(self._messages(user_text, content), temperature=RELAY_TEMPERATURE),
                           mouth, times)

    def _answer_from(self, user_text: str, tool: str, result: str, mouth, times) -> str:
        return self._relay(f"{user_text}\n[{tool} returned: {result}]\n{_WEATHER_HOW}", user_text, mouth, times)

    def _relay_tool_result(self, user_text: str, tool: str, result: str, mouth, times) -> str:
        content = (f"{self._user_message(user_text)}\n[{tool} returned: {result}]\n"
                   f"[Tell the user what matters from this, accurately, in your own way of talking.]")
        return self._relay(content, user_text, mouth, times)

    def _signature_answer(self, user_text: str, mouth, times) -> str | None:
        line = self.lines.answer_line(user_text)
        if line is None:
            return None
        self._say_line(line, mouth, times)
        return line  # the transcript shows the line exactly as the boss wrote it, directions and all

    def _hard_question(self, user_text: str, mouth, times) -> str:
        holding = self._say_line(self.lines.pick("cloud" if needs_live_info(user_text) else "thinking"), mouth, times)
        answer = self.escalator.answer(user_text, context=self._plain_history(),
                                       on_slow=lambda: self._say_line(self.lines.pick("still"), mouth, times))
        self.delivery.ground(answer.text, fact=True)
        content = relay_prompt(user_text, answer)
        said = self._relay(content, user_text, mouth, times)
        return f"{holding} {said}".strip()

    def _conversation(self, user_text: str, mouth, times) -> str:
        offer = self.lines.offer(user_text)
        content = self._user_message(user_text)
        if offer is not None:
            content += self.lines.hint(offer)
            self.delivery.allow(offer.text)
        messages = self._messages(user_text, content)
        parts: list[str] = []
        nudged = False
        chat_opened = False  # whatsapp_open_chat worked, and nothing has typed into it since
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            calls: list[dict] | None = [] if self.dispatcher is not None and round_ < MAX_TOOL_ROUNDS else None
            opener = self.delivery.opener() if round_ == 0 else ""
            raw: list[str] = []
            parts.append(self._speak(self._stream(messages, calls, raw=raw), mouth, times, opener=opener))
            if not calls:
                # He said something was done without calling a tool (live: "Settings are now
                # open.", "Message sent."). That was dropped; once per turn he is told nothing
                # happened and gets to call the tool, or say he can't.
                if calls is not None and not nudged and self.delivery.false_actions and not self.delivery.acted:
                    nudged = True
                    messages = messages + [{"role": "assistant", "content": "".join(raw)},
                                           {"role": "user", "content": _NOTHING_DONE}]
                    continue
                break
            tool_calls = [{"id": c["id"] or f"call_{round_}_{i}", "type": "function",
                           "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                          for i, c in enumerate(calls)]
            messages = messages + [{"role": "assistant", "content": parts[-1] or None, "tool_calls": tool_calls}]
            for call in tool_calls:
                name = call["function"]["name"]
                if name == "type_text":  # his words for someone else: the log keeps only their length
                    self.typed_turn = len(user_text)
                result = self.dispatcher.run(name, call["function"]["arguments"])
                self._ran(name, call["function"]["arguments"], result)
                content = str(result)
                if getattr(result, "outcome", "ok") == "ok":
                    if name == "whatsapp_open_chat":
                        chat_opened, content = True, f"{content}\n{_CHAT_OPEN_HOW}"
                    elif name in _TYPES:
                        chat_opened = False
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        said = " ".join(p for p in parts if p).strip()
        if chat_opened:  # the model opened a chat: he's asked what to say, and his answer is typed in
            ask = self._listen_for_message()
            if ask and not _ASKED_WHAT.search(said):
                said = f"{said} {self._say_line(ask, mouth, times)}".strip()
        if offer is not None:
            self.lines.settle(offer, said)
        if not said and self.delivery.invented:  # everything he said was made up: say something true
            pool = "not_done" if self.delivery.false_actions and not self.delivery.acted else "fallback"
            said = self._say_line(self.lines.pick(pool), mouth, times)
        return said

    def _plain_history(self) -> list[dict]:
        """Recent turns, as context for the thinker."""
        return [dict(m) for m in self.history[-6:]]
