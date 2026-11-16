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
    assert app.spin is not None and app.spin.album_id == app.lib.albums("Test LP")[0]["id"]   # still following
    assert app.lib.db.execute("SELECT id FROM songs WHERE title='Something Else'").fetchone() is None
    # 40 s of it against a track listed at a minute: it did not end where Song One should, so
    # nothing of it was learned as Song One
    assert app.lib.db.execute("SELECT COALESCE(SUM(hashes), 0) FROM songs").fetchone()[0] == 0


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


LP = [{"id": 1, "side": "A", "pos": 1, "title": "One", "length": 60},
      {"id": 2, "side": "A", "pos": 2, "title": "Two", "length": 60},
      {"id": 3, "side": "A", "pos": 3, "title": "Three", "length": 60},
      {"id": 4, "side": "B", "pos": 1, "title": "Four", "length": 60}]


def test_a_gap_just_after_the_length_ran_out_is_the_same_change():
    sp = Spin(1, [dict(t) for t in LP], 0)
    sp.started(0.0)
    assert sp.tick(64.0) and sp.i == 1               # the listed minute is over: on to track 2
    assert sp.track_change(68.0) and sp.i == 1       # the real gap, 8 s late: still track 2, from now
    assert sp.start == 68.0
    assert sp.track_change(130.0) and sp.i == 2      # the next gap is a real change again


def test_a_track_heard_past_its_length_waits_for_the_gap():
    sp = Spin(1, [dict(t) for t in LP], 0, start=0.0)
    sp.overrun = True                                # identification placed it 70 s into a 60 s track
    assert not sp.tick(75.0) and sp.i == 0
    assert sp.track_change(80.0) and sp.i == 1 and not sp.overrun


def test_a_quick_flip_and_the_end_of_the_album():
    sp = Spin(1, [dict(t) for t in LP], 2, start=0.0)            # last track of side A
    assert not sp.track_change(50.0, dip=2.0)                    # an ordinary gap: nothing follows on this side
    assert sp.track_change(90.0, dip=20.0) and sp.cur["side"] == "B"   # 20 s of quiet: turned over
    sp.stopped(200.0)                                            # side B, the last one, played out
    assert sp.started(230.0) == "resume"                         # an album you picked is kept
    sp.auto = True
    sp.stopped(300.0)
    assert sp.started(330.0) == "expired"                        # a guessed one is let go


def test_a_late_gap_before_a_short_jingle():
    lp = [dict(t) for t in LP]
    lp[1]["length"] = 10                                         # track 2 is a 10 s jingle
    sp = Spin(1, lp, 0)
    sp.started(0.0)
    assert sp.tick(64.0) and sp.i == 1
    assert sp.track_change(66.0) and sp.i == 1 and sp.start == 66.0     # track 1's own gap, 6 s late
    assert sp.track_change(76.0) and sp.i == 2                           # the jingle's end


def test_lost_sound_on_a_sides_last_track_is_not_a_flip():
    sp = Spin(1, [dict(t) for t in LP], 2, start=0.0)
    sp.stopped(50.0, side_end=False)
    assert sp.started(60.0) == "resume" and sp.cur["side"] == "A"


def test_the_start_time_does_not_drift_between_polls():
    sp = Spin(1, [dict(t) for t in LP], 0, start=100.0)
    a = sp.info(now=130.0, wall=1_000_000.0)["track_started_at"]
    assert sp.info(now=131.0, wall=1_000_001.9)["track_started_at"] == a


def test_same_title_by_someone_else_is_not_folded_into_the_track(tmp_path):
    lib = Library(tmp_path)
    album = lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks())
    tr = lib.album_tracks(album)[0]
    own = lib.song_for_track(tr["id"])
    other, _ = lib.upsert("Song One", "Someone Else", "Their LP", "shz7")
    assert lib.attach_song(other, tr) == own
    assert lib.song(other) is not None and lib.shazam_song("shz7") == other


def _measured(app):
    return {t["title"]: (t["length"], t["source"]) for t in app.lib.album_tracks(app.lib.albums("Test LP")[0]["id"])}


