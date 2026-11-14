"""The record as a radio-style stream that names its songs.

Players show what their source tells them. A line-in has nothing to tell, so Vinyl ID
serves the same audio again as an MP3 stream with the current song in it (ICY metadata,
what internet radio uses). Play this stream on your speakers instead of the line-in and
they show the song.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from . import audio
from .audio import ffmpeg_input
from .config import APP_NAME

if TYPE_CHECKING:
    from .__main__ import App

log = logging.getLogger(__name__)
META_EVERY = 16000   # bytes of audio between two metadata blocks (about 0.4 s)
MAX_LISTENERS = 4    # each listener is its own encoder; a player normally opens one
WRITE_SECONDS = 15   # a player that takes nothing for this long is gone
_listeners = 0


def icy_block(title: str, url: str = "") -> bytes:
    """One ICY metadata block: a length byte (in 16-byte units), then the padded text."""
    clean = title.replace("';", "' ;")
    text = f"StreamTitle='{clean}';" + (f"StreamUrl='{url}';" if url else "")
    raw = text.encode("utf-8")[:4080]
    units = (len(raw) + 15) // 16
    return bytes([units]) + raw.ljust(units * 16, b"\0")


async def record_stream(req: web.Request, app: App) -> web.StreamResponse:
    global _listeners
    if _listeners >= MAX_LISTENERS:
        raise web.HTTPServiceUnavailable(text="Too many listeners on the record stream")
    _listeners += 1
    try:
        return await _serve(req, app)
    finally:
        _listeners -= 1


async def _serve(req: web.Request, app: App) -> web.StreamResponse:
    with_meta = req.headers.get("Icy-MetaData") == "1"
    headers = {"Content-Type": "audio/mpeg", "Cache-Control": "no-cache", "icy-name": APP_NAME}
    if with_meta:
        headers["icy-metaint"] = str(META_EVERY)
    resp = web.StreamResponse(headers=headers)
    await resp.prepare(req)
    proc = await asyncio.create_subprocess_exec(
        *ffmpeg_input(app.s.audio_input, app.s.audio_input_format),
        "-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    assert proc.stdout is not None
    log.info("Record stream opened by %s", req.remote)
    sent, now, n = None, None, 0
    try:
        while True:
            # a source that hangs ends the stream; the player then opens it again
            data = await asyncio.wait_for(proc.stdout.readexactly(META_EVERY), audio.STALL_SECONDS)
            await asyncio.wait_for(resp.write(data), WRITE_SECONDS)
            if with_meta:
                if n % 5 == 0:                      # about every 2 s is plenty
                    now = app.stream_title()
                n += 1
                block = b"\0" if now == sent else icy_block(*now)             # 0 = nothing new
                await asyncio.wait_for(resp.write(block), WRITE_SECONDS)
                sent = now
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        log.info("Record stream closed by %s", req.remote)
    return resp
