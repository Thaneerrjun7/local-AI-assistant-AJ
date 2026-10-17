"""Ultron's voice when it's Chatterbox Turbo, copying the user's own recording (2026-10-06).

It runs as a separate process in Chatterbox's own environment (E:\\Ultron\\chatterbox\\.venv),
because Chatterbox needs PyTorch and Ultron's environment doesn't have it. Ultron starts it
when he's switched on and ties it to himself (core/proc.bind_to_ultron), so it ends the
moment he does, however he ends; nothing else ever starts it. It never goes online: the model
files are already in E:\\Ultron\\models\\hf.

Talks over stdin/stdout, one request at a time: Ultron sends a JSON line {"text": ...}; the
reply is a JSON line {"samples": n} followed by n float32 samples (or {"error": ...}).
Whatever the libraries print goes to stderr (Ultron's log), never into the audio channel.
This file imports nothing from Ultron: it runs with the other environment's Python.
"""
import json
import os
import sys

REPO = "ResembleAI/chatterbox-turbo"
FILES = ["t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors", "ve.safetensors", "conds.pt", "*.json", "*.txt"]


def main() -> int:
    channel = os.fdopen(os.dup(1), "wb")  # the audio channel to Ultron
    os.dup2(2, 1)                          # anything printed from here on lands in the log
    sys.stdout = sys.stderr

    def send(reply: dict, data: bytes = b"") -> None:
        channel.write((json.dumps(reply) + "\n").encode() + data)
        channel.flush()

    config = json.loads(sys.argv[1])
    os.environ["HF_HOME"] = config["hf_home"]
    os.environ["HF_HUB_OFFLINE"] = "1"  # the files are already here: never reach out
    os.environ["TQDM_DISABLE"] = "1"
    try:
        import numpy as np
        import torch
        from huggingface_hub import snapshot_download
        from chatterbox.tts_turbo import ChatterboxTurboTTS

        model = ChatterboxTurboTTS.from_local(snapshot_download(REPO, allow_patterns=FILES), device="cuda")
        if config.get("clip"):
            # NumPy 2: the loudness step multiplies by a NumPy float64, which turns the clip into
            # float64 and the tokenizer then fails ("expected Double but found Float").
            norm = model.norm_loudness
            model.norm_loudness = lambda wav, sr, target_lufs=-27: norm(wav, sr, target_lufs).astype(np.float32)
            model.prepare_conditionals(config["clip"])
        with torch.inference_mode():
            model.generate("Ready.")  # the first render is slow: get it over with here
    except Exception as e:
        send({"error": f"{type(e).__name__}: {e}"})
        return 1
    send({"ready": True, "rate": model.sr})

    for line in sys.stdin:
        try:
            with torch.inference_mode():
                wav = model.generate(json.loads(line)["text"])
            audio = wav.squeeze().float().cpu().numpy().astype("<f4")
            send({"samples": int(audio.size)}, audio.tobytes())
        except Exception as e:
            send({"error": f"{type(e).__name__}: {e}"})
    return 0  # Ultron closed the pipe: he's switching off


if __name__ == "__main__":
    sys.exit(main())
