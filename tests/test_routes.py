"""The page's web API and the record stream, end to end over HTTP."""

import asyncio
import base64
import sys

from aiohttp.test_utils import TestClient, TestServer

import vinyl_id.__main__ as main_mod
from vinyl_id import stream, web
from vinyl_id.config import Settings
from test_spin import Pub, tracks

JPEG = b"\xff\xd8\xffcover"
PIC = {"data": base64.b64encode(JPEG).decode()}


def serve(tmp_path, monkeypatch, check, **settings):
    monkeypatch.setattr(main_mod, "Publisher", Pub)

    async def go():
        app = main_mod.App(Settings(data_dir=str(tmp_path), audio_input="x", **settings))
        try:
            async with TestClient(TestServer(web.build(app))) as client:
                await check(app, client)
        finally:
            await app.close()

    asyncio.run(go())


async def new_album(c, artist="Band", album="LP", song="Song") -> tuple[int, int]:
    album_id = (await (await c.post("/api/albums", json={"artist": artist, "album": album})).json())["id"]
    song_id = (await (await c.post(f"/api/albums/{album_id}/songs", json={"title": song})).json())["id"]
    return album_id, song_id


def test_status_and_page(tmp_path, monkeypatch):
    async def check(app, c):
        st = await (await c.get("/api/status")).json()
        assert st["playing"] is False and st["now"] is None and st["listener"] == "Vinyl ID Listener"
        assert st["signature"].startswith("Vinyl ID v") and {"level", "start_level", "library_mb"} <= set(st)
        album = app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks())
        assert (await c.post("/api/spin", json={"album_id": album})).status == 200
        assert len((await (await c.get("/api/status")).json())["spin"]["tracks"]) == 3
        assert "tracks" not in (await (await c.get("/api/status?tracks=0")).json())["spin"]
        assert (await c.delete("/api/spin")).status == 200
        assert (await (await c.get("/api/status")).json())["spin"] is None
        page = await c.get("/")
        assert page.status == 200 and page.headers["Cache-Control"] == "no-cache"
        assert (await c.get("/ui/app.js")).headers["Cache-Control"] == "no-cache"

    serve(tmp_path, monkeypatch, check)


def test_albums_songs_and_covers(tmp_path, monkeypatch):
    async def check(app, c):
        album, song = await new_album(c)
        assert [(a["album"], a["songs"], a["has_art"]) for a in await (await c.get("/api/albums")).json()] == [("LP", 1, 0)]
        assert (await c.get(f"/api/albums/{album}/art")).status == 404
        assert (await c.post(f"/api/albums/{album}/art", json={"data": "bm90IGFuIGltYWdl"})).status == 400
        assert (await c.post(f"/api/albums/{album}/art", json={"url": "file:///etc/passwd"})).status == 400
        assert (await c.post(f"/api/albums/{album}/art", json=PIC)).status == 200
        art = await c.get(f"/api/albums/{album}/art?v=1")
        assert await art.read() == JPEG and "immutable" in art.headers["Cache-Control"]
        assert (await c.get(f"/api/albums/{album}/art")).headers["Cache-Control"] == "no-cache"

        assert (await c.patch(f"/api/songs/{song}", json={"title": "New", "artist": "Band"})).status == 200
        assert (await (await c.get(f"/api/albums/{album}/songs")).json())[0]["title"] == "New"
        assert (await c.patch(f"/api/songs/{song}", json={"title": "New"})).status == 400      # a field is missing

        other, _ = await new_album(c, album="Second LP")
        assert (await c.patch(f"/api/albums/{other}", json={"artist": "Band", "album": "lp"})).status == 409
        assert (await c.patch(f"/api/albums/{other}", json={"artist": "Band", "album": "Third LP"})).status == 200

        assert (await c.post(f"/api/albums/{album}/discs/2/art", json=PIC)).status == 404      # no such LP
        assert (await c.post("/api/albums/999/art", json=PIC)).status == 404                   # no such album
        assert (await c.post("/api/albums/999/songs", json={"title": "T"})).status == 404
        assert (await c.get("/api/albums/99999999999999999999999/art")).status == 400          # not a 500
        assert (await c.post("/api/spin", json={"album_id": album})).status == 404             # no track list
        assert (await c.post("/api/now/label", json={"title": "T", "artist": "A"})).status == 409   # nothing plays

    serve(tmp_path, monkeypatch, check)


