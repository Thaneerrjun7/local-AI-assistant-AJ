"""Phase 1 of docs/HARDWARE_AWARE_INFERENCE_PLAN.md: AJ's two models on each unit that can run them,
plugged in and on battery, measured with scripts/bench_llm.py.

    .venv\\Scripts\\python.exe scripts\\bench_placement.py ac         plugged in: the RTX 4050 and the Radeon 780M
    .venv\\Scripts\\python.exe scripts\\bench_placement.py battery    on battery: the 780M only
    .venv\\Scripts\\python.exe scripts\\bench_placement.py report     data/bench/placement/phase1.md

Options: --only <run name> (one run of the set), --note (e.g. the Lenovo Vantage power mode).

Which runs, and why:
- Qwen3-4B with AJ's own persona and settings (16k context, 8-bit KV cache, flash attention): its
  latency, how long it takes to read AJ's first prompt (the persona and every tool), tool-call
  accuracy, and memory-extraction accuracy plugged in.
- MiniCPM5-2B, the thinker, in thinking mode on the hard set (8k context, as core/thinker.py).
- On battery the RTX 4050 is switched off (Phase 0, scripts/bench_baseline.py), so only the 780M
  runs there, and without the memory-extraction set: same chip, same maths as plugged in, so its
  accuracy can't differ; the battery runs are for watts and speed.
- The CPU isn't re-run: Phase 0 measured what matters there (AJ's first prompt took 577.7 s).

Each run refuses to start on the wrong power source or while AJ is on. Results go to
data/bench/placement/<run>.json, next to Phase 0's baseline.
"""
import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwprobe  # noqa: E402

ROOT = hwprobe.ROOT
sys.path.insert(0, str(ROOT))  # for aj.core.switch (is AJ on?)
OUT = ROOT / "data" / "bench" / "placement"
QWEN = ROOT / "models" / "llm" / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
MINICPM = ROOT / "models" / "llm" / "MiniCPM5-2B-Q4_K_M.gguf"
# --extra takes everything after it, so it goes last.
QWEN_ARGS = ["--persona", "app", "--ctx", "16384", "--extra", "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0"]
THINKER_ARGS = ["--think", "--ctx", "8192"]
COOLDOWN_SECS = 60  # between battery runs: the battery's reported draw is a smoothed average


@dataclass(frozen=True)
class Run:
    name: str
    model: Path
    backend: str
    device: str
    sections: str
    extra: tuple[str, ...]

    @property
    def unit(self) -> str:
        return "Radeon 780M" if self.device == "Vulkan0" else "RTX 4050" if self.backend in ("cuda", "vulkan") else "CPU"

    def argv(self) -> list[str]:
        return [str(ROOT / "scripts" / "bench_llm.py"), "--name", self.name, "--model", str(self.model),
                "--backend", self.backend, *(["--device", self.device] if self.device else []),
                "--sections", self.sections, "--out-dir", str(OUT), *self.extra]


RUNS = {
    "ac": [
        Run("qwen-rtx4050-ac", QWEN, "cuda", "", "latency,prefill,tools,json", tuple(QWEN_ARGS)),
        Run("qwen-780m-ac", QWEN, "vulkan", "Vulkan0", "latency,prefill,tools,json", tuple(QWEN_ARGS)),
        Run("thinker-rtx4050-ac", MINICPM, "cuda", "", "hard", tuple(THINKER_ARGS)),
        Run("thinker-780m-ac", MINICPM, "vulkan", "Vulkan0", "hard", tuple(THINKER_ARGS)),
    ],
    "battery": [
        Run("qwen-780m-battery", QWEN, "vulkan", "Vulkan0", "latency,prefill,tools", tuple(QWEN_ARGS)),
        Run("thinker-780m-battery", MINICPM, "vulkan", "Vulkan0", "hard", tuple(THINKER_ARGS)),
    ],
}


def not_ready(plugged: bool) -> str | None:
    """Why the set can't run now, or None."""
    from aj.core import switch
    battery = hwprobe.battery()
    if battery is None:
        return "the battery can't be read"
    if battery.on_ac != plugged:
        return "plug the charger in first" if plugged else "unplug the charger first"
    if switch.is_running():
        return "AJ is on: turn it off first"
    return None


