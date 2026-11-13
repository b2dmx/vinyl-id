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
from .library import Library
from .mqtt import Publisher
from .recognize import Recognizer
from .spin import EXPIRE_AFTER, LENGTH_GRACE, Spin

log = logging.getLogger("vinyl_id")
FRAMES_PER_BLOCK = BLOCK_SECONDS * FRAMES_PER_SECOND
MAX_LEARN_BLOCKS = int(300 / BLOCK_SECONDS)   # 5 min of unconfirmed audio at most
TAIL_TRIM_BLOCKS = int(3 / BLOCK_SECONDS)     # drop the last 3 s before a gap
RECENT_BLOCKS = int(90 / BLOCK_SECONDS)       # audio kept for labelling an unknown song


class App:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.clock = time.monotonic           # swappable in tests
        self.loop: asyncio.AbstractEventLoop | None = None
        self.det = Detector(s)
        self.lib = Library(Path(s.data_dir))
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
        self.spin: Spin | None = self._load_spin()   # 'now spinning': the album and track being played
        self.song_start = 0.0                  # block index where the current song began (learning is stored relative to it)
        self.start_exact = False               # True once Shazam or a local match told us the real position

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
        self._bg(lambda: self.lib.learn(song_id, hashes(pcm), t0))

    def _rel(self, block: float) -> int:
        """Frames from the start of the current song (what the library stores)."""
        return max(0, int((block - self.song_start) * FRAMES_PER_BLOCK))

    def _mark_start(self) -> None:
        self.song_start, self.start_exact = self.block_i, False

    def _reset_learning(self) -> None:
        self.learn_blocks, self.learn_start = [], self.block_i

    def _commit_learning(self, trim_tail: bool = False, extra_trim: float = 0.0) -> None:
        """Add confirmed audio to the current song's fingerprint."""
        cut = (TAIL_TRIM_BLOCKS if trim_tail else 0) + int(extra_trim / BLOCK_SECONDS)
        blocks = self.learn_blocks[:-cut] if cut else self.learn_blocks
        if self.s.learn and self.current is not None and len(blocks) * BLOCK_SECONDS >= 5:
            self._learn(self.current, np.concatenate(blocks), self._rel(self.learn_start))
        self._reset_learning()

    # --- main loop ---------------------------------------------------------------
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.pub.start()
        self.pub.playing(False)
        self.pub.library(self.lib.size())
        async for block in ffmpeg_blocks(self.s.audio_input, self.s.audio_input_format):
            db = level_db(block)
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
                if self.spin:
                    if self.spin.started(self.clock()) == "expired":
                        self.spin = None
                        self._save_spin()
                    else:
                        self._spin_sync()
                self._schedule(self.s.settle_seconds, quick=True)
            elif ev is Event.TRACK_CHANGE:
                log.info("Track change detected")
                self._commit_learning(trim_tail=True)
                self._clear_recent()
                self._mark_start()
                if self.spin and self.spin.track_change(self.clock()):
                    self._spin_sync()
                self._schedule(self.s.settle_seconds, quick=True)
            elif ev is Event.STOPPED:
                log.info("Record stopped")
                self._commit_learning(trim_tail=True)
                self.next_check, self.current, self.unknown = None, None, False
                self.epoch += 1
                if self.spin:
                    self.spin.stopped(self.clock())
                self.pub.playing(False)
                self.pub.track(None)

            if self.spin and self.det.playing and not self.det.in_dip and self.spin.tick(self.clock()):
                # songs running together: the listed length ran out before a gap was heard
                # (never in a quiet stretch: that is a gap, or the side has ended)
                self._commit_learning(trim_tail=True, extra_trim=LENGTH_GRACE)
                self._clear_recent()
                self._spin_sync()
                self._schedule(self.s.settle_seconds, quick=True)

            if (self.det.playing and not self.busy and self.next_check is not None
                    and self.clock() >= self.next_check and len(self.buf) >= self._blocks_needed()):
                self.busy = True
                clip_start = self.block_i - len(self.buf)
                self._task = asyncio.create_task(
                    self._identify(np.concatenate(self.buf), clip_start, quick=self._quick))

    def _blocks_needed(self) -> int:
        quick = int(self.s.quick_sample_seconds / BLOCK_SECONDS)
        return min(quick, self.buf.maxlen) if self._quick else self.buf.maxlen

    async def _identify(self, pcm: np.ndarray, clip_start: int, quick: bool = False) -> None:
        epoch = self.epoch
        try:
            hs = await asyncio.to_thread(hashes, pcm)
            local = await asyncio.to_thread(self.lib.match, hs) if self.s.learn else None
            pos: float | None = None          # seconds into the song where this clip started
            if local:
                song_id, source, pos = local[0], "local", local[2] / FRAMES_PER_SECOND
            else:
                track = await self.rec.identify(pcm)
                if epoch != self.epoch:
                    return
                if track is None and quick:
                    # too little to go on: keep collecting and ask again with the full sample
                    log.info("Not recognized from a short sample; trying a longer one")
                    self._quick, self.next_check = False, self.clock()
                    return
                if track is None:
                    log.info("Not recognized; retrying in %.0f s", self.s.retry_seconds)
                    if self.current is None and not self.unknown:
                        self.unknown = True
                        self.pub.track({"title": "Unknown", "source": "unknown"})
                    self._schedule(self.s.retry_seconds)
                    return
                song_id, needs_art = self.lib.upsert(track.title, track.artist, track.album, track.shazam_id)
                song = self.lib.song(song_id)
                if needs_art and track.art_url and song and song.album_id:
                    if data := await self.rec.fetch(track.art_url):
                        self.lib.set_art(song.album_id, data, locked=False)
                source = "shazam"
                pos = track.offset

            if epoch != self.epoch:       # the record stopped or moved on while this was worked out
                return
            song_id = self._reconcile(song_id)
            if song_id is None:                               # not on the album being spun: ask, don't learn
                self._schedule(self.s.recheck_seconds)
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
                if not self.spin.mismatch:
                    self.spin.mismatch = {"title": song.title, "artist": song.artist, "album": song.album}
                    self.refresh_now()
                return None
            self.spin.mismatch = None
            song_id = self.lib.attach_song(song_id, tr)
            j = next(i for i, t in enumerate(self.spin.tracks) if t["id"] == tr["id"])
            if j != self.spin.i:
                self.spin.jump(j, None)        # a different track of the same album: follow it
            return song_id
        tr = self.lib.find_track(song.title, song.artist)
        if tr:                                  # auto-attach: tracklist and timer start by themselves
            tracks = self.lib.album_tracks(tr["album_id"])
            song_id = self.lib.attach_song(song_id, tr)
            self.spin = Spin(tr["album_id"], tracks, next(i for i, t in enumerate(tracks) if t["id"] == tr["id"]),
                             auto=True)
            log.info("Attached to pre-loaded album %s", tr["album_id"])
        return song_id

    def _apply_position(self, pos: float | None, clip_start: int) -> bool:
        """Shazam or a local match said where in the song the clip started: fix the song's start."""
        if pos is None:
            return False
        self.song_start, self.start_exact = clip_start - pos / BLOCK_SECONDS, True
        if self.spin:
            now_pos = pos + (self.block_i - clip_start) * BLOCK_SECONDS
            old = self.spin.start
            self.spin.start = self.clock() - now_pos
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
                    {"album_id": self.spin.album_id, "track_id": self.spin.cur["id"], "saved": time.time()}))
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
        except (OSError, ValueError, KeyError, StopIteration):
            return None
        spin = Spin(saved["album_id"], tracks, i)
        spin.stopped_at = self.clock()     # the next start resumes this track, position unknown
        return spin

    def start_spin(self, album_id: int, side: str = "", pos: int = 1) -> bool:
        """Pick the album and where on it you are (default A1)."""
        tracks = self.lib.album_tracks(album_id)
        if not tracks:
            return False
        i = next((k for k, t in enumerate(tracks)
                  if t["side"].upper() == (side or tracks[0]["side"]).upper() and t["pos"] == pos), 0)
        self.spin = Spin(album_id, tracks, i)
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
        if self.current is not None and self.current_source != "manual":
            # replacing a wrong match: drop the audio that was learned under it this play
            self._reset_learning()
        album_id = self.lib.add_album(artist, album) if album else None
        song_id = self.lib.add_song(album_id, title, artist)
        self.spin = None                  # you said what it is: stop following the album
        self._save_spin()
        if self.s.learn and len(self.recent) * BLOCK_SECONDS >= 5:
            self._learn(song_id, np.concatenate(self.recent), self._rel(self.recent_start))
        self.current, self.current_source, self.unknown = song_id, "manual", False
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
        # ?v= changes only when the cover does, so HA and the M-1 don't download it again per song
        art_url = f"{base}{path}?v={self.lib.art_version(name)}" if name else ""
        info = {"id": song.id, "title": song.title, "artist": song.artist, "album": song.album,
                "album_id": song.album_id, "art_url": art_url, "source": self.current_source}
        if self.spin:
            # no polling: HA and the M-1 work progress out from the start time and the length
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

    def status(self) -> dict:
        return {"playing": self.det.playing, "unknown": self.unknown, "now": self.now_info(),
                "library": self.lib.size(), "learn": self.s.learn, "spin": self.spin_status()}

    def refresh_now(self) -> None:
        """Send the current song to Home Assistant (new song, or after an edit in the UI)."""
        info = self.now_info()
        if info:
            self.pub.track(info, self.lib.art_file_bytes(self._art_name) if info["art_url"] else None)

    def forget(self, song_id: int) -> None:
        self.lib.forget(song_id)
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
        raise SystemExit("No audio to listen to: with the Stream audio source, set the stream address (AUDIO_INPUT)")
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
