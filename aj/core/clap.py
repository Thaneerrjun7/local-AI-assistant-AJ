"""Two claps wake AJ from sleep mode (2026-11-18, the user: "I clap twice, and AJ pops up, says
hello, sir", and only while he's asleep in the tray). With a recording of the user's own claps
(scripts/record_clap.py), only claps that sound like his count: "record my clap and only open to
its exact sound".

The pipeline hands every mic block to ClapDetector.feed() while AJ is asleep and not answering;
feed() returns True on the block where a double clap is complete. No speech model is involved:
it's a few NumPy operations per 10 ms of audio.

What counts as a clap (each rule keeps a different sound out):
- A sharp attack: the sound is well above the room's own level (RISE_DB over the noise floor)
  and jumps there within one 10 ms frame (ATTACK_DB over the frame before). Speech and music
  mostly swell; a clap is there at once.
- Short: back within DECAY_DB of its peak within MAX_CLAP_SECS. A vowel, a song note or a
  blender lasts longer.
- Bright: the spectrum's centre of mass is at least MIN_CENTROID_HZ. A door slam, a footstep or a
  fist on the desk is mostly low frequencies; a clap is broadband.
- With a recording: its spectral shape (band levels relative to each other, so how loud it is
  doesn't matter) is within the recording's own spread of the user's average clap.

What counts as a double clap: two claps between the recorded (or default) gap apart, nothing
loud for QUIET_BEFORE_SECS before the first, and nothing for QUIET_AFTER_SECS after the second.
A third clap, or any loud sound in between, cancels it: applause, a beat in a song and someone
hammering don't wake him.

It can still be fooled by a sound that is short, sharp and bright twice in a row (two book slaps,
say): rare, and all it costs is AJ saying hello. Another person's claps may match the recording
too; it narrows things down, it is not a lock.
"""
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

RATE = 16000
FRAME = 160                 # 10 ms
WINDOW = 512                # the samples a clap's spectrum is taken from (32 ms from its start)
RISE_DB = 18.0              # above the noise floor
ATTACK_DB = 9.0             # above the frame before
DECAY_DB = 20.0             # "over" once this far below its peak
MAX_CLAP_SECS = 0.16
MIN_PEAK = 0.02             # the loudest sample, as the mic gives it (-1..1): a clap across the room
MIN_CENTROID_HZ = 1000.0
MIN_GAP_SECS, MAX_GAP_SECS = 0.12, 0.9   # between the two claps, without a recording
QUIET_BEFORE_SECS = 0.5
QUIET_AFTER_SECS = 0.45
FLOOR_FLOOR = 1e-8          # the quietest the noise floor is allowed to read (a muted mic)
FLOOR_ADAPT = 0.02          # how fast the floor follows the room (per quiet frame)
# 10 bands, log-spaced: the shape compared with the recording. Narrower low bands held only a
# couple of FFT bins each and jumped about from clap to clap.
BANDS = np.geomspace(400.0, 7800.0, 11)
MIN_TOLERANCE_DB = 3.0      # a recording with very even claps still allows this much difference
TOLERANCE_MARGIN = 1.6      # times the recording's own widest spread
GAP_MARGIN = 0.4            # the recorded gaps, widened by this fraction each way


@dataclass(frozen=True)
class Clap:
    at: float          # seconds, on the detector's own clock (samples fed / RATE)
    peak: float
    secs: float
    centroid: float
    shape: tuple[float, ...]


@dataclass
class Template:
    """The user's clap, from scripts/record_clap.py: its average spectral shape, how far his own
    claps strayed from it, and the gaps he leaves between two claps."""
    shape: list[float]
    tolerance_db: float
    gap: tuple[float, float]
    takes: int = 0
    centroid: float = 0.0

    def distance(self, clap: Clap) -> float:
        """How different the clap's spectral shape is, in dB (RMS over the bands)."""
        diff = np.asarray(clap.shape) - np.asarray(self.shape)
        return float(np.sqrt(np.mean(diff ** 2)))

    def to_json(self) -> dict:
        data = asdict(self)
        data["gap"] = list(self.gap)
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Template":
        shape = [float(v) for v in data["shape"]]
        if len(shape) != len(BANDS) - 1:
            raise ValueError("a clap recording from a different version")
        lo, hi = (float(v) for v in data["gap"])
        return cls(shape, float(data["tolerance_db"]), (lo, hi), int(data.get("takes", 0)),
                   float(data.get("centroid", 0.0)))


