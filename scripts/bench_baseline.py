"""Phase 0 of docs/HARDWARE_AWARE_INFERENCE_PLAN.md: today's AJ, measured as it is. No changes to AJ.

One condition per run, so the cable can go in or out in between; each refuses to run on the wrong
power source, or while AJ or the wake listener is already on:

    .venv\\Scripts\\python.exe scripts\\bench_baseline.py ac-idle        plugged in, AJ on, nobody talking
    .venv\\Scripts\\python.exe scripts\\bench_baseline.py ac-talk        plugged in, the test recordings as a conversation
    .venv\\Scripts\\python.exe scripts\\bench_baseline.py listener       plugged in, AJ off, the wake listener on
    .venv\\Scripts\\python.exe scripts\\bench_baseline.py battery-off    on battery, AJ off: what the laptop draws without him
    .venv\\Scripts\\python.exe scripts\\bench_baseline.py battery-idle   on battery, AJ on, nobody talking
    .venv\\Scripts\\python.exe scripts\\bench_baseline.py battery-talk   on battery, the test recordings as a conversation
    .venv\\Scripts\\python.exe scripts\\bench_baseline.py report         data/bench/placement/baseline.md from the results

Options: --minutes (idle, off, listener; default 5), --rounds (talk: passes through the nine clips
in data/test_audio; default 2), --settle (seconds after AJ is up before an idle run is measured;
default 60), --every (seconds between samples; default 5), --note (e.g. the Lenovo Vantage power
mode, kept with the result).

How AJ runs here: `python -m aj --wav ...`, the console test mode. "Idle" feeds it a recording of
silence, so it is awake with its ears working and nothing to answer, and no stray room sound can
start a turn. "Talk" feeds the nine test clips, each after the last answer, and AJ's own per-turn
timings (first sound after you stopped) are read from what it prints. The listener runs with
--no-start, so it never starts AJ.

What is sampled every few seconds: the battery's draw in watts (from its driver), whether the RTX
4050 is awake (D0) or powered down (D3) from Windows' records, AJ's processes' RAM (working set, in
all and per process: AJ, llama-server) and CPU, and free system RAM. Not private bytes: for
llama-server they count graphics memory mapped on the CUDA side (7-8.5 GB in the trial run). Graphics memory comes
from nvidia-smi, which wakes the card, so it is read once, at the end of a plugged-in run, and
never on battery (scripts/hwprobe.py).

Results are merged into data/bench/placement/baseline.json under the condition's name.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwprobe  # noqa: E402

ROOT = hwprobe.ROOT
sys.path.insert(0, str(ROOT))  # for aj.core.switch and aj.listener (is AJ on?)
OUT = ROOT / "data" / "bench" / "placement" / "baseline.json"
REPORT = OUT.with_name("baseline.md")
DEVICES = OUT.with_name("devices.json")
CLIPS = sorted((ROOT / "data" / "test_audio").glob("*.wav"))
PYTHON = sys.executable
NO_WINDOW = hwprobe.NO_WINDOW


@dataclass(frozen=True)
class Condition:
    on_ac: bool
    run: str | None  # "idle", "talk", "listener" or None (nothing of AJ's runs)
    what: str


CONDITIONS = {
    "ac-idle": Condition(True, "idle", "plugged in, AJ on, nobody talking"),
    "ac-talk": Condition(True, "talk", "plugged in, AJ answering the nine test recordings"),
    "listener": Condition(True, "listener", "plugged in, AJ off, the wake listener listening"),
    "battery-off": Condition(False, None, "on battery, AJ off"),
    "battery-idle": Condition(False, "idle", "on battery, AJ on, nobody talking"),
    "battery-talk": Condition(False, "talk", "on battery, AJ answering the nine test recordings"),
}

_LOADED = re.compile(r"AJ is on \((\d+(?:\.\d+)?) s to load\)")
_PROMPT_EVAL = re.compile(r"prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens.*?([\d.]+) tokens per second")
_TIMES = re.compile(r"first token (\d+|-) \| .*?SOUND (\d+|-) ms after you stopped")


# ---- reading AJ's console ------------------------------------------------------------------

def turn_times(lines: list[str]) -> list[dict]:
    """Each answered turn's first token and first sound, in ms after the speaker stopped."""
    out = []
    for line in lines:
        m = _TIMES.search(line)
        if m:
            out.append({"first_token_ms": None if m[1] == "-" else int(m[1]),
                        "first_sound_ms": None if m[2] == "-" else int(m[2])})
    return out


