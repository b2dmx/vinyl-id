"""Times each track of the album being followed, from playback.

A length is the time from a track's start to the next track's start (to the start of the
quiet, for a side's last track). Starts come from the needle drop, from gaps between songs,
and from recognizing a song at a known position; the last is how songs that run together
are timed. Pure logic: it is told what happened and answers with the lengths now known.
"""

from __future__ import annotations

from dataclasses import dataclass

from .library import Q_GAP, Q_HEARD, Q_STOP
from .spin import Spin


@dataclass
class Timing:
    track_id: int
    began: float      # on the app's clock
    q: int            # how sure that start is


Measured = tuple[int, float, int]      # (track id, seconds, how sure)
Candidate = tuple[int, float, int, float]   # the same, plus when the track began


class TrackTimer:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.open: Timing | None = None     # the track being timed
        self.wait: Timing | None = None     # the one before it, whose end has not been heard yet
        self.cand: Candidate | None = None  # measured at a gap that nothing has confirmed yet

    def begin(self, track_id: int, began: float, q: int = Q_GAP) -> None:
        self.open = Timing(track_id, began, q)

    def settle(self, confirmed: bool) -> list[Measured]:
        """A length measured at a gap is kept: fully trusted if the next song was then recognized
        as the next track, less so if nothing could confirm that the gap was a real one."""
        cand, self.cand = self.cand, None
        if not cand:
            return []
        return [(cand[0], cand[1], cand[2] if confirmed else min(cand[2], Q_HEARD))]

    def gap(self, sp: Spin, now: float, dip: float, late: bool, moved: bool, was: int) -> list[Measured]:
        """A gap was heard. `was`: the track index before it; `late`: the listed length had
        already moved the album on, so the gap ends the track before that one."""
        done = sp.tracks[was - 1] if late and was > 0 else sp.tracks[was]
        if late and moved and self.cand and self.cand[0] == done["id"]:
            # A song may end in several short silences. The first moved the album on; this one
            # belongs to the same ending, so the track ran until now.
            track_id, _, q, began = self.cand
            self.cand = (track_id, now - began, q, began)
            self.begin(sp.cur["id"], now)
            return []
        out = self.settle(False)
        src = self.wait if late else self.open
        self.wait = self.open = None
        if moved:
            if src and src.track_id == done["id"]:
                end = now - dip if sp.cur["side"] != done["side"] else now   # a flip: up to where the quiet began
                self.cand = (done["id"], end - src.began, min(src.q, Q_GAP), src.began)
            self.begin(sp.cur["id"], now)
        return out

    def moved_on(self) -> list[Measured]:
        """The listed length ran out and the album moved on with no gap heard."""
        out = self.settle(False)
        self.wait, self.open = self.open, None
        return out

    def jumped(self, sp: Spin, i: int, j: int) -> None:
        """Recognition says the album is on another track than thought (i -> j)."""
        self.cand = None                     # a gap measured just before this was not what it seemed
        follows = j == i + 1 and sp.tracks[j]["side"] == sp.tracks[i]["side"]
        if follows and self.open and self.open.track_id == sp.tracks[i]["id"]:
            self.wait, self.open = self.open, None      # ran into the next song with no gap
        else:
            self.wait = self.open = None

    def heard(self, sp: Spin, start: float) -> list[Measured]:
        """The current track was recognized, and with it when it began."""
        out = []
        wait, self.wait = self.wait, None
        before = sp.tracks[sp.i - 1] if sp.i > 0 else None
        if wait and before and wait.track_id == before["id"] and before["side"] == sp.cur["side"]:
            out.append((wait.track_id, start - wait.began, min(wait.q, Q_HEARD)))
        if not self.open or self.open.track_id != sp.cur["id"]:
            self.begin(sp.cur["id"], start, Q_HEARD)
        return out

    def stopped(self, sp: Spin, now: float, stop_seconds: float, lost: bool) -> list[Measured]:
        """The record stopped (lost: only the sound went away, which ends nothing)."""
        out = self.settle(False)
        timed = self.open
        if not lost and timed and timed.track_id == sp.cur["id"] and sp.next_in_side() is None:
            out.append((timed.track_id, now - stop_seconds - timed.began, min(timed.q, Q_STOP)))
        self.reset()
        return out
