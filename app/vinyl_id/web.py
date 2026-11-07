"""Small local web API (and, later, the library UI) for fixing songs and covers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from .config import AI_NOTE, SIGNATURE

if TYPE_CHECKING:
    from .__main__ import App

log = logging.getLogger(__name__)
UI_DIR = Path(__file__).parent / "ui"


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
        artist = b.get("artist", "").strip() or next(
            (a["artist"] for a in lib.albums() if a["id"] == album_id), "")
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
        """Either an uploaded image (multipart 'file') or JSON {"url": ...} from the picker."""
        album_id = int(req.match_info["id"])
        if req.content_type.startswith("multipart/"):
            field = await (await req.multipart()).next()
            data = await field.read(decode=False) if field else None
        else:
            data = await app.rec.fetch((await req.json())["url"])
        if not data:
            raise web.HTTPBadRequest(text="No image")
        lib.set_art(album_id, bytes(data), locked=True)   # locked: never auto-replaced
        app.refresh_now()
        return web.json_response({"ok": True})

    @routes.get("/api/art-search")
    async def art_search(req):
        return web.json_response(await app.rec.search_art(req.query.get("q", "")))

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

    allowed = {ip.strip() for ip in app.s.edit_from.split(",") if ip.strip()}

    @web.middleware
    async def edits_guard(req, handler):
        """Covers stay readable on the LAN (for the M-1); changes only from allowed IPs."""
        if allowed and req.method not in ("GET", "HEAD") and req.remote not in allowed:
            raise web.HTTPForbidden(text="Edit from the Vinyl ID page in Home Assistant")
        return await handler(req)

    wa = web.Application(client_max_size=10 * 1024 * 1024, middlewares=[edits_guard])
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
