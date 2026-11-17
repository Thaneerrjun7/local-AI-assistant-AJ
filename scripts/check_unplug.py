"""Phase 2 of docs/HARDWARE_AWARE_INFERENCE_PLAN.md: what happens when the charger comes out while
AJ's model runs on the RTX 4050, and when it goes back in ([placement] mode = "auto").

    .venv\\Scripts\\python.exe scripts\\check_unplug.py

Start it plugged in. It starts AJ (the console test mode, fed silence) and, once his model is up on
the RTX 4050, puts up a box: "Unplug the charger now". Later another: "Plug the charger back in".
Every second it notes the cable, whether Windows still lists the RTX 4050 and its power state
(D0 on, D3 off), how many model servers AJ has running, and what AJ says about his model moving.

What it answers: does Windows take the RTX 4050 away from a server that is using it, or wait? Does
that server die? How long until AJ's model is on the 780M and ready again, and back?

Writes data/bench/placement/unplug.json. Needs [placement] mode = "auto" in the settings AJ reads.
"""
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwprobe  # noqa: E402

ROOT = hwprobe.ROOT
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "bench" / "placement" / "unplug.json"
_MODEL_LINE = re.compile(r"\(AJ's model (?:is on|moved to) (.+?)[.:,]")
MIN_CHARGE = 20  # percent: it runs a few minutes on battery
NUDGE_SECS = 120  # the box again while the cable hasn't moved
GIVE_UP_SECS = 600  # never unplugged: stop rather than wait out AJ's 20 minutes


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ac", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("percent", ctypes.c_ubyte),
                ("saver", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full_life", ctypes.c_ulong)]


def charge() -> int | None:
    """The battery's charge in percent, as Windows shows it (None when it doesn't know)."""
    status = _PowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)) or status.percent == 255:
        return None
    return status.percent


def say(text: str) -> None:
    """A box on screen for the person at the laptop (it doesn't wait for OK)."""
    print(f">>> {text}", flush=True)
    threading.Thread(target=ctypes.windll.user32.MessageBoxW, args=(None, text, "AJ placement check", 0x40040),
                     daemon=True).start()


def summarize(timeline: list[dict]) -> dict:
    """The moments that matter, as seconds from the start."""
    def first(pred, after: float = -1.0) -> float | None:
        return next((t["t"] for t in timeline if t["t"] > after and pred(t)), None)
    unplugged = first(lambda t: t["on_ac"] is False)
    out: dict = {"unplugged_at": unplugged}
    if unplugged is not None:
        out["rtx_gone_after_s"] = _after(first(lambda t: not t["rtx_present"], unplugged), unplugged)
        out["on_780m_after_s"] = _after(first(lambda t: "780M" in t["model"], unplugged), unplugged)
        out["servers_at_once_max"] = max(t["servers"] for t in timeline if t["t"] >= unplugged)
        out["no_server_seconds"] = sum(1 for t in timeline if t["t"] >= unplugged and t["servers"] == 0)
        plugged = first(lambda t: t["on_ac"] is True, unplugged)
        out["plugged_back_at"] = plugged
        if plugged is not None:
            out["rtx_back_after_s"] = _after(first(lambda t: t["rtx_present"], plugged), plugged)
            out["on_rtx_after_s"] = _after(first(lambda t: "RTX" in t["model"], plugged), plugged)
    return out


def _after(at: float | None, start: float) -> float | None:
    return None if at is None else round(at - start, 1)