def spectrum_shape(samples: np.ndarray) -> tuple[float, tuple[float, ...]]:
    """(the centre of mass of the spectrum in Hz, the band levels in dB relative to their mean)."""
    window = np.zeros(WINDOW, dtype=np.float64)
    window[:min(WINDOW, len(samples))] = samples[:WINDOW]
    power = np.abs(np.fft.rfft(window * np.hanning(WINDOW))) ** 2
    freqs = np.fft.rfftfreq(WINDOW, 1.0 / RATE)
    total = float(power.sum()) or 1e-20
    centroid = float((freqs * power).sum() / total)
    levels = []
    for lo, hi in zip(BANDS[:-1], BANDS[1:]):
        band = power[(freqs >= lo) & (freqs < hi)]
        levels.append(10.0 * np.log10(float(band.sum()) + 1e-20))
    levels = np.asarray(levels)
    return centroid, tuple(float(v) for v in levels - levels.mean())


@dataclass
class _Sound:
    """A loud onset being followed until it's over."""
    at: float
    start: int                  # sample index where it began
    peak_energy: float
    frames: int = 0
    peak: float = 0.0
    samples: list[np.ndarray] = field(default_factory=list)


class ClapDetector:
    """Fed 16 kHz mono float32 blocks of any size; feed() is True when a double clap completes.
    `template`: the user's recorded clap (None: any clap that passes the general rules).
    `strictness`: 1.0 as recorded; above 1 tighter, below 1 looser."""

    def __init__(self, template: Template | None = None, strictness: float = 1.0):
        self.template = template
        self.strictness = max(0.25, float(strictness))
        self.reset()

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        self._history = deque(maxlen=4)  # the last few frames, for the start of a clap's window
        self._samples = 0                # fed so far: the detector's clock
        self._floor: float | None = None
        self._prev = FLOOR_FLOOR
        self._sound: _Sound | None = None
        self._first: Clap | None = None
        self._second: Clap | None = None
        self._last_loud = -1e9           # when the last loud sound that wasn't part of a pattern began
        self.heard: list[Clap] = []      # every clap, for scripts/record_clap.py and the tests
        self.rejected: list[tuple[Clap, str]] = []  # claps that didn't count, and why

    @property
    def now(self) -> float:
        return self._samples / RATE

    @property
    def gap(self) -> tuple[float, float]:
        if self.template is None:
            return MIN_GAP_SECS, MAX_GAP_SECS
        lo, hi = self.template.gap
        return max(0.08, lo * (1 - GAP_MARGIN)), hi * (1 + GAP_MARGIN)

    def feed(self, block: np.ndarray) -> bool:
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        self._buf = np.concatenate([self._buf, block]) if len(self._buf) else block
        fired = False
        while len(self._buf) >= FRAME:
            frame, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            fired = self._frame(frame) or fired
        return fired

    # ----- one 10 ms frame -----

    def _frame(self, frame: np.ndarray) -> bool:
        at = self.now
        self._samples += FRAME
        energy = float(np.mean(frame.astype(np.float64) ** 2)) + FLOOR_FLOOR
        if self._floor is None:
            self._floor = max(energy, FLOOR_FLOOR)
        fired = False
        if self._sound is not None:
            self._follow(frame, energy)
        elif energy > self._floor * _db(RISE_DB) and float(np.abs(frame).max()) >= MIN_PEAK:
            sharp = energy > self._prev * _db(ATTACK_DB)
            start = self._samples - FRAME
            back = list(self._history)[-1:] if sharp else []  # a few ms before, so the attack is in the window
            self._sound = _Sound(at=at, start=start, peak_energy=energy, frames=1,
                                 peak=float(np.abs(frame).max()), samples=[*back, frame.copy()])
            if not sharp:
                self._loud(at)
                self._sound = _Sound(at=at, start=start, peak_energy=energy, frames=1, peak=0.0)  # swallow it
        else:
            self._floor += FLOOR_ADAPT * (max(energy, FLOOR_FLOOR) - self._floor)
        self._prev = energy
        self._history.append(frame.copy())
        return self._check_pattern(at) or fired

    def _follow(self, frame: np.ndarray, energy: float) -> None:
        sound = self._sound
        sound.frames += 1
        if sound.peak:  # a clap candidate: keep its audio for the spectrum
            sound.peak = max(sound.peak, float(np.abs(frame).max()))
            if sum(len(s) for s in sound.samples) < WINDOW:
                sound.samples.append(frame.copy())
        sound.peak_energy = max(sound.peak_energy, energy)
        over = energy < sound.peak_energy / _db(DECAY_DB) or energy < self._floor * _db(RISE_DB / 2)
        too_long = sound.frames * FRAME / RATE > MAX_CLAP_SECS
        if not over and not too_long:
            return
        self._sound = None
        if not sound.peak:
            return  # a soft-onset sound, already counted as loud
        if too_long and not over:
            self._loud(sound.at)  # a long sound: a vowel, a note, a crash
            self._sound = _Sound(at=sound.at, start=sound.start, peak_energy=sound.peak_energy,
                                 frames=sound.frames, peak=0.0)  # follow it to its end, as noise
            return
        samples = np.concatenate(sound.samples)
        centroid, shape = spectrum_shape(samples)
        clap = Clap(at=sound.at, peak=sound.peak, secs=sound.frames * FRAME / RATE, centroid=centroid, shape=shape)
        why = self._not_a_clap(clap)
        if why:
            self.rejected.append((clap, why))
            self._loud(clap.at)
            return
        self.heard.append(clap)
        self._clap(clap)

    def _not_a_clap(self, clap: Clap) -> str:
        if clap.centroid < MIN_CENTROID_HZ:
            return f"too dull ({clap.centroid:.0f} Hz)"
        if self.template is not None:
            allowed = self.template.tolerance_db / self.strictness
            distance = self.template.distance(clap)
            if distance > allowed:
                return f"doesn't sound like your clap ({distance:.1f} dB off, {allowed:.1f} allowed)"
        return ""

    # ----- the pattern -----

    def _loud(self, at: float) -> None:
        """A loud sound that isn't a clap (or a clap that broke the pattern): no double clap
        can use the claps before it, and the next one needs quiet before it."""
        self._first = self._second = None
        self._last_loud = at

    def _clap(self, clap: Clap) -> None:
        lo, hi = self.gap
        if self._second is not None:  # a third: applause, a rhythm
            self._loud(clap.at)
            return
        if self._first is not None:
            gap = clap.at - self._first.at
            if lo <= gap <= hi:
                self._second = clap
                return
            if gap < lo:
                self._loud(clap.at)  # too fast: a rattle, a drum roll
                return
            self._first = None  # too slow: this one may start a new pair
        if clap.at - self._last_loud >= QUIET_BEFORE_SECS:
            self._first = clap
        else:
            self._last_loud = clap.at

    def _check_pattern(self, now: float) -> bool:
        if self._second is not None and self._sound is None and now - self._second.at >= QUIET_AFTER_SECS:
            self._first = self._second = None
            self._last_loud = now  # a fresh pair needs quiet before it, like any other
            return True
        lo, hi = self.gap
        if self._first is not None and self._second is None and now - self._first.at > hi:
            self._first = None  # one clap alone
        return False


