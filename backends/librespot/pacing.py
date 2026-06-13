"""Adaptive inter-track pacing for librespot audio-key requests.

AIMD (additive-increase, multiplicative-decrease): fast while Spotify allows it,
10s floor once throttled, decay back when clean. ``LibrespotBackend`` owns a
``KeyThrottlePacer`` instance per run (``max_concurrency = 1``), so the lock is
defensive rather than load-bearing.
"""

from __future__ import annotations

import random
import threading

THROTTLE_FLOOR_S = 10.0
ESCALATE_FACTOR = 1.5
FLOOR_CAP_S = 30.0
DECAY_FACTOR = 0.5
MIN_FLOOR_S = 1.0


class KeyThrottlePacer:
    """AIMD inter-track delay: floor + base jitter."""

    def __init__(self, base_jitter_s: tuple[float, float] = (0.4, 1.3)):
        self._base_jitter_s = base_jitter_s
        self._floor_s = 0.0
        self._lock = threading.Lock()

    @property
    def is_elevated(self) -> bool:
        with self._lock:
            return self._floor_s > 0

    def next_delay(self) -> float:
        with self._lock:
            floor = self._floor_s
        lo, hi = self._base_jitter_s
        return floor + random.uniform(lo, hi)

    def note_throttle(self) -> float:
        with self._lock:
            if self._floor_s < THROTTLE_FLOOR_S:
                self._floor_s = THROTTLE_FLOOR_S
            else:
                self._floor_s = min(self._floor_s * ESCALATE_FACTOR, FLOOR_CAP_S)
            return self._floor_s

    def note_success(self) -> float:
        with self._lock:
            self._floor_s *= DECAY_FACTOR
            if self._floor_s < MIN_FLOOR_S:
                self._floor_s = 0.0
            return self._floor_s