def main() -> int:
    from aj.config import load_settings
    from aj.core import placement, switch
    if load_settings().placement.mode != "auto":
        print("Not run: set [placement] mode = \"auto\" in the settings AJ reads first.")
        return 2
    battery = hwprobe.battery()
    if battery is None or not battery.on_ac:
        print("Not run: start it plugged in.")
        return 2
    if (percent := charge()) is not None and percent < MIN_CHARGE:
        print(f"Not run: the battery is at {percent}%. Let it charge to {MIN_CHARGE}% first.")
        return 2
    if switch.is_running():
        print("Not run: AJ is on: turn it off first.")
        return 2
    gpu = next((d["InstanceId"] for d in hwprobe.pnp_devices() if "RTX 4050" in (d.get("FriendlyName") or "")), None)
    with tempfile.TemporaryDirectory(prefix="aj-unplug-") as tmp:
        quiet = Path(tmp) / "silence.wav"
        with wave.open(str(quiet), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\0\0" * 16000 * 60 * 20)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        aj = subprocess.Popen([sys.executable, "-m", "aj", "--wav", str(quiet)], cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env,
                              creationflags=hwprobe.NO_WINDOW)
        lines: list[str] = []
        threading.Thread(target=lambda: lines.extend(line.rstrip() for line in aj.stdout), daemon=True).start()
        timeline: list[dict] = []
        t0, stage, model = time.perf_counter(), "start", "?"
        try:
            while aj.poll() is None and time.perf_counter() - t0 < 25 * 60:
                said = [ln for ln in lines if _MODEL_LINE.search(ln)]
                if said:
                    model = _MODEL_LINE.search(said[-1])[1]
                now = round(time.perf_counter() - t0, 1)
                b = hwprobe.battery()
                found = placement.adapters()
                servers = sum(1 for _, name in hwprobe.tree(aj.pid) if name.lower() == "llama-server.exe")
                timeline.append({"t": now, "on_ac": None if b is None else b.on_ac,
                                 "rtx_present": any("RTX" in a for a in found),
                                 "rtx_state": hwprobe.power_state(gpu) if gpu else None,
                                 "servers": servers, "model": model})
                if stage == "start" and "RTX" in model and any("AJ is on" in ln for ln in lines):
                    stage, ready_at = "plugged", now
                elif stage == "plugged" and now - ready_at > 30:
                    say("Unplug the charger now, and leave it out until the next box.")
                    stage, asked_at, nudged_at = "asked to unplug", now, now
                elif stage == "asked to unplug" and timeline[-1]["on_ac"] is False:
                    stage, unplugged_at = "unplugged", now
                elif stage == "asked to unplug" and now - asked_at > GIVE_UP_SECS:
                    print(f"Stopped: the charger stayed in for {GIVE_UP_SECS // 60} minutes after the box.", flush=True)
                    break
                elif stage == "asked to unplug" and now - nudged_at > NUDGE_SECS:
                    say("Still plugged in: unplug the charger now, please.")
                    nudged_at = now
                elif stage == "unplugged" and ("780M" in model and now - unplugged_at > 60 or now - unplugged_at > 300):
                    say("Plug the charger back in now.")
                    stage, nudged_at = "asked to plug in", now
                elif stage == "asked to plug in" and timeline[-1]["on_ac"] is True:
                    stage, plugged_at = "plugged back", now
                elif stage == "asked to plug in" and now - nudged_at > NUDGE_SECS:
                    say("Still on battery: plug the charger back in now, please.")
                    nudged_at = now
                elif stage == "plugged back" and ("RTX" in model and now - plugged_at > 30 or now - plugged_at > 300):
                    break
                time.sleep(1)
        finally:
            subprocess.run(["taskkill", "/PID", str(aj.pid), "/T", "/F"], capture_output=True,
                           creationflags=hwprobe.NO_WINDOW)
        result = {"when": time.strftime("%Y-%m-%d %H:%M"), "ended_at_stage": stage, "summary": summarize(timeline),
                  "aj_lines": [ln for ln in lines if ln.startswith("(") or "Error" in ln or "Traceback" in ln][-40:],
                  "timeline": timeline}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result["summary"], indent=1))
    print("\n".join(result["aj_lines"]))
    print(f"Written to {OUT}")
    say("Done. Thank you: you can use the laptop as normal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
