"""Ultron's voice effects: a deep stock voice made lower, metallic and theatrical.

Applied to each finished sentence buffer (not a live stream; pedalboard's live
stream has a reported Windows crash), then played through sounddevice. The values
are starting points to tune by ear (docs/PLAN.md, "The voice").
"""
from dataclasses import dataclass

import numpy as np
from pedalboard import (Chorus, Compressor, Delay, Distortion, HighpassFilter, Limiter, LowShelfFilter,
                        Pedalboard, PeakFilter, PitchShift, Reverb)


@dataclass
class FxSettings:
    enabled: bool = True
    pitch_semitones: float = -2.0   # artifacts creep in past about -3
    ring_mod_hz: float = 60.0       # faint machine hum
    ring_mod_mix: float = 0.12
    chorus_mix: float = 0.25
    comb_mix: float = 0.15          # 12 ms slap = metallic comb
    drive_db: float = 3.0
    reverb_wet: float = 0.12
    low_shelf_db: float = 3.0       # chest weight
    presence_hz: float = 3000.0     # consonant clarity lives around 2.5-4.5 kHz
    presence_db: float = 2.0
    tail_ms: int = 300              # room for the reverb to ring out


# A clearer, more enunciated robot: half the pitch drop and much less of the effects that
# smear consonants (ring-mod, comb, drive), plus a stronger presence boost. The user found the
# full chain sounded robotic in a dull way ("lacking enunciation"), 2026-10-04.
CLEAR = FxSettings(pitch_semitones=-1.0, ring_mod_mix=0.05, chorus_mix=0.12, comb_mix=0.06, drive_db=1.0,
                   reverb_wet=0.08, low_shelf_db=2.0, presence_hz=3500.0, presence_db=4.0)


class VoiceFx:
    def __init__(self, settings: FxSettings | None = None):
        self.settings = s = settings or FxSettings()
        self._pre = Pedalboard([HighpassFilter(cutoff_frequency_hz=70), PitchShift(semitones=s.pitch_semitones)])
        self._post = Pedalboard([
            Chorus(rate_hz=0.3, depth=0.25, centre_delay_ms=7.0, mix=s.chorus_mix),
            Delay(delay_seconds=0.012, feedback=0.25, mix=s.comb_mix),
            Distortion(drive_db=s.drive_db),
            LowShelfFilter(cutoff_frequency_hz=180, gain_db=s.low_shelf_db),
            PeakFilter(cutoff_frequency_hz=s.presence_hz, gain_db=s.presence_db, q=1.0),
            Reverb(room_size=0.35, damping=0.6, wet_level=s.reverb_wet, dry_level=0.9),
            Compressor(threshold_db=-18, ratio=3),
            Limiter(threshold_db=-1),
        ])

    def apply(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Process one mono float32 sentence and return it with the effect tail added."""
        s = self.settings
        if not s.enabled:
            return audio.astype(np.float32, copy=False)
        tail = np.zeros(int(sample_rate * s.tail_ms / 1000), dtype=np.float32)
        x = np.concatenate([audio.astype(np.float32, copy=False), tail])
        x = self._pre(x, sample_rate)
        if x.ndim > 1:
            x = x[0]
        t = np.arange(x.shape[-1], dtype=np.float32) / sample_rate
        x = (1 - s.ring_mod_mix) * x + s.ring_mod_mix * x * np.sin(2 * np.pi * s.ring_mod_hz * t)
        x = self._post(x.astype(np.float32), sample_rate)
        return (x[0] if x.ndim > 1 else x).astype(np.float32)
