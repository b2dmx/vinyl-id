"""End-to-end: first play goes to Shazam and is learned; the replay is identified locally."""

import asyncio

import numpy as np

import vinyl_id.__main__ as main_mod
from vinyl_id.config import BLOCK_SECONDS, SAMPLE_RATE, Settings
from vinyl_id.recognize import Track
from test_library import degrade, fake_song

BLOCK = int(SAMPLE_RATE * BLOCK_SECONDS)


class FakeClock:
    t = 0.0

    def __call__(self):
        return self.t


class FakePub:
    def __init__(self, *_a, **_k):
        self.tracks = []

    def start(self): ...
    def stop(self): ...
    def playing(self, on): ...
    def library(self, n): ...
    def level(self, db): ...

    def track(self, info, art=None):
        self.tracks.append(info and (info["title"], info.get("source", "")))


def test_learn_then_local(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(main_mod, "Publisher", FakePub)

    silence = np.zeros(SAMPLE_RATE * 50, np.int16)
    side = np.concatenate([fake_song(1, 200), silence, degrade(fake_song(1, 200), 3), silence])

    async def blocks(*_a):
        for i in range(0, len(side) - BLOCK, BLOCK):
            clock.t += BLOCK_SECONDS
            yield side[i:i + BLOCK]
            await asyncio.sleep(0.001)  # let worker threads keep up (fast-forwarded clock)

    monkeypatch.setattr(main_mod, "ffmpeg_blocks", blocks)
    calls = []

    async def fake_identify(self, pcm):
        calls.append(1)
        return Track("Song 1", "Artist", "LP", "", "shz1")

    monkeypatch.setattr(main_mod.Recognizer, "identify", fake_identify)

    s = Settings(data_dir=str(tmp_path), audio_input="x", recheck_seconds=40)
    app = main_mod.App(s)
    app.clock = clock
    asyncio.run(app.run())

    sources = [t[1] for t in app.pub.tracks if t]
    assert sources[0] == "shazam"
    assert sources[-1] == "local"           # replay found without Shazam
    assert app.lib.hash_count(1) > 5000      # most of the song was learned
    assert len(calls) <= 8                   # Shazam only during the first play (rechecks included)


def _run(tmp_path, monkeypatch, identify, learn=True, on_block=None):
    clock = FakeClock()
    monkeypatch.setattr(main_mod, "Publisher", FakePub)
    silence = np.zeros(SAMPLE_RATE * 50, np.int16)
    side = np.concatenate([fake_song(5, 120), silence, degrade(fake_song(5, 120), 2), silence])
    holder = {}

    async def blocks(*_a):
        for n, i in enumerate(range(0, len(side) - BLOCK, BLOCK)):
            clock.t += BLOCK_SECONDS
            if on_block:
                on_block(holder["app"], n)
            yield side[i:i + BLOCK]
            await asyncio.sleep(0.001)

    monkeypatch.setattr(main_mod, "ffmpeg_blocks", blocks)
    monkeypatch.setattr(main_mod.Recognizer, "identify", identify)
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x", recheck_seconds=40, learn=learn))
    app.clock = clock
    holder["app"] = app
    asyncio.run(app.run())
    return app


def test_unknown_record_labelled_by_hand(tmp_path, monkeypatch):
    async def never(self, pcm):
        return None

    def label(app, n):
        if n == int(100 / BLOCK_SECONDS):          # 100 s into the first play
            assert app.unknown
            app.label_now("Rare B-side", "Local Band", "Demo Tape")

    app = _run(tmp_path, monkeypatch, never, on_block=label)
    titles = [t for t in app.pub.tracks if t]
    assert ("Rare B-side", "manual") in titles
    assert titles[-1] == ("Rare B-side", "local")   # replay recognized from the hand label


def test_learning_off_always_uses_shazam(tmp_path, monkeypatch):
    calls = []

    async def shazam(self, pcm):
        calls.append(1)
        return Track("Song 5", "Artist", "LP", "", "shz5")

    app = _run(tmp_path, monkeypatch, shazam, learn=False)
    assert all(src == "shazam" for _, src in filter(None, app.pub.tracks))
    assert app.lib.hash_count(1) == 0 and len(calls) >= 4


def test_answer_for_a_song_that_already_ended_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "Publisher", FakePub)
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    app.det.playing = True

    async def slow_shazam(self, pcm):
        app._schedule(6)                           # a track change arrives while Shazam is thinking
        return Track("Old Song", "Artist", "LP", "", "shz9")

    monkeypatch.setattr(main_mod.Recognizer, "identify", slow_shazam)
    asyncio.run(app._identify(fake_song(3, 12), 0))
    assert app.current is None and app.pub.tracks == [] and not app.busy


def test_failed_identification_retries_later(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "Publisher", FakePub)
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x", retry_seconds=30))
    app.clock = FakeClock()
    app.det.playing = True

    async def broken(self, pcm):
        raise RuntimeError("boom")

    monkeypatch.setattr(main_mod.Recognizer, "identify", broken)
    asyncio.run(app._identify(fake_song(3, 12), 0))
    assert app.next_check == 30 and not app.busy
