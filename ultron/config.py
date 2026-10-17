"""Paths and settings.

User data lives in %APPDATA%\\Ultron so it survives code changes and reinstalls.
Settings are optional: anything missing from settings.toml keeps its default.
"""
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = APP_DIR / "models"
SPEECH_DIR = MODELS_DIR / "speech"
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Ultron"
SETTINGS_PATH = DATA_DIR / "settings.toml"
DB_PATH = DATA_DIR / "ultron.db"
LOG_DIR = DATA_DIR / "logs"


@dataclass
class LlmSettings:
    server: str = str(MODELS_DIR / "llama.cpp" / "llama-server.exe")
    model: str = str(MODELS_DIR / "llm" / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf")
    gpu_layers: int = 99
    context: int = 8192
    port: int = 8077
    temperature: float = 0.7
    max_tokens: int = 450  # room for a full explanation when asked; short replies stop early anyway
    history_turns: int = 6


@dataclass
class VoiceSettings:
    # "chatterbox": Chatterbox Turbo copying the user's own recording (their pick, 2026-10-06).
    # "kokoro": the older stock voices below; also what he falls back to if Chatterbox won't start.
    engine: str = "chatterbox"
    voice_clip: str = str(APP_DIR / "data" / "voice" / "my_voice.wav")  # made with scripts/record_voice.py
    chatterbox_python: str = str(APP_DIR / "chatterbox" / ".venv" / "Scripts" / "python.exe")
    hf_home: str = str(MODELS_DIR / "hf")  # Chatterbox's model files
    kokoro_dir: str = str(SPEECH_DIR / "kokoro-multi-lang-v1_0")
    speaker_id: int = 24  # bm_daniel: the user's pick of the British voices by ear (2026-10-06), over
                          # bm_fable 25 and bm_lewis 27. Was am_onyx (17); bm_george 26 is banned (speech.py)
    speed: float = 1.2    # user, 2026-10-06: 1.1 then 1.4 ("too fast"), settled one notch down; 1.0 sounded slow
    fx_preset: str = "clear"  # "clear" (crisper consonants) or "classic" (heavier robot), when effects are on
    tts_threads: int = 4
    tts_provider: str = "cuda"  # "cpu" works too, but first audio takes ~1.4 s there (Phase 1)
    effects: bool = False  # user, 2026-10-06: plain Onyx; "lose the Ultron effects, he sounds ridiculous"
    stt_dir: str = str(SPEECH_DIR / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8")
    stt_threads: int = 4
    stt_provider: str = "cpu"
    vad_model: str = str(SPEECH_DIR / "silero_vad.onnx")
    end_silence_secs: float = 0.3   # pause that ends your turn
    unfinished_wait_secs: float = 0.6  # extra wait when you trail off on "and", "the", ...


@dataclass
class Settings:
    hotkey: str = "ctrl+alt+u"
    persona_intensity: int = 3  # 1 (mild) .. 5 (full Ultron)
    brief_mode: bool = False
    followup_secs: int = 8
    llm: LlmSettings = field(default_factory=LlmSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)


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
