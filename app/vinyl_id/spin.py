"""'Now spinning': which track of a pre-loaded album is playing, and when it started.

Pure logic, no I/O. Times are the app's monotonic clock in seconds. `start` is when the
current track began, or None when it is not known (the progress bar is then indeterminate
until the next gap between songs).
"""

from __future__ import annotations

from datetime import datetime, timezone

SIDE_FLIP_WINDOW = 300.0     # a restart this soon after a side ends = the next side
EXPIRE_AFTER = 1800.0        # stopped this long = the record is off the turntable
LENGTH_GRACE = 3.0           # seconds past a track's listed length before moving on
LATE_GAP = 15.0              # a gap this soon after moving on by length is that same song change
FLIP_GAP = 8.0               # quiet this long after a side's last track = the record was turned over


class Spin:
    def __init__(self, album_id: int, tracks: list[dict], i: int = 0, start: float | None = None,
                 auto: bool = False) -> None:
        self.album_id = album_id
        self.tracks = tracks
        self.i = i
        self.start = start
        self.auto = auto                 # attached by itself after Shazam, not picked by hand
        self.stopped_at: float | None = None
        self.at_side_end = False         # the last stop was at the end of a side
        self.mismatch: dict | None = None   # a song from outside this album was heard
        self.ticked_at: float | None = None  # when the listed length last moved it on
        self.overrun = False             # the track was heard past its listed length: wait for the gap
        self._wall: tuple[float | None, float] = (None, 0.0)   # (start, the same as wall-clock time)

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
        if self.start is None or self.overrun or not self.cur.get("length"):
            return False
        nxt = self.next_in_side()
        if nxt is not None and now - self.start >= self.cur["length"] + LENGTH_GRACE:
            self.start += self.cur["length"]
            self.i, self.ticked_at = nxt, now
            return True
        return False

    def late_gap(self, now: float) -> bool:
        """The listed length ran out a moment ago and moved on already: a gap heard now is that
        same change, heard late (the song ran longer than listed). Not a second one."""
        if self.ticked_at is None or self.start is None:
            return False
        return now - self.start <= min(LATE_GAP, 0.9 * (self.cur.get("length") or LATE_GAP))

    def track_change(self, now: float, dip: float = 0.0) -> bool:
        """A gap between songs was heard (`dip` = how long it was quiet)."""
        late = self.late_gap(now)
        self.ticked_at, self.overrun = None, False
        if late:
            self.start = now
            return True
        nxt = self.next_in_side()
        if nxt is None and dip >= FLIP_GAP:
            nxt = self.first_of_next_side()      # turned over faster than a stop is noticed
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
        self.ticked_at, self.overrun = None, False
        if gone > EXPIRE_AFTER:
            return "expired"
        nxt = self.first_of_next_side()
        ended, self.at_side_end = self.at_side_end, False
        if ended and nxt is None and self.auto:
            return "expired"                     # the last side was played out: whatever starts now is new
        if gone <= SIDE_FLIP_WINDOW and nxt is not None and ended:
            self.i, self.start = nxt, now
            return "flip"
        self.start = None
        return "resume"

    def stopped(self, now: float, side_end: bool = True) -> None:
        """side_end=False: the sound was lost, which says nothing about where the needle is."""
        self.at_side_end = side_end and self.next_in_side() is None
        self.stopped_at, self.start = now, None

    def jump(self, i: int, start: float | None) -> None:
        self.i, self.start = i, start
        self.ticked_at, self.overrun = None, False

    # --- for display ---------------------------------------------------------------
    def info(self, now: float, wall: float) -> dict:
        """Attributes for Home Assistant: no polling, the start time is enough to compute progress."""
        t = self.cur
        side_total = sum(1 for x in self.tracks if x["side"] == t["side"])
        return {
            "side": t["side"], "track_number": t["pos"], "side_tracks": side_total,
            "album_tracks": len(self.tracks), "track_length": t.get("length"),
            "track_started_at": self._started_wall(now, wall),
        }

    def _started_wall(self, now: float, wall: float) -> str | None:
        """Worked out once per start, so two clocks drifting never move it by a second mid-song."""
        if self.start is None:
            return None
        if self._wall[0] != self.start:
            self._wall = (self.start, wall - (now - self.start))
        return _iso(self._wall[1])


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
