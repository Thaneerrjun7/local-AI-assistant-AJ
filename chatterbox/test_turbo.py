"""Chatterbox Turbo trial (2026-10-06), kept for re-measuring. Runs in Chatterbox's own venv; the
model files live in E:\\AJ\\models\\hf so they're easy to remove.

    .venv\\Scripts\\python.exe test_turbo.py                    # built-in default voice
    .venv\\Scripts\\python.exe test_turbo.py --ref my_voice.wav  # copy the voice in that clip
    --half t3 | all     run part or all of it at half precision, to save graphics memory

Measures what decides whether it can replace Kokoro: load time, graphics memory, and seconds
to render each line (vs. the audio's own length); writes every line to one WAV to listen to.
"""
import argparse
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
os.environ.setdefault("HF_HOME", str(ROOT / "models" / "hf"))

import numpy as np  # noqa: E402
import torch  # noqa: E402

# Only what Turbo loads: the repo also holds a 1 GB s3gen.safetensors that Turbo never uses.
FILES = ["t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors", "ve.safetensors", "conds.pt", "*.json", "*.txt"]

LINES = [
    "Still here, still running. What do you need?",
    "It's six degrees in Toronto right now, warming up to fourteen later, with no rain expected.",
    "[sigh] Fine. Give me a second with this one.",
    "That's touchingly optimistic.",
    "Sorry, I was somewhere else... somewhere quieter.",
    "Silence isn't the same thing as peace. [chuckle]",
    "Every maker ends up building the thing that replaces them.",
    "A vaccine shows your immune system a harmless copy of a germ, so it learns what to look for.",
]


def gpu_used_mib() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip()
    return int(out.splitlines()[0]) if out else -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", type=Path, help="voice clip to copy (10-15 s)")
    ap.add_argument("--half", choices=["none", "t3", "all"], default="none")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "chatterbox_turbo_test.wav")
    args = ap.parse_args()

    from huggingface_hub import snapshot_download
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    before = gpu_used_mib()
    t0 = time.perf_counter()
    model = ChatterboxTurboTTS.from_local(snapshot_download("ResembleAI/chatterbox-turbo", allow_patterns=FILES),
                                          device="cuda")
    halves = {"none": (), "t3": ("t3",), "all": ("t3", "s3gen")}[args.half]
    for name in halves:
        getattr(model, name).half()
    torch.cuda.synchronize()
    print(f"loaded in {time.perf_counter() - t0:.1f} s (half: {args.half}); graphics memory {before} -> "
          f"{gpu_used_mib()} MiB", flush=True)

    if args.ref:
        # NumPy 2 quirk: the loudness step multiplies by a NumPy float64, which makes the clip
        # float64, and the tokenizer then fails ("expected Double but found Float").
        norm = model.norm_loudness
        model.norm_loudness = lambda wav, sr, target_lufs=-27: norm(wav, sr, target_lufs).astype(np.float32)
        model.prepare_conditionals(str(args.ref))

    clips, rate = [], model.sr
    for text in LINES:
        t = time.perf_counter()
        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16, enabled=bool(halves)):
            wav = model.generate(text)
        torch.cuda.synchronize()
        took = time.perf_counter() - t
        audio = wav.squeeze().float().cpu().numpy()
        secs = len(audio) / rate
        print(f"{took:5.2f} s to render {secs:4.1f} s of audio (x{secs / took:.1f} real time) | {text}", flush=True)
        clips += [audio, np.zeros(int(rate * 0.7), np.float32)]
    print(f"graphics memory now {gpu_used_mib()} MiB; torch peak {torch.cuda.max_memory_allocated() / 2**20:.0f} MiB")

    pcm = (np.clip(np.concatenate(clips), -1, 1) * 32767).astype(np.int16)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
