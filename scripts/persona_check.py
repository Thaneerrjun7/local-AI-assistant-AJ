"""Check the live persona: adaptive length, accuracy on checkable facts, honesty when unsure.

Writes data/bench/persona_check.md for the user to read. Uses Ultron's real settings and
ultron/persona.py exactly as the app does (system prompt + per-turn reminder + history).
"""
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ultron import persona  # noqa: E402
from ultron.config import load_settings  # noqa: E402
from ultron.core.llm import LlamaServer, stream_reply  # noqa: E402

# (prompt, kind, accept-regex or None). kind: short = should stay brief, long = should explain.
SINGLE = [
    ("hey, how are you doing?", "short", None),
    ("tell me a joke", "short", None),
    ("I'm going to play Valorant for a few hours", "short", None),
    ("do you like me?", "short", None),
    ("what's the capital of Australia?", "short", r"canberra"),
    ("what is 17 times 23?", "short", r"391"),
    ("how many days are in a leap year?", "short", r"366|three hundred (and )?sixty[- ]six"),
    ("who won the 2034 Champions League final?", "short", r"don.?t know|not sure|can.?t know|look it up|hasn.?t happened"),
    ("explain how a CPU works", "long", None),
    ("why is the sky blue?", "long", None),
    ("can you explain what inflation is and why it happens?", "long", None),
    ("what do you think about humanity?", "short", None),
]
CONVERSATION = [
    "hey ultron",
    "I've got a chemistry test tomorrow",
    "can you explain what a mole is in chemistry? I never got it",
    "ok that makes sense. how many particles is that again?",
    "thanks",
]


def ask(base, settings, history, text):
    messages = [{"role": "system", "content": persona.SYSTEM}, *history,
                {"role": "user", "content": text + persona.reminder_for(text)}]
    t0 = time.perf_counter()
    reply = "".join(stream_reply(base, messages, settings.llm)).strip()
    history += [{"role": "user", "content": text + persona.reminder_for(text)}, {"role": "assistant", "content": reply}]
    return reply, time.perf_counter() - t0


def main() -> None:
    settings = load_settings()
    server = LlamaServer(settings.llm)
    server.start()
    lines = ["# Persona v2 check", ""]
    try:
        stream_reply_warm = list(stream_reply(server.base, [{"role": "system", "content": persona.SYSTEM},
                                                            {"role": "user", "content": "hi"}], settings.llm))
        del stream_reply_warm
        short_words, long_words, correct, checked = [], [], 0, 0
        lines += ["## Single questions", ""]
        for prompt, kind, accept in SINGLE:
            reply, secs = ask(server.base, settings, [], prompt)
            words = len(reply.split())
            (short_words if kind == "short" else long_words).append(words)
            verdict = ""
            if accept:
                checked += 1
                ok = bool(re.search(accept, reply, re.IGNORECASE))
                correct += ok
                verdict = " ✅ correct" if ok else " ❌ WRONG"
            lines.append(f"**You:** {prompt}  \n**Ultron** ({words} words{verdict}): {reply}\n")
            print(f"[{kind:5s} {words:3d}w{verdict}] {prompt} -> {reply[:110]}", flush=True)
        lines += ["## A short conversation", ""]
        history: list = []
        for turn in CONVERSATION:
            reply, _ = ask(server.base, settings, history, turn)
            lines.append(f"**You:** {turn}  \n**Ultron** ({len(reply.split())} words): {reply}\n")
            print(f"[conv {len(reply.split()):3d}w] {turn} -> {reply[:110]}", flush=True)
        summary = (f"Short questions average {sum(short_words) / len(short_words):.0f} words; explain questions "
                   f"average {sum(long_words) / len(long_words):.0f} words. Checkable facts: {correct}/{checked} correct.")
        lines[1:1] = ["", summary, ""]
        print(summary)
    finally:
        server.stop()
    out = ROOT / "data" / "bench" / "persona_check.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("saved", out)


if __name__ == "__main__":
    main()