def _db(db: float) -> float:
    return 10.0 ** (db / 10.0)


# ----- learning the user's clap -----

class NotLearned(Exception):
    """The recordings didn't give a usable clap: the message says why, in plain words."""


def learn(takes: list[np.ndarray]) -> Template:
    """A Template from recordings of the user clapping twice (one double clap per recording)."""
    claps: list[Clap] = []
    gaps: list[float] = []
    bad = 0
    for audio in takes:
        found = _claps_in(audio)
        if len(found) != 2:
            bad += 1
            continue
        claps += found
        gaps.append(found[1].at - found[0].at)
    if len(gaps) < 3:
        raise NotLearned(f"Only {len(gaps)} of {len(takes)} takes had exactly two clear claps; at least 3 are "
                         f"needed. Clap twice, sharply, with a short pause, in a quiet room.")
    shapes = np.asarray([c.shape for c in claps])
    mean = shapes.mean(axis=0)
    off = np.sqrt(np.mean((shapes - mean) ** 2, axis=1))
    # An odd one out (a clap that glanced, a cough on top) would widen the tolerance for every
    # other sound in the room: claps far further out than the typical one are left out.
    keep = off <= 2.0 * float(np.median(off)) if len(off) >= 6 else np.ones(len(off), dtype=bool)
    mean = shapes[keep].mean(axis=0)
    spread = float(np.sqrt(np.mean((shapes[keep] - mean) ** 2, axis=1)).max())
    return Template(shape=[float(v) for v in mean], tolerance_db=max(MIN_TOLERANCE_DB, spread * TOLERANCE_MARGIN),
                    gap=(min(gaps), max(gaps)), takes=len(gaps),
                    centroid=float(np.mean([c.centroid for c in claps])))


def _claps_in(audio: np.ndarray) -> list[Clap]:
    """Every clap the general rules accept in a recording (no pattern, no recording to match)."""
    detector = ClapDetector()
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    for i in range(0, len(audio), 512):
        detector.feed(audio[i:i + 512])
    detector.feed(np.zeros(int(RATE * 0.3), dtype=np.float32))  # let the last one finish
    return detector.heard


def load_template(path: Path) -> Template | None:
    """The saved recording, or None when there's none (or it's unreadable: the general rules then)."""
    from aj.core.safe_store import read_json
    try:
        data = read_json(path, None) if path.exists() else None
        return Template.from_json(data) if data else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_template(path: Path, template: Template) -> None:
    from aj.core.safe_store import write_json
    write_json(path, template.to_json())
