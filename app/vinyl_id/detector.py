"""Turns audio levels into events: record started, track changed, record stopped.

Three levels, all relative to the signal threshold (the level of the idle turntable's
hum and hiss, plus a little):
  below threshold + gap_ceiling   quiet: a gap between songs, or the end of the side
  above threshold + start_margin  music: starts a record
  in between                      a soft passage: keeps a record going, starts nothing
"""

from __future__ import annotations

import logging
from enum import Enum

from .config import BLOCK_SECONDS, Settings

log = logging.getLogger(__name__)
LOG_DIPS_FROM = 1.0     # dips at least this long are logged, so the settings can be tuned
DIGITAL_SILENCE = -88   # a record's groove is never this quiet: the stream itself dropped out
POP_BLOCKS = 2          # this many loud blocks in a row (0.5 s) inside quiet are a click or pop, not music


class Event(Enum):
    STARTED = "started"
    TRACK_CHANGE = "track_change"
    STOPPED = "stopped"


class Detector:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.playing = False
        self._loud = 0.0       # seconds of continuous music (while idle)
        self._quiet = 0.0      # seconds of continuous quiet (while playing)
        self._dip = 0.0        # seconds of the current dip
        self._dip_lo = self._dip_hi = 0.0   # quietest and loudest block of the current dip
        self._pop = 0          # loud blocks in a row since the last quiet one
        # Over a network receiver, lost audio arrives as exact silence. That is never a gap.
        self._dropouts = s.audio_input_format == "pulse"

    @property
    def in_dip(self) -> bool:
        """Quiet right now: a gap between songs, or the side has ended."""
        return self._dip > 0

    @property
    def start_level(self) -> float:
        """How loud the sound must be for a record to start."""
        return self.s.signal_threshold_db + self.s.start_margin_db

    def input_lost(self) -> bool:
        """The audio input went away. Returns whether a record was playing until then."""
        was, self.playing = self.playing, False
        self._loud = self._quiet = self._dip = 0.0
        return was

    def feed(self, db: float) -> Event | None:
        s, dt = self.s, BLOCK_SECONDS
        quiet = db < s.signal_threshold_db + s.gap_ceiling_db

        if not self.playing:
            self._loud = self._loud + dt if db >= self.start_level else 0.0
            if self._loud >= s.start_seconds:
                self.playing, self._quiet, self._dip = True, 0.0, 0.0
                return Event.STARTED
            return None

        # A click or pop in a gap or in the run-out groove: it neither ends the gap nor keeps the record going
        self._pop = 0 if quiet else self._pop + 1
        pop = not quiet and self._pop <= POP_BLOCKS and self._quiet > 0

        # Playing: nothing but quiet for a long time means the record stopped.
        self._quiet = self._quiet + dt if quiet or pop else 0.0
        if self._quiet >= s.stop_seconds:
            self.playing, self._loud, self._dip = False, 0.0, 0.0
            return Event.STOPPED
        if pop:
            return None

        if quiet:
            if self._dip == 0.0:
                self._dip_lo = self._dip_hi = db
            self._dip += dt
            self._dip_lo, self._dip_hi = min(self._dip_lo, db), max(self._dip_hi, db)
            return None

        dip, self._dip = self._dip, 0.0
        if dip == 0.0:
            return None
        # Sound is back. A gap is long enough, and was the groove's own quiet, not lost audio.
        dropout = self._dropouts and self._dip_hi <= DIGITAL_SILENCE
        is_gap = dip >= s.gap_seconds and not dropout
        if dip >= LOG_DIPS_FROM:
            log.info("Dip %.1f s, %.0f to %.0f dB: %s", dip, self._dip_lo, self._dip_hi,
                     "track change" if is_gap else "lost audio, not a gap" if dropout else "too short")
        return Event.TRACK_CHANGE if is_gap else None
