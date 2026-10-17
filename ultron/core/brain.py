"""Ultron's brain: decides how each thing you say is handled, then speaks the result.

1. Instant commands ("open spotify", "volume 30") match a tool intent and run with no model
   call at all, so they feel immediate. A slow one (the weather service, a search) gets a short
   waiting line so there's no dead air. A question about facts a tool fetched ("does it rain
   today?") goes through Qwen with the exact question, so it gets a direct answer ("No, I
   don't think so...") instead of the same report read out again.
2. A few things you can say call for one of Ultron's signature lines as the whole answer
   ("sing me something"); see ultron/lines.py.
3. Hard questions (maths, planning, multi-step, live information) get a holding line at once,
   so there's never silence. Meanwhile the thinker works on it, with Gemini as backup.
   Qwen then voices the answer in character with the facts kept intact.
4. Everything else streams from Qwen, which may call laptop tools itself mid-turn. Tool
   results go back to Qwen for one spoken follow-up. When one of the signature lines fits what
   you said, Qwen is offered it to use, in full or in part, or not at all.

Every sentence passes through Delivery on its way to the voice: it adds a spoken reaction now
and then ("Hm.", "Right.") and drops an aside he has already made recently. Memory, when present, adds what Ultron knows about
you to each turn. It goes after the persona prefix and never into it, so the model's prompt
cache stays valid.
"""
import threading
import time
from collections.abc import Iterable
from datetime import datetime

from ultron import persona
from ultron.config import Settings
from ultron.core.chunker import SentenceChunker, clean_for_speech, strip_marks
from ultron.core.delivery import Delivery
from ultron.core.escalate import looks_hard, needs_live_info, relay_prompt
from ultron.core.llm import Cancelled, stream_events, stream_reply
from ultron.lines import Lines

# The model sees a fixed, short tool list: a stable list keeps the prompt cache valid, and the
# fast path already handles the common one-liners. Unregistered names are skipped.
LLM_TOOLS = ["open_app", "close_app", "switch_to_app", "set_volume", "mute", "set_brightness", "media",
             "screenshot", "battery_status", "current_time", "create_reminder", "start_timer",
             "list_reminders", "cancel_reminder", "get_weather", "web_search", "open_website",
             "play_youtube", "find_files", "open_file", "open_folder"]
# Results from these are raw material (search hits, file listings) for Qwen to summarise,
# not sentences to read out word for word.
_SUMMARISE = {"web_search", "find_files", "list_folder", "read_text_file", "list_reminders"}
# Facts the boss asks questions about: Qwen answers the question from them.
_ANSWER_FROM = {"get_weather"}
# Tools that can take a second or two: a waiting line plays if they haven't finished by then.
_SLOW = {"get_weather", "web_search", "find_files"}
_SPEAK_DIRECTLY_MAX = 160
WAIT_LINE_AFTER_SECS = 0.6
SAME_PLACE_TURNS = 3  # a weather follow-up without a place keeps the last one for this many turns
RELAY_TEMPERATURE = 0.3  # retelling facts: a little variety in the wording, none in the facts

_WEATHER_HOW = ("[That's the forecast. Answer exactly what the boss asked, the way a person would, not like a "
                "report. If it's a yes-or-no question, answer it first (\"No, I don't think so.\") and add only "
                "the detail that backs it up. If they asked about the weather in general, describe it in two or "
                "three natural spoken sentences: how it is now, how the day goes, anything worth knowing. Use "
                "only what the forecast says: no timings, causes or details it doesn't give, and don't repeat what you "
                "already told them.]")


