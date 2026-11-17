"""How the orb follows a voice: like a meter's needle, up fast and down a little slower, so it
moves with syllables without jittering and settles in the gaps. No Qt here, so it's testable."""
import math

ATTACK_SECS = 0.015   # how fast it rises to a louder level
RELEASE_SECS = 0.12   # how fast it falls back
QUIET = 0.003         # below this, with silence coming in, it is silence: the orb comes to rest


class Follower:
    def __init__(self, attack: float = ATTACK_SECS, release: float = RELEASE_SECS):
        self.attack, self.release = attack, release
        self.value = 0.0

    def step(self, target: float, dt: float) -> float:
        tau = self.attack if target > self.value else self.release
        self.value += (target - self.value) * (1 - math.exp(-max(dt, 0.0) / tau))
        if target <= 0.0 and self.value < QUIET:
            self.value = 0.0
        return self.value

    def reset(self) -> None:
        self.value = 0.0