def test_track_lengths_are_measured_from_the_gaps(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    # listed: 60 s and 90 s. On this pressing: 50 s and 70 s.
    side = np.concatenate([fake_song(11, 50), silence(3), fake_song(12, 70), silence(50)])

    async def never(self, pcm):
        return None

    app = _run(tmp_path, monkeypatch, side, never, before=lambda app: app.start_spin(
        app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks()), "A", 1))
    got = _measured(app)
    assert abs(got["Song One"][0] - 53) <= 1.5 and got["Song One"][1] == "measured"       # start to next start
    assert abs(got["Song Two (Remastered)"][0] - 70) <= 1.5                               # up to the quiet
    assert got["Song Three"] == (120, "listed")                                           # not played: placeholder
    q = dict(app.lib.db.execute("SELECT title, measured_q FROM tracks"))
    assert q["Song One"] == 2 and q["Song Two (Remastered)"] == 1                         # nothing confirmed the gap


def test_a_gap_confirmed_by_recognition_is_fully_trusted(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    side = np.concatenate([fake_song(11, 50), silence(3), fake_song(12, 70), silence(50)])
    heard = {"n": 0}

    async def shazam(self, pcm):
        heard["n"] += 1
        return Track("Song One" if app_at["app"].spin.i == 0 else "Song Two", "The Band", "", "", f"s{app_at['app'].spin.i}")

    app_at = {}

    def pick(app):
        app_at["app"] = app
        app.start_spin(app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks()), "A", 1)

    app = _run(tmp_path, monkeypatch, side, shazam, before=pick)
    q = dict(app.lib.db.execute("SELECT title, measured_q FROM tracks"))
    assert q["Song One"] == 3 and abs(_measured(app)["Song One"][0] - 53) <= 1.5


def test_a_false_gap_is_not_measured(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    # Song One (listed 60 s) has three seconds of silence in it after 10 s
    side = np.concatenate([fake_song(11, 10), silence(3), fake_song(11, 45), silence(50)])

    async def never(self, pcm):
        return None

    app = _run(tmp_path, monkeypatch, side, never, before=lambda app: app.start_spin(
        app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks()), "A", 1))
    assert _measured(app)["Song One"] == (60, "listed")           # 13 s against a listed minute: not believed


def test_a_flip_guessed_from_quiet_is_marked_until_something_confirms_it():
    sp = Spin(1, [dict(t) for t in LP], 2, start=0.0)
    assert sp.track_change(90.0, dip=20.0) and sp.cur["side"] == "B" and sp.guessed
    sp.jump(3, 100.0)
    assert not sp.guessed


def test_a_guessed_flip_is_believed_when_the_track_ends_on_time():
    sp = Spin(1, [dict(t) for t in LP], 2, start=0.0)
    assert sp.track_change(90.0, dip=20.0) and sp.guessed            # side B, track "Four", listed 60 s
    assert not sp.flip_held(100.0)                                   # 10 s in: could still be a quiet passage
    assert sp.flip_held(150.0)                                       # a minute in: it really was side B


def test_a_quiet_passage_mid_track_is_too_early_to_be_its_end():
    sp = Spin(1, [dict(t) for t in LP], 0, start=0.0)          # track "One", listed 60 s
    assert sp.early_gap(20.0) == 40.0                          # 40 s too early: a quiet passage
    assert sp.early_gap(50.0) == 0.0                           # within 15 s of the end: the end
    long = Spin(1, [{"id": 1, "side": "A", "pos": 1, "title": "Long", "length": 600},
                    {"id": 2, "side": "A", "pos": 2, "title": "Next", "length": 60}], 0, start=0.0)
    assert long.early_gap(540.0) > 0 and long.early_gap(560.0) == 0.0     # 8% of ten minutes: 48 s
    assert Spin(1, [dict(t) for t in LP], 0).early_gap(20.0) == 0.0       # start not known: nothing to go by


def test_a_gap_a_little_before_the_end_and_then_the_real_one_are_one_change():
    sp = Spin(1, [dict(t) for t in LP], 0, start=0.0)
    assert sp.track_change(52.0) and sp.i == 1                 # 8 s before the listed end
    assert sp.track_change(60.0) and sp.i == 1 and sp.start == 60.0      # the real gap: still track 2
    assert sp.track_change(63.0) and sp.i == 1                 # and one more before the listed end + 5 s
    assert sp.track_change(125.0) and sp.i == 2                # a minute on: the next song


