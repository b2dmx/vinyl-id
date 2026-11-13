"""The library page and its small local web API, for fixing songs and covers."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from .config import AI_NOTE, SIGNATURE

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


def build(app: App) -> web.Application:
    lib, routes = app.lib, web.RouteTableDef()

    @routes.get("/api/status")
    async def status(_):
        return web.json_response({**app.status(), "signature": SIGNATURE, "ai_note": AI_NOTE})

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
        return web.json_response({"id": lib.add_album(b["artist"].strip(), b["album"].strip())})

    @routes.post("/api/albums/{id:\\d+}/songs")
    async def add_song(req):
        b = await req.json()
        album_id = int(req.match_info["id"])
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
        return web.Response(body=data, content_type="image/jpeg",
                            headers={"Cache-Control": "no-cache"})

    @routes.patch("/api/albums/{id:\\d+}")
    async def edit_album(req):
        body = await req.json()
        lib.edit_album(int(req.match_info["id"]), body["artist"].strip(), body["album"].strip())
        app.refresh_now()
        return web.json_response({"ok": True})

    @routes.post("/api/albums/{id:\\d+}/art")
    async def set_art(req):
        """An uploaded image (multipart 'file'), JSON {"url": ...} from the picker,
        or JSON {"data": <base64 image>} for tools that can only send JSON."""
        album_id = int(req.match_info["id"])
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
        lib.set_art(album_id, bytes(data), locked=True)   # locked: never auto-replaced
        app.refresh_now()
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
            lib.add_tracks(album_id, mbid, rel["tracks"])
        else:
            album_id = lib.add_album_from_release(rel["artist"], rel["album"], mbid, rel["tracks"])
        if not lib.album_has_art(album_id):
            if data := await app.rec.front_cover(mbid):
                lib.set_art(album_id, data, locked=False)
        return web.json_response({"id": album_id, "tracks": len(rel["tracks"])})

    @routes.get("/api/albums/{id:\\d+}/tracks")
    async def album_tracks(req):
        return web.json_response(lib.album_tracks(int(req.match_info["id"])))

    @routes.patch("/api/albums/{id:\\d+}/tracks")
    async def set_track_lengths(req):
        """Track lengths, for releases MusicBrainz lists without them:
        {"lengths": {"A1": "2:01", "A2": 41, ...}} (m:ss or seconds)."""
        try:
            lengths = {str(k): _seconds(v) for k, v in (await req.json())["lengths"].items()}
        except (KeyError, AttributeError, ValueError, TypeError):
            raise web.HTTPBadRequest(text='Send {"lengths": {"A1": "2:01", ...}}') from None
        unknown = lib.set_track_lengths(int(req.match_info["id"]), lengths)
        return web.json_response({"set": len(lengths) - len(unknown), "not_on_album": unknown})

    @routes.post("/api/spin")
    async def start_spin(req):
        """Now spinning: {"album_id", "side" (default first), "pos" (default 1)}."""
        b = await req.json()
        if not app.start_spin(int(b["album_id"]), str(b.get("side", "")), int(b.get("pos", 1))):
            raise web.HTTPNotFound(text="That album has no track list yet")
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
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise web.HTTPBadRequest(text=f"Bad request: {exc!r}") from exc

    allowed = {ip.strip() for ip in app.s.edit_from.split(",") if ip.strip()}

    @web.middleware
    async def edits_guard(req, handler):
        """Covers stay readable on the LAN (for the M-1); changes only from allowed IPs."""
        if allowed and req.method not in ("GET", "HEAD") and req.remote not in allowed:
            raise web.HTTPForbidden(text="Edit from the Vinyl ID page in Home Assistant")
        return await handler(req)

    wa = web.Application(client_max_size=10 * 1024 * 1024, middlewares=[edits_guard, bad_request])
    wa.add_routes(routes)
    if UI_DIR.exists():
        wa.router.add_get("/", lambda _: web.FileResponse(UI_DIR / "index.html"))
        wa.router.add_static("/ui/", UI_DIR)
    return wa


async def serve(app: App, port: int) -> web.AppRunner:
    runner = web.AppRunner(build(app))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Library UI on port %s", port)
    return runner
