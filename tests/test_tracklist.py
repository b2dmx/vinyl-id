"""Track lists fetched from MusicBrainz by themselves."""

import asyncio

import numpy as np

import vinyl_id.__main__ as main_mod
from vinyl_id.config import SAMPLE_RATE, Settings
from vinyl_id.recognize import Recognizer, Track, usual_pressings
from test_library import fake_song
from test_spin import RELEASE, _run, tracks

REAL = Recognizer.vinyl_release          # conftest stands in for it during each test


def found(mbid, count, date, fmt='12" Vinyl', status="Official", title="Test LP", artist="The Band"):
    return {"id": mbid, "title": title, "date": date, "status": status, "track-count": count,
            "media": [{"format": fmt}], "artist-credit": [{"name": artist}]}


def test_the_usual_vinyl_pressing_comes_first():
    releases = [
        found("box", 30, "1970"),
        found("late", 3, "2011"),
        found("first", 3, "1979"),
        found("undated", 3, ""),
        found("cd", 3, "1960", fmt="CD"),
        found("promo", 3, "1960", status="Promotion"),
        found("other", 3, "1960", title="Another LP"),
        found("cover", 3, "1960", artist="Tribute Players"),
    ]
    assert [r["id"] for r in usual_pressings(releases, "The Band", "Test LP")] == ["first", "box"]


def ask(search, release=RELEASE, album="Test LP (Remastered)", title="Song Two"):
    rec, asked = Recognizer(), []

    async def mb(path, params):
        asked.append(params.get("query", path))
        return search if path == "release" else release

    rec._mb = mb
    return asyncio.run(REAL(rec, "The Band feat. A Guest", album, title)), asked


def test_vinyl_release_wants_the_song_that_was_heard():
    search = {"releases": [found("mb-1", 3, "1979")]}
    rel, asked = ask(search)
    assert rel["mbid"] == "mb-1" and len(rel["tracks"]) == 3
    assert asked[0] == 'release:"Test LP" AND artist:"The Band" AND format:*Vinyl*'
    assert ask(search, title="Not On It")[0] is False
    assert ask({"releases": []})[0] is False
    assert ask(None)[0] is None                        # MusicBrainz did not answer
    assert ask(search, release=None)[0] is None


SIDE = np.concatenate([fake_song(11, 60), np.zeros(SAMPLE_RATE * 50, np.int16)])


async def shazam(self, pcm):
    return Track("Song One", "The Band", "Test LP", "", "shz1", offset=2.0)


def test_a_recognized_album_gets_its_track_list(tmp_path, monkeypatch):
    asked = []

    async def release(self, artist, album, title):
        asked.append((artist, album, title))
        return {"mbid": "mb-1", "tracks": tracks()}

    monkeypatch.setattr(Recognizer, "vinyl_release", release)
    app = _run(tmp_path, monkeypatch, SIDE, shazam)
    assert asked == [("The Band", "Test LP", "Song One")]
    assert app.spin is not None and app.spin.auto and app.spin.cur["title"] == "Song One"
    assert len(app.lib.albums()) == 1 and app.lib.albums()[0]["tracks"] == 3
    timed = [i for i in app.pub.infos if i.get("track_started_at")]
    assert timed and timed[0]["position_known"] and timed[0]["track_length"] == 60


def test_no_vinyl_list_is_asked_for_once(tmp_path, monkeypatch):
    asked = []

    async def none(self, artist, album, title):
        asked.append(album)
        return False

    monkeypatch.setattr(Recognizer, "vinyl_release", none)
    app = _run(tmp_path, monkeypatch, SIDE, shazam)
    assert asked == ["Test LP"] and app.spin is None


def test_no_answer_is_asked_again_later(tmp_path):
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    sid, _ = app.lib.upsert("Song One", "The Band", "Test LP", "shz1")
    answers = [None, {"mbid": "mb-1", "tracks": tracks()}]
    now = [0.0]
    app.clock = lambda: now[0]

    async def release(artist, album, title):
        return answers.pop(0)

    app.rec.vinyl_release = release

    async def go():
        for at in (0, 10, main_mod.TRACKLIST_RETRY + 1):
            now[0] = at
            app._want_tracklist(sid)
            if app._tracklist_task:
                await app._tracklist_task

    asyncio.run(go())
    assert not answers and app.lib.album_tracks(app.lib.song(sid).album_id)


def test_another_record_ends_an_album_followed_by_itself(tmp_path):
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    album = app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks())

    def follow(auto, sure):
        app.spin = main_mod.Spin(album, app.lib.album_tracks(album), 0, auto=auto)
        app.spin.seen = {1, 2}                         # two of its tracks were recognized: trusted
        app.spin.sure = sure

    def hear(title, artist="Other Act", lp="Other LP"):
        return app._foreign(Track(title, artist, lp, "", title))

    follow(True, False)
    assert not hear("One") and app.spin
    assert hear("Two") and app.spin is None
    for auto, sure in ((False, False), (True, True)):  # picked by you, or playing on unbroken: kept
        follow(auto, sure)
        assert not hear("One") and not hear("Two") and app.spin
    follow(True, False)                                # the same act's other record
    assert not hear("One", "The Band") and hear("Two", "The Band")
    follow(True, False)                                # other names for this album's own tracks
    assert not hear("One", "The Band", "Test LP") and not hear("Two", "The Band", "Test LP") and app.spin


def test_a_fetched_list_goes_with_the_last_song(tmp_path):
    app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x"))
    for fetched, left in ((True, 0), (False, 1)):          # a list you added keeps its album
        sid, _ = app.lib.upsert("Song One", "The Band", f"LP {fetched}", f"shz-{fetched}")
        album = app.lib.song(sid).album_id
        app.lib.add_tracks(album, "mb-1", tracks(), fetched=fetched)
        app.lib.attach_song(sid, app.lib.album_tracks(album)[0])
        app.spin = main_mod.Spin(album, app.lib.album_tracks(album), 0, auto=True)

        async def forget():
            app.forget(sid)

        asyncio.run(forget())
        assert len(app.lib.albums(f"LP {fetched}")) == left
        assert len(app.lib.album_tracks(album)) == 3 * left and (app.spin is None) == fetched
