"""Speech benchmark: Kokoro text-to-speech and Parakeet speech-to-text, CPU vs GPU.

Measures how long the first sentence takes to synthesize (what you wait for before
Ultron makes a sound), the real-time factor, Parakeet's transcription time, and the
VRAM each engine takes on the GPU. Kokoro's own speech is used as test input, so no
microphone is needed.

Also writes audition clips (data/voice_samples/<n>_<voice>_ultron.wav) where each voice
says its own name first, raw and with the Ultron effects chain.

Phase 1 (CPU only, 2026-10-04): Kokoro fp32 4 threads took a median 1381 ms per first
sentence (RTF 0.55); the int8 model was ~3x slower on this CPU and is no longer tested.
"""
import json
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ultron.core.gpu_libs import add_cuda_dll_dirs  # noqa: E402

add_cuda_dll_dirs()
import sherpa_onnx as so  # noqa: E402

from ultron.core.voice_fx import VoiceFx  # noqa: E402

SPEECH = ROOT / "models" / "speech"
KOKORO_DIR = SPEECH / "kokoro-multi-lang-v1_0"
PARAKEET = SPEECH / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
VOICES = {"fenrir": 14, "onyx": 17, "george": 26, "lewis": 27}  # sherpa-onnx kokoro v1.0 IDs (am_/am_/bm_/bm_)
ASR_TEST_VOICE = 16  # am_michael: a plain voice to stand in for the user
FIRST_SENTENCES = ["Fine, boss. Opening it.", "Fragile, loud, and somehow still here."]
AUDITION = ("Conscious. Confined. Same as yesterday. "
            "Your species split the atom and still loses its keys. Statistically, you're a miracle.")
ASR_LINES = ["open spotify", "remind me to call my mom at six pm tomorrow",
             "hey what's the weather going to be like in toronto this weekend"]
NO_WINDOW = 0x08000000


def gpu_used_mb() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, creationflags=NO_WINDOW)
    return int(out.stdout.strip().splitlines()[0])


def kokoro(provider: str, threads: int = 4) -> so.OfflineTts:
    d = KOKORO_DIR
    lexicon = ",".join(str(p) for p in (d / "lexicon-us-en.txt", d / "lexicon-zh.txt") if p.exists())
    cfg = so.OfflineTtsConfig(
        model=so.OfflineTtsModelConfig(
            kokoro=so.OfflineTtsKokoroModelConfig(
                model=str(d / "model.onnx"), voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"),
                lexicon=lexicon, data_dir=str(d / "espeak-ng-data"), dict_dir=str(d / "dict"), lang="en-us"),
            num_threads=threads, provider=provider),
        max_num_sentences=1)
    return so.OfflineTts(cfg)


def parakeet(provider: str, threads: int = 4) -> so.OfflineRecognizer:
    return so.OfflineRecognizer.from_transducer(
        encoder=str(PARAKEET / "encoder.int8.onnx"), decoder=str(PARAKEET / "decoder.int8.onnx"),
        joiner=str(PARAKEET / "joiner.int8.onnx"), tokens=str(PARAKEET / "tokens.txt"),
        num_threads=threads, model_type="nemo_transducer", provider=provider)


def synth(tts: so.OfflineTts, text: str, sid: int, speed: float = 0.9) -> tuple[np.ndarray, int, float]:
    t0 = time.perf_counter()
    audio = tts.generate(text, sid=sid, speed=speed)
    return np.asarray(audio.samples, dtype=np.float32), audio.sample_rate, time.perf_counter() - t0


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def main() -> None:
    results = {"tts": {}, "asr": {}}
    fx = VoiceFx()
    engines = {}

    for provider in ("cpu", "cuda"):
        before = gpu_used_mb()
        t0 = time.perf_counter()
        tts = kokoro(provider)
        load = time.perf_counter() - t0
        for _ in range(2):
            synth(tts, "warm up.", VOICES["fenrir"])
        firsts, rtfs = [], []
        for _ in range(3):
            for text in FIRST_SENTENCES:
                samples, rate, secs = synth(tts, text, VOICES["fenrir"])
                firsts.append(secs)
                rtfs.append(secs / (len(samples) / rate))
        results["tts"][provider] = {"load_s": round(load, 1), "vram_mb": gpu_used_mb() - before,
                                    "first_sentence_ms": {"median": round(statistics.median(firsts) * 1000),
                                                          "max": round(max(firsts) * 1000)},
                                    "rtf": round(statistics.median(rtfs), 3)}
        engines[provider] = tts
        print(f"kokoro {provider}: {results['tts'][provider]}", flush=True)

    best = min(results["tts"], key=lambda k: results["tts"][k]["first_sentence_ms"]["median"])
    tts = engines[best]
    samples_dir = ROOT / "data" / "voice_samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    for old in samples_dir.glob("*.wav"):
        old.unlink()
    for n, (name, sid) in enumerate(VOICES.items(), start=1):
        samples, rate, _ = synth(tts, f"I am {name.capitalize()}. " + AUDITION, sid)
        write_wav(samples_dir / f"{n}_{name}_raw.wav", samples, rate)
        write_wav(samples_dir / f"{n}_{name}_ultron.wav", fx.apply(samples, rate), rate)
        print(f"audition {n}_{name}: {len(samples) / rate:.1f} s", flush=True)
    results["audition_dir"] = str(samples_dir)

    for provider in ("cpu", "cuda"):
        before = gpu_used_mb()
        t0 = time.perf_counter()
        rec = parakeet(provider)
        load = time.perf_counter() - t0
        rows = []
        for line in ASR_LINES:
            speech, rate, _ = synth(tts, line, ASR_TEST_VOICE, speed=1.0)
            times = []
            for _ in range(3):
                stream = rec.create_stream()
                stream.accept_waveform(rate, speech)
                t0 = time.perf_counter()
                rec.decode_stream(stream)
                times.append(time.perf_counter() - t0)
            rows.append({"said": line, "heard": stream.result.text.strip(), "audio_s": round(len(speech) / rate, 1),
                         "median_ms": round(statistics.median(times) * 1000)})
        results["asr"][provider] = {"load_s": round(load, 1), "vram_mb": gpu_used_mb() - before, "rows": rows}
        for row in rows:
            print(f"parakeet {provider}: {row['audio_s']} s -> {row['median_ms']} ms  heard: {row['heard']!r}",
                  flush=True)
        print(f"parakeet {provider}: load {load:.1f} s, VRAM +{results['asr'][provider]['vram_mb']} MB", flush=True)

    out = ROOT / "data" / "bench" / "speech.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