def test_quiet_passages_do_not_run_through_the_track_list(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    # Song One (listed 60 s) has two silences in it; Song Two follows after a real gap
    side = np.concatenate([fake_song(11, 15), silence(3), fake_song(11, 15), silence(3), fake_song(11, 21),
                           silence(3), fake_song(12, 60), silence(50)])

    async def never(self, pcm):
        return None

    app = _run(tmp_path, monkeypatch, side, never, before=lambda app: app.start_spin(
        app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks()), "A", 1))
    titles = [i["title"] for i in app.pub.infos]
    assert "Song Three" not in titles and titles[-1] == "Song Two (Remastered)"
    assert abs(_measured(app)["Song One"][0] - 60) <= 1.5       # 57 s of song and silences, plus the gap


def test_an_album_that_proved_itself_is_kept_when_shazam_uses_another_name(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    side = np.concatenate([fake_song(11, 60), silence(3), fake_song(12, 90), silence(3), fake_song(13, 60), silence(50)])
    one_side = [{"n": n, "side": "A", "pos": n, "title": t, "length": sec}
                for n, (t, sec) in enumerate((("Song One", 60), ("Song Two", 90), ("Song Three", 60)), 1)]
    box = {}

    async def shazam(self, pcm):
        t = box["app"].clock.t
        if t > 158:                                                # the third song: under a name of Shazam's own
            return Track("Some Other Name", "The Band", "", "", "shz-other")
        return Track("Song One" if t < 63 else "Song Two", "The Band", "", "", "s1" if t < 63 else "s2")

    def preload(app):
        box["app"] = app
        app.lib.add_album_from_release("The Band", "Test LP", "mb-1", one_side)

    app = _run(tmp_path, monkeypatch, side, shazam, before=preload)
    assert app.spin is not None and app.spin.auto and app.spin.cur["title"] == "Song Three"      # still followed
    assert app.lib.shazam_song("shz-other") is None                                              # nothing saved for it


def test_a_start_that_was_heard_is_marked_and_a_guessed_one_is_not():
    sp = Spin(1, [dict(t) for t in LP], 0)
    assert sp.started(0.0) == "new" and sp.anchored             # the needle drop
    assert sp.tick(64.0) and not sp.anchored                    # moved on by the clock: an estimate
    assert sp.track_change(66.0) and sp.anchored                # the gap came after all
    sp.jump(2, None)
    assert not sp.anchored


def test_a_heard_start_is_not_moved_by_a_fingerprint_that_disagrees(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "Publisher", Pub)
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    app.clock = FakeClock()
    app.clock.t = 100.0
    app.block_i = 400
    app.spin = Spin(1, [dict(t) for t in LP], 0, start=40.0)
    app.spin.anchored = True
    assert not app._apply_position(20.0, 396)                   # "the clip began 20 s in": the song started at 79?
    assert app.spin.start == 40.0                               # no: it started when the gap was heard
    assert app._apply_position(58.5, 396) is False and abs(app.spin.start - 40.5) < 0.01   # agrees: fine-tuned
    app.spin.anchored = False
    app._apply_position(20.0, 396)
    assert abs(app.spin.start - 79.0) < 0.01                    # nothing heard to go by: the fingerprint decides


def test_an_old_record_does_not_look_fresh_after_a_later_save(tmp_path, monkeypatch):
    import json as _json
    import time as _time

    monkeypatch.setattr(main_mod, "Publisher", Pub)
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    app.clock = FakeClock()
    album = app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks())
    app.start_spin(album)
    app.clock.t = 1000.0
    app.spin.stopped(1000.0)
    app.clock.t = 1000.0 + 3 * 3600                              # three hours later, an edit on the page
    app.tracks_changed(album)
    saved = _json.loads((tmp_path / "spin.json").read_text())["saved"]
    assert _time.time() - saved > 3 * 3600 - 5
    assert main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x")).spin is None      # too old to resume


