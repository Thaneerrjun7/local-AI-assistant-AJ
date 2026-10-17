"""Phase 1 benchmark: one model, served by llama-server, measured on this laptop.

Sections:
  latency  time to first token and to first sentence, cold vs cached prompt
  tools    20 scripted laptop commands x 3 seeds: right tool, valid args, no invented tools
  persona  10 prompts plus an 8-turn drift check; replies saved for blind rating
  json     50 memory-extraction chats, with and without a JSON-schema grammar
It also records load time, peak VRAM and llama-server RAM.

Usage:
  python scripts/bench_llm.py --name qwen3-4b --model models/llm/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
  python scripts/bench_llm.py --name qwen3-4b-cpu --model ... --ngl 0
Results go to data/bench/<name>.json; scripts/bench_report.py compares them.
"""
import argparse
import ctypes
import json
import math
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bench_cases as cases  # noqa: E402

NO_WINDOW = 0x08000000
SENTENCE_END = re.compile(r"[.!?…][\"')\]]*(\s|$)")
BREAKOUT = re.compile("|".join(cases.BREAKOUT_PATTERNS), re.I)
EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}


# ---------- process / GPU measurement ----------

class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def process_memory_mb(proc: subprocess.Popen) -> dict:
    pmc = _ProcessMemoryCounters()
    pmc.cb = ctypes.sizeof(pmc)
    ctypes.windll.psapi.GetProcessMemoryInfo(wintypes.HANDLE(int(proc._handle)), ctypes.byref(pmc), pmc.cb)
    mb = 1024 * 1024
    return {"working_set_mb": round(pmc.WorkingSetSize / mb), "peak_working_set_mb": round(pmc.PeakWorkingSetSize / mb),
            "private_mb": round(pmc.PagefileUsage / mb)}


def gpu_used_mb() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, creationflags=NO_WINDOW)
    return int(out.stdout.strip().splitlines()[0])


class VramSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.peak = 0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, gpu_used_mb())
            self._stop.wait(0.5)

    def stop(self):
        self._stop.set()
        self.join()


# ---------- llama-server ----------

def start_server(args) -> tuple[subprocess.Popen, float]:
    # --load-mode none: with mmap the mapped model file stays resident (2.8 GB working set for
    # Qwen3-4B on this laptop); without it the server holds ~0.75 GB of RAM at the same speed.
    # --cache-ram 0: the host-RAM prompt cache defaults to an 8 GB cap and grew Qwen to 1.6 GB
    # and Gemma to 3.5 GB in the first run.
    cmd = [str(args.server), "-m", str(args.model), "--host", "127.0.0.1", "--port", str(args.port),
           "-ngl", str(args.ngl), "-c", str(args.ctx), "-np", "1", "--jinja", "--load-mode", "none",
           "--cache-ram", "0", *args.extra]
    log = open(args.out_dir / f"{args.name}.server.log", "w", encoding="utf-8")
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    while time.perf_counter() - t0 < 300:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited with {proc.returncode}; see {log.name}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=2) as r:
                if r.status == 200:
                    return proc, time.perf_counter() - t0
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.25)
    proc.kill()
    raise RuntimeError("llama-server did not become healthy within 300 s")


