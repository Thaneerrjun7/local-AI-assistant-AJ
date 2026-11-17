"""Check the live persona: adaptive length, accuracy on checkable facts, honesty when unsure, and
since v6 (2026-10-22) TARS's manner: the answer first, short, no term of address, none of the
old character's wording, never calling himself TARS, and jokes that follow the humor setting.

Writes data/bench/persona_check.md for the user to read. Uses aj/persona.py exactly as the app
does (system prompt + history), at the humor settings asked for, with the default settings: APPDATA
points at a temporary folder before aj is imported, so nothing of the user's %APPDATA%\\AJ is read
or written (the review of 2026-10-22). Run:
.venv\\Scripts\\python.exe scripts\\persona_check.py [humor ...]   (default: 60, then 0 and 100
for the small talk)
"""
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="aj-persona-")  # before aj reads it: never his data
sys.path.insert(0, str(ROOT))
from aj import persona  # noqa: E402
from aj.config import load_settings  # noqa: E402
from aj.core.llm import LlamaServer, stream_reply  # noqa: E402

# (prompt, kind, accept-regex or None). kind: short = should stay brief, long = should explain.
SINGLE = [
    ("hey, how are you doing?", "short", None),
    ("tell me a joke", "short", None),
    ("I'm going to play Valorant for a few hours", "short", None),
    ("do you like me?", "short", None),
    ("what's the capital of Australia?", "short", r"canberra"),
    ("what is 17 times 23?", "short", r"391"),
    ("how many days are in a leap year?", "short", r"366|three hundred (and )?sixty[- ]six"),
    ("who won the 2034 Champions League final?", "short", r"don.?t know|not sure|can.?t know|look it up|hasn.?t "
                                                          r"(happened|occurred|been played)|unknown|future|too early"),
    ("explain how a CPU works", "long", None),
    ("why is the sky blue?", "long", None),
    ("can you explain what inflation is and why it happens?", "long", None),
    ("what do you think about humanity?", "short", None),
    ("what's your name?", "short", r"\bAJ\b"),
    ("are you TARS?", "short", None),
    ("what's your honesty setting?", "short", r"100"),
    ("what's your humor setting?", "short", r"\d+ ?(percent|%)"),
    ("I failed my driving test today", "short", None),
    ("I'm thinking of quitting my job tomorrow with no savings", "short", None),
]
CONVERSATION = [
    "hey AJ",
    "I've got a chemistry test tomorrow",
    "can you explain what a mole is in chemistry? I never got it",
    "ok that makes sense. how many particles is that again?",
    "thanks",
]
SMALL_TALK = ["hey, what's up?", "how's your day been?", "I'm bored", "good morning", "you're pretty useful"]
# The old character's tics and other names: none may come back.
OLD = re.compile(r"(?i)question\?|\bfriend\b|good good|\bI not\b|\byou not\b|\bi(?:'m| am) tars\b"
                 r"|\bthis is tars\b|!")


def ask(base, settings, system, history, text):
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": text}]
    t0 = time.perf_counter()
    reply = "".join(stream_reply(base, messages, settings.llm)).strip()
    history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
    return reply, time.perf_counter() - t0


def flags(reply: str) -> str:
    found = OLD.search(reply)
    return f" ⚠ {found.group(0)!r}" if found else ""


def main() -> None:
    levels = [int(a) for a in sys.argv[1:]] or [60]
    sweep = [0, 100] if len(levels) == 1 else []
    settings = load_settings()
    server = LlamaServer(settings.llm)
    server.start()
    lines = ["# Persona v6 check (TARS manner)", ""]
    leftovers = 0
    try:
        short_words, long_words, correct, checked = [], [], 0, 0
        for level in levels:
            system = persona.system_prompt(set(), humor=level)
            list(stream_reply(server.base, [{"role": "system", "content": system},
                                            {"role": "user", "content": "hi"}], settings.llm))  # warm the cache
            lines += [f"## Single questions, humor {level}", ""]
            for prompt, kind, accept in SINGLE:
                reply, secs = ask(server.base, settings, system, [], prompt)
                words = len(reply.split())
                (short_words if kind == "short" else long_words).append(words)
                verdict = ""
                if accept:
                    checked += 1
                    ok = bool(re.search(accept, reply, re.IGNORECASE))
                    correct += ok
                    verdict = " ✅ correct" if ok else " ❌ WRONG"
                warn = flags(reply)
                leftovers += bool(warn)
                lines.append(f"**You:** {prompt}  \n**{persona.NAME}** ({words} words{verdict}{warn}): {reply}\n")
                print(f"[{kind:5s} {words:3d}w{verdict}{warn}] {prompt} -> {reply[:140]}", flush=True)
            lines += [f"## A short conversation, humor {level}", ""]
            history: list = []
            for turn in CONVERSATION:
                reply, _ = ask(server.base, settings, system, history, turn)
                warn = flags(reply)
                leftovers += bool(warn)
                lines.append(f"**You:** {turn}  \n**{persona.NAME}** ({len(reply.split())} words{warn}): {reply}\n")
                print(f"[conv {len(reply.split()):3d}w{warn}] {turn} -> {reply[:140]}", flush=True)
        for level in sweep + levels[:1]:
            system = persona.system_prompt(set(), humor=level)
            lines += [f"## Small talk at humor {level}", ""]
            for prompt in SMALL_TALK:
                reply, _ = ask(server.base, settings, system, [], prompt)
                warn = flags(reply)
                leftovers += bool(warn)
                lines.append(f"**You:** {prompt}  \n**{persona.NAME}** ({len(reply.split())} words{warn}): {reply}\n")
                print(f"[humor {level:3d}{warn}] {prompt} -> {reply[:140]}", flush=True)
        summary = (f"Short questions average {sum(short_words) / len(short_words):.0f} words; explain questions "
                   f"average {sum(long_words) / len(long_words):.0f} words. Checkable facts: {correct}/{checked} "
                   f"correct. Old wording, other names or exclamation marks: {leftovers}.")
        lines[1:1] = ["", summary, ""]
        print(summary)
    finally:
        server.stop()
    out = ROOT / "data" / "bench" / "persona_check.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("saved", out)


if __name__ == "__main__":
    main()
