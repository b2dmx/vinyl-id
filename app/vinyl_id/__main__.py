"""Vinyl ID: listens to the record player feed, identifies songs, tells Home Assistant.

Identification is local-first: the saved library is checked before Shazam.
Learning is verified: audio is only added to a song's fingerprint after a later
check confirms the same song was still playing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import statistics
import logging
import signal
import time
from collections import deque
from pathlib import Path

import numpy as np

from . import web
from .audio import ffmpeg_blocks, level_db
from .calibrate import Calibrator
from .config import AI_NOTE, APP_NAME, BLOCK_SECONDS, DEFAULT_THRESHOLD_DB, SAMPLE_RATE, SIGNATURE, Settings
from .detector import Detector, Event
from .fingerprint import FRAMES_PER_SECOND, hashes
from .library import Library, _norm, _same_artist
from .measure import TrackTimer
from .mqtt import Publisher
from .recognize import Recognizer
from .spin import EXPIRE_AFTER, LENGTH_GRACE, LONG_QUIET, Spin

log = logging.getLogger("vinyl_id")
FRAMES_PER_BLOCK = BLOCK_SECONDS * FRAMES_PER_SECOND
BLOCK_SAMPLES = int(SAMPLE_RATE * BLOCK_SECONDS)
MAX_LEARN_BLOCKS = int(300 / BLOCK_SECONDS)   # 5 min of unconfirmed audio at most
TAIL_TRIM_BLOCKS = int(3 / BLOCK_SECONDS)     # drop the last 3 s before a gap
RECENT_BLOCKS = int(90 / BLOCK_SECONDS)       # audio kept for labelling an unknown song
SHAZAM_TIMEOUT = 25                           # seconds; a lookup that hangs must not hold up the next one
TRACKLIST_RETRY = 120                         # seconds before MusicBrainz is asked again after no answer


class App:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self._threshold_option = s.signal_threshold_db   # as set in the options
        self._idle: deque[float] = deque(maxlen=int(5 / BLOCK_SECONDS))   # levels of the last 5 s with no record
        self._quiet_set = False                          # a quiet level was found or set before
        self._load_quiet_level()
        s = self.s
        # A quiet level you typed yourself is left alone; at the default it is found automatically.
        self._auto_quiet = self._threshold_option == DEFAULT_THRESHOLD_DB
        self.cal = Calibrator(s.signal_threshold_db, self._quiet_set)
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
        self._counted: set[int] = set()        # the songs whose play was counted since the record started
        self._found_at = -1e9                  # when the song on screen was last recognized
        self._marked_at = 0.0                  # when the current song began (as far as is known)
        self._unkept = 0                       # blocks not kept for learning (the 5-minute limit)
        self.timer = TrackTimer()              # measures the followed album's track lengths
        self._held_back: tuple | None = None   # audio kept back until a gap shows its track ended on time
        self._tails: set[asyncio.Task] = set() # last parts of songs being checked before they are learned
        self._tracklist_after: dict[int, float] = {}      # album -> when its track list may be asked for
        self._tracklist_task: asyncio.Task | None = None  # the track list being fetched

    # --- the quiet level (signal threshold), set from the page ---------------------
    @property
    def _settings_file(self) -> Path:
        return Path(self.s.data_dir) / "settings.json"

    def _load_quiet_level(self) -> None:
        """A level set with the page's button is used until the option itself is changed."""
        try:
            saved = json.loads(self._settings_file.read_text())
            level = float(saved["signal_threshold_db"])
            if saved["option"] == self._threshold_option and -90 <= level <= -10:
                self.s = dataclasses.replace(self.s, signal_threshold_db=level)
                self._quiet_set = True
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def use_quiet_level(self) -> float | None:
        """Take the level heard right now, with the turntable on and the needle up, as the quiet
        level. None when a record is playing, or nothing has been heard for long enough."""
        if self.det.playing or len(self._idle) < self._idle.maxlen:
            return None
        level = round(statistics.median(self._idle))
        if level <= -88 or level > -10:              # no sound is arriving at all, or far too loud to be quiet
            return None
        self._set_quiet_level(level, "from the page")
        return level

    def _set_quiet_level(self, level: float, how: str) -> None:
        self.s = self.det.s = dataclasses.replace(self.s, signal_threshold_db=float(level))
        self.cal.threshold, self.cal.calibrated, self._quiet_set = float(level), True, True
        try:
            self._settings_file.write_text(json.dumps(
                {"signal_threshold_db": level, "option": self._threshold_option}))
        except OSError as exc:
            log.warning("Could not save the quiet level: %s", exc)
        log.info("Quiet level set to %d dB %s", level, how)

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
        self._marked_at = self.clock()

    def _reset_learning(self) -> None:
        self.learn_blocks, self.learn_start, self._unkept = [], self.block_i, 0

    def _count_play(self, song_id: int) -> None:
        """Once per song and side: a late gap, a re-sync, or the title moving on early and back
        again is not a new play."""
        if song_id not in self._counted:
            self._counted.add(song_id)
            self.lib.played(song_id)

    def _commit_learning(self, trim_tail: bool = False, extra_trim: float = 0.0,
                         ended: float | None = None, confirmed: bool = False,
                         upto: int | None = None, hold: bool = False) -> None:
        """Add confirmed audio to the current song's fingerprint.
        ended: when the song stopped (a gap, or the record stopping). confirmed: recognition
        has just named this very song, in a clip that ended at block `upto`; what was heard
        after that is not confirmed yet and stays. hold: the track list moved on by the clock
        while there was reason to doubt it; the audio is kept back until a gap bears it out."""
        rest: list[np.ndarray] = []
        if upto is not None and not self._unkept and 0 < upto - self.learn_start < len(self.learn_blocks):
            k = upto - self.learn_start
            self.learn_blocks, rest = self.learn_blocks[:k], self.learn_blocks[k:]
        cut = (TAIL_TRIM_BLOCKS if trim_tail else 0) + int(extra_trim / BLOCK_SECONDS)
        cut = max(0, cut - self._unkept)     # what was not kept is already "trimmed"
        blocks = self.learn_blocks[:-cut] if cut else self.learn_blocks
        # A track guessed from the album is not learned once something else has been heard on it
        doubted = self.spin is not None and (self.spin.mismatch is not None or self.spin.guessed)
        # A track known only by its place on the album (nothing recognized it) is learned when that
        # place is sure. If Shazam had another name for it, it must also have ended where its length
        # says it should: that tells "the same track under another name" from "another record".
        if self.current_source == "spin" and self.spin is not None and not confirmed:
            fits = ended is not None and self.spin.fits(ended)
            doubted = doubted or not self.spin.sure or (self.spin.named_otherwise and not fits)
        # Picked up mid-song (a resume, or an album chosen while it plays): where the song began is
        # not known, and audio stored from a made-up start would give wrong positions later.
        unplaced = self.spin is not None and self.spin.start is None and not self.start_exact
        if (self.s.learn and self.current is not None and not doubted and not unplaced
                and len(blocks) * BLOCK_SECONDS >= 5):
            what = (self.current, np.concatenate(blocks), self._rel(self.learn_start))
            if confirmed or (self.spin is not None and not hold):
                self._learn(*what)
            elif self.spin is not None:
                self._held_back = what
            else:
                # No track list says where this song ends, and it may have run into the next one
                # since recognition last heard it: ask about its last seconds before keeping them.
                task = asyncio.create_task(self._learn_tail(*what, np.concatenate(blocks[-self.buf.maxlen:])))
                self._tails.add(task)
                task.add_done_callback(self._tails.discard)
        self._reset_learning()
        if rest:
            self.learn_blocks, self.learn_start = rest, upto

    async def _learn_tail(self, song_id: int, pcm: np.ndarray, t0: int, clip: np.ndarray) -> None:
        """Learn the last part of a song (heard after recognition's last look) if its end is
        still recognized as that song."""
        try:
            hit = await asyncio.to_thread(self.lib.match, await asyncio.to_thread(hashes, clip))
            if hit:
                same = hit[0] == song_id
            else:
                track = await asyncio.wait_for(self.rec.identify(clip), SHAZAM_TIMEOUT)
                same = bool(track and track.shazam_id) and self.lib.shazam_song(track.shazam_id) == song_id
            if same:
                self._learn(song_id, pcm, t0)
        except asyncio.TimeoutError:
            pass
        except Exception:
            log.exception("Checking the last part of a song failed")

    def _record_stopped(self, lost: bool = False, dropout: bool = False) -> None:
        """lost: the audio input went away (the record itself may still be turning).
        dropout: the same, noticed as a long stretch of exact silence."""
        # A stop is heard only after stop_seconds of quiet: that stretch is the run-out groove, not the song
        quiet = 0.0 if lost else self.s.stop_seconds
        lost = lost or dropout
        self._commit_learning(trim_tail=True, extra_trim=quiet, ended=self.clock() - quiet)
        self._held_back = None
        self.next_check, self.current, self.unknown = None, None, False
        if not lost:
            self._counted.clear()
        self.epoch += 1
        if self.spin:
            self._keep(self.timer.stopped(self.spin, self.clock(), self.s.stop_seconds, lost))
            self.spin.stopped(self.clock(), side_end=not lost)
            self._save_spin()
        self.pub.playing(False)
        self.pub.track(None)
        if self._compact_due:
            self._compact_due = False
            self._bg(self.lib.compact)

    def _keep(self, measured: list) -> None:
        """Save the track lengths the timer has worked out, and use them from now on."""
        for track_id, seconds, q in measured:
            if not self.lib.set_measured(track_id, seconds, q):
                continue
            log.info("Measured track %s: %d:%02d", track_id, seconds // 60, seconds % 60)
            for t in self.spin.tracks if self.spin else ():
                if t["id"] == track_id and t.get("source") != "typed":
                    t["length"], t["measured"], t["source"] = round(seconds, 1), round(seconds, 1), "measured"

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
            if self.det.playing:
                self._idle.clear()
            else:
                self._idle.append(db)
            if self._auto_quiet:
                # a song named by its place on the album alone does not show that a record is playing
                named = self.current is not None and (
                    self.current_source != "spin" or self._found_at >= self._marked_at)
                found = self.cal.feed(db, self.det.playing, named)
                if found is not None:
                    was_playing = self.det.playing
                    self._set_quiet_level(found, "by itself")
                    if was_playing and self.det.input_lost():     # it was hum all along, not a record
                        log.info("That was the turntable's hum, not a record")
                        self._record_stopped(lost=True)
                        continue
            self._report_level(db)
            self.buf.append(block)
            if len(self.recent) == self.recent.maxlen:
                self.recent_start += 1
            self.recent.append(block)
            if self.current is not None:
                if len(self.learn_blocks) < MAX_LEARN_BLOCKS:
                    self.learn_blocks.append(block)
                else:
                    self._unkept += 1
            self.block_i += 1

            ev = self.det.feed(db)
            if ev is Event.STARTED:
                log.info("Record started")
                self.pub.playing(True)
                self._held_back = None
                self._reset_learning()
                self._clear_recent()
                self._mark_start()
                self.timer.reset()
                if self.spin:
                    how = self.spin.started(self.clock())
                    if how == "expired":
                        self.spin = None
                        self._save_spin()
                    else:
                        self._spin_sync()
                        if how in ("new", "flip") and not self.spin.guessed:   # a needle drop at the top of a side
                            self.timer.begin(self.spin.cur["id"], self.clock() - self.s.start_seconds)
                self._schedule(self.s.settle_seconds, quick=True)
            elif ev is Event.TRACK_CHANGE and self.spin and self.spin.flip_again(self.clock()):
                log.info("The needle came down: %s%s starts here", self.spin.cur["side"], self.spin.cur["pos"])
                self._held_back = None
                self._reset_learning()
                self._clear_recent()
                self._mark_start()
                self.timer.begin(self.spin.cur["id"], self.clock())
                self._spin_sync()
                self._schedule(self.s.settle_seconds, quick=True)
            elif ev is Event.TRACK_CHANGE and self.spin and (early := self.spin.early_gap(self.clock())):
                # the title stays; but listen again, in case the record really is somewhere else
                log.info("Quiet passage, not a gap: this track should run another %d s", early)
                self.spin.held = True            # ... or the track is shorter on this pressing: not known yet
                if self.det.last_dip >= LONG_QUIET:
                    self.spin.sure = False       # long enough to have changed the record
                self._schedule(self.s.settle_seconds, quick=True)
            elif ev is Event.TRACK_CHANGE:
                log.info("Track change detected")
                if self.spin and self.spin.flip_held(self.clock()):
                    self.spin.guessed = False    # so what was heard of it may be learned
                if (self.spin and not self.spin.sure and not self.spin.guessed and self.spin.mismatch is None
                        and self.spin.fits(self.clock())):
                    log.info("%s%s ended on time: the place on the record is sure again",
                             self.spin.cur["side"], self.spin.cur["pos"])
                    self.spin.sure = True
                if self.spin and self.spin.late_gap(self.clock()):
                    if self._held_back:          # the gap came where the length said: it was that track
                        self._learn(*self._held_back)
                    self._reset_learning()       # heard since the track moved on: the previous song's tail
                else:                            # the quiet of the gap itself is not part of the song
                    self._commit_learning(trim_tail=True, extra_trim=max(0.0, self.det.last_dip - 3),
                                          ended=self.clock())
                self._held_back = None
                if self.spin and self.det.last_dip >= LONG_QUIET:
                    self.spin.sure = False       # long enough to have turned or changed the record
                self._clear_recent()
                self._mark_start()
                moved = False
                if self.spin:
                    self.spin.mismatch = None            # a new track is a new chance to match the album
                    late, was = self.spin.late_gap(self.clock()), self.spin.i
                    moved = self.spin.track_change(self.clock(), self.det.last_dip)
                    self._keep(self.timer.gap(self.spin, self.clock(), self.det.last_dip, late, moved, was))
                if moved:
                    self._spin_sync()
                else:
                    # Not known what this is yet. It is not the previous song any more, so nothing
                    # heard from here on may be learned into that one.
                    self.current = None
                self._schedule(self.s.settle_seconds, quick=True)
            elif ev is Event.STOPPED:
                log.info("Record stopped (the sound dropped out)" if self.det.stop_lost else "Record stopped")
                self._record_stopped(dropout=self.det.stop_lost)

            # A gap held as a quiet passage, or another name from Shazam: the track may not be what
            # the list says, and running out its listed length does not show that it is.
            doubt = bool(self.spin and (self.spin.held or self.spin.named_otherwise))
            if self.spin and self.det.playing and not self.det.in_dip and self.spin.tick(self.clock()):
                # songs running together: the listed length ran out before a gap was heard
                # (never in a quiet stretch: that is a gap, or the side has ended)
                self._commit_learning(trim_tail=True, extra_trim=LENGTH_GRACE, hold=doubt)
                self._clear_recent()
                self._keep(self.timer.moved_on())
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
                    self.rec.reachable = False
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
                        # not on this album: save nothing; ask again soon only while in doubt
                        self._schedule(self.s.recheck_seconds if self.spin.sure else self.s.retry_seconds)
                        return
                if song_id is None:
                    song_id, needs_art = self.lib.upsert(track.title, track.artist, track.album, track.shazam_id)
                    song = self.lib.song(song_id)
                    if needs_art and song and song.album_id and (art := await self.rec.art_for(track)):
                        if data := await self.rec.fetch(art):
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
                sure = self.spin is not None and self.spin.sure
                self._schedule(self.s.recheck_seconds if sure else self.s.retry_seconds)   # soon while in doubt
                return

            self._found_at = self.clock()
            if song_id == self.current:
                moved = self._apply_position(pos, clip_start) if not self.start_exact else False
                # confirmed: keep what was heard, up to the end of the clip that was recognized
                self._commit_learning(confirmed=True, upto=clip_start + len(pcm) // BLOCK_SAMPLES)
                if moved:
                    self.refresh_now()
            else:
                self._reset_learning()                        # new song: discard unverified audio
                self.unknown = False
                self._apply_position(pos, clip_start)
                if source == "shazam" and self.s.learn:       # Shazam-verified clip
                    self._bg(self.lib.learn, song_id, hs, self._rel(clip_start))
                self.current, self.current_source = song_id, source
                self._count_play(song_id)
                self.refresh_now()
                self.pub.library(self.lib.size())
                info = self.now_info() or {}
                log.info("Now playing (%s): %s - %s", source, info.get("artist"), info.get("title"))
            self._want_tracklist(song_id)
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
            return None                         # forgotten while it was being looked up
        if self.spin:
            tr = self.lib.track_in_album(self.spin.album_id, song_id)
            if tr is None:
                if self._place_is_sure():
                    self.spin.named_otherwise = True
                    return None                 # the record has played on unbroken: it is still this album
                seen = self.spin.mismatch
                if (self.spin.auto and not self.spin.trusted and seen and seen.get("id") == song_id) \
                        or self._record_changed(song.artist, song.album, song_id):
                    # the album was only a guess, or another record is on: let go
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
            self.spin.sure = True
            self.spin.seen.add(tr["id"])
            song_id = self.lib.attach_song(song_id, tr)
            j = next(i for i, t in enumerate(self.spin.tracks) if t["id"] == tr["id"])
            if j != self.spin.i:
                self.timer.jumped(self.spin, self.spin.i, j)
                self.spin.jump(j, None)        # a different track of the same album: follow it
                self._save_spin()
            else:
                self.spin.guessed = False
                self._keep(self.timer.settle(True))     # the gap before this song was a real one
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

    def _record_changed(self, artist: str, album: str, key) -> bool:
        """An album followed only because a song of it was recognized, not played on unbroken
        since, and now a second song that is not from it: another record is on."""
        sp = self.spin
        seen = sp.mismatch if sp else None
        if not sp or not seen or not sp.auto or sp.sure:
            return False
        mine = self.lib.album(sp.album_id) or {}
        if not any(_same_artist(mine.get("artist", ""), a) for a in (artist, seen.get("artist", ""))):
            return True                                    # two songs by someone else
        return (key != (seen.get("key") or seen.get("id"))   # two different songs, both from other albums
                and all(a and _norm(a) != _norm(mine.get("album", "")) for a in (album, seen.get("album", ""))))

    # --- track lists fetched by themselves ------------------------------------------
    def _want_tracklist(self, song_id: int) -> None:
        """A song was recognized on an album that has no track list: fetch one, so the track,
        the side and the progress are known without adding the album by hand."""
        if self.spin or (self._tracklist_task and not self._tracklist_task.done()):
            return
        song = self.lib.song(song_id)
        if not song or not song.album_id or not song.album or self.lib.album_tracks(song.album_id):
            return
        if self.clock() < self._tracklist_after.get(song.album_id, float("-inf")):
            return
        self._tracklist_task = asyncio.create_task(self._fetch_tracklist(song.album_id, song_id))

    async def _fetch_tracklist(self, album_id: int, song_id: int) -> None:
        try:
            album, song = self.lib.album(album_id), self.lib.song(song_id)
            if not album or not song:
                return
            self._tracklist_after[album_id] = self.clock() + TRACKLIST_RETRY
            rel = await self.rec.vinyl_release(album["artist"], album["album"], song.title)
            if rel is None:
                return                                      # no answer: asked again later
            self._tracklist_after[album_id] = float("inf")
            if not rel:
                log.info("MusicBrainz has no vinyl track list for %s - %s", album["artist"], album["album"])
                return
            if not self.lib.album(album_id) or self.lib.album_tracks(album_id):
                return                                      # gone, or you added a list meanwhile
            self.lib.add_tracks(album_id, rel["mbid"], rel["tracks"], fetched=True)
            log.info("Track list fetched for %s - %s (%d tracks)", album["artist"], album["album"],
                     len(rel["tracks"]))
            self._follow(song_id)
        except Exception:
            log.exception("Fetching a track list failed")

    def _follow(self, song_id: int) -> None:
        """The song on screen has a track list now: start following its album straight away."""
        if self.spin or not self.det.playing or self.current != song_id \
                or self.current_source not in ("shazam", "local"):
            return
        found = self._reconcile(song_id)
        if found is None or not self.spin:
            return
        self.current = found
        if self.start_exact:                               # where in the song we are is already known
            self._apply_position((self.block_i - self.song_start) * BLOCK_SECONDS, self.block_i)
        self.refresh_now()
        self._save_spin()

    def _place_is_sure(self) -> bool:
        """The album is not a mere guess, and the record has played on since its place was known:
        a title from outside the album is then no reason to doubt where the record is."""
        return self.spin.sure and (not self.spin.auto or self.spin.trusted)

    def _foreign(self, track) -> bool:
        """Shazam named a song that is neither on the album being followed nor in the library.
        Nothing is saved for it. True = the album was only a guess and this is the second time
        the same song was heard: stop following, and take the song."""
        if self._place_is_sure():
            self.spin.named_otherwise = True
            return False                        # Shazam only knows this track by another name
        key = track.shazam_id or f"{track.title}|{track.artist}"
        seen = self.spin.mismatch
        if self._record_changed(track.artist, track.album, key):
            log.info("Another record is on; following stopped")
            self.spin = None
            self._save_spin()
            return True
        if seen and seen.get("key") == key:
            if not self.spin.auto or self.spin.trusted:
                return False
            log.info("Not this album after all; following stopped")
            self.spin = None
            self._save_spin()
            return True
        self.spin.mismatch = {"id": None, "key": key, "shazam_id": track.shazam_id, "title": track.title,
                              "artist": track.artist, "album": track.album}
        self.refresh_now()
        return False

    def _apply_position(self, pos: float | None, clip_start: int) -> bool:
        """Shazam or a local match said where in the song the clip started: fix the song's start."""
        if pos is None:
            return False
        if self.spin and self.spin.anchored and self.spin.start is not None:
            # This track's start was heard: the needle drop, or the gap before it. A position worked
            # out from a fingerprint may be off (learned from an earlier play that began elsewhere),
            # so where the two disagree, what was heard stands.
            now_pos = pos + (self.block_i - clip_start) * BLOCK_SECONDS
            if abs((self.clock() - now_pos) - self.spin.start) > 5:
                self.song_start = self.block_i - (self.clock() - self.spin.start) / BLOCK_SECONDS
                self.start_exact = True
                return False
        self.song_start, self.start_exact = clip_start - pos / BLOCK_SECONDS, True
        if self.spin:
            now_pos = pos + (self.block_i - clip_start) * BLOCK_SECONDS
            old = self.spin.start
            self.spin.start = self.clock() - now_pos
            # heard past its listed length: this pressing's track is longer, so wait for the gap
            length = self.spin.cur.get("length")
            self.spin.overrun = bool(length) and now_pos >= length
            self._keep(self.timer.heard(self.spin, self.spin.start))
            return old is None or abs(old - self.spin.start) > 2
        return False

    def _spin_sync(self) -> None:
        """The spin moved to a new track: make it the current song."""
        sp = self.spin
        sid = self.lib.song_for_track(sp.cur["id"])
        self.current, self.current_source, self.unknown = sid, "spin", False
        if sp.start is not None:
            self.song_start = self.block_i - (self.clock() - sp.start) / BLOCK_SECONDS
        self._count_play(sid)
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
                # "saved" is when the record was last heard: now while it plays, else when it stopped.
                # (Saving again later, after an edit on the page, must not make an old record look fresh.)
                idle = self.clock() - self.spin.stopped_at if self.spin.stopped_at is not None else 0.0
                self._spin_file.write_text(json.dumps(
                    {"album_id": self.spin.album_id, "track_id": self.spin.cur["id"],
                     "saved": time.time() - max(0.0, idle),
                     "auto": self.spin.auto, "side_ended": self.spin.at_side_end,
                     "fresh": self.spin.fresh}))
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
        spin.fresh = bool(saved.get("fresh"))
        if spin.fresh:
            return spin                    # picked with the needle up: the first start is a needle drop
        # the next start resumes this track (position unknown); the time since counts as stopped time
        spin.stopped_at = self.clock() - max(0.0, time.time() - saved["saved"])
        spin.at_side_end = bool(saved.get("side_ended"))
        spin.sure = False                  # the app was away: the record may have been changed
        return spin

    def start_spin(self, album_id: int, side: str = "", pos: int = 1) -> bool:
        """Pick the album and where on it you are (default A1)."""
        tracks = self.lib.album_tracks(album_id)
        if not tracks:
            return False
        i = next((k for k, t in enumerate(tracks)
                  if t["side"].upper() == (side or tracks[0]["side"]).upper() and t["pos"] == pos), 0)
        # The song on screen was recognized during this very song, as another track of this album,
        # and you point at the right one: it was this track all along. Remember that.
        if (self.det.playing and self.spin and self.spin.album_id == album_id and self.current is not None
                and self.current_source in ("shazam", "local") and self._found_at >= self._marked_at
                and tracks[i]["id"] != self.spin.cur["id"]):
            self.lib.retrack(self.current, tracks[i])
            log.info("Learned: what was found as %s is %s%s", self.spin.cur["title"], tracks[i]["side"], tracks[i]["pos"])
            tracks = self.lib.album_tracks(album_id)
        self.spin = Spin(album_id, tracks, i)
        self.spin.fresh = not self.det.playing
        self.timer.reset()
        self._save_spin()
        if self.det.playing:                    # needle is already down: position unknown until the next gap
            self.spin.stopped_at = None
            self._reset_learning()
            self._mark_start()
            self._spin_sync()
            self._schedule(self.s.settle_seconds, quick=True)
        return True

    def same_track(self) -> bool:
        """The song Shazam named, which is not on the album's list, is the track now playing:
        Shazam only knows it under another name. Remember that."""
        m = self.spin.mismatch if self.spin else None
        if not m or not (m.get("id") or m.get("shazam_id")):
            return False
        if m.get("id"):
            self.lib.retrack(m["id"], self.spin.cur)
            self.spin.tracks = self.lib.album_tracks(self.spin.album_id)
        else:
            self.lib.alias_shazam(self.lib.song_for_track(self.spin.cur["id"]), m["shazam_id"])
        log.info("Learned: %s is %s%s", m["title"], self.spin.cur["side"], self.spin.cur["pos"])
        self.spin.mismatch = None
        self.spin.sure = True              # you said where the record is
        self.spin.seen.add(self.spin.cur["id"])
        self._spin_sync()
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
        wrong = None
        if self.current is not None and self.current_source != "manual":
            # replacing a wrong match: drop the audio that was learned under it this play,
            # and don't let the next check bring the wrong song back
            self.rejected = self.current
            # Found within the last moments, so it is this very sound that got the wrong name.
            # Later, the record may have run on into the next song while the old title still shows.
            fresh = self.clock() - self._found_at <= 40
            wrong = self.current if fresh and self.current_source in ("shazam", "local") else None
            self._reset_learning()
        album_id = self.lib.add_album(artist, album) if album else None
        song_id = self.lib.add_song(album_id, title, artist)
        if wrong is not None and self.lib.fold_into(wrong, song_id):
            self.rejected = None          # found under that name again, it is now this song
            self._counted.add(song_id)    # ... and its play just now was this song's play
        self.spin = None                  # you said what it is: stop following the album
        self._save_spin()
        if self.s.learn and len(self.recent) * BLOCK_SECONDS >= 5:
            self._learn(song_id, np.concatenate(self.recent), self._rel(self.recent_start))
        self.current, self.current_source, self.unknown = song_id, "manual", False
        self._labelled_at = self.clock()
        self._reset_learning()
        self._count_play(song_id)
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
                "quiet_level": round(self.s.signal_threshold_db), "quiet_auto": self._auto_quiet,
                "quiet_set": self._quiet_set,
                "library_mb": round(self.lib.size_bytes() / 1048576, 1),
                "library_limit_mb": self.s.max_library_mb, "library_full": self.lib.full,
                "shazam_ok": getattr(self.rec, "reachable", True)}

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
            self.timer.reset()                 # another track list: what was being timed is gone
        if not tracks:
            self.end_spin()
            return
        self.spin.tracks = tracks
        self.spin.i = next((k for k, t in enumerate(tracks)
                            if (t["side"], t["pos"]) == (old["side"], old["pos"])), 0)
        self._save_spin()
        self.refresh_now()

    def forget(self, song_id: int) -> None:
        if not self.lib.forget(song_id):
            return                        # no such song: nothing changed
        if self.spin and not self.lib.album_tracks(self.spin.album_id):
            self.end_spin()               # it was the only song of an album fetched by mistake
        if self.det.playing:              # giving the space back holds the library for a moment:
            self._compact_due = True      # not while a record plays
        else:
            self._bg(self.lib.compact)    # so a full library can learn again
        self.pub.library(self.lib.size())
        if self.busy and self.det.playing:
            # a lookup under way may be about to name this very song: its answer is dropped
            self.epoch += 1
            self.next_check = self.clock()
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
