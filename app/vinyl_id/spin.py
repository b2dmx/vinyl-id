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
END_WINDOW = 15.0            # a gap this close to a track's listed end (or within 8% of its length) is its end
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
        # After moving on a little before or after a track's real end: until this time, a gap
        # that is heard is still that same song change, not another one.
        self.same_until: float | None = None
        self.seen: set[int] = set()      # the tracks recognition has confirmed since this album was picked
        self.anchored = False            # `start` was heard (needle drop, or the gap before this track)
        self.overrun = False             # the track was heard past its listed length: wait for the gap
        self.guessed = False             # on this track only because a long quiet looked like a flip
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
            self.i, self.same_until, self.anchored = nxt, now + LATE_GAP, False
            return True
        return False

    @property
    def trusted(self) -> bool:
        """Recognition has placed two different tracks on this album: it is the right record."""
        return len(self.seen) >= 2

    def late_gap(self, now: float) -> bool:
        """The album moved on a moment ago already (the listed length ran out, or a gap came
        just before the track's end): a gap heard now is that same change, not a second one."""
        if self.same_until is None or self.start is None or now > self.same_until:
            return False
        return now - self.start <= 0.9 * (self.cur.get("length") or LATE_GAP)

    def early_gap(self, now: float) -> float:
        """How many seconds too early a gap heard now is, going by the track's length (0 = it
        fits). A quiet passage in the middle of a song sounds exactly like a gap between two;
        what tells them apart is that the song is not nearly over yet."""
        length = self.cur.get("length")
        if self.start is None or not length or self.overrun or self.late_gap(now):
            return 0.0
        left = length - (now - self.start)
        return left if left > max(END_WINDOW, 0.08 * length) else 0.0

    def flip_held(self, now: float) -> bool:
        """A flip guessed from a long quiet: has this track now run about as long as it should?
        Then the guess was right (a quiet stretch inside a song would have ended it far too soon)."""
        length = self.cur.get("length")
        return bool(self.guessed and self.start is not None and length
                    and 0.5 * length <= now - self.start <= 1.5 * length + 5)

    def track_change(self, now: float, dip: float = 0.0) -> bool:
        """A gap between songs was heard (`dip` = how long it was quiet)."""
        late = self.late_gap(now)
        self.overrun, self.guessed = False, False
        if late:
            self.start, self.anchored = now, True
            return True
        # Moving on before the listed end: until that end has passed, another gap is this same change
        length = self.cur.get("length")
        ends = self.start + length if self.start is not None and length else None
        self.same_until = ends + 5 if ends is not None and now < ends else None
        nxt = self.next_in_side()
        if nxt is None and dip >= FLIP_GAP:
            nxt = self.first_of_next_side()      # turned over faster than a stop is noticed
            self.guessed = nxt is not None       # ... or a very quiet stretch in the side's last song
        if nxt is None:
            return False
        self.i, self.start, self.anchored = nxt, now, True
        return True

    def started(self, now: float) -> str:
        """The record started playing: 'expired', 'flip' (next side), 'resume' or 'new'."""
        if self.stopped_at is None:
            self.start, self.anchored = now, True
            return "new"
        gone, self.stopped_at = now - self.stopped_at, None
        self.same_until, self.overrun, self.guessed = None, False, False
        if gone > EXPIRE_AFTER:
            return "expired"
        nxt = self.first_of_next_side()
        ended, self.at_side_end = self.at_side_end, False
        if ended and nxt is None and self.auto:
            return "expired"                     # the last side was played out: whatever starts now is new
        if gone <= SIDE_FLIP_WINDOW and nxt is not None and ended:
            self.i, self.start, self.anchored = nxt, now, True
            return "flip"
        self.start, self.anchored = None, False
        return "resume"

    def stopped(self, now: float, side_end: bool = True) -> None:
        """side_end=False: the sound was lost, which says nothing about where the needle is."""
        self.at_side_end = side_end and self.next_in_side() is None
        self.stopped_at, self.start = now, None

    def jump(self, i: int, start: float | None) -> None:
        self.i, self.start, self.anchored = i, start, False
        self.same_until, self.overrun, self.guessed = None, False, False

    # --- for display ---------------------------------------------------------------
    def info(self, now: float, wall: float) -> dict:
        """Attributes for Home Assistant: no polling, the start time is enough to compute progress."""
        t = self.cur
        side_total = sum(1 for x in self.tracks if x["side"] == t["side"])
        return {
            "side": t["side"], "track_number": t["pos"], "side_tracks": side_total,
            "album_tracks": len(self.tracks), "track_length": t.get("length"),
            "length_source": t.get("source"),      # typed, measured, or listed (a placeholder)
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
