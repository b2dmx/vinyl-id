"""Turns audio levels into events: record started, track changed, record stopped."""

from __future__ import annotations

from collections import deque
from enum import Enum
from statistics import median

from .config import BLOCK_SECONDS, Settings


class Event(Enum):
    STARTED = "started"
    TRACK_CHANGE = "track_change"
    STOPPED = "stopped"


class Detector:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.playing = False
        self._loud = 0.0       # seconds of continuous signal (while idle)
        self._quiet = 0.0      # seconds below threshold (while playing)
        self._dip = 0.0        # seconds of a between-songs dip
        self._in_gap = False
        self._recent: deque[float] = deque(maxlen=int(20 / BLOCK_SECONDS))  # ~20 s of song levels

    def feed(self, db: float) -> Event | None:
        s, dt = self.s, BLOCK_SECONDS
        has_signal = db >= s.signal_threshold_db

        if not self.playing:
            self._loud = self._loud + dt if has_signal else 0.0
            if self._loud >= s.start_seconds:
                self.playing, self._quiet, self._dip, self._in_gap = True, 0.0, 0.0, False
                self._recent.clear()
                return Event.STARTED
            return None

        # Playing: long silence means the record stopped.
        self._quiet = 0.0 if has_signal else self._quiet + dt
        if self._quiet >= s.stop_seconds:
            self.playing, self._loud = False, 0.0
            return Event.STOPPED

        # Between-songs dip: well below the song's typical level (or fully quiet).
        song_level = median(self._recent) if len(self._recent) >= 8 else None
        dipping = not has_signal or (song_level is not None and db < song_level - s.gap_drop_db)

        if dipping:
            self._dip += dt
            if self._dip >= s.gap_seconds:
                self._in_gap = True
            return None

        self._dip = 0.0
        self._recent.append(db)
        if self._in_gap:  # music is back after a real gap
            self._in_gap = False
            return Event.TRACK_CHANGE
        return None