def load_seconds(lines: list[str]) -> float | None:
    return next((float(m[1]) for m in map(_LOADED.search, lines) if m), None)


def prompt_evals(text: str) -> list[dict]:
    """llama-server's own timing of each prompt it read: on battery (2026-10-26) the first one, the
    persona and the tools, took 577.7 s on the CPU, and AJ could answer nothing meanwhile."""
    return [{"ms": float(m[1]), "tokens": int(m[2]), "tokens_per_s": float(m[3])} for m in _PROMPT_EVAL.finditer(text)]


def server_log_evals() -> list[dict]:
    """prompt_evals() of the llama-server.log the last AJ run left (each start rewrites it)."""
    from aj.config import LOG_DIR
    logs = sorted(LOG_DIR.glob("llama-server*.log"), key=lambda p: p.stat().st_mtime)  # one per unit
    return prompt_evals(logs[-1].read_text(encoding="utf-8", errors="replace")) if logs else []


# ---- sampling -----------------------------------------------------------------------------

class Sampler(threading.Thread):
    """Every `every` seconds: battery watts, the RTX 4050's power state, the processes' RAM and
    CPU (the tree under `root`, when there is one) and free system RAM."""

    def __init__(self, every: float, gpu_id: str | None, root: int | None = None):
        super().__init__(daemon=True, name="sampler")
        self.every, self.gpu_id, self.root = every, gpu_id, root
        self.samples: list[dict] = []
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            self.samples.append(self.take())
            self._stop.wait(self.every)

    def take(self) -> dict:
        sample = {"t": time.perf_counter()}
        battery = hwprobe.battery()
        if battery is not None:
            sample.update(on_ac=battery.on_ac, watts=battery.watts)
        if self.gpu_id:
            sample["gpu"] = hwprobe.power_state(self.gpu_id)
        sample["free_ram_mb"] = hwprobe.ram_mb()[1]
        if self.root is not None:
            used = [(name, u) for pid, name in hwprobe.tree(self.root) if (u := hwprobe.usage(pid)) is not None]
            by_name: dict[str, float] = {}
            for name, u in used:
                by_name[name] = round(by_name.get(name, 0.0) + u.working_set_mb, 1)
            sample.update(working_set_mb=round(sum(u.working_set_mb for _, u in used), 1),
                          cpu_seconds=sum(u.cpu_seconds for _, u in used), by_process_mb=by_name)
        return sample

    def stop(self) -> list[dict]:
        self._stop.set()
        self.join()
        return self.samples


def summarize(samples: list[dict], threads: int) -> dict:
    """Averages and spreads over the samples; CPU as a share of the whole processor."""
    def values(key: str) -> list[float]:
        return [s[key] for s in samples if s.get(key) is not None]

    out: dict = {"samples": len(samples)}
    if samples:
        out["seconds"] = round(samples[-1]["t"] - samples[0]["t"], 1)
    watts = values("watts")
    if watts and not any(s.get("on_ac") for s in samples):
        out["watts"] = {"mean": round(sum(watts) / len(watts), 2), "p50": hwprobe.pct(watts, 50),
                        "p95": hwprobe.pct(watts, 95)}
    states = values("gpu")
    if states:
        present = [s for s in states if s != "absent"]
        out["rtx4050_present_share"] = round(len(present) / len(states), 3)
        if present:
            out["rtx4050_awake_share"] = round(sum(s == "D0" for s in present) / len(present), 3)
    for key in ("working_set_mb", "free_ram_mb"):
        if v := values(key):
            out[key] = {"mean": round(sum(v) / len(v)), "max": round(max(v)), "min": round(min(v))}
    names = sorted({name for s in samples for name in s.get("by_process_mb", {})})
    if names:
        out["by_process_mb"] = {}
        for name in names:
            v = [s["by_process_mb"][name] for s in samples if name in s.get("by_process_mb", {})]
            out["by_process_mb"][name] = {"mean": round(sum(v) / len(v)), "max": round(max(v))}
    cpu = [s for s in samples if s.get("cpu_seconds") is not None]
    if len(cpu) >= 2 and cpu[-1]["t"] > cpu[0]["t"]:
        share = (cpu[-1]["cpu_seconds"] - cpu[0]["cpu_seconds"]) / (cpu[-1]["t"] - cpu[0]["t"])
        out["cpu"] = {"percent_of_one_core": round(share * 100, 2),
                      "percent_of_whole_cpu": round(share * 100 / max(1, threads), 2)}
    return out