def run_set(which: str, only: str, note: str) -> int:
    failed = []
    for i, run in enumerate(r for r in RUNS[which] if not only or r.name == only):
        if i and which == "battery":
            # The battery reports a smoothed rate: right after a busy run, "idle" still carries it
            # (2026-10-27: the thinker read 53.5 W idle straight after Qwen's tools at 63 W).
            time.sleep(COOLDOWN_SECS)
        print(f"\n=== {run.name}: {run.unit}, {run.sections} ({time.strftime('%H:%M:%S')})", flush=True)
        done = subprocess.run([sys.executable, *run.argv()], cwd=ROOT).returncode
        path = OUT / f"{run.name}.json"
        if done != 0 or not path.exists():
            failed.append(run.name)
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        result.update(unit=run.unit, note=note, when=time.strftime("%Y-%m-%d %H:%M"))
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\ndone{'; failed: ' + ', '.join(failed) if failed else ''}", flush=True)
    return 1 if failed else 0


# ---- the report ---------------------------------------------------------------------------

def _get(data: dict, *keys, default=None):
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return default
        data = data[key]
    return data


def row(r: dict) -> str:
    """One line of the table for one run's result."""
    lat = _get(r, "latency", "cached", "first_sentence")
    sentence = f"{lat['median_ms']} / {lat['p95_ms']} ms" if lat else "-"
    decode = _get(r, "latency", "cached", "decode_tps")
    pre = r.get("prefill")
    prefill = f"{pre['seconds']} s ({pre['tokens_per_s']} tok/s)" if pre and pre.get("seconds") is not None else "-"
    tools = f"{_get(r, 'tools', 'score_pct')}%" if r.get("tools") else "-"
    recall = _get(r, "json", "grammar", "recall_pct")
    hard = r.get("hard")
    hard_s = f"{hard['score_pct']}%, median {hard['median_s']} s" if hard else "-"
    ram = f"{_get(r, 'memory_after_load', 'working_set_mb')} / {_get(r, 'system_ram_mb', 'model')} MB"
    vram = f"{r['vram_model_mb']} MB" if r.get("vram_model_mb") is not None else "-"
    power = r.get("power_w")
    if power:
        busy = max(v for k, v in power.items() if k not in ("load", "idle")) if len(power) > 2 else None
        watts = f"{power.get('idle')} W idle" + (f", up to {busy} W working" if busy is not None else "")
    else:
        watts = "plugged in"
    return (f"| {r['name']} | {r.get('unit', r.get('backend'))} | {r.get('load_seconds')} s | {prefill} | {sentence} | "
            f"{decode or '-'} | {tools} | {f'{recall}%' if recall is not None else '-'} | {hard_s} | {ram} | {vram} | {watts} |")