def test_track_lengths_and_lp_covers(tmp_path, monkeypatch):
    async def check(app, c):
        album = app.lib.add_album_from_release("The Band", "Test LP", "mb-1", tracks())
        r = await c.patch(f"/api/albums/{album}/tracks", json={"lengths": {"A1": "1:30", "b1": 45, "Z9": 10}})
        assert await r.json() == {"set": 2, "not_on_album": ["Z9"]}
        assert [t["length"] for t in await (await c.get(f"/api/albums/{album}/tracks")).json()] == [90, 90, 45]
        assert (await c.patch(f"/api/albums/{album}/tracks", json={"lengths": {"A1": "soon"}})).status == 400
        assert (await c.patch(f"/api/albums/{album}/tracks", json=["A1"])).status == 400

        assert (await c.post(f"/api/albums/{album}/discs/1/art", json=PIC)).status == 200
        assert await (await c.get(f"/api/albums/{album}/discs")).json() == [1]
        assert await (await c.get(f"/api/albums/{album}/discs/1/art")).read() == JPEG
        assert (await c.put(f"/api/albums/{album}/lp-covers", json={"on": False})).status == 200
        assert (await (await c.get("/api/albums")).json())[0]["lp_covers"] == 0

    serve(tmp_path, monkeypatch, check)


def test_covers_of_removed_albums(tmp_path, monkeypatch):
    async def check(app, c):
        gone, song = await new_album(c, album="Gone LP")
        await c.post(f"/api/albums/{gone}/art", json=PIC)
        kept, _ = await new_album(c, album="Kept LP")
        assert (await c.delete(f"/api/songs/{song}")).status == 200          # the album's last song
        [left] = await (await c.get("/api/leftovers")).json()
        assert left["album"] == "Gone LP" and left["days_left"] == 30
        assert await (await c.get(f"/api/leftovers/{left['id']}/art")).read() == JPEG
        assert (await c.post(f"/api/leftovers/{left['id']}/restore", json={"album_id": 999})).status == 404
        assert (await c.post(f"/api/leftovers/{left['id']}/restore", json={"album_id": kept})).status == 200
        assert await (await c.get(f"/api/albums/{kept}/art")).read() == JPEG
        assert await (await c.get("/api/leftovers")).json() == []
        assert (await c.delete(f"/api/leftovers/{left['id']}")).status == 404

    serve(tmp_path, monkeypatch, check)


def test_only_allowed_addresses_may_change_things(tmp_path, monkeypatch):
    async def check(app, c):
        assert (await c.post("/api/albums", json={"artist": "Band", "album": "LP"})).status == 403
        assert (await c.delete("/api/spin")).status == 403
        assert (await c.get("/api/albums")).status == 200                    # reading stays open

    serve(tmp_path, monkeypatch, check, edit_from="10.9.9.9")


def test_record_stream_names_its_song(tmp_path, monkeypatch):
    every = stream.META_EVERY
    script = f"import sys; sys.stdout.buffer.write(b'a' * {2 * every})"
    monkeypatch.setattr(stream, "ffmpeg_input", lambda *_a: [sys.executable, "-c", script])

    async def check(app, c):
        r = await c.get("/stream.mp3", headers={"Icy-MetaData": "1"})
        assert r.status == 200 and r.headers["icy-metaint"] == str(every)
        assert await r.content.readexactly(every) == b"a" * every
        units = (await r.content.readexactly(1))[0]
        assert (await r.content.readexactly(units * 16)).startswith(b"StreamTitle='Vinyl ID';")
        assert await r.content.readexactly(every) == b"a" * every
        assert await r.content.readexactly(1) == b"\0"                       # nothing new to say
        r.close()

        plain = await c.get("/stream.mp3")                                   # a player that wants no titles
        assert "icy-metaint" not in plain.headers and await plain.read() == b"a" * 2 * every

        assert (await c.head("/stream.mp3")).status == 200 and stream._listeners == 0   # a check starts no encoder

        monkeypatch.setattr(stream, "_listeners", stream.MAX_LISTENERS)
        assert (await c.get("/stream.mp3")).status == 503

    serve(tmp_path, monkeypatch, check)
