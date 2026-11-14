"""Finds the quiet level (the signal threshold) by itself.

The quiet level is how loud the input is with the turntable on and no record playing: its
hum and hiss. That sound is steady, which music is not. So a stretch of steady sound is
taken as the quiet level in three cases only:

  first time   nothing is playing and no level was ever set: whatever steady level is heard
  louder hum   nothing is playing and the steady level is clearly above the one in use
  false start  a "record" has been flat from its first second and nothing was recognized:
               it is hum that is louder than the level in use, not a record

It never lowers a level by itself once one is set (a turntable switched off must not make
the hum of switching it on again look like a record). The button on the page sets it both ways.
"""

from __future__ import annotations

from collections import deque

from .config import BLOCK_SECONDS

WINDOW = int(30 / BLOCK_SECONDS)   # blocks of sound looked at
SPREAD = 4.0                       # dB between the 10th and 90th percentile that still counts as steady
CEILING = -40.0                    # a steady sound louder than this is never taken for hum
RISE = 3.0                         # "clearly above": this many dB over the level in use
NO_INPUT = -88.0                   # at or below: nothing is arriving at all
EVERY = 8                          # look once per this many blocks (2 s)


class Calibrator:
    def __init__(self, threshold: float, calibrated: bool) -> None:
        self.threshold = threshold     # the quiet level in use
        self.calibrated = calibrated   # one was set before (by this, or with the button)
        self._win: deque[float] = deque(maxlen=WINDOW)
        self._was_playing = False
        self._since_start = 0          # blocks since the record "started"
        self._loud = 0                 # of those, how many stood out above the rest
        self._n = 0

    def _steady(self) -> float | None:
        """The level of the last 30 s if it was steady, quiet enough to be hum, and real input."""
        if len(self._win) < WINDOW:
            return None
        levels = sorted(self._win)
        low, mid, high = levels[WINDOW // 10], levels[WINDOW // 2], levels[WINDOW - 1 - WINDOW // 10]
        return mid if high - low <= SPREAD and NO_INPUT < low and mid <= CEILING else None

    def feed(self, db: float, playing: bool, recognized: bool) -> float | None:
        """Give it every block. Returns a new quiet level to use, or None."""
        if playing != self._was_playing:           # a start or a stop: what was heard before is another matter
            self._was_playing = playing
            self._win.clear()
            self._since_start = self._loud = 0
        if db <= NO_INPUT:
            self._win.clear()
            return None
        self._win.append(db)
        self._n += 1
        if playing:
            self._since_start += 1
            if recognized:
                self._loud = WINDOW                # a recognized song is a record, whatever its level does
            elif len(self._win) >= 8 and db > sorted(self._win)[len(self._win) // 2] + 6:
                self._loud += 1
        if self._n % EVERY:
            return None
        level = self._steady()
        if level is None:
            return None
        if playing:
            flat_from_the_start = self._since_start <= 3 * WINDOW and self._loud <= 2
            return round(level) if flat_from_the_start and not recognized else None
        if not self.calibrated or level >= self.threshold + RISE:
            return round(level)
        return None
