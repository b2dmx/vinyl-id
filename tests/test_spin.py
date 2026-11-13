"""Track lists, 'now spinning', and song positions."""

import asyncio

import numpy as np

import vinyl_id.__main__ as main_mod
from vinyl_id.config import BLOCK_SECONDS, SAMPLE_RATE, Settings
from vinyl_id.fingerprint import FRAMES_PER_SECOND, hashes
from vinyl_id.library import Library
from vinyl_id.recognize import Track, parse_release
from vinyl_id.spin import Spin
from test_flow import FakeClock
from test_library import fake_song

BLOCK = int(SAMPLE_RATE * BLOCK_SECONDS)

RELEASE = {
    "id": "mb-1", "title": "Test LP", "date": "1979-05-01",
    "artist-credit": [{"name": "The Band", "joinphrase": ""}],
    "media": [{"position": 1, "format": '12" Vinyl', "tracks": [
        {"number": "A1", "title": "Song One", "length": 60000},
        {"number": "A2", "title": "Song Two (Remastered)", "length": 90000},
        {"number": "B1", "title": "Song Three", "length": None, "recording": {"length": 120000}},
    ]}],
}


def tracks():
    return parse_release(RELEASE)["tracks"]


def test_parse_release_vinyl_numbers():
    rel = parse_release(RELEASE)
    assert (rel["artist"], rel["album"], rel["year"]) == ("The Band", "Test LP", "1979")
    assert [(t["side"], t["pos"], t["n"]) for t in rel["tracks"]] == [("A", 1, 1), ("A", 2, 2), ("B", 1, 3)]
    assert rel["tracks"][0]["length"] == 60 and rel["tracks"][2]["length"] == 120


def test_second_lp_sides_continue():
    lp = lambda nums: {"position": 1, "format": "12\" Vinyl", "tracks": [{"number": x, "title": x} for x in nums]}  # noqa: E731
    rel = parse_release({"id": "m", "media": [lp(["A1", "A2", "B1"]), {**lp(["A1", "B1", "B2"]), "position": 2}]})
    assert [(t["disc"], t["side"], t["pos"]) for t in rel["tracks"]] == [
        (1, "A", 1), (1, "A", 2), (1, "B", 1), (2, "C", 1), (2, "D", 1), (2, "D", 2)]
    rel = parse_release({"id": "m", "media": [lp(["A1", "B1"]), {**lp(["C1", "D1"]), "position": 2}]})
    assert [t["side"] for t in rel["tracks"]] == ["A", "B", "C", "D"]          # already continuing: left alone


def test_library_tracks_and_titles(tmp_path):
    lib = Library(tmp_path)
    album = lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks())
    sid, _ = lib.upsert("Song Two", "The Band", "Test LP (Deluxe)", "shz2")   # Shazam's album name differs
    t = lib.find_track("Song Two", "The Band")
    assert t and t["album_id"] == album and t["side"] == "A"
    assert lib.attach_song(sid, t) == sid
    assert lib.song(sid).album_id == album
    assert lib.track_in_album(album, sid)["title"].startswith("Song Two")
    assert lib.find_track("Song Two", "Someone Else") is None
    lib.forget(sid)
    assert lib.album_tracks(album)                    # the pre-loaded album stays


def test_match_returns_position(tmp_path):
    lib = Library(tmp_path)
    sid, _ = lib.upsert("A", "B", "", "x")
    song = fake_song(7, 120)
    lib.learn(sid, hashes(song), 0)                   # learned from the song's start
    clip = song[SAMPLE_RATE * 40:SAMPLE_RATE * 52]
    hit = lib.match(hashes(clip))
    assert hit and hit[0] == sid
    assert abs(hit[2] / FRAMES_PER_SECOND - 40) < 1.0


def test_spin_moves_through_a_side():
    sp = Spin(1, tracks(), 0)
    assert sp.started(10.0) == "new" and sp.start == 10.0
    assert not sp.tick(40.0)                          # still inside track 1 (60 s)
    assert sp.tick(75.0) and sp.i == 1                # length ran out: on to track 2
    assert sp.start == 70.0                           # the new track started when track 1 ended
    assert not sp.tick(200.0)                         # last track of the side: wait for the stop
    sp.stopped(300.0)
    assert sp.started(400.0) == "flip" and sp.cur["side"] == "B" and sp.start == 400.0