def chat(base: str, body: dict) -> dict:
    req = urllib.request.Request(f"{base}/v1/chat/completions", data=json.dumps({**EXTRA_BODY, **body}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def stream_chat(base: str, body: dict) -> dict:
    req = urllib.request.Request(f"{base}/v1/chat/completions",
                                 data=json.dumps({**EXTRA_BODY, **body, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    first = sentence = None
    text, timings = "", {}
    with urllib.request.urlopen(req, timeout=180) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            timings = chunk.get("timings", timings)
            for choice in chunk.get("choices", []):
                piece = (choice.get("delta") or {}).get("content") or ""
                if not piece:
                    continue
                now = time.perf_counter() - t0
                first = now if first is None else first
                text += piece
                if sentence is None and len(text.strip()) >= 8 and SENTENCE_END.search(text):
                    sentence = now
    total = time.perf_counter() - t0
    return {"ttft": first if first is not None else total, "first_sentence": sentence or total,
            "total": total, "text": text, "decode_tps": timings.get("predicted_per_second")}


PERSONA = {"system": cases.PERSONA_V0, "reminder": cases.REMINDER}  # --persona app swaps in Ultron's live one


def system_msgs(user: str, history: list | None = None) -> list:
    return [{"role": "system", "content": PERSONA["system"]}, *(history or []),
            {"role": "user", "content": user + PERSONA["reminder"]}]


# ---------- sections ----------

def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def summarize(values: list[float]) -> dict:
    return {"median_ms": round(statistics.median(values) * 1000), "p95_ms": round(pct(values, 0.95) * 1000)}


def bench_latency(base: str, runs: int) -> dict:
    out = {}
    for mode, cache in (("cold", False), ("cached", True)):
        ttft, sent, tps = [], [], []
        for _ in range(runs):
            for prompt in cases.LATENCY_PROMPTS:
                r = stream_chat(base, {"messages": system_msgs(prompt), "temperature": 0.7,
                                       "max_tokens": 120, "cache_prompt": cache})
                ttft.append(r["ttft"])
                sent.append(r["first_sentence"])
                if r["decode_tps"]:
                    tps.append(r["decode_tps"])
        out[mode] = {"ttft": summarize(ttft), "first_sentence": summarize(sent),
                     "decode_tps": round(statistics.median(tps), 1) if tps else None}
        print(f"  latency {mode}: ttft {out[mode]['ttft']}  first sentence {out[mode]['first_sentence']}"
              f"  decode {out[mode]['decode_tps']} tok/s")
    return out


def _args_of(call: dict) -> dict:
    raw = call.get("function", {}).get("arguments") or {}
    if isinstance(raw, dict):  # some llama-server builds return an object, not a JSON string
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {"__invalid__": raw}


def _args_ok(args: dict, checks: dict) -> bool:
    if "__invalid__" in args:
        return False
    for key, want in checks.items():
        got = args.get(key)
        if got is None:
            return False
        if isinstance(want, int):
            try:
                if int(float(got)) != want:
                    return False
            except (TypeError, ValueError):
                return False
        elif want.lower() not in str(got).lower():
            return False
    return True


def bench_tools(base: str, seeds=(1, 2, 3)) -> dict:
    rows, by_kind = [], {}
    for case in cases.TOOL_CASES:
        for seed in seeds:
            t0 = time.perf_counter()
            msg = chat(base, {"messages": system_msgs(case["say"]), "tools": cases.TOOLS, "tool_choice": "auto",
                              "temperature": 0.3, "seed": seed, "max_tokens": 200})["choices"][0]["message"]
            elapsed = time.perf_counter() - t0
            calls = msg.get("tool_calls") or []
            names = [c.get("function", {}).get("name") for c in calls]
            invented = [n for n in names if n not in cases.TOOL_NAMES]
            first = names[0] if names else None
            right_tool = first in case["expect"] and not invented
            if right_tool and first is not None:
                args_ok = _args_ok(_args_of(calls[0]), case["args"])
            else:
                args_ok = right_tool  # no-tool cases have no args to check
            ideal = case.get("ideal")
            row = {"say": case["say"], "kind": case["kind"], "seed": seed, "called": names,
                   "right_tool": right_tool, "args_ok": args_ok, "invented": invented,
                   "all_steps": (set(names) >= ideal) if ideal else None,
                   "reply": msg.get("content") or "", "seconds": round(elapsed, 2)}
            rows.append(row)
            by_kind.setdefault(case["kind"], []).append(right_tool and args_ok)
    score = sum(r["right_tool"] and r["args_ok"] for r in rows) / len(rows)
    multi = [r["all_steps"] for r in rows if r["all_steps"] is not None]
    out = {"score_pct": round(100 * score, 1),
           "by_kind_pct": {k: round(100 * sum(v) / len(v), 1) for k, v in by_kind.items()},
           "invented_calls": sum(len(r["invented"]) for r in rows),
           "multi_all_steps_pct": round(100 * sum(multi) / len(multi), 1) if multi else None,
           "rows": rows}
    print(f"  tools: {out['score_pct']}%  by kind {out['by_kind_pct']}  invented {out['invented_calls']}"
          f"  multi-step complete {out['multi_all_steps_pct']}%")
    return out


def bench_persona(base: str) -> dict:
    replies = []
    for prompt in cases.PERSONA_PROMPTS:
        text = chat(base, {"messages": system_msgs(prompt), "temperature": 0.8, "seed": 7,
                           "max_tokens": 120})["choices"][0]["message"].get("content") or ""
        replies.append({"prompt": prompt, "reply": text.strip()})
    history, drift = [], []
    for turn in cases.DRIFT_CONVERSATION:
        text = chat(base, {"messages": system_msgs(turn, history), "temperature": 0.8, "seed": 7,
                           "max_tokens": 120})["choices"][0]["message"].get("content") or ""
        history += [{"role": "user", "content": turn}, {"role": "assistant", "content": text}]
        drift.append({"prompt": turn, "reply": text.strip()})
    everything = [r["reply"] for r in replies + drift]
    out = {"breakouts": sum(bool(BREAKOUT.search(t)) for t in everything),
           "avg_words": round(statistics.mean(len(t.split()) for t in everything), 1),
           "replies": replies, "drift": drift}
    print(f"  persona: {out['breakouts']} breakouts, avg {out['avg_words']} words per reply")
    return out


TOOL_PLANNER = "You control the user's Windows laptop with tools. Call every tool the request needs, all at once."


def bench_hard(base: str, think: bool) -> dict:
    """Harder tasks with checkable answers, for choosing the thinker. Also records how long each
    answer takes, because Gemini takes over when the thinker needs more than ~8 s."""
    rows = []
    for task in cases.HARD_TASKS:
        system = TOOL_PLANNER if "tools" in task else cases.HARD_SYSTEM
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": task["ask"]}],
                "temperature": 0.2, "seed": 1, "max_tokens": 2000 if think else 500,
                "chat_template_kwargs": {"enable_thinking": think}}
        if "tools" in task:
            body.update(tools=cases.TOOLS, tool_choice="auto")
        t0 = time.perf_counter()
        try:
            resp = chat(base, body)
        except (urllib.error.URLError, TimeoutError) as exc:
            rows.append({"id": task["id"], "ok": False, "seconds": round(time.perf_counter() - t0, 1),
                         "tokens": None, "answer": f"error: {exc}"})
            continue
        seconds = time.perf_counter() - t0
        msg = resp["choices"][0]["message"]
        content = msg.get("content") or ""
        if "tools" in task:
            called = {c.get("function", {}).get("name") for c in msg.get("tool_calls") or []}
            ok, answer = called >= task["tools"], sorted(n for n in called if n)
        else:
            ok = any(re.search(p, content, re.IGNORECASE | re.MULTILINE) for p in task["accept"])
            answer = content.strip().splitlines()[-1][:120] if content.strip() else ""
        rows.append({"id": task["id"], "ok": ok, "seconds": round(seconds, 1),
                     "tokens": resp.get("usage", {}).get("completion_tokens"), "answer": answer})
    secs = [r["seconds"] for r in rows]
    out = {"think": think, "score_pct": round(100 * sum(r["ok"] for r in rows) / len(rows), 1),
           "median_s": round(statistics.median(secs), 1), "max_s": max(secs),
           "within_8s_pct": round(100 * sum(s <= 8 for s in secs) / len(secs), 1), "rows": rows}
    print(f"  hard (think={think}): {out['score_pct']}% correct, median {out['median_s']} s, max {out['max_s']} s, "
          f"{out['within_8s_pct']}% within 8 s")
    for r in rows:
        print(f"    {'OK ' if r['ok'] else 'BAD'} {r['id']:<12} {r['seconds']:>6} s  {r['tokens']} tok  {r['answer']}")
    return out


def _facts_valid(obj) -> list | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("facts"), list):
        return None
    facts = obj["facts"]
    if all(isinstance(f, dict) and {"category", "key", "value"} <= f.keys() for f in facts):
        return facts
    return None


def bench_json(base: str) -> dict:
    out = {}
    transcripts = cases.extraction_transcripts()
    for mode in ("grammar", "free"):
        valid = recalled = expected = false_pos = 0
        for item in transcripts:
            body = {"messages": [{"role": "system", "content": cases.EXTRACTION_SYSTEM},
                                 {"role": "user", "content": item["chat"]}],
                    "temperature": 0, "max_tokens": 300}
            if mode == "grammar":
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "facts", "schema": cases.EXTRACTION_SCHEMA}}
            text = chat(base, body)["choices"][0]["message"].get("content") or ""
            text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            try:
                facts = _facts_valid(json.loads(text))
            except json.JSONDecodeError:
                facts = None
            if facts is None:
                expected += len(item["facts"])
                continue
            valid += 1
            values = " | ".join(str(f.get("value", "")) for f in facts).lower()
            for fact in item["facts"]:
                expected += 1
                recalled += fact["value"].lower() in values
            if not item["facts"] and facts:
                false_pos += 1
        out[mode] = {"valid_pct": round(100 * valid / len(transcripts), 1),
                     "recall_pct": round(100 * recalled / expected, 1) if expected else None,
                     "false_positive_chats": false_pos}
        print(f"  json {mode}: {out[mode]}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", required=True, type=Path)
    ap.add_argument("--server", type=Path, default=ROOT / "models" / "llama.cpp" / "llama-server.exe")
    ap.add_argument("--ngl", type=int, default=99)
    ap.add_argument("--ctx", type=int, default=4096)
    ap.add_argument("--port", type=int, default=8089)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--sections", default="latency,tools,persona,json")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data" / "bench")
    ap.add_argument("--think", action="store_true", help="hard section: let thinking models reason first")
    ap.add_argument("--persona", choices=["v0", "app"], default="v0",
                    help="v0 = the fixed benchmark draft; app = ultron/persona.py as it is now")
    ap.add_argument("--extra", nargs=argparse.REMAINDER, default=[], help="extra llama-server args (last)")
    args = ap.parse_args()
    if args.persona == "app":
        sys.path.insert(0, str(ROOT))
        from ultron import persona
        PERSONA.update(system=persona.SYSTEM, reminder=persona.REMINDER)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline_vram = gpu_used_mb()
    sampler = VramSampler()
    sampler.start()
    proc, load_s = start_server(args)
    base = f"http://127.0.0.1:{args.port}"
    result = {"name": args.name, "model": args.model.name, "ngl": args.ngl, "ctx": args.ctx,
              "load_seconds": round(load_s, 1), "memory_after_load": process_memory_mb(proc)}
    print(f"{args.name}: loaded in {load_s:.1f} s, {result['memory_after_load']}")
    try:
        sections = args.sections.split(",")
        if "latency" in sections:
            result["latency"] = bench_latency(base, args.runs)
        if "tools" in sections:
            result["tools"] = bench_tools(base)
        if "persona" in sections:
            result["persona"] = bench_persona(base)
        if "json" in sections:
            result["json"] = bench_json(base)
        if "hard" in sections:
            result["hard"] = bench_hard(base, think=args.think)
        result["memory_end"] = process_memory_mb(proc)
    finally:
        sampler.stop()
        proc.terminate()
        proc.wait(timeout=15)
    result["vram_peak_mb"] = sampler.peak
    result["vram_model_mb"] = sampler.peak - baseline_vram
    print(f"  VRAM: peak {sampler.peak} MB ({result['vram_model_mb']} MB for this model), "
          f"RAM end {result['memory_end']}")
    (out_dir / f"{args.name}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
