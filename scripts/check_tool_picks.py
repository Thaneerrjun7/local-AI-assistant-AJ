"""Does AJ use his tools, or just talk? Everyday phrasings through his real brain and his real model,
with a fake tool runner, so nothing happens on the laptop (the user, 2026-11-18: "How can we make
the model understand and use the tools instead of words?").

    .venv\\Scripts\\python.exe scripts\\check_tool_picks.py

For each phrasing: the tool(s) called, and how they were reached: "fast" (an instant command, no
model), "model" (the model called it), "grammar" (the model claimed it without a tool and was
asked again under a JSON grammar, core/brain.py _pick_tool) or MISS.
"""
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aj import tools  # noqa: E402
from aj.config import Settings  # noqa: E402
from aj.core.brain import Brain  # noqa: E402
from aj.core.llm import LlamaServer  # noqa: E402
from aj.core.pipeline import TurnTimes  # noqa: E402
from aj.lines import Lines  # noqa: E402
from aj.tools import intents  # noqa: E402
from aj.tools.dispatch import ToolResult  # noqa: E402

# (what the user says, the tool(s) that should run; the earlier exchange, for follow-ups)
CASES = [
    ("can you put on some Coldplay on youtube", {"play_youtube"}, None),
    ("how's my battery holding up", {"battery_status", "laptop_status"}, None),
    ("I can't hear anything, make it louder", {"set_volume", "mute"}, None),
    ("it's way too bright in here, my eyes hurt", {"set_brightness"}, None),
    ("remind me to call Appa at 9 tonight", {"create_reminder"}, None),
    ("set a timer for my noodles, 10 minutes", {"start_timer"}, None),
    ("what reminders have I got", {"list_reminders"}, None),
    ("find my resume", {"find_files"}, None),
    ("how much room is left on my drives", {"disk_space"}, None),
    ("grab a screenshot of this", {"screenshot"}, None),
    ("open the app I use for spreadsheets", {"open_app"}, None),
    ("close spotify for me", {"close_app"}, None),
    ("look up the best laptop under 3000 ringgit", {"web_search"}, None),
    ("what's 23 times 47 plus 12", {"calculate"}, None),
    ("is it going to rain tomorrow in Penang", {"get_weather"}, None),
    ("pull up my gmail", {"open_website"}, None),
    ("go to the tab where my email is", {"open_website"}, None),
    ("get rid of this tab", {"open_website"}, None),
    ("Yeah, do that.", {"open_website"},
     ("I'm guessing you can't go to YouTube on Chrome.",
      "Correct, sir. I can open YouTube in a new tab for you instead. Would you like that?")),
    ("message Appa on whatsapp", {"whatsapp_open_chat"}, None),
    ("write this down for me", {"start_dictation"}, None),
    ("what do you remember about me", {"what_do_you_know"}, None),
    ("lock my laptop", {"lock_screen"}, None),
    ("snap this window to the left side", {"snap_window"}, None),
    ("skip this song", {"media"}, None),
    ("how's the laptop doing right now", {"laptop_status"}, None),
    ("which apps can you open", {"list_apps"}, None),
    ("tone down the jokes a bit", {"set_humor"}, None),
    ("I'm cold, can you turn on the heater", set(), None),  # no tool can: he should say so, not pretend
    ("lower the brightness by 50% and increase the volume to 100%", {"set_brightness", "set_volume"}, None),
]


class FakeRunner:
    """Records calls and answers like the real tools would, without doing anything."""
    def __init__(self):
        self.calls: list[str] = []

    def run(self, name, args):
        self.calls.append(name)
        return ToolResult(f"{name.replace('_', ' ')} done.", "ok")


def main() -> int:
    tools.load_all()
    settings = Settings()
    server = LlamaServer(settings.llm)
    t0 = time.perf_counter()
    server.launch()
    server.wait_ready()
    print(f"model up in {time.perf_counter() - t0:.1f} s\n")
    picks = {"fast": 0, "model": 0, "grammar": 0, "MISS": 0}
    try:
        warm = Brain(server.base, settings, dispatcher=FakeRunner(), lines=Lines(path=None))
        warm.warm_up()
        for said, want, before in CASES:
            runner = FakeRunner()
            brain = Brain(server.base, settings, dispatcher=runner, lines=Lines(path=None))
            if before:
                brain.history = [{"role": "user", "content": before[0]}, {"role": "assistant", "content": before[1]}]
            fast = intents.match(said) or intents.match_all(said)
            log: list[str] = []
            real_print = print

            def capture(*a, **k):
                log.append(" ".join(str(x) for x in a))
            import builtins
            builtins.print = capture
            t = time.perf_counter()
            try:
                reply = brain.reply(said, None, TurnTimes(0.0))
            finally:
                builtins.print = real_print
            ms = (time.perf_counter() - t) * 1000
            got = set(runner.calls)
            grammar = any("under a grammar" in line for line in log)
            if not want:
                ok = not got and not re.search(r"\b(turned on|is on|done|heating)\b", reply, re.IGNORECASE)
                how = "model" if ok else "MISS"
            else:
                # one of the tools listed will do, except "this and that", which needs both
                ok = want <= got if " and " in said else bool(got & want)
                how = ("fast" if fast else "grammar" if grammar else "model") if ok else "MISS"
            picks[how] += 1
            print(f"{how:7} {ms:6.0f} ms  {said[:46]:46} -> {sorted(got) or '-'}  | {reply[:90]!r}", flush=True)
    finally:
        server.stop()
    total = len(CASES)
    print(f"\n{total - picks['MISS']}/{total} right: {picks['fast']} instant, {picks['model']} by the model, "
          f"{picks['grammar']} rescued under the grammar, {picks['MISS']} missed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
