"""Vinyl ID: listens to the record player feed, identifies songs, tells Home Assistant.

Identification is local-first: the saved library is checked before Shazam.
Learning is verified: audio is only added to a song's fingerprint after a later
check confirms the same song was still playing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from collections import deque
from pathlib import Path

import numpy as np

from . import web
from .audio import ffmpeg_blocks, level_db
from .config import AI_NOTE, APP_NAME, BLOCK_SECONDS, SIGNATURE, Settings
from .detector import Detector, Event
from .fingerprint import FRAMES_PER_SECOND, hashes
from .library import Q_GAP, Q_HEARD, Q_STOP, Library
from .mqtt import Publisher
from .recognize import Recognizer
from .spin import EXPIRE_AFTER, LENGTH_GRACE, Spin

log = logging.getLogger("vinyl_id")
FRAMES_PER_BLOCK = BLOCK_SECONDS * FRAMES_PER_SECOND
MAX_LEARN_BLOCKS = int(300 / BLOCK_SECONDS)   # 5 min of unconfirmed audio at most
TAIL_TRIM_BLOCKS = int(3 / BLOCK_SECONDS)     # drop the last 3 s before a gap
RECENT_BLOCKS = int(90 / BLOCK_SECONDS)       # audio kept for labelling an unknown song
SHAZAM_TIMEOUT = 25                           # seconds; a lookup that hangs must not hold up the next one


class App:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.clock = time.monotonic           # swappable in tests
        self.loop: asyncio.AbstractEventLoop | None = None
        self.det = Detector(s)
        self.lib = Library(Path(s.data_dir))
        self.lib.max_bytes = int(s.max_library_mb * 1024 * 1024)
        self.rec = Recognizer()
        self.pub = Publisher(s, on_forget=self._forget_current_threadsafe)
        self.buf: deque[np.ndarray] = deque(maxlen=int(s.sample_seconds / BLOCK_SECONDS))
        self.block_i = 0                       # running block counter = play timeline
        self.current: int | None = None        # song id on screen
        self.current_source = ""
        self.learn_blocks: list[np.ndarray] = []
        self.recent: deque[np.ndarray] = deque(maxlen=RECENT_BLOCKS)
        self.recent_start = 0                  # block index of recent[0]
        self.unknown = False                   # playing, but not recognized
        self.learn_start = 0                   # block index of learn_blocks[0]
        self.next_check: float | None = None
        self.busy = False
        self.epoch = 0                         # goes up whenever what's playing may have changed
        self._task: asyncio.Task | None = None  # the identification in flight (kept so it isn't dropped)
        self._last_level: tuple[float, float] = (0.0, -999.0)
        self._art_name: str | None = None      # the cover file now_info() picked
        self._quick = False                    # the next identification may use a short sample
        self.level = -90.0                     # the input's level right now, in dB
        self.spin: Spin | None = self._load_spin()   # 'now spinning': the album and track being played
        self.song_start = 0.0                  # block index where the current song began (learning is stored relative to it)
        self.start_exact = False               # True once Shazam or a local match told us the real position
        self.rejected: int | None = None       # the song you said this is not (until the next song)
        self._compact_due = False              # songs were forgotten while a record played: tidy up after it
        self._labelled_at = -1e9               # when a song was last named by hand
        # Measuring track lengths from playback. Each is (track id, when it began, how sure that is).
        self._m_open: tuple | None = None      # the track being timed
        self._m_wait: tuple | None = None      # the one before it, whose end has not been heard yet
        self._m_cand: tuple | None = None      # (track id, seconds, how sure): measured at a gap, not yet confirmed

    # --- scheduling ------------------------------------------------------------
    def _schedule(self, delay: float, quick: bool = False) -> None:
        """Identify again after `delay`. Anything still being identified is now out of date.
        quick: a new song has just begun, so try on a short sample first to name it sooner."""
        self.next_check = self.clock() + delay
        self.buf.clear()
        self.epoch += 1
        self._quick = quick

    def _bg(self, fn, *args) -> None:
        """Run slow work off the audio loop, and say so in the log if it fails."""
        def work() -> None:
            try:
                fn(*args)
            except Exception:
                log.exception("Background job failed")

        asyncio.get_running_loop().run_in_executor(None, work)

    def _learn(self, song_id: int, pcm: np.ndarray, t0: int) -> None:
        if self.lib.can_learn(song_id):       # else the fingerprinting would be work for nothing
            self._bg(lambda: self.lib.learn(song_id, hashes(pcm), t0))

    def _rel(self, block: float) -> int:
        """Frames from the start of the current song (what the library stores)."""
        return max(0, int((block - self.song_start) * FRAMES_PER_BLOCK))

    def _mark_start(self) -> None:
        self.song_start, self.start_exact = self.block_i, False
        self.rejected = None

    def _reset_learning(self) -> None:
        self.learn_blocks, self.learn_start = [], self.block_i

    def _commit_learning(self, trim_tail: bool = False, extra_trim: float = 0.0) -> None:
        """Add confirmed audio to the current song's fingerprint."""
        cut = (TAIL_TRIM_BLOCKS if trim_tail else 0) + int(extra_trim / BLOCK_SECONDS)
        blocks = self.learn_blocks[:-cut] if cut else self.learn_blocks
        # A track guessed from the album is not learned once something else has been heard on it
        doubted = self.spin is not None and self.spin.mismatch is not None
        # Picked up mid-song (a resume, or an album chosen while it plays): where the song began is
        # not known, and audio stored from a made-up start would give wrong positions later.
        unplaced = self.spin is not None and self.spin.start is None and not self.start_exact
        if (self.s.learn and self.current is not None and not doubted and not unplaced
                and len(blocks) * BLOCK_SECONDS >= 5):
            self._learn(self.current, np.concatenate(blocks), self._rel(self.learn_start))
        self._reset_learning()

    def _record_stopped(self, lost: bool = False) -> None:
        """lost: the audio input went away (the record itself may still be turning)."""
        # A stop is heard only after stop_seconds of quiet: that stretch is the run-out groove, not the song
        self._commit_learning(trim_tail=True, extra_trim=0.0 if lost else self.s.stop_seconds)
        self.next_check, self.current, self.unknown = None, None, False
        self.epoch += 1
        if self.spin:
            self._m_stopped(self.clock(), lost)
            self.spin.stopped(self.clock(), side_end=not lost)
            self._save_spin()
        self.pub.playing(False)
        self.pub.track(None)
        if self._compact_due:
            self._compact_due = False
            self._bg(self.lib.compact)

    # --- measuring track lengths -------------------------------------------------
    # A length is the time from a track's start to the next track's start (to the start of the
    # quiet, for a side's last track). Starts come from the needle drop, from gaps, and from
    # recognizing a song at a known position; the last is how songs that run together are timed.
    def _m_reset(self) -> None:
        self._m_open = self._m_wait = self._m_cand = None

    def _m_store(self, track_id: int, seconds: float, q: int) -> None:
        if not self.lib.set_measured(track_id, seconds, q):
            return
        log.info("Measured track %s: %d:%02d", track_id, seconds // 60, seconds % 60)
        for t in self.spin.tracks if self.spin else ():
            if t["id"] == track_id and t.get("source") != "typed":
                t["length"], t["measured"], t["source"] = round(seconds, 1), round(seconds, 1), "measured"

    def _m_settle(self, confirmed: bool) -> None:
        """A length measured at a gap is kept: fully trusted if the next song was then recognized
        as the next track, less so if nothing could confirm that the gap was a real one."""
        cand, self._m_cand = self._m_cand, None
        if cand:
            self._m_store(cand[0], cand[1], cand[2] if confirmed else min(cand[2], Q_HEARD))

    def _m_gap(self, now: float, dip: float, late: bool, moved: bool, was: int) -> None:
        """A gap was heard. `was`: the track index before it; `late`: the listed length had
        already moved the album on, so the gap ends the track before that one."""
        sp = self.spin
        self._m_settle(False)
        src = self._m_wait if late else self._m_open
        done = sp.tracks[was - 1] if late and was > 0 else sp.tracks[was]
        self._m_wait = self._m_open = None
        if not moved:
            return
        if src and src[0] == done["id"]:
            end = now - dip if sp.cur["side"] != done["side"] else now     # a flip: up to where the quiet began
            self._m_cand = (done["id"], end - src[1], min(src[2], Q_GAP))
        self._m_open = (sp.cur["id"], now, Q_GAP)

    def _m_moved_on(self) -> None:
        """The listed length ran out and the album moved on with no gap heard."""
        self._m_settle(False)
        self._m_wait, self._m_open = self._m_open, None

    def _m_jumped(self, i: int, j: int) -> None:
        """Recognition says the album is on another track than thought (i -> j)."""
        sp, self._m_cand = self.spin, None       # a gap measured just before this was not what it seemed
        follows = j == i + 1 and sp.tracks[j]["side"] == sp.tracks[i]["side"]
        if follows and self._m_open and self._m_open[0] == sp.tracks[i]["id"]:
            self._m_wait, self._m_open = self._m_open, None    # ran into the next song with no gap
        else:
            self._m_wait = self._m_open = None

    def _m_heard(self, start: float) -> None:
        """The current track was recognized, and with it when it began."""
        sp = self.spin
        wait, self._m_wait = self._m_wait, None
        before = sp.tracks[sp.i - 1] if sp.i > 0 else None
        if wait and before and wait[0] == before["id"] and before["side"] == sp.cur["side"]:
            self._m_store(wait[0], start - wait[1], min(wait[2], Q_HEARD))
        if not self._m_open or self._m_open[0] != sp.cur["id"]:
            self._m_open = (sp.cur["id"], start, Q_HEARD)

    def _m_stopped(self, now: float, lost: bool) -> None:
        self._m_settle(False)
        sp, timed = self.spin, self._m_open
        if not lost and timed and timed[0] == sp.cur["id"] and sp.next_in_side() is None:
            self._m_store(timed[0], now - self.s.stop_seconds - timed[1], min(timed[2], Q_STOP))
        self._m_reset()

    # --- main loop ---------------------------------------------------------------
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.pub.start()
        self.pub.playing(False)
        self.pub.track(None)                     # a song left on screen by the last run is not playing now
        self.pub.library(self.lib.size())
        async for block in ffmpeg_blocks(self.s.audio_input, self.s.audio_input_format):
            if block is None:                    # the input dropped: a record can't still be playing
                self.level = -90.0
                if self.det.input_lost():
                    log.info("Record stopped (the audio input went away)")
                    self._record_stopped(lost=True)
                continue
            db = self.level = level_db(block)
            self._report_level(db)
            self.buf.append(block)
            if len(self.recent) == self.recent.maxlen:
                self.recent_start += 1
            self.recent.append(block)
            if self.current is not None and len(self.learn_blocks) < MAX_LEARN_BLOCKS:
                self.learn_blocks.append(block)
            self.block_i += 1

            ev = self.det.feed(db)
            if ev is Event.STARTED:
                log.info("Record started")
                self.pub.playing(True)
                self._reset_learning()
                self._clear_recent()
                self._mark_start()
                self._m_reset()
                if self.spin:
                    how = self.spin.started(self.clock())
                    if how == "expired":
                        self.spin = None
                        self._save_spin()
                    else:
                        self._spin_sync()
                        if how in ("new", "flip"):       # a needle drop at the top of a side
                            self._m_open = (self.spin.cur["id"], self.clock() - self.s.start_seconds, Q_GAP)
                self._schedule(self.s.settle_seconds, quick=True)
            elif ev is Event.TRACK_CHANGE:
                log.info("Track change detected")
                if self.spin and self.spin.late_gap(self.clock()):
                    self._reset_learning()       # heard since the track moved on: the previous song's tail
                else:                            # the quiet of the gap itself is not part of the song
                    self._commit_learning(trim_tail=True, extra_trim=max(0.0, self.det.last_dip - 3))
                self._clear_recent()
                self._mark_start()
                moved = False
                if self.spin:
                    self.spin.mismatch = None            # a new track is a new chance to match the album
                    late, was = self.spin.late_gap(self.clock()), self.spin.i
                    moved = self.spin.track_change(self.clock(), self.det.last_dip)
                    self._m_gap(self.clock(), self.det.last_dip, late, moved, was)
                if moved:
                    self._spin_sync()
                else:
                    # Not known what this is yet. It is not the previous song any more, so nothing
                    # heard from here on may be learned into that one.
                    self.current = None
                self._schedule(self.s.settle_seconds, quick=True)
            elif ev is Event.STOPPED:
                log.info("Record stopped")
                self._record_stopped()

            if self.spin and self.det.playing and not self.det.in_dip and self.spin.tick(self.clock()):
                # songs running together: the listed length ran out before a gap was heard
                # (never in a quiet stretch: that is a gap, or the side has ended)
                self._commit_learning(trim_tail=True, extra_trim=LENGTH_GRACE)
                self._clear_recent()
                self._m_moved_on()
                self._spin_sync()
                self.start_exact = False     # the new song's own position is still to be heard
                self._schedule(self.s.settle_seconds, quick=True)

            if (self.det.playing and not self.busy and self.next_check is not None
                    and self.clock() >= self.next_check and len(self.buf) >= self._blocks_needed()):
                self.busy = True
                clip_start = self.block_i - len(self.buf)
                self._task = asyncio.create_task(
                    self._identify(np.concatenate(self.buf), clip_start, quick=self._quick, epoch=self.epoch))

    def _blocks_needed(self) -> int:
        quick = int(self.s.quick_sample_seconds / BLOCK_SECONDS)
        return min(quick, self.buf.maxlen) if self._quick else self.buf.maxlen

    async def _identify(self, pcm: np.ndarray, clip_start: int, quick: bool = False,
                        epoch: int | None = None) -> None:
        epoch = self.epoch if epoch is None else epoch     # as it was when the clip was taken
        try:
            hs = await asyncio.to_thread(hashes, pcm)
            local = await asyncio.to_thread(self.lib.match, hs) if self.s.learn else None
            pos: float | None = None          # seconds into the song where this clip started
            if local:
                song_id, source, pos = local[0], "local", local[2] / FRAMES_PER_SECOND
            else:
                try:
                    track = await asyncio.wait_for(self.rec.identify(pcm), SHAZAM_TIMEOUT)
                except asyncio.TimeoutError:
                    log.warning("Shazam did not answer within %d s", SHAZAM_TIMEOUT)
                    track = None
                if epoch != self.epoch:
                    return
                if track is None and quick:
                    # too little to go on: keep collecting and ask again with the full sample
                    log.info("Not recognized from a short sample; trying a longer one")
                    self._quick, self.next_check = False, self.clock()
                    return
                if track is None:
                    log.info("Not recognized; retrying in %.0f s", self.s.retry_seconds)
                    if self.current_source != "spin":
                        self._reset_learning()   # it may be the next song already: keep nothing unconfirmed
                    if self.current is None and not self.unknown:
                        self.unknown = True
                        self.pub.track({"title": "Unknown", "source": "unknown"})
                    self._schedule(self.s.retry_seconds)
                    return
                song_id = self.lib.shazam_song(track.shazam_id)
                if song_id is None:
                    # A song on a pre-loaded album is that album's track: no second copy of the
                    # song, and no second album (with its own cover) under Shazam's album name.
                    tr = (self.lib.title_in_album(self.spin.album_id, track.title) if self.spin
                          else self.lib.find_track(track.title, track.artist))
                    if tr:
                        song_id = self.lib.song_for_track(tr["id"])
                        self.lib.tag_shazam(song_id, track.shazam_id, track.artist)
                    elif self.spin and not self._foreign(track):
                        self._schedule(self.s.retry_seconds)        # not on this album: ask, save nothing
                        return
                if song_id is None:
                    song_id, needs_art = self.lib.upsert(track.title, track.artist, track.album, track.shazam_id)
                    song = self.lib.song(song_id)
                    if needs_art and track.art_url and song and song.album_id:
                        if data := await self.rec.fetch(track.art_url):
                            await asyncio.to_thread(self.lib.set_art, song.album_id, data, False)
                source = "shazam"
                pos = track.offset

            if epoch != self.epoch:       # the record stopped or moved on while this was worked out
                return
            if song_id == self.rejected and self.current_source == "manual":
                self._schedule(self.s.recheck_seconds)        # you already said it isn't this one
                return
            song_id = self._reconcile(song_id)
            if song_id is None:                               # not on the album being spun: ask, don't learn
                self._schedule(self.s.retry_seconds)          # soon: twice in a row lets a guessed album go
                return

            if song_id == self.current:
                moved = self._apply_position(pos, clip_start) if not self.start_exact else False
                self._commit_learning()                       # confirmed: keep what we heard
                if moved:
                    self.refresh_now()
            else:
                self._reset_learning()                        # new song: discard unverified audio
                self.unknown = False
                self._apply_position(pos, clip_start)
                if source == "shazam" and self.s.learn:       # Shazam-verified clip
                    self._bg(self.lib.learn, song_id, hs, self._rel(clip_start))
                self.current, self.current_source = song_id, source
                self.lib.played(song_id)
                self.refresh_now()
                self.pub.library(self.lib.size())
                info = self.now_info() or {}
                log.info("Now playing (%s): %s - %s", source, info.get("artist"), info.get("title"))
            self._schedule(self.s.recheck_seconds)
        except Exception:
            log.exception("Identifying failed; retrying in %.0f s", self.s.retry_seconds)
            if epoch == self.epoch:
                self._schedule(self.s.retry_seconds)
        finally:
            self.busy = False

    # --- album track list ('now spinning') -----------------------------------------
    def _reconcile(self, song_id: int) -> int | None:
        """Check a heard song against the album being spun, or attach to a pre-loaded album.
        Returns the song id to use, or None when it isn't on the spun album."""
        song = self.lib.song(song_id)
        if song is None:
            return song_id
        if self.spin:
            tr = self.lib.track_in_album(self.spin.album_id, song_id)
            if tr is None:
                seen = self.spin.mismatch
                if self.spin.auto and seen and seen.get("id") == song_id:
                    # the album was only a guess, and the same other song was heard twice: let go
                    log.info("Not this album after all; following stopped")
                    self.spin = None
                    self._save_spin()
                    return song_id
                if not seen or seen.get("id") != song_id:
                    self.spin.mismatch = {"id": song_id, "title": song.title, "artist": song.artist,
                                          "album": song.album}
                    self.refresh_now()
                return None
            self.spin.mismatch = None
            song_id = self.lib.attach_song(song_id, tr)
            j = next(i for i, t in enumerate(self.spin.tracks) if t["id"] == tr["id"])
            if j != self.spin.i:
                self._m_jumped(self.spin.i, j)
                self.spin.jump(j, None)        # a different track of the same album: follow it
                self._save_spin()
            else:
                self._m_settle(True)           # the gap before this song was a real one
            return song_id
        tr = self.lib.find_track(song.title, song.artist)
        if tr:                                  # auto-attach: tracklist and timer start by themselves
            tracks = self.lib.album_tracks(tr["album_id"])
            song_id = self.lib.attach_song(song_id, tr)
            self.spin = Spin(tr["album_id"], tracks, next(i for i, t in enumerate(tracks) if t["id"] == tr["id"]),
                             auto=True)
            log.info("Attached to pre-loaded album %s", tr["album_id"])
            self._save_spin()
        return song_id

    def _foreign(self, track) -> bool:
        """Shazam named a song that is neither on the album being followed nor in the library.
        Nothing is saved for it. True = the album was only a guess and this is the second time
        the same song was heard: stop following, and take the song."""
        key = track.shazam_id or f"{track.title}|{track.artist}"
        seen = self.spin.mismatch
        if seen and seen.get("key") == key:
            if not self.spin.auto:
                return False
            log.info("Not this album after all; following stopped")
            self.spin = None
            self._save_spin()
            return True
        self.spin.mismatch = {"id": None, "key": key, "title": track.title, "artist": track.artist,
                              "album": track.album}
        self.refresh_now()
        return False

    def _apply_position(self, pos: float | None, clip_start: int) -> bool:
        """Shazam or a local match said where in the song the clip started: fix the song's start."""
        if pos is None:
            return False
        self.song_start, self.start_exact = clip_start - pos / BLOCK_SECONDS, True
        if self.spin:
            now_pos = pos + (self.block_i - clip_start) * BLOCK_SECONDS
            old = self.spin.start
            self.spin.start = self.clock() - now_pos
            # heard past its listed length: this pressing's track is longer, so wait for the gap
            length = self.spin.cur.get("length")
            self.spin.overrun = bool(length) and now_pos >= length
            self._m_heard(self.spin.start)
            return old is None or abs(old - self.spin.start) > 2
        return False

    def _spin_sync(self) -> None:
        """The spin moved to a new track: make it the current song."""
        sp = self.spin
        sid = self.lib.song_for_track(sp.cur["id"])
        self.current, self.current_source, self.unknown = sid, "spin", False
        if sp.start is not None:
            self.song_start = self.block_i - (self.clock() - sp.start) / BLOCK_SECONDS
        self.lib.played(sid)
        self.pub.library(self.lib.size())
        self.refresh_now()
        self._save_spin()

    # The album being followed is remembered across restarts (an update mid-record).
    @property
    def _spin_file(self) -> Path:
        return Path(self.s.data_dir) / "spin.json"

    def _save_spin(self) -> None:
        try:
            if self.spin:
                self._spin_file.write_text(json.dumps(
                    {"album_id": self.spin.album_id, "track_id": self.spin.cur["id"], "saved": time.time(),
                     "auto": self.spin.auto, "side_ended": self.spin.at_side_end}))
            else:
                self._spin_file.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not save the album being followed: %s", exc)

    def _load_spin(self) -> Spin | None:
        try:
            saved = json.loads(self._spin_file.read_text())
            tracks = self.lib.album_tracks(saved["album_id"])
            i = next(k for k, t in enumerate(tracks) if t["id"] == saved["track_id"])
            if time.time() - saved["saved"] > EXPIRE_AFTER:
                return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration):
            return None
        spin = Spin(saved["album_id"], tracks, i, auto=bool(saved.get("auto")))
        # the next start resumes this track (position unknown); the time since counts as stopped time
        spin.stopped_at = self.clock() - max(0.0, time.time() - saved["saved"])
        spin.at_side_end = bool(saved.get("side_ended"))
        return spin

    def start_spin(self, album_id: int, side: str = "", pos: int = 1) -> bool:
        """Pick the album and where on it you are (default A1)."""
        tracks = self.lib.album_tracks(album_id)
        if not tracks:
            return False
        i = next((k for k, t in enumerate(tracks)
                  if t["side"].upper() == (side or tracks[0]["side"]).upper() and t["pos"] == pos), 0)
        self.spin = Spin(album_id, tracks, i)
        self._m_reset()
        self._save_spin()
        if self.det.playing:                    # needle is already down: position unknown until the next gap
            self.spin.stopped_at = None
            self._reset_learning()
            self._mark_start()
            self._spin_sync()
            self._schedule(self.s.settle_seconds, quick=True)
        return True

    def stream_title(self) -> tuple[str, str]:
        """(what the record stream calls itself right now, cover address)."""
        info = self.now_info() if self.det.playing else None
        if not info:
            return APP_NAME, ""
        return " - ".join(x for x in (info["artist"], info["title"]) if x), info["art_url"]

    def end_spin(self) -> None:
        self.spin = None
        self._save_spin()
        if self.det.playing:
            self._schedule(self.s.settle_seconds, quick=True)
            self.refresh_now()

    def _clear_recent(self) -> None:
        self.recent.clear()
        self.recent_start = self.block_i

    # --- now playing / edits -------------------------------------------------------
    def label_now(self, title: str, artist: str, album: str) -> int | None:
        """Name what's playing (unknown, or wrong). Learns it from the last ~90 s heard."""
        if not self.det.playing:
            return None
        if (self.current is not None and self.current_source == "manual"
                and self.clock() - self._labelled_at <= 60):
            # fixing what you typed a moment ago: the same song renamed, not a second copy of it
            self.lib.edit_song(self.current, title, artist)
            if album:
                self.lib.set_song_album(self.current, self.lib.add_album(artist, album))
            self.refresh_now()
            return self.current
        if self.current is not None and self.current_source != "manual":
            # replacing a wrong match: drop the audio that was learned under it this play,
            # and don't let the next check bring the wrong song back
            self.rejected = self.current
            self._reset_learning()
        album_id = self.lib.add_album(artist, album) if album else None
        song_id = self.lib.add_song(album_id, title, artist)
        self.spin = None                  # you said what it is: stop following the album
        self._save_spin()
        if self.s.learn and len(self.recent) * BLOCK_SECONDS >= 5:
            self._learn(song_id, np.concatenate(self.recent), self._rel(self.recent_start))
        self.current, self.current_source, self.unknown = song_id, "manual", False
        self._labelled_at = self.clock()
        self._reset_learning()
        self.lib.played(song_id)
        self.pub.library(self.lib.size())
        self.refresh_now()
        self._schedule(self.s.recheck_seconds)
        return song_id

    def now_info(self) -> dict | None:
        song = self.lib.song(self.current) if self.current is not None else None
        if not song:
            return None
        # The disc's own cover (box sets) wins over the album's.
        disc = None
        if self.spin and self.spin.album_id == song.album_id:
            disc = self.spin.cur.get("disc")
        elif song.album_id:
            disc = (self.lib.track_in_album(song.album_id, song.id) or {}).get("disc")
        base = f"{self.s.public_url}/api/albums/{song.album_id}"
        use_lp = disc and (self.lib.album(song.album_id) or {}).get("lp_covers")
        if use_lp and (name := self.lib.disc_art_file(song.album_id, disc)):
            path = f"/discs/{disc}/art"
        else:
            name, path = song.art_file, "/art"
        self._art_name = name
        # ?v= changes only when the cover does, so displays don't download it again per song
        art_url = f"{base}{path}?v={self.lib.art_version(name)}" if name else ""
        info = {"id": song.id, "title": song.title, "artist": song.artist, "album": song.album,
                "album_id": song.album_id, "art_url": art_url, "source": self.current_source}
        if self.spin:
            # no polling: displays work progress out from the start time and the length
            info |= self.spin.info(self.clock(), time.time())
            info["position_known"] = self.spin.start is not None
            info["spin_mismatch"] = self.spin.mismatch
        return info

    def spin_status(self) -> dict | None:
        sp = self.spin
        if not sp:
            return None
        album = self.lib.album(sp.album_id) or {}
        return {"album_id": sp.album_id, "album": album.get("album", ""), "artist": album.get("artist", ""),
                "index": sp.i, "auto": sp.auto, "mismatch": sp.mismatch,
                "tracks": sp.tracks, **sp.info(self.clock(), time.time())}

    def status(self, tracks: bool = True) -> dict:
        spin = self.spin_status()
        if spin and not tracks:
            del spin["tracks"]
        return {"playing": self.det.playing, "unknown": self.unknown, "now": self.now_info(),
                "library": self.lib.size(), "learn": self.s.learn, "spin": spin,
                "listener": self.s.receiver_name,
                # for set-up: how loud the input is now, and how loud it must be for a record to start
                "level": round(self.level), "start_level": round(self.det.start_level),
                "library_mb": round(self.lib.size_bytes() / 1048576, 1),
                "library_limit_mb": self.s.max_library_mb, "library_full": self.lib.full}

    def refresh_now(self) -> None:
        """Send the current song to Home Assistant (new song, or after an edit in the UI)."""
        info = self.now_info()
        if info:
            self.pub.track(info, self.lib.art_file_bytes(self._art_name) if info["art_url"] else None)

    def tracks_changed(self, album_id: int) -> None:
        """The followed album's track list was replaced or edited: follow the new one."""
        if not self.spin or self.spin.album_id != album_id:
            return
        tracks, old = self.lib.album_tracks(album_id), self.spin.cur
        if [t["id"] for t in tracks] != [t["id"] for t in self.spin.tracks]:
            self._m_reset()                    # another track list: what was being timed is gone
        if not tracks:
            self.end_spin()
            return
        self.spin.tracks = tracks
        self.spin.i = next((k for k, t in enumerate(tracks)
                            if (t["side"], t["pos"]) == (old["side"], old["pos"])), 0)
        self._save_spin()
        self.refresh_now()

    def forget(self, song_id: int) -> None:
        self.lib.forget(song_id)
        if self.det.playing:              # giving the space back holds the library for a moment:
            self._compact_due = True      # not while a record plays
        else:
            self._bg(self.lib.compact)    # so a full library can learn again
        self.pub.library(self.lib.size())
        if song_id == self.current:
            self.current = None
            self._reset_learning()
            self.pub.track(None)
            if self.det.playing:
                self._schedule(self.s.settle_seconds, quick=True)

    def _forget_current_threadsafe(self) -> None:
        if self.loop and self.current is not None:
            self.loop.call_soon_threadsafe(self.forget, self.current)

    def _report_level(self, db: float) -> None:
        now = self.clock()
        last_t, last_db = self._last_level
        if now - last_t >= 5 and abs(db - last_db) >= 3:
            self._last_level = (now, db)
            self.pub.level(db)

    async def close(self) -> None:
        self.pub.stop()
        await self.rec.close()


async def main() -> None:
    s = Settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(message)s")
    log.info("%s — %s", SIGNATURE, AI_NOTE)
    if not s.audio_input:
        raise SystemExit("No audio to listen to: the audio source is Stream, but the Stream address is empty")
    app = App(s)
    runner = await web.serve(app, s.web_port)
    task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
        except NotImplementedError:   # Windows (local test runs): Ctrl+C still stops it
            pass
    try:
        await app.run()
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
