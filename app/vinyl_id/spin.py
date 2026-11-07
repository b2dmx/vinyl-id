"""'Now spinning': which track of a pre-loaded album is playing, and when it started.

Pure logic, no I/O. Times are the app's monotonic clock in seconds. `start` is when the
current track began, or None when it is not known (the progress bar is then indeterminate
until the next gap between songs).
"""

from __future__ import annotations

SIDE_FLIP_WINDOW = 300.0     # a restart this soon after a side ends = the next side
EXPIRE_AFTER = 1800.0        # stopped this long = the record is off the turntable
LENGTH_GRACE = 3.0           # seconds past a track's listed length before moving on


class Spin:
    def __init__(self, album_id: int, tracks: list[dict], i: int = 0, start: float | None = None,
                 auto: bool = False) -> None:
        self.album_id = album_id
        self.tracks = tracks
        self.i = i
        self.start = start
        self.auto = auto                 # attached by itself after Shazam, not picked by hand
        self.stopped_at: float | None = None
        self.mismatch: dict | None = None   # a song from outside this album was heard

    @property
    def cur(self) -> dict:
        return self.tracks[self.i]

    def _same_side(self, i: int) -> bool:
        return self.tracks[i]["side"] == self.cur["side"]

    def next_in_side(self) -> int | None:
        j = self.i + 1
        return j if j < len(self.tracks) and self._same_side(j) else None

    def first_of_next_side(self) -> int | None:
        for j in range(self.i + 1, len(self.tracks)):
            if not self._same_side(j):
                return j
        return None

    # --- events ------------------------------------------------------------------
    def tick(self, now: float) -> bool:
        """Songs that run together: move on once the listed length has passed."""
        if self.start is None or not self.cur.get("length"):
            return False
        nxt = self.next_in_side()
        if nxt is not None and now - self.start >= self.cur["length"] + LENGTH_GRACE:
            self.start += self.cur["length"]
            self.i = nxt
            return True
        return False

    def track_change(self, now: float) -> bool:
        """A gap between songs was heard."""
        nxt = self.next_in_side()
        if nxt is None:
            return False
        self.i, self.start = nxt, now
        return True

    def started(self, now: float) -> str:
        """The record started playing: 'expired', 'flip' (next side), 'resume' or 'new'."""
        if self.stopped_at is None:
            self.start = now
            return "new"
        gone, self.stopped_at = now - self.stopped_at, None
        if gone > EXPIRE_AFTER:
            return "expired"
        nxt = self.first_of_next_side()
        if gone <= SIDE_FLIP_WINDOW and nxt is not None and self.at_side_end:
            self.i, self.start = nxt, now
            return "flip"
        self.start = None
        return "resume"

    def stopped(self, now: float) -> None:
        self.at_side_end = self.next_in_side() is None
        self.stopped_at, self.start = now, None

    at_side_end = False

    def jump(self, i: int, start: float | None) -> None:
        self.i, self.start = i, start

    # --- for display ---------------------------------------------------------------
    def info(self, now: float, wall: float) -> dict:
        """Attributes for Home Assistant: no polling, the start time is enough to compute progress."""
        t = self.cur
        side_total = sum(1 for x in self.tracks if x["side"] == t["side"])
        return {
            "side": t["side"], "track_number": t["pos"], "side_tracks": side_total,
            "album_tracks": len(self.tracks), "track_length": t.get("length"),
            "track_started_at": None if self.start is None else
            _iso(wall - (now - self.start)),
        }


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