def test_the_place_is_sure_until_the_record_stops_and_again_when_a_track_ends_on_time():
    sp = Spin(1, [dict(t) for t in LP], 0)
    assert sp.sure and sp.started(0.0) == "new" and sp.sure          # you picked it, then dropped the needle
    assert sp.track_change(61.0) and sp.sure                         # an ordinary gap: still sure
    sp.stopped(130.0)
    assert sp.started(200.0) == "resume" and not sp.sure             # it stopped: flipped? changed?
    sp.start = 200.0
    assert not sp.fits(215.0) and sp.fits(262.0)                     # a minute-long track ending after a minute


def test_tracks_shazam_misnames_are_learned_by_their_place_on_the_album(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    side = np.concatenate([fake_song(11, 60), silence(3), fake_song(12, 90), silence(50)])

    async def other_names(self, pcm):
        return Track("Some Other Name", "Somebody", "Elsewhere", "", "shz-x")      # for every track

    app = _run(tmp_path, monkeypatch, side, other_names, before=lambda app: app.start_spin(
        app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks()), "A", 1))
    assert not any(i.get("spin_mismatch") for i in app.pub.infos)                 # no questions asked
    ids = {t["title"]: t["song_id"] for t in app.lib.album_tracks(app.spin.album_id)}
    assert app.lib.hash_count(ids["Song One"]) > 1000 and app.lib.hash_count(ids["Song Two (Remastered)"]) > 1000
    assert [a["album"] for a in app.lib.albums()] == ["Test LP"] and app.lib.shazam_song("shz-x") is None


def test_after_a_stop_nothing_is_learned_by_place_until_a_track_ends_on_time(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    # Side A is played; then, after a minute of nothing, a different record whose first song runs 30 s
    side = np.concatenate([fake_song(11, 60), silence(3), fake_song(12, 90), silence(60),
                           fake_song(21, 30), silence(3), fake_song(22, 40), silence(50)])

    async def never(self, pcm):
        return None

    app = _run(tmp_path, monkeypatch, side, never, before=lambda app: app.start_spin(
        app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks()), "A", 1))
    three = next(t for t in app.lib.album_tracks(app.spin.album_id) if t["title"] == "Song Three")
    assert not three["song_id"] or app.lib.hash_count(three["song_id"]) == 0     # the stranger was not learned as B1


# --- audit of 2026-11-16 ---------------------------------------------------------------
def test_a_flip_after_a_long_break_goes_to_the_next_side_as_a_guess():
    sp = Spin(1, [dict(t) for t in LP], 2, start=0.0)            # last track of side A
    sp.stopped(100.0)
    assert sp.started(100.0 + 600) == "flip" and sp.cur["side"] == "B"      # ten minutes: still the next side
    assert sp.guessed and not sp.anchored and not sp.sure                    # ... but nothing is learned on trust
    sp = Spin(1, [dict(t) for t in LP], 2, start=0.0)
    sp.stopped(100.0)
    assert sp.started(160.0) == "flip" and not sp.guessed and sp.anchored    # turned over at once: as before


def test_the_arm_being_lifted_is_not_the_start_of_the_next_side():
    sp = Spin(1, [dict(t) for t in LP], 2, start=0.0)
    assert sp.track_change(90.0, dip=15.0) and sp.cur["side"] == "B"         # a thump after 15 s of quiet
    assert sp.guessed and not sp.anchored
    assert sp.flip_again(110.0) and sp.start == 110.0 and sp.i == 3          # the needle comes down 20 s later
    assert not sp.flip_again(172.0)                                          # a minute on: this is the track's end
    sp = Spin(1, [dict(t) for t in LP], 0, start=0.0)
    assert not sp.flip_again(10.0)                                           # no flip was guessed: an ordinary gap


def test_a_held_gap_is_remembered_for_the_track_only():
    sp = Spin(1, [dict(t) for t in LP], 0, start=0.0)
    sp.held = True
    assert sp.tick(64.0) and not sp.held
    sp.held = True
    assert sp.track_change(125.0) and not sp.held
    sp.held = True
    sp.jump(0, None)
    assert not sp.held