def latency(turns: list[dict]) -> dict:
    """p50/p95 of first sound and first token, the first turn left out as the warm-up."""
    counted = turns[1:]
    sound = [t["first_sound_ms"] for t in counted if t["first_sound_ms"] is not None]
    token = [t["first_token_ms"] for t in counted if t["first_token_ms"] is not None]
    return {"turns": len(turns), "counted": len(counted),
            "first_sound_ms": {"p50": hwprobe.pct(sound, 50), "p95": hwprobe.pct(sound, 95)},
            "first_token_ms": {"p50": hwprobe.pct(token, 50), "p95": hwprobe.pct(token, 95)}}


# ---- running things -----------------------------------------------------------------------

def silence(seconds: float, folder: Path) -> Path:
    path = folder / "silence.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * int(16000 * seconds))
    return path


class Console(threading.Thread):
    """Reads a child's console as it prints."""

    def __init__(self, proc: subprocess.Popen):
        super().__init__(daemon=True, name="console")
        self.proc, self.lines = proc, []
        self.loaded = threading.Event()

    def run(self) -> None:
        for line in self.proc.stdout:
            self.lines.append(line.rstrip())
            if _LOADED.search(line):
                self.loaded.set()


def start(args: list[str]) -> tuple[subprocess.Popen, Console]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([PYTHON, *args], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env, creationflags=NO_WINDOW)
    console = Console(proc)
    console.start()
    return proc, console


def stop_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)


def read_vram(root: int) -> dict:
    """Graphics memory in use, from nvidia-smi: wakes the card, so plugged in only. The card's
    total: on this laptop (Hybrid mode, the WDDM driver) nvidia-smi names no processes, so AJ's
    own share can't be told apart (trial run, 2026-10-26: 3,795 MB in use, no process listed)."""
    smi = hwprobe.nvidia_smi()
    mine = {pid for pid, _ in hwprobe.tree(root)}
    listed = smi.get("used_by_pid_mb", {})
    return {"card_used": smi.get("used_mb"), "aj": sum(mb for pid, mb in listed.items() if pid in mine) if listed else None}


def gpu_instance() -> str | None:
    """The RTX 4050's device id, present or not: unplugged, Windows removes the card (2026-10-26)."""
    found = hwprobe.powershell_json("Get-PnpDevice | Where-Object FriendlyName -match 'RTX 4050' | "
                                    "Select-Object InstanceId | ConvertTo-Json -Compress")
    return found[0]["InstanceId"] if found else None


def measure(name: str, cond: Condition, opts: argparse.Namespace) -> dict:
    gpu = gpu_instance()
    threads = os.cpu_count() or 1
    result: dict = {"condition": name, "what": cond.what, "when": time.strftime("%Y-%m-%d %H:%M"), "note": opts.note}

    if cond.run is None:  # AJ off: the laptop on its own
        sampler = Sampler(opts.every, gpu)
        sampler.start()
        time.sleep(opts.minutes * 60)
        result["measured"] = summarize(sampler.stop(), threads)
        return result

    with tempfile.TemporaryDirectory(prefix="aj-baseline-") as tmp:
        if cond.run == "listener":
            proc, console = start(["-m", "aj.listener", "--no-start"])
            time.sleep(opts.settle)
        elif cond.run == "idle":
            proc, console = start(["-m", "aj", "--wav", str(silence(opts.settle + opts.minutes * 60 + 30, Path(tmp)))])
        else:
            clips = CLIPS[:opts.clips] if opts.clips else CLIPS
            proc, console = start(["-m", "aj", "--wav", *map(str, clips * opts.rounds)])
        try:
            if cond.run != "listener":
                if not console.loaded.wait(180):
                    raise RuntimeError("AJ didn't come up within 180 s:\n" + "\n".join(console.lines[-20:]))
                result["load_seconds"] = load_seconds(console.lines)
                if cond.run == "idle":
                    time.sleep(opts.settle)
            sampler = Sampler(opts.every, gpu, proc.pid)
            sampler.start()
            vram_wanted = cond.on_ac and cond.run != "listener"
            if cond.run == "talk":
                # AJ exits by itself after the last clip: graphics memory is read after the third
                # answer, while he still runs (the card is awake then anyway).
                deadline = time.monotonic() + 60 * 30
                while proc.poll() is None and time.monotonic() < deadline:
                    if vram_wanted and "vram_mb" not in result and len(turn_times(console.lines)) >= 3:
                        result["vram_mb"] = read_vram(proc.pid)
                    time.sleep(1)
                samples = sampler.stop()
            else:
                time.sleep(opts.minutes * 60)
                samples = sampler.stop()
                if vram_wanted:
                    result["vram_mb"] = read_vram(proc.pid)  # at the end: the sampling is done
        finally:
            if cond.run == "listener":
                from aj import listener
                listener.request_stop(wait_secs=10)
            stop_tree(proc)
            proc.wait(timeout=30)
        result["measured"] = summarize(samples, threads)
        if cond.run == "talk":
            result["latency"] = latency(turn_times(console.lines))
            result["turns"] = turn_times(console.lines)
        result["console_tail"] = console.lines[-6:]
        if cond.run != "listener":
            result["prompt_evals"] = server_log_evals()[:6]
    return result


