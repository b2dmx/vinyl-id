"""Vinyl ID: listens to the record player feed, identifies songs, tells Home Assistant.

Identification is local-first: the saved library is checked before Shazam.
Learning is verified: audio is only added to a song's fingerprint after a later
check confirms the same song was still playing.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections import deque
from pathlib import Path

import numpy as np

from . import web
from .audio import ffmpeg_blocks, level_db
from .config import AI_NOTE, BLOCK_SECONDS, SAMPLE_RATE, SIGNATURE, Settings
from .detector import Detector, Event
from .fingerprint import FRAMES_PER_SECOND, hashes
from .library import Library
from .mqtt import Publisher
from .recognize import Recognizer

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
        self._last_level: tuple[float, float] = (0.0, -999.0)

    # --- scheduling ------------------------------------------------------------
    def _schedule(self, delay: float) -> None:
        self.next_check = self.clock() + delay
        self.buf.clear()

    def _reset_learning(self) -> None:
        self.learn_blocks, self.learn_start = [], self.block_i

    def _commit_learning(self, trim_tail: bool = False) -> None:
        """Add confirmed audio to the current song's fingerprint."""
        blocks = self.learn_blocks[:-TAIL_TRIM_BLOCKS] if trim_tail else self.learn_blocks
        if self.s.learn and self.current is not None and len(blocks) * BLOCK_SECONDS >= 5:
            song_id, t0 = self.current, int(self.learn_start * FRAMES_PER_BLOCK)
            pcm = np.concatenate(blocks)

            def work() -> None:   # off the audio loop
                hs = hashes(pcm)
                self.lib.learn(song_id, hs, t0)
                log.debug("Learned %d hashes for song %s", len(hs), song_id)

            asyncio.get_running_loop().run_in_executor(None, work)
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
                self._schedule(self.s.settle_seconds)
            elif ev is Event.TRACK_CHANGE:
                log.info("Track change detected")
                self._commit_learning(trim_tail=True)
                self._clear_recent()
                self._schedule(self.s.settle_seconds)
            elif ev is Event.STOPPED:
                log.info("Record stopped")
                self._commit_learning(trim_tail=True)
                self.next_check, self.current, self.unknown = None, None, False
                self.pub.playing(False)
                self.pub.track(None)

            if (self.det.playing and not self.busy and self.next_check is not None
                    and self.clock() >= self.next_check and len(self.buf) == self.buf.maxlen):
                self.busy = True
                clip_start = self.block_i - len(self.buf)
                asyncio.create_task(self._identify(np.concatenate(self.buf), clip_start))

    async def _identify(self, pcm: np.ndarray, clip_start: int) -> None:
        try:
            hs = await asyncio.to_thread(hashes, pcm)
            local = await asyncio.to_thread(self.lib.match, hs) if self.s.learn else None
            if local:
                song_id, source = local[0], "local"
            else:
                track = await self.rec.identify(pcm)
                if not self.det.playing:
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

            if song_id == self.current:
                self._commit_learning()                       # confirmed: keep what we heard
            else:
                self._reset_learning()                        # new song: discard unverified audio
                self.unknown = False
                if source == "shazam" and self.s.learn:       # Shazam-verified clip
                    self.lib.learn(song_id, hs, int(clip_start * FRAMES_PER_BLOCK))
                self.current, self.current_source = song_id, source
                self.lib.played(song_id)
                self.refresh_now()
                self.pub.library(self.lib.size())
                info = self.now_info() or {}
                log.info("Now playing (%s): %s - %s", source, info.get("artist"), info.get("title"))
            self._schedule(self.s.recheck_seconds)
        finally:
            self.busy = False

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
        if self.s.learn and len(self.recent) * BLOCK_SECONDS >= 5:
            pcm, t0 = np.concatenate(self.recent), int(self.recent_start * FRAMES_PER_BLOCK)
            asyncio.get_running_loop().run_in_executor(
                None, lambda: self.lib.learn(song_id, hashes(pcm), t0))
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
        art_url = f"{self.s.public_url}/api/albums/{song.album_id}/art" if song.art_file else ""
        return {"id": song.id, "title": song.title, "artist": song.artist, "album": song.album,
                "album_id": song.album_id, "art_url": art_url, "source": self.current_source}

    def status(self) -> dict:
        return {"playing": self.det.playing, "unknown": self.unknown, "now": self.now_info(),
                "library": self.lib.size(), "learn": self.s.learn}

    def refresh_now(self) -> None:
        """Re-send the current song (after an edit in the UI)."""
        info = self.now_info()
        if info:
            art = self.lib.art_bytes(info["album_id"]) if info["album_id"] else None
            # cache-bust so HA/M-1 pick up a replaced cover
            if info["art_url"]:
                info["art_url"] += f"?v={int(time.time())}"
            self.pub.track(info, art)

    def forget(self, song_id: int) -> None:
        self.lib.forget(song_id)
        self.pub.library(self.lib.size())
        if song_id == self.current:
            self.current = None
            self._reset_learning()
            self.pub.track(None)
            if self.det.playing:
                self._schedule(self.s.settle_seconds)

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
        raise SystemExit("AUDIO_INPUT is not set (see docker-compose.yml)")
    app = App(s)
    runner = await web.serve(app, s.web_port)
    task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
    try:
        await app.run()
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()
        await app.close()


assert SAMPLE_RATE == 16000

if __name__ == "__main__":
    asyncio.run(main())
