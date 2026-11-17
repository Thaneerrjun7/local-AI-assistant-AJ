"""Paths and settings.

User data lives in %APPDATA%\\AJ so it survives code changes and reinstalls. An install from before
the rename to AJ (2026-10-22) kept it under the project's old name; core/migrate.py moves it here
at start-up.
Settings are optional: anything missing from settings.toml keeps its default.

Started from inside a packaged (MSIX) app, such as the Claude desktop app, a process doesn't
write to the real %APPDATA%: Windows hands it a private copy under
%LOCALAPPDATA%\\Packages\\<app>\\LocalCache\\Roaming, while it still reads the real files where it
has no copy of its own. Found 2026-10-23: the rename's move ran that way on 2026-10-22, so the
user's memory (224 messages) went into that app's private copy, and the AJ his shortcut starts had an
empty database from then on. Worse, SQLite then saw a database from one copy beside a write-ahead
file from the other, and replayed one into the other. So such a process gets a data folder of
its own, AJ-sandbox, that the real AJ never uses, and core/migrate.py moves nothing.
"""
import os
import tomllib
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = APP_DIR / "models"
SPEECH_DIR = MODELS_DIR / "speech"


def appdata_redirected(appdata: Path, local: Path | None = None) -> Path | None:
    """Where this process's writes to `appdata` really land when Windows redirects them (see
    above), or None when they land where they say. A probe file is made and looked for in every
    packaged app's private copy, then removed: about 30 ms."""
    if local is None:
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    name = f".aj-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    probe = appdata / name
    try:
        probe.open("x").close()
    except OSError:
        return None  # nothing can be written there anyway
    try:
        found = next((local / "Packages").glob(f"*/LocalCache/Roaming/{name}"), None)
        return found.parent if found is not None else None
    except OSError:
        return None
    finally:
        probe.unlink(missing_ok=True)


_APPDATA = Path(os.environ.get("APPDATA", Path.home()))
REDIRECTED_TO = appdata_redirected(_APPDATA)  # the private copy this process's writes go to, if any
DATA_DIR = _APPDATA / ("AJ" if REDIRECTED_TO is None else "AJ-sandbox")
SETTINGS_PATH = DATA_DIR / "settings.toml"
DB_PATH = DATA_DIR / "aj.db"
CLAP_PATH = DATA_DIR / "clap.json"  # the user's recorded clap (scripts/record_clap.py, core/clap.py)
LOG_DIR = DATA_DIR / "logs"


@dataclass
class LlmSettings:
    server: str = str(MODELS_DIR / "llama.cpp" / "llama-server.exe")
    # The same llama.cpp release built for Vulkan (scripts\fetch_models.ps1 -Vulkan): it reaches the
    # Radeon 780M, which the CUDA build can't (docs/HARDWARE_AWARE_INFERENCE_PLAN.md).
    vulkan_server: str = str(MODELS_DIR / "llama.cpp-vulkan" / "llama-server.exe")
    # Where the model runs: "cuda" (the RTX 4050, as always until 2026-10-27), "vulkan" (with
    # `device` naming the GPU, e.g. "Vulkan0") or "cpu". [placement] mode = "auto" chooses these
    # itself, by the power cable and what's running (core/placement.py).
    backend: str = "cuda"
    device: str = ""  # llama-server --device; "" = the build's own choice
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
    # "piper": the default from 2026-10-21, the user's pick by ear of the Piper samples (voice 2,
    # northern English male). On the CPU: no graphics memory, ready in under 2 s.
    # "kokoro": the Kokoro voices below (Daniel), the default 2026-10-06 to 2026-10-21. Its model
    # and the CUDA libraries it ran on were removed at the user's request (2026-10-21); the code
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
    piper_voice: str = "vits-piper-en_GB-northern_english_male-medium"  # a folder in piper_dir (the user's pick)
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
    # The pause that ends your turn. 0.3 s until 2026-10-23, when the user said he had to speak
    # carefully to be understood: a normal pause mid-sentence ended his turn, and the rest came
    # as a turn of its own ("my battery presentation right now.", "you to send it to to contact
    # APPA in WhatsApp."). It costs that much more wait before every answer.
    end_silence_secs: float = 0.5
    # Names Parakeet should listen for (core/speech.py), because it mishears them. Measured
    # 2026-10-23 on noisy test clips: "Message Appa" was heard "Message Apple", "Go to Appa" "Go to
    # Atlas", "Open Claude" "Open code", "AJ" "A J"; with these, all four were right, and no common
    # word turned into one ("open code", "the cloud folder", "a wonderful world" stayed). Ordinary
    # words don't belong here: "Quick Share" made "Quipshare". Empty: no hints, plain decoding.
    hotwords: list[str] = field(default_factory=lambda: ["AJ", "Appa", "Suhinish", "Claude", "WhatsApp", "Spotify"])
    hotwords_score: float = 2.0  # how hard: 1.0 changed little, 2.0 fixed the four above
    # Talk over him through the laptop speakers and he stops and listens (core/barge.py).
    interrupt: bool = True
    unfinished_wait_secs: float = 0.6  # extra wait when you trail off on "and", "the", ...