def test_spin_resume_and_expiry():
    sp = Spin(1, tracks(), 0)
    sp.started(0.0)
    sp.stopped(50.0)                                  # lifted mid-side
    assert sp.started(80.0) == "resume" and sp.start is None and sp.i == 0
    sp.stopped(100.0)
    assert sp.started(100.0 + 3600) == "expired"


def test_spin_info_has_no_polling_fields():
    sp = Spin(1, tracks(), 1, start=100.0)
    info = sp.info(now=130.0, wall=1_000_000.0)
    assert (info["side"], info["track_number"], info["track_length"]) == ("A", 2, 90)
    assert info["track_started_at"].startswith("1970-01-12")      # wall - 30 s
    assert Spin(1, tracks()).info(0.0, 0.0)["track_started_at"] is None


class Pub:
    def __init__(self, *_a, **_k):
        self.infos = []

    def start(self): ...
    def stop(self): ...
    def playing(self, on): ...
    def library(self, n): ...
    def level(self, db): ...

    def track(self, info, art=None):
        if info:
            self.infos.append(info)


def _run(tmp_path, monkeypatch, side, identify, before=None):
    clock = FakeClock()
    monkeypatch.setattr(main_mod, "Publisher", Pub)
    holder = {}

    async def blocks(*_a):
        for n, i in enumerate(range(0, len(side) - BLOCK, BLOCK)):
            clock.t += BLOCK_SECONDS
            if before and n == 0:
                before(holder["app"])
            yield side[i:i + BLOCK]
            await asyncio.sleep(0.001)

    monkeypatch.setattr(main_mod, "ffmpeg_blocks", blocks)
    monkeypatch.setattr(main_mod.Recognizer, "identify", identify)
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x", recheck_seconds=40))
    app.clock = clock
    holder["app"] = app
    asyncio.run(app.run())
    return app


def test_now_spinning_follows_the_record_and_learns(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    side = np.concatenate([fake_song(11, 60), silence(3), fake_song(12, 60), silence(50)])

    async def never(self, pcm):
        return None                                   # no internet: the track list alone has to do it

    def pick(app):
        album = app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks())
        assert app.start_spin(album, "A", 1)

    app = _run(tmp_path, monkeypatch, side, never, before=pick)
    titles = [i["title"] for i in app.pub.infos]
    assert titles[0] == "Song One" and "Song Two (Remastered)" in titles
    two = next(i for i in app.pub.infos if i["title"].startswith("Song Two"))
    assert two["track_number"] == 2 and two["side"] == "A" and two["track_started_at"]
    ids = {t["title"]: t["song_id"] for t in app.lib.album_tracks(app.spin.album_id)}
    assert app.lib.hash_count(ids["Song One"]) > 1000 and app.lib.hash_count(ids["Song Two (Remastered)"]) > 1000
    # learned relative to each song's own start, not to when the record started
    low = app.lib.db.execute("SELECT MIN(t) FROM hashes WHERE song_id=?", (ids["Song Two (Remastered)"],)).fetchone()[0]
    assert low < 5 * FRAMES_PER_SECOND


def test_shazam_attaches_album_and_gives_position(tmp_path, monkeypatch):
    side = np.concatenate([fake_song(11, 60), np.zeros(SAMPLE_RATE * 50, np.int16)])

    async def shazam(self, pcm):
        return Track("Song One", "The Band", "Test LP (Deluxe)", "", "shz1", offset=2.0)

    def preload(app):
        app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks())

    # the side ends in silence: the track must not move on by its listed length there
    app = _run(tmp_path, monkeypatch, side, shazam, before=preload)
    assert app.spin is not None and app.spin.auto and app.spin.cur["title"] == "Song One"
    timed = [i for i in app.pub.infos if i.get("track_started_at")]
    assert timed and timed[0]["position_known"] and timed[0]["track_length"] == 60