class Brain:
    def __init__(self, base: str, settings: Settings, dispatcher=None, escalator=None, memory=None,
                 lines: Lines | None = None, real_sighs: bool = False):
        self.base, self.settings = base, settings.llm
        self.dispatcher, self.escalator, self.memory = dispatcher, escalator, memory
        self.lines = lines if lines is not None else Lines(path=None)
        self.delivery = Delivery(real_sighs=real_sighs)
        self.history: list[dict] = []
        self.cancel = threading.Event()
        self._tools: list[dict] | None = None
        self._place: tuple[str, int] | None = None  # the last place asked about, and on which turn

    # ----- prompt building -----

    def _tool_list(self) -> list[dict] | None:
        if self.dispatcher is None:
            return None
        if self._tools is None:
            from ultron.tools import registry
            known = {s.name for s in registry.specs()}
            self._tools = registry.openai_tools([n for n in LLM_TOOLS if n in known])
        return self._tools or None

    def _user_message(self, user_text: str) -> str:
        """What is stored in history: your words plus the length rule (cache-friendly)."""
        return user_text + persona.reminder_for(user_text)

    def _messages(self, user_text: str, content: str | None = None) -> list[dict]:
        recent = self.history[-2 * self.settings.history_turns:]
        content = content if content is not None else self._user_message(user_text)
        if self.memory is not None:
            known = self.memory.context_for(user_text)
            if known:
                self.delivery.ground(known)
                content = f"[What you remember about the boss: {known}]\n{content}"
        # The real clock, every turn: asked the time without it, the model once answered
        # "Thursday, April 4" on Sunday 4 October. It goes in the user turn, not the system
        # prompt, so the cached persona prefix stays valid.
        content = f"[Now: {datetime.now().strftime('%A %d %B %Y, %I:%M %p')}]\n{content}"
        return [{"role": "system", "content": persona.SYSTEM}, *recent, {"role": "user", "content": content}]

    def warm_up(self) -> None:
        """Load the persona (and tool list) into the model's cache so the first real turn is fast."""
        for _ in stream_events(self.base, self._messages("hi"), self.settings, tools=self._tool_list()):
            pass

    # ----- speaking -----

    def _speak(self, pieces: Iterable[str], mouth, times, opener: str = "") -> str:
        """Stream the model's words to the voice sentence by sentence; returns what was said."""
        chunker, spoken = SentenceChunker(), []

        def out(chunks: list[str]) -> None:
            nonlocal opener
            for chunk in chunks:
                chunk, opener = opener + chunk, ""
                chunk = self.delivery.shape(chunk)
                if chunk is None:
                    continue  # an aside he made too recently
                if not times.first_chunk:
                    times.first_chunk = time.perf_counter()
                spoken.append(chunk)
                if mouth is not None:
                    mouth.say(chunk, times)

        try:
            for piece in pieces:
                if not times.first_token:
                    times.first_token = time.perf_counter()
                out(chunker.feed(piece))
            out(chunker.flush())
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
        self.cancel.clear()
        times.asked = time.perf_counter()
        self.lines.new_turn()
        self.delivery.start_turn(user_text, explain=persona.reminder_for(user_text) == persona.EXPLAIN_REMINDER)
        for earlier in [m["content"] for m in self.history if m["role"] == "user"][-2:]:
            self.delivery.ground(earlier)  # "my meeting's at 3" two turns ago still counts
        spoken = self._instant_command(user_text, mouth, times)
        if spoken is None:
            spoken = self._signature_answer(user_text, mouth, times)
        if spoken is None and self.escalator is not None and self.escalator.can_help() and looks_hard(user_text):
            spoken = self._hard_question(user_text, mouth, times)
        if spoken is None:
            spoken = self._conversation(user_text, mouth, times)
        spoken = strip_marks(spoken)  # voice marks ([chuckle], [pause]) are for the ears, not the transcript
        self.history += [{"role": "user", "content": self._user_message(user_text)},
                         {"role": "assistant", "content": spoken}]
        if self.memory is not None:
            self.memory.after_turn(user_text, spoken)
        self.lines.save()
        return spoken

    def _instant_command(self, user_text: str, mouth, times) -> str | None:
        if self.dispatcher is None:
            return None
        from ultron.tools import intents
        hit = intents.match(user_text)
        if hit is None:
            return None
        tool, args = hit
        if tool == "get_weather":
            args = self._same_place(args)
        result = self._run_tool(tool, args, mouth, times)
        self.delivery.ground(str(result))
        failed = getattr(result, "outcome", "ok") != "ok"
        self.delivery.acted = not failed
        if tool in _ANSWER_FROM and not failed:
            return self._answer_from(user_text, tool, result, mouth, times)
        if tool in _SUMMARISE or len(result) > _SPEAK_DIRECTLY_MAX:
            return self._relay_tool_result(user_text, tool, result, mouth, times)
        extra = None if failed else self.lines.after_tool(tool)
        return self._say_line(f"{result} {extra}" if extra else str(result), mouth, times)

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
        waiting line meanwhile, so the wait doesn't sound like Ultron froze."""
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

    def _relay(self, content: str, user_text: str, mouth, times) -> str:
        self.delivery.strict = False  # the facts asked for again are the point, not a repeat
        return self._speak(stream_reply(self.base, self._messages(user_text, content), self.settings, self.cancel,
                                        temperature=RELAY_TEMPERATURE), mouth, times)

    def _answer_from(self, user_text: str, tool: str, result: str, mouth, times) -> str:
        return self._relay(f"{user_text}\n[{tool} returned: {result}]\n{_WEATHER_HOW}", user_text, mouth, times)

    def _relay_tool_result(self, user_text: str, tool: str, result: str, mouth, times) -> str:
        content = (f"{self._user_message(user_text)}\n[{tool} returned: {result}]\n"
                   f"[Tell the boss what matters from this, accurately, in your own voice.]")
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
        self.delivery.ground(answer.text)
        content = relay_prompt(user_text, answer) + persona.reminder_for(user_text)
        said = self._relay(content, user_text, mouth, times)
        return f"{holding} {said}".strip()

    def _conversation(self, user_text: str, mouth, times) -> str:
        offer = self.lines.offer(user_text)
        content = self._user_message(user_text)
        if offer is not None:
            content += self.lines.hint(offer)
            self.delivery.allow(offer.text)
        messages = self._messages(user_text, content)
        calls: list[dict] = []

        def text_only():
            for kind, value in stream_events(self.base, messages, self.settings, tools=self._tool_list(),
                                             cancel=self.cancel):
                if kind == "text":
                    yield value
                else:
                    calls.extend(value)

        said = self._speak(text_only(), mouth, times, opener=self.delivery.opener())
        if calls and self.dispatcher is not None:
            tool_calls = [{"id": c["id"] or f"call_{i}", "type": "function",
                           "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                          for i, c in enumerate(calls)]
            follow_up = messages + [{"role": "assistant", "content": said or None, "tool_calls": tool_calls}]
            for call in tool_calls:
                result = self.dispatcher.run(call["function"]["name"], call["function"]["arguments"])
                self.delivery.ground(str(result))
                self.delivery.acted |= getattr(result, "outcome", "ok") == "ok"  # "Done, it's set." is true now
                follow_up.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})
            more = self._speak(stream_reply(self.base, follow_up, self.settings, self.cancel), mouth, times)
            said = f"{said} {more}".strip()
        if offer is not None:
            self.lines.settle(offer, said)
        if not said and self.delivery.invented:  # everything he said was made up: say something true
            said = self._say_line(self.lines.pick("fallback"), mouth, times)
        return said

    def _plain_history(self) -> list[dict]:
        """Recent turns without the per-turn reminders, as context for the thinker."""
        recent = self.history[-6:]
        return [{"role": m["role"], "content": m["content"].split("\n\n[Stay Ultron")[0]} for m in recent]