def findings(results: dict, baseline: dict) -> list[str]:
    """The comparisons that matter for placement, each only when both sides were measured."""
    out = []
    rtx, igpu, igpu_bat = (results.get(n, {}) for n in ("qwen-rtx4050-ac", "qwen-780m-ac", "qwen-780m-battery"))
    cpu = (_get(baseline, "battery-talk", "prompt_evals", default=None) or [None])[0]
    reads = [f"{_get(r, 'prefill', 'seconds')} s on the {label}" for r, label in
             ((rtx, "RTX 4050"), (igpu, "780M plugged in"), (igpu_bat, "780M on battery")) if _get(r, "prefill", "seconds")]
    if cpu:
        reads.append(f"{cpu['ms'] / 1000:.1f} s on the CPU on battery (Phase 0)")
    if reads:
        out.append("- **Reading AJ's first prompt** (before he can answer at all after starting): " + "; ".join(reads) + ".")
    idle_780m = _get(igpu_bat, "power_w", "idle")
    alone = _get(baseline, "battery-off", "measured", "watts", "mean")
    cpu_idle = _get(baseline, "battery-idle", "measured", "watts", "mean")
    if idle_780m is not None and alone is not None:
        out.append(f"- **On battery, idle with Qwen loaded:** {idle_780m} W with Qwen's server alone on the 780M, against "
                   f"{alone} W for the laptop without AJ" + (f" and {cpu_idle} W for all of AJ (speech included) idle on "
                                                              f"the CPU fallback (Phase 0)." if cpu_idle else "."))
    acc = [(label, _get(r, "tools", "score_pct"), _get(r, "json", "grammar", "recall_pct"))
           for r, label in ((rtx, "RTX 4050"), (igpu, "780M")) if r.get("tools")]
    if len(acc) == 2:
        out.append("- **Accuracy, same model file:** tools " + " vs ".join(f"{t}% ({l})" for l, t, _ in acc)
                   + "; memory recall " + " vs ".join(f"{m}% ({l})" for l, _, m in acc if m is not None) + ".")
    hard = [(label, _get(results.get(n, {}), "hard", "rows")) for n, label in
            (("thinker-rtx4050-ac", "RTX 4050"), ("thinker-780m-ac", "780M"))]
    if all(rows for _, rows in hard):
        said = [f"{sum(r['ok'] for r in rows)}/{len(rows)} on the {label}" for label, rows in hard]
        out.append(f"- **The thinker's hard set:** {', '.join(said)}: the Vulkan build's maths changed some answers.")
    if _get(igpu, "system_ram_mb", "model") is not None and _get(rtx, "system_ram_mb", "model") is not None:
        out.append(f"- **The 780M's price is RAM:** Qwen took {igpu['system_ram_mb']['model']} MB of system RAM there, "
                   f"against {rtx['system_ram_mb']['model']} MB on the RTX 4050, which holds the model in its own memory.")
    return out


def report() -> str:
    names = [run.name for runs in RUNS.values() for run in runs]
    results = {n: json.loads((OUT / f"{n}.json").read_text(encoding="utf-8")) for n in names if (OUT / f"{n}.json").exists()}
    lines = ["# Phase 1: AJ's models on each unit", "",
             "| Run | Unit | Load | AJ's first prompt read | First sentence (cached) p50 / p95 | Decode tok/s | Tools | "
             "Memory recall | Hard set | RAM: server / system taken | Graphics memory | Battery draw |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    lines += [row(results[n]) if n in results else f"| {n} | not run | | | | | | | | | | |" for n in names]
    path = OUT / "baseline.json"
    baseline = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if found := findings(results, baseline):
        lines += ["", "## What it shows", "", *found]
    notes = sorted({f"{n}: {r['note']}" for n, r in results.items() if r.get("note")})
    lines += ["", "## How it was measured", "",
              "- scripts/bench_placement.py runs scripts/bench_llm.py per model and unit: the 780M through the Vulkan "
              "build (`--device Vulkan0`), the RTX 4050 through the CUDA build, as AJ runs it.",
              "- First sentence: AJ's persona, cached prompt, 5 prompts x 5 runs after 3 warm-ups. AJ's first prompt: "
              "the persona and every tool AJ offers, read from scratch, the median of 2.",
              "- Tools: 20 laptop commands x 3 seeds. Memory recall: 50 extraction chats with the JSON grammar. Hard set: "
              "12 tasks in thinking mode.",
              "- RAM: llama-server's working set after loading, and how much free system RAM dropped (the 780M's memory "
              "is system RAM). Graphics memory only for runs on the RTX 4050, plugged in (nvidia-smi wakes it).",
              "- Battery draw from the battery driver every 2 s: idle with the model loaded, and the busiest section. The "
              "battery reports a smoothed rate, so an idle reading right after heavy work still carries it: "
              "thinker-780m-battery's ran straight after Qwen's tools (since then the set pauses a minute between "
              "battery runs).", ""]
    if notes:
        lines += ["Notes kept with the runs:", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench_placement")
    ap.add_argument("which", choices=[*RUNS, "report"])
    ap.add_argument("--only", default="")
    ap.add_argument("--note", default="")
    opts = ap.parse_args(argv)
    if opts.which == "report":
        path = OUT / "phase1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report(), encoding="utf-8")
        print(f"Written to {path}")
        return 0
    if (why := not_ready(opts.which == "ac")) is not None:
        print(f"Not run: {why}.")
        return 2
    return run_set(opts.which, opts.only, opts.note)


if __name__ == "__main__":
    sys.exit(main())
