"""Paths and settings.

User data lives in %APPDATA%\\AJ so it survives code changes and reinstalls. An install from before
the rename to AJ (2026-10-22) kept it under the project's old name; core/migrate.py moves it here
at start-up.
Settings are optional: anything missing from settings.toml keeps its default.
"""
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = APP_DIR / "models"
SPEECH_DIR = MODELS_DIR / "speech"
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "AJ"
SETTINGS_PATH = DATA_DIR / "settings.toml"
DB_PATH = DATA_DIR / "aj.db"
LOG_DIR = DATA_DIR / "logs"


@dataclass
class LlmSettings:
    server: str = str(MODELS_DIR / "llama.cpp" / "llama-server.exe")
    model: str = str(MODELS_DIR / "llm" / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf")
    gpu_layers: int = 99
    # Per slot this is context / 2 (-np 2). v4's prompt (persona + every tool) is ~4,450 tokens,
    # past the 4,096 a slot had at 8192, so 16384 with the KV cache in 8 bits: 8,192 per slot in
    # about the VRAM the 16-bit cache took at 8192.
    context: int = 16384
    kv_cache_type: str = "q8_0"  # needs flash attention, which llama-server then turns on
    port: int = 8077
    temperature: float = 0.5
    max_tokens: int = 450  # room for a full explanation when asked; short replies stop early anyway
    history_turns: int = 6


@dataclass
class VoiceSettings:
    # "piper": the default from 2026-10-21, the boss's pick by ear of the Piper samples (voice 2,
    # northern English male). On the CPU: no graphics memory, ready in under 2 s.
    # "kokoro": the Kokoro voices below (Daniel), the default 2026-10-06 to 2026-10-21. Its model
    # and the CUDA libraries it ran on were removed at the boss's request (2026-10-21); the code
    # stays only so a future voice choice can bring it back.
    # "chatterbox": Chatterbox Turbo copying the user's recording, kept for a bigger graphics card
    # (it took 4.5-11 s to speak on this GPU and 8.4-17.7 s on the CPU).
    engine: str = "piper"
    voice_clip: str = str(APP_DIR / "data" / "voice" / "my_voice.wav")  # made with scripts/record_voice.py
    chatterbox_python: str = str(APP_DIR / "chatterbox" / ".venv" / "Scripts" / "python.exe")
    hf_home: str = str(MODELS_DIR / "hf")  # Chatterbox's model files
    # Where Chatterbox runs. On the GPU next to Qwen it filled the 6 GB card and took 4.5-11 s to
    # start speaking (2026-10-18), so the user asked to try the CPU, leaving the GPU to Qwen.
    chatterbox_device: str = "cpu"
    chatterbox_threads: int = 8  # CPU threads for it (the Ryzen 7 7840HS has 8 cores, 16 threads)
    # "piper": a Piper voice on the CPU (no graphics memory), from models/speech/piper (2026-10-19)
    piper_dir: str = str(SPEECH_DIR / "piper")
    piper_voice: str = "vits-piper-en_GB-northern_english_male-medium"  # a folder in piper_dir (the boss's pick)
    piper_speaker: int = 0  # for the voices with several speakers (semaine, southern_english_male)
    piper_speed: float = 1.0
    piper_noise: float = 0.667    # Piper's variation in delivery: lower is steadier and clearer, flatter
    piper_noise_w: float = 0.8    # its variation in timing
    piper_threads: int = 4
    kokoro_dir: str = str(SPEECH_DIR / "kokoro-multi-lang-v1_0")
    speaker_id: int = 24  # bm_daniel: the user's pick of the British voices by ear (2026-10-06), over
                          # bm_fable 25 and bm_lewis 27. Was am_onyx (17); bm_george 26 is banned (speech.py)
    speed: float = 1.2    # user, 2026-10-06: 1.1 then 1.4 ("too fast"), settled one notch down; 1.0 sounded slow
    fx_preset: str = "clear"  # "clear" (crisper consonants) or "classic" (heavier robot), when effects are on
    tts_threads: int = 4
    tts_provider: str = "cpu"  # Kokoro only; its GPU libraries are gone (Piper is always on the CPU)
    effects: bool = False  # user, 2026-10-06: plain Onyx; lose the robot effects, "he sounds ridiculous"
    stt_dir: str = str(SPEECH_DIR / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8")
    stt_threads: int = 4
    stt_provider: str = "cpu"
    vad_model: str = str(SPEECH_DIR / "silero_vad.onnx")
    end_silence_secs: float = 0.3   # pause that ends your turn
    # Talk over him through the laptop speakers and he stops and listens (core/barge.py).
    interrupt: bool = True
    unfinished_wait_secs: float = 0.6  # extra wait when you trail off on "and", "the", ...


@dataclass
class DictationSettings:
    """"Write this down": what the boss says next is typed where his cursor is (core/dictation.py).
    The boss, 2026-10-22: "it has to get it accurately, exactly the way I want it", and "it can
    take longer". So Gemini hears it, and Parakeet stands in only when Gemini can't."""
    # People pause mid-sentence while dictating, so dictation ends only after this much quiet, not
    # the 0.3 s that ends a normal turn. The VAD still cuts at 0.3 s; the pieces are joined.
    silence_secs: float = 1.5
    max_secs: float = 60.0    # the longest dictation; past it he types what he has and says so
    start_secs: float = 10.0  # nothing said this long after "Go ahead.": dictation is off again
    # Who writes the words down. Measured live 2026-10-22 on Piper-voiced test sentences with
    # names, numbers, questions and spoken punctuation (core/dictation.py has the numbers): the
    # transcription model, median 1.8 s, word error 13% (Parakeet 23%); gemini-3.8-flash was too
    # overloaded that day to answer.
    gemini_model: str = "gemini-3.5-transcribe"
    # Tried when that one is overloaded or rate-limited twice running ("" for none: then Parakeet).
    backup_model: str = ""
    # Who rewrites them when he asks for it polite or refined (the transcription model can't).
    # Live 2026-10-22: gemini-3.1-flash-lite in 4.9 s; gemini-3.8-flash no answer within 20 s.
    polish_model: str = "gemini-3.1-flash-lite"
    timeout_secs: float = 20.0  # each Gemini answer; past it, Parakeet (or for polish, the local model)


@dataclass
class SleepSettings:
    """Sleep mode (core/wake.py): the window goes to the tray and only its name wakes it ("Hey AJ",
    "Computer"), and only while the laptop is plugged in (the boss, 2026-10-22)."""
    # Asleep, let go of the model too (llama-server stops), so the graphics card is free. Off by
    # default: waking then waits for the model to load again before it can answer.
    free_gpu: bool = False
    # Fall asleep after this many minutes with nothing answered. 0: never (the default).
    idle_mins: float = 0.0
    # Asleep, how often to look at the power cable: on battery the mic closes within this long.
    power_check_secs: float = 2.0


@dataclass
class Settings:
    # What the assistant calls itself: the boss's choice, "AJ" since 2026-10-22 (was "Computer",
    # which he still uses too: persona.ALIAS).
    name: str = "AJ"
    # Other names it answers to, wake words included ("Hey Computer", core/wake.py).
    aliases: list[str] = field(default_factory=lambda: ["Computer"])
    hotkey: str = "ctrl+alt+u"
    persona_intensity: int = 3  # 1 (mild) .. 5 (full character)
    brief_mode: bool = False
    followup_secs: int = 8
    llm: LlmSettings = field(default_factory=LlmSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    dictation: DictationSettings = field(default_factory=DictationSettings)
    sleep: SleepSettings = field(default_factory=SleepSettings)

    @property
    def wake_names(self) -> list[str]:
        """Every name it answers to: its own first, then the aliases."""
        return [self.name, *(a for a in self.aliases if a and a.lower() != self.name.lower())]


def _apply(target, values: dict) -> None:
    for f in fields(target):
        if f.name not in values:
            continue
        current = getattr(target, f.name)
        if is_dataclass(current) and isinstance(values[f.name], dict):
            _apply(current, values[f.name])
        else:
            setattr(target, f.name, values[f.name])


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    settings = Settings()
    if path.exists():
        with path.open("rb") as fh:
            _apply(settings, tomllib.load(fh))
    return settings
