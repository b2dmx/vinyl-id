"""Turns audio levels into events: record started, track changed, record stopped."""

from __future__ import annotations

import logging
from collections import deque
from enum import Enum
from statistics import median

from .config import BLOCK_SECONDS, Settings

log = logging.getLogger(__name__)
LOG_DIPS_FROM = 1.0   # dips at least this long are logged, so the gap settings can be tuned


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
        self._dip = 0.0        # seconds of the current dip
        self._dip_lo = self._dip_hi = 0.0   # quietest and loudest block of the current dip
        self._recent: deque[float] = deque(maxlen=int(20 / BLOCK_SECONDS))  # ~20 s of song levels

    @property
    def in_dip(self) -> bool:
        """Quiet right now: a gap between songs, a soft passage, or the end of the side."""
        return self._dip > 0

    def feed(self, db: float) -> Event | None:
        s, dt = self.s, BLOCK_SECONDS
        has_signal = db >= s.signal_threshold_db

        if not self.playing:
            self._loud = self._loud + dt if has_signal else 0.0
            if self._loud >= s.start_seconds:
                self.playing, self._quiet, self._dip = True, 0.0, 0.0
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
            if self._dip == 0.0:
                self._dip_lo = self._dip_hi = db
            self._dip += dt
            self._dip_lo, self._dip_hi = min(self._dip_lo, db), max(self._dip_hi, db)
            return None

        dip, self._dip = self._dip, 0.0
        self._recent.append(db)
        if dip == 0.0:
            return None
        # Music is back. Quiet passages in soft music dip too, so a gap has to be long enough.
        is_gap = dip >= s.gap_seconds
        if dip >= LOG_DIPS_FROM:
            log.info("Dip %.1f s, %.0f to %.0f dB (song %s dB): %s", dip, self._dip_lo, self._dip_hi,
                     "?" if song_level is None else f"{song_level:.0f}",
                     "track change" if is_gap else "too short")
        return Event.TRACK_CHANGE if is_gap else None
