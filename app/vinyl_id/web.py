"""The library page and its small local web API, for fixing songs and covers."""

from __future__ import annotations

import asyncio
import base64
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from aiohttp.web_log import AccessLogger

from .config import AI_NOTE, SIGNATURE
from .stream import record_stream

if TYPE_CHECKING:
    from .__main__ import App

log = logging.getLogger(__name__)
UI_DIR = Path(__file__).parent / "ui"


def is_image(data: bytes | None) -> bool:
    """JPEG, PNG, GIF or WebP, judged by the first bytes."""
    return bool(data) and (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
                           or data[:4] == b"GIF8" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _seconds(v: str | float) -> float:
    """'2:01' or 121 -> 121.0"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        secs = float(v)
    else:
        m, _, s = str(v).strip().rpartition(":")
        secs = int(m or 0) * 60 + float(s)
    if not 0 < secs < 3600:
        raise ValueError(v)
    return secs


def cover_cache(req: web.Request) -> dict[str, str]:
    """A cover address with ?v= changes whenever the cover does, so that copy can be kept."""
    return {"Cache-Control": "public, max-age=31536000, immutable" if "v" in req.query else "no-cache"}


def build(app: App) -> web.Application:
    lib, routes = app.lib, web.RouteTableDef()

    def named(body) -> tuple[str, str]:
        artist, album = body["artist"].strip(), body["album"].strip()
        if not artist or not album:
            raise web.HTTPBadRequest(text="An album needs an artist and a name")
        return artist, album

    def album_of(req) -> int:
        album_id = int(req.match_info["id"])
        if not lib.album(album_id):
            raise web.HTTPNotFound(text="That album is gone")
        return album_id

    @routes.get("/api/status")
    async def status(req):
        """?tracks=0 leaves the album's track list out (for things that ask every few seconds)."""
        st = app.status(tracks=req.query.get("tracks") != "0")
        return web.json_response({**st, "signature": SIGNATURE, "ai_note": AI_NOTE})

    @routes.post("/api/quiet-level")
    async def quiet_level(_):
        """The level heard now (turntable on, needle up) becomes the signal threshold."""
        if app.use_quiet_level() is None:
            raise web.HTTPConflict(text="Lift the needle and wait about a minute. If the input level "
                                        "stays at -90, the record player is not reaching Vinyl ID yet.")
        return web.json_response(app.status(tracks=False))

    @routes.get("/stream.mp3")
    async def stream(req):
        """The record as a stream that names its songs: play this on your speakers."""
        if req.method == "HEAD":            # a player checking the address: no encoder for that
            return web.Response(headers={"Content-Type": "audio/mpeg"})
        return await record_stream(req, app)

    @routes.post("/api/now/label")
    async def label_now(req):
        """Name the record that's playing right now (unknown or wrong)."""
        b = await req.json()
        sid = app.label_now(b["title"].strip(), b["artist"].strip(), b.get("album", "").strip())
        if sid is None:
            raise web.HTTPConflict(text="Nothing is playing")
        return web.json_response({"id": sid, "album_id": lib.song(sid).album_id})

    @routes.post("/api/albums")
    async def add_album(req):
        b = await req.json()
        return web.json_response({"id": lib.add_album(*named(b))})

    @routes.post("/api/albums/{id:\\d+}/songs")
    async def add_song(req):
        b = await req.json()
        album_id = album_of(req)
        artist = b.get("artist", "").strip() or (lib.album(album_id) or {}).get("artist", "")
        return web.json_response({"id": lib.add_song(album_id, b["title"].strip(), artist)})

    @routes.get("/api/albums")
    async def albums(req):
        return web.json_response(lib.albums(req.query.get("q", "")))

    @routes.get("/api/albums/{id:\\d+}/songs")
    async def album_songs(req):
        return web.json_response(lib.album_songs(int(req.match_info["id"])))

    @routes.get("/api/albums/{id:\\d+}/art")
    async def art(req):
        data = lib.art_bytes(int(req.match_info["id"]))
        if not data:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="image/jpeg", headers=cover_cache(req))

    @routes.patch("/api/albums/{id:\\d+}")
    async def edit_album(req):
        body = await req.json()
        try:
            lib.edit_album(int(req.match_info["id"]), *named(body))
        except sqlite3.IntegrityError:
            raise web.HTTPConflict(text="You already have an album with that artist and name") from None
        app.refresh_now()
        return web.json_response({"ok": True})

    async def image_from(req) -> bytes:
        """An uploaded image (multipart 'file'), JSON {"url": ...} from the picker,
        or JSON {"data": <base64 image>} for tools that can only send JSON."""
        if req.content_type.startswith("multipart/"):
            field = await (await req.multipart()).next()
            data = await field.read(decode=False) if field else None
        else:
            b = await req.json()
            if "data" in b:
                try:
                    data = base64.b64decode(str(b["data"]), validate=True)
                except ValueError:
                    raise web.HTTPBadRequest(text="Not base64") from None
            else:
                url = str(b["url"])
                if not url.lower().startswith(("http://", "https://")):
                    raise web.HTTPBadRequest(text="Not a web address")
                data = await app.rec.fetch(url)
        if not is_image(data):        # also stops the picker being used to read other web addresses
            raise web.HTTPBadRequest(text="No image")
        return bytes(data)

    @routes.post("/api/albums/{id:\\d+}/art")
    async def set_art(req):
        # in a thread: converting a PNG or WebP can take seconds, and the audio loop must keep running
        await asyncio.to_thread(lib.set_art, album_of(req), await image_from(req), True)
        app.refresh_now()
        return web.json_response({"ok": True})

    @routes.put("/api/albums/{id:\\d+}/lp-covers")
    async def set_lp_covers(req):
        """{"on": true} = show each LP's own cover; false = always the album cover."""
        lib.set_lp_covers(int(req.match_info["id"]), bool((await req.json())["on"]))
        app.refresh_now()
        return web.json_response({"ok": True})

    @routes.get("/api/albums/{id:\\d+}/discs")
    async def discs_with_art(req):
        return web.json_response(lib.discs_with_art(int(req.match_info["id"])))

    @routes.get("/api/albums/{id:\\d+}/discs/{disc:\\d+}/art")
    async def disc_art(req):
        name = lib.disc_art_file(int(req.match_info["id"]), int(req.match_info["disc"]))
        data = lib.art_file_bytes(name) if name else None
        if not data:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="image/jpeg", headers=cover_cache(req))

    @routes.post("/api/albums/{id:\\d+}/discs/{disc:\\d+}/art")
    async def set_disc_art(req):
        """A box set's disc cover: shown while that disc plays, instead of the album's."""
        album_id, disc = int(req.match_info["id"]), int(req.match_info["disc"])
        if not any(t["disc"] == disc for t in lib.album_tracks(album_id)):
            raise web.HTTPNotFound(text="That album has no such LP")
        await asyncio.to_thread(lib.set_disc_art, album_id, disc, await image_from(req))
        app.refresh_now()
        return web.json_response({"ok": True})

    @routes.get("/api/leftovers")
    async def leftovers(_):
        """Covers of albums that are gone, kept for 30 days."""
        return web.json_response(lib.leftovers())

    @routes.get("/api/leftovers/{id:\\d+}/art")
    async def leftover_art(req):
        data = lib.leftover_bytes(int(req.match_info["id"]))
        if not data:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @routes.post("/api/leftovers/{id:\\d+}/restore")
    async def restore_leftover(req):
        """{"album_id": ...}: put the cover on that album."""
        if not lib.restore_leftover(int(req.match_info["id"]), int((await req.json())["album_id"])):
            raise web.HTTPNotFound(text="That cover or album is gone")
        app.refresh_now()
        return web.json_response({"ok": True})

    @routes.delete("/api/leftovers/{id:\\d+}")
    async def delete_leftover(req):
        if not lib.delete_leftover(int(req.match_info["id"])):
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    @routes.get("/api/art-search")
    async def art_search(req):
        return web.json_response(await app.rec.search_art(req.query.get("q", "")))

    @routes.get("/api/mb/search")
    async def mb_search(req):
        """Pressings from MusicBrainz, to pick the exact one (vinyl first)."""
        return web.json_response(await app.rec.search_releases(req.query.get("q", "")))

    @routes.post("/api/albums/from-mb")
    async def album_from_mb(req):
        """Add an album with its track list (side, position, title, length) and cover."""
        b = await req.json()
        mbid = b["mbid"]
        rel = await app.rec.release(mbid)
        if not rel or not rel["tracks"]:
            raise web.HTTPBadGateway(text="MusicBrainz has no track list for that release")
        if b.get("album_id"):               # give an album you already have its track list
            album_id = int(b["album_id"])
            if not lib.album(album_id):
                raise web.HTTPNotFound(text="That album is gone")
            lib.add_tracks(album_id, mbid, rel["tracks"])
            app.tracks_changed(album_id)
        else:
            album_id = lib.add_album_from_release(rel["artist"], rel["album"], mbid, rel["tracks"])
            app.tracks_changed(album_id)    # adding an album you already have replaces its track list
        if not lib.album_has_art(album_id):
            if data := await app.rec.front_cover(mbid):
                await asyncio.to_thread(lib.set_art, album_id, data, False)
        return web.json_response({"id": album_id, "tracks": len(rel["tracks"])})

    @routes.get("/api/albums/{id:\\d+}/tracks")
    async def album_tracks(req):
        return web.json_response(lib.album_tracks(int(req.match_info["id"])))

    @routes.patch("/api/albums/{id:\\d+}/tracks")
    async def set_track_lengths(req):
        """Track lengths you set yourself: {"lengths": {"A1": "2:01", "A2": 41, "A3": null}}
        (m:ss or seconds; null takes yours away and has the track measured again).
        With "placeholder": true they are listed lengths instead: stand-ins that the app
        replaces with what it measures when the record plays."""
        try:
            body = await req.json()
            lengths = {str(k): None if v is None else _seconds(v) for k, v in body["lengths"].items()}
            placeholder = body.get("placeholder") is True
            if placeholder and None in lengths.values():
                raise ValueError
        except (KeyError, AttributeError, ValueError, TypeError):
            raise web.HTTPBadRequest(text='Send {"lengths": {"A1": "2:01", ...}}') from None
        unknown = lib.set_track_lengths(int(req.match_info["id"]), lengths, placeholder)
        app.tracks_changed(int(req.match_info["id"]))
        app.refresh_now()
        return web.json_response({"set": len(lengths) - len(unknown), "not_on_album": unknown})

    @routes.post("/api/spin")
    async def start_spin(req):
        """Now spinning: {"album_id", "side" (default first), "pos" (default 1)}."""
        b = await req.json()
        if not app.start_spin(int(b["album_id"]), str(b.get("side", "")), int(b.get("pos", 1))):
            raise web.HTTPNotFound(text="That album has no track list yet")
        return web.json_response(app.spin_status())

    @routes.post("/api/spin/same")
    async def same_track(_):
        """What Shazam named is the track now playing, under another name."""
        if not app.same_track():
            raise web.HTTPConflict(text="There is nothing to match right now")
        return web.json_response(app.spin_status())

    @routes.delete("/api/spin")
    async def end_spin(_):
        app.end_spin()
        return web.json_response({"ok": True})

    @routes.patch("/api/songs/{id:\\d+}")
    async def edit_song(req):
        body = await req.json()
        lib.edit_song(int(req.match_info["id"]), body["title"].strip(), body["artist"].strip())
        app.refresh_now()
        return web.json_response({"ok": True})

    @routes.delete("/api/songs/{id:\\d+}")
    async def forget_song(req):
        app.forget(int(req.match_info["id"]))
        return web.json_response({"ok": True})

    @web.middleware
    async def bad_request(req, handler):
        """A missing field or malformed body is the caller's mistake (400), not a server error."""
        try:
            return await handler(req)
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            raise web.HTTPBadRequest(text=f"Bad request: {exc!r}") from exc

    allowed = {ip.strip() for ip in app.s.edit_from.split(",") if ip.strip()}

    @web.middleware
    async def edits_guard(req, handler):
        """Covers stay readable on the LAN (for displays); changes only from allowed IPs."""
        if allowed and req.method not in ("GET", "HEAD") and req.remote not in allowed:
            raise web.HTTPForbidden(text="Changes only work from the Vinyl ID page inside Home Assistant (the sidebar, or Open Web UI on the app's page)")
        return await handler(req)

    @web.middleware
    async def fresh_page(req, handler):
        """The page and its script are checked with the server on every load, so an update
        never leaves a browser with the old page and the new script."""
        resp = await handler(req)
        if req.path == "/" or req.path.startswith("/ui/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    wa = web.Application(client_max_size=10 * 1024 * 1024, middlewares=[edits_guard, bad_request, fresh_page])
    wa.add_routes(routes)
    if UI_DIR.exists():
        async def page(_):
            return web.FileResponse(UI_DIR / "index.html")

        wa.router.add_get("/", page)
        wa.router.add_static("/ui/", UI_DIR)
    return wa


class QuietAccessLog(AccessLogger):
    """The usual request log, without the status checks that arrive every few seconds."""

    def log(self, request, response, time) -> None:
        if not (request.method == "GET" and request.path == "/api/status" and response.status == 200):
            super().log(request, response, time)


async def serve(app: App, port: int) -> web.AppRunner:
    runner = web.AppRunner(build(app), access_log_class=QuietAccessLog)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Library UI on port %s", port)
    return runner