def _album(app):
    return app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks())


def _hashes_of(app, title):
    t = next(t for t in app.lib.album_tracks(app.lib.albums("Test LP")[0]["id"]) if t["title"] == title)
    return app.lib.hash_count(t["song_id"]) if t["song_id"] else 0


async def _never(self, pcm):
    return None


def test_a_track_much_shorter_than_listed_does_not_take_the_next_songs_opening(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    # Song One is listed at a minute; this pressing's runs 30 s. Its gap looks like a quiet passage.
    side = np.concatenate([fake_song(11, 30), silence(3), fake_song(12, 90), silence(50)])
    app = _run(tmp_path, monkeypatch, side, _never, before=lambda app: app.start_spin(_album(app), "A", 1))
    assert _hashes_of(app, "Song One") == 0           # half of what was heard as "Song One" was Song Two


def test_a_quiet_passage_and_a_late_gap_still_teach_the_track(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    # Song One (listed 60 s) has a quiet passage and runs 64 s: the list moves on first, then the gap comes
    side = np.concatenate([fake_song(11, 20), silence(3), fake_song(11, 41), silence(3), fake_song(12, 60), silence(50)])
    app = _run(tmp_path, monkeypatch, side, _never, before=lambda app: app.start_spin(_album(app), "A", 1))
    assert _hashes_of(app, "Song One") > 500


def test_another_record_running_past_the_listed_length_is_not_learned(tmp_path, monkeypatch):
    side = np.concatenate([fake_song(11, 100), np.zeros(SAMPLE_RATE * 50, np.int16)])   # no gap at one minute

    async def other(self, pcm):
        return Track("Something Else", "Other Act", "Other LP", "", "shz9")

    app = _run(tmp_path, monkeypatch, side, other, before=lambda app: app.start_spin(_album(app), "A", 1))
    assert app.lib.db.execute("SELECT COALESCE(SUM(hashes), 0) FROM songs").fetchone()[0] == 0


def test_without_a_track_list_the_next_song_is_not_learned_into_the_last(tmp_path, monkeypatch):
    # Song One runs straight into another song; the gap comes only after that one
    two = fake_song(12, 75)
    side = np.concatenate([fake_song(11, 30), two, np.zeros(SAMPLE_RATE * 3, np.int16), fake_song(13, 20),
                           np.zeros(SAMPLE_RATE * 50, np.int16)])
    calls = {"n": 0}

    async def first_only(self, pcm):
        calls["n"] += 1
        return Track("Song One", "The Band", "", "", "shz1", offset=0.0) if calls["n"] == 1 else None

    app = _run(tmp_path, monkeypatch, side, first_only)
    assert app.lib.match(hashes(two[SAMPLE_RATE * 52:SAMPLE_RATE * 64])) is None


def test_a_confirmed_clip_is_learned_and_what_came_after_it_waits(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "Publisher", Pub)
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    app.current, _ = app.lib.upsert("A", "B", "", "x")
    song = fake_song(7, 30)
    app.learn_blocks = [song[i:i + BLOCK] for i in range(0, len(song), BLOCK)]      # 120 blocks from block 0
    app.block_i = 120

    async def go():
        app._commit_learning(confirmed=True, upto=80)             # recognition heard blocks 68-80
        await asyncio.sleep(0.5)

    asyncio.run(go())
    assert len(app.learn_blocks) == 40 and app.learn_start == 80  # the last 10 s are not confirmed yet
    assert app.lib.match(hashes(song[SAMPLE_RATE * 5:SAMPLE_RATE * 17]))
    assert app.lib.match(hashes(song[SAMPLE_RATE * 21:SAMPLE_RATE * 30])) is None


def test_hum_under_a_picked_album_is_still_found_out(tmp_path, monkeypatch):
    seen = []
    side = np.concatenate([fake_song(11, 20), np.zeros(SAMPLE_RATE * 50, np.int16)])

    def pick(app):
        assert app._auto_quiet
        app.start_spin(_album(app), "A", 1)
        feed = app.cal.feed
        app.cal.feed = lambda db, playing, recognized: (seen.append((playing, recognized)), feed(db, playing, recognized))[1]

    _run(tmp_path, monkeypatch, side, _never, before=pick)
    assert (True, False) in seen and (True, True) not in seen     # a place on the list is not "recognized"


def test_a_track_ending_on_time_does_not_settle_an_open_question(tmp_path, monkeypatch):
    silence = lambda s: np.zeros(SAMPLE_RATE * s, np.int16)       # noqa: E731
    one_side = [{"n": n, "side": "A", "pos": n, "title": f"Song {n}", "length": sec}
                for n, sec in enumerate((60, 90, 60, 60), 1)]
    # Song 1 plays; a minute of nothing; then another record, whose second song happens to run 90 s
    side = np.concatenate([fake_song(11, 60), silence(60), fake_song(21, 30), silence(3), fake_song(22, 90),
                           silence(3), fake_song(23, 40), silence(50)])
    box = {}

    async def shazam(self, pcm):
        return Track("Something Else", "Other Act", "Other LP", "", "shz9") if box["app"].clock.t > 110 else None

    def pick(app):
        box["app"] = app
        app.start_spin(app.lib.add_album_from_release("The Band", "Test LP", "mb-1", one_side), "A", 1)

    app = _run(tmp_path, monkeypatch, side, shazam, before=pick)
    assert app.spin.mismatch is not None and not app.spin.sure    # still asking; nothing taken on trust
    assert app.lib.db.execute("SELECT COALESCE(SUM(hashes), 0) FROM songs WHERE title != 'Song 1'").fetchone()[0] == 0


def test_lost_audio_is_not_the_end_of_the_side():
    from vinyl_id.detector import Detector, Event

    def run(det, db, seconds):
        return [e for e in (det.feed(db) for _ in range(int(seconds / BLOCK_SECONDS))) if e]

    d = Detector(Settings(audio_input_format="pulse"))
    assert run(d, -20, 30) == [Event.STARTED]
    assert run(d, -90, 50) == [Event.STOPPED] and d.stop_lost           # exact silence: the stream dropped
    assert run(d, -20, 30) == [Event.STARTED]
    assert run(d, -70, 50) == [Event.STOPPED] and not d.stop_lost       # the run-out groove: the side ended
    d = Detector(Settings())
    run(d, -20, 30)
    assert run(d, -90, 50) == [Event.STOPPED] and not d.stop_lost       # a wired input has no dropouts


def test_an_album_picked_before_a_restart_still_starts_at_the_needle_drop(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "Publisher", Pub)
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    album = _album(app)
    app.start_spin(album, "A", 1)                                 # picked with the needle up
    again = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    assert again.spin.stopped_at is None and again.spin.sure and again.spin.started(5.0) == "new"
    app.spin.started(5.0)                                         # the same, but saved while the record played
    app._save_spin()
    again = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    assert again.spin.stopped_at is not None and not again.spin.sure


def test_a_play_is_counted_once_however_the_title_moves(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "Publisher", Pub)
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    a, _ = app.lib.upsert("A", "B", "", "a")
    b, _ = app.lib.upsert("B", "B", "", "b")
    for sid in (a, b, a, b):                                      # on early, back, on again
        app._count_play(sid)
    assert [r[0] for r in app.lib.db.execute("SELECT plays FROM songs ORDER BY id")] == [1, 1]


def test_a_song_forgotten_while_it_is_being_looked_up_does_not_come_back(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "Publisher", Pub)
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    app.clock = FakeClock()
    app.det.playing = True
    sid, _ = app.lib.upsert("A", "B", "", "x")
    song = fake_song(7, 60)
    app.lib.learn(sid, hashes(song), 0)
    match = app.lib.match

    def forgets(hs):
        hit = match(hs)
        app.loop.call_soon_threadsafe(app.forget, sid)            # "forget this song", pressed just now
        return hit

    app.lib.match = forgets

    async def go():
        app.loop = asyncio.get_running_loop()
        app.busy = True
        await app._identify(song[SAMPLE_RATE * 20:SAMPLE_RATE * 32], 0)

    asyncio.run(go())
    assert app.current is None and app.lib.song(sid) is None