def check_ready(cond: Condition) -> str | None:
    """Why this condition can't run now, or None."""
    from aj import listener
    from aj.core import switch
    battery = hwprobe.battery()
    if battery is None:
        return "the battery can't be read"
    if battery.on_ac != cond.on_ac:
        return "plug the charger in first" if cond.on_ac else "unplug the charger first"
    if switch.is_running():
        return "AJ is on: turn it off first"
    if listener.is_listener_running():
        return "the wake listener is on: stop it first (python -m aj.listener --stop)"
    return None


# ---- the report ---------------------------------------------------------------------------

def verdict(results: dict) -> str:
    """The battery question of the plan (§1): does AJ, awake and idle, keep the RTX 4050 up and
    cost battery? Answered from battery-off against battery-idle."""
    off, idle = results.get("battery-off"), results.get("battery-idle")
    if not off or not idle:
        return "Not answered yet: needs both battery-off and battery-idle."
    w_off = off["measured"].get("watts", {}).get("mean")
    w_idle = idle["measured"].get("watts", {}).get("mean")
    on_battery = [r for name, r in results.items() if name.startswith("battery")]
    card_off = any(r["measured"].get("rtx4050_present_share") == 0 for r in on_battery)
    if card_off and None not in (w_off, w_idle):
        cpu = idle["measured"].get("cpu", {}).get("percent_of_whole_cpu")
        said = (f"Different from the guess: on battery this laptop switches the RTX 4050 off altogether, so AJ can't "
                f"keep it awake. llama-server then finds no CUDA device and runs Qwen on the CPU instead: AJ awake and "
                f"idle drew {round(w_idle - w_off, 2)} W more ({w_off} W to {w_idle} W)"
                + (f", with {cpu}% of the whole CPU busy." if cpu is not None else "."))
        first = next((r["prompt_evals"][0] for r in on_battery if r.get("prompt_evals")), None)
        if first:
            said += (f" Its first prompt, the persona and the tools ({first['tokens']} tokens), took "
                     f"{first['ms'] / 1000:.1f} s on the CPU ({first['tokens_per_s']} tokens/s): AJ could answer "
                     f"nothing for that long after starting.")
        talk = results.get("battery-talk")
        if talk and talk.get("latency", {}).get("turns") == 0:
            said += (f" In the conversation test on battery he answered nothing in "
                     f"{talk['measured'].get('seconds', 0) / 60:.0f} minutes.")
        return said
    up_off = off["measured"].get("rtx4050_awake_share")
    up_idle = idle["measured"].get("rtx4050_awake_share")
    if None in (w_off, w_idle, up_off, up_idle):
        return "Not answered: a reading is missing."
    extra = round(w_idle - w_off, 2)
    awake = f"the RTX 4050 was awake {up_idle:.0%} of the time with AJ on, {up_off:.0%} without"
    if up_idle > 0.5 and extra > 1.0:
        return f"Confirmed: AJ awake and idle draws {extra} W more on battery ({w_off} W to {w_idle} W), and {awake}."
    if extra > 1.0:
        return f"Partly: AJ awake and idle draws {extra} W more on battery ({w_off} W to {w_idle} W), but {awake}."
    return f"Rejected: AJ awake and idle draws {extra} W more on battery ({w_off} W to {w_idle} W), and {awake}."