@dataclass
class DictationSettings:
    """"Write this down": what the user says next is typed where his cursor is (core/dictation.py).
    The user, 2026-10-22: "it has to get it accurately, exactly the way I want it", and "it can
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
    "Computer"), and only while the laptop is plugged in (the user, 2026-10-22)."""
    # Asleep, let go of the model too (llama-server stops), so the graphics card is free. Off by
    # default: waking then waits for the model to load again before it can answer.
    free_gpu: bool = False
    # Fall asleep after this many minutes with nothing answered. 0: never (the default).
    idle_mins: float = 0.0
    # Asleep, how often to look at the power cable: on battery the mic closes within this long.
    power_check_secs: float = 2.0
    # Two claps wake him while he's asleep (core/clap.py, the user, 2026-11-18). Like his name,
    # only while the mic is listening, so not on battery. With a recording of the user's own clap
    # (scripts/record_clap.py) only claps that sound like it count.
    clap: bool = True
    # How close to the recording a clap must sound: 1.0 as recorded, higher is stricter (fewer
    # false wakes, more missed claps), lower is looser.
    clap_strictness: float = 1.0


@dataclass
class PlacementSettings:
    """Where AJ's models run while he's on (core/placement.py; docs/HARDWARE_AWARE_INFERENCE_PLAN.md,
    Phase 2). Measured 2026-10-26/27: unplugged, this laptop switches the RTX 4050 off altogether and
    the model fell back to the CPU, where AJ's first prompt took 577.7 s to read and he answered
    nothing for minutes; on the Radeon 780M the same prompt took 24.4 s, and the model idled at
    14.5 W. The 780M's price is about 3.8 GB of system RAM."""
    # "fixed": always [llm] backend and device, as before 2026-10-27. "auto": the RTX 4050 when
    # plugged in, the 780M on battery (or when the RTX 4050 isn't there, or a GPU-heavy app is open).
    mode: str = "fixed"
    battery_unit: str = "780m"  # on battery: "780m" or "cpu"
    # A change (the cable, an app) must last this long before AJ moves: a loose cable doesn't
    # restart him. A server that dies is replaced at once, whatever this says.
    settle_secs: float = 30.0
    check_secs: float = 5.0     # how often he looks
    alternate_port: int = 8079  # the new server's port while he moves; the next goes back to [llm] port (8078 is the thinker's)
    # Apps that want the RTX 4050 to themselves (process or Start Menu names, e.g. "DaVinci Resolve"):
    # while one is open AJ runs on the 780M, plugged in or not. Empty: never.
    gpu_heavy_apps: list[str] = field(default_factory=list)
    announce: bool = False      # say it out loud when he moves ("Moving to the Radeon: on battery.")


@dataclass
class Settings:
    # What the assistant calls itself: the user's choice, "AJ" since 2026-10-22 (was "Computer",
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
    placement: PlacementSettings = field(default_factory=PlacementSettings)

    # Whose laptop he runs on (2026-11-18, the user: "a local assistant running on <my> laptop").
    # "" means the Windows account's name, so no one's name is written into the code.
    owner: str = ""

    def owner_name(self) -> str:
        return (self.owner or os.environ.get("USERNAME", "")).strip()

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