def test_song_from_another_album_asks_instead_of_learning(tmp_path, monkeypatch):
    side = np.concatenate([fake_song(11, 40), np.zeros(SAMPLE_RATE * 50, np.int16)])   # shorter than track 1

    async def other(self, pcm):
        return Track("Something Else", "Other Act", "Other LP", "", "shz9")

    def pick(app):
        assert app.start_spin(app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks()))

    app = _run(tmp_path, monkeypatch, side, other, before=pick)
    assert any(i.get("spin_mismatch", {}) and i["spin_mismatch"]["title"] == "Something Else"
               for i in app.pub.infos)
    assert app.spin is not None and app.spin.album_id == app.lib.albums("Test LP")[0]["id"]   # still following
    stray = app.lib.db.execute("SELECT id FROM songs WHERE title='Something Else'").fetchone()
    assert stray is None or app.lib.hash_count(stray[0]) == 0                                 # and never learned it


def test_side_end_mark_is_used_once():
    """After a flip the side-end mark is gone, so a restart mid-side resumes instead of flipping again."""
    sp = Spin(1, [{"id": i, "side": s, "pos": 1, "title": s, "length": 60} for i, s in enumerate("ABC")], 0)
    sp.stopped(100.0)
    assert sp.at_side_end and sp.started(130.0) == "flip" and sp.cur["side"] == "B"
    assert not sp.at_side_end
    sp.stopped_at = 200.0                    # as after a restart mid-side
    assert sp.started(230.0) == "resume" and sp.cur["side"] == "B"


def test_unknown_song_after_a_known_one_is_not_learned_into_it(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    side = np.concatenate([fake_song(11, 60), silence(3), fake_song(12, 60), silence(50)])
    calls = {"n": 0}

    async def first_only(self, pcm):
        calls["n"] += 1
        return Track("Song One", "The Band", "", "", "shz1", offset=0.0) if calls["n"] == 1 else None

    app = _run(tmp_path, monkeypatch, side, first_only)
    sid = app.lib.db.execute("SELECT id FROM songs WHERE title='Song One'").fetchone()[0]
    assert app.lib.hash_count(sid) <= len(hashes(fake_song(11, 60))) * 1.2     # song two is not in there


def _one_song_side(tmp_path, monkeypatch, before):
    side = np.concatenate([fake_song(11, 60), np.zeros(SAMPLE_RATE * 50, np.int16)])

    async def shazam(self, pcm):
        return Track("Song One", "The Band", "Test LP (Deluxe)", "", "shz1", offset=2.0)

    app = _run(tmp_path, monkeypatch, side, shazam, before=before)
    assert [a["album"] for a in app.lib.albums()] == ["Test LP"]              # no album under Shazam's name
    assert app.lib.db.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 1
    assert app.lib.shazam_song("shz1") == app.lib.album_tracks(app.spin.album_id)[0]["song_id"]
    assert app.lib.leftovers() == []


def test_shazam_song_on_the_followed_album_is_not_saved_twice(tmp_path, monkeypatch):
    _one_song_side(tmp_path, monkeypatch, lambda app: app.start_spin(
        app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks()), "A", 1))


def test_shazam_song_on_a_preloaded_album_is_not_saved_twice(tmp_path, monkeypatch):
    _one_song_side(tmp_path, monkeypatch,
                   lambda app: app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks()))


def test_a_song_heard_while_following_another_album_is_not_saved(tmp_path, monkeypatch):
    side = np.concatenate([fake_song(11, 40), np.zeros(SAMPLE_RATE * 50, np.int16)])

    async def other(self, pcm):
        return Track("Something Else", "Other Act", "Other LP", "", "shz9")

    app = _run(tmp_path, monkeypatch, side, other, before=lambda app: app.start_spin(
        app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks())))
    assert [a["album"] for a in app.lib.albums()] == ["Test LP"]
    assert app.lib.shazam_song("shz9") is None


def test_a_second_copy_of_a_track_is_folded_into_it(tmp_path):
    lib = Library(tmp_path)
    album = lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks())
    tr = lib.album_tracks(album)[0]
    own = lib.song_for_track(tr["id"])
    dup, _ = lib.upsert("Song One", "The Band", "Other Pressing", "shz1")
    lib.set_art(lib.song(dup).album_id, b"\xff\xd8\xfffetched", locked=False)
    lib.learn(dup, [(1, 0), (2, 1)], 0)
    lib.played(dup)
    assert lib.attach_song(dup, tr) == own
    assert lib.song(dup) is None and lib.hash_count(own) == 2 and lib.shazam_song("shz1") == own
    assert [a["album"] for a in lib.albums()] == ["Test LP"] and lib.leftovers() == []
    assert not list(lib.art_dir.iterdir())