def report() -> str:
    results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    devices = json.loads(DEVICES.read_text(encoding="utf-8")) if DEVICES.exists() else {}
    lines = ["# Phase 0 baseline: today's AJ", ""]
    if devices:
        lines += [f"Devices checked {devices.get('when')} (BIOS {devices.get('bios')}, Windows build "
                  f"{devices.get('windows')}, {devices.get('ram_mb', {}).get('total')} MB RAM):", ""]
        lines += [f"- **{k}**: {'usable' if u['usable'] else 'not usable'}: {u['reason']}"
                  for k, u in devices.get("units", {}).items()]
        lines.append("")
    lines += ["| Condition | Battery draw | RTX 4050 awake | AJ RAM (working set) | AJ CPU | Graphics memory | "
              "First sound p50 / p95 |", "|---|---|---|---|---|---|---|"]
    for name in [*CONDITIONS, *sorted(set(results) - set(CONDITIONS))]:
        r = results.get(name)
        if not r:
            lines.append(f"| {name} | not run | | | | | |")
            continue
        m = r["measured"]
        watts = f"{m['watts']['mean']} W" if "watts" in m else "plugged in"
        awake = ("switched off" if m.get("rtx4050_present_share") == 0 else
                 f"{m['rtx4050_awake_share']:.0%}" if "rtx4050_awake_share" in m else "-")
        ram = f"{m['working_set_mb']['mean']} MB (max {m['working_set_mb']['max']})" if "working_set_mb" in m else "-"
        cpu = f"{m['cpu']['percent_of_whole_cpu']}%" if "cpu" in m else "-"
        vram = f"{r['vram_mb']['card_used']} MB in use" if r.get("vram_mb", {}).get("card_used") is not None else "-"
        lat = r.get("latency", {}).get("first_sound_ms", {})
        sound = (f"{lat['p50']} / {lat['p95']} ms" if lat.get("p50") is not None else
                 f"no answer in {m.get('seconds', 0) / 60:.0f} min" if r.get("latency", {}).get("turns") == 0 else "-")
        lines.append(f"| {name} | {watts} | {awake} | {ram} | {cpu} | {vram} | {sound} |")
    lines += ["", "## The battery question", "", verdict(results), "", "## How it was measured", "",
              "- One condition per run, `scripts/bench_baseline.py <condition>`; samples every few seconds.",
              "- Battery draw from the battery driver (IOCTL_BATTERY_QUERY_STATUS), the RTX 4050's power state "
              "(D0 awake, D3 off) from Windows' Plug and Play records; nvidia-smi only once, at the end of a "
              "plugged-in run, since it wakes the card.",
              "- Idle: AJ fed a recording of silence (ears working, nothing to answer), measured after it had "
              "settled. Talk: the nine clips in data/test_audio, answered in turn; first sound is AJ's own "
              "timing, from when the clip stopped, the first turn left out as the warm-up.",
              "- Run from inside the Claude desktop app, AJ used its separate AJ-sandbox data folder (config.py).",
              "- The first battery runs could not yet tell a card Windows had removed from one it couldn't read: "
              "battery-off's share is of the samples taken before the card went, and battery-idle has none. "
              "battery-card-check repeats the check with that fixed.", ""]
    notes = sorted({f"{name}: {r['note']}" for name, r in results.items() if r.get("note")})
    if notes:
        lines += ["Notes kept with the runs:", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench_baseline")
    ap.add_argument("condition", choices=[*CONDITIONS, "report"])
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--clips", type=int, default=0, help="talk: only the first N clips (0: all nine)")
    ap.add_argument("--settle", type=float, default=60.0)
    ap.add_argument("--every", type=float, default=5.0)
    ap.add_argument("--note", default="")
    ap.add_argument("--save-as", default="", help="keep the result under this name, not the condition's")
    opts = ap.parse_args(argv)
    if opts.condition == "report":
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(report(), encoding="utf-8")
        print(f"Written to {REPORT}")
        return 0
    cond = CONDITIONS[opts.condition]
    if (why := check_ready(cond)) is not None:
        print(f"Not run: {why}.")
        return 2
    print(f"{opts.condition}: {cond.what}. Leave the laptop alone until it says done.", flush=True)
    result = measure(opts.condition, cond, opts)
    results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    results[opts.save_as or opts.condition] = result
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("turns", "console_tail")}, indent=1))
    print(f"done; written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
