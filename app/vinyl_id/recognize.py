"""Song identification (Shazam) and album-art lookup (iTunes, then Shazam's art)."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
import wave
from dataclasses import dataclass

import aiohttp
import numpy as np
from shazamio import Shazam

from .config import SAMPLE_RATE, VERSION

log = logging.getLogger(__name__)


@dataclass
class Track:
    title: str
    artist: str
    album: str = ""
    art_url: str = ""
    shazam_id: str = ""
    offset: float | None = None   # seconds into the song where the sample started (Shazam's match offset)

    @property
    def key(self) -> str:
        return (self.shazam_id or f"{self.artist}|{self.title}").lower()


def _shift(side: str, by: int) -> str:
    """A -> C when shifted by two sides (single letters only; anything else is left alone)."""
    return chr(ord(side) + by) if by and len(side) == 1 and side.isalpha() else side


def parse_release(data: dict) -> dict:
    """A MusicBrainz release -> {mbid, artist, album, year, tracks:[{n, side, pos, title, length}]}.
    Vinyl track numbers look like 'A1', 'B3'; for anything else each medium counts as one side."""
    tracks, n, used = [], 0, []          # used: side letters of earlier discs, in order
    for disc, medium in enumerate(data.get("media") or [], start=1):
        parsed = [(re.match(r"^([A-Za-z]+)(\d+)$", (t.get("number") or "").strip()), t)
                  for t in medium.get("tracks") or []]
        letters = list(dict.fromkeys(m.group(1).upper() for m, _ in parsed if m))
        # a second LP whose sides restart at A/B becomes C/D, so every side is unique
        shift = len(used) if any(s in used for s in letters) else 0
        for i, (m, t) in enumerate(parsed, start=1):
            n += 1
            if m:
                side, pos = _shift(m.group(1).upper(), shift), int(m.group(2))
            else:
                side, pos = str(medium.get("position", disc)), i
            ms = t.get("length") or (t.get("recording") or {}).get("length")
            tracks.append({"n": n, "disc": disc, "side": side, "pos": pos, "title": t.get("title", ""),
                           "length": ms / 1000 if ms else None})
        used += [s for s in dict.fromkeys(x["side"] for x in tracks if x["disc"] == disc)]
    return {
        "mbid": data.get("id", ""), "album": data.get("title", ""),
        "artist": "".join(c.get("name", "") + c.get("joinphrase", "")
                          for c in data.get("artist-credit", [])),
        "year": (data.get("date") or "")[:4], "tracks": tracks,
    }


def _wav(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return buf.getvalue()


class Recognizer:
    def __init__(self) -> None:
        self._shazam = Shazam()
        self._http: aiohttp.ClientSession | None = None
        self._mb_lock = asyncio.Lock()
        self._mb_last = 0.0

    async def close(self) -> None:
        if self._http:
            await self._http.close()

    async def identify(self, pcm: np.ndarray) -> Track | None:
        """Fingerprint is made locally; only the fingerprint goes to Shazam."""
        try:
            out = await self._shazam.recognize(_wav(pcm))
        except Exception as exc:  # network / API change
            log.warning("Shazam lookup failed: %s", exc)
            return None
        t = out.get("track")
        if not t:
            return None

        album = ""
        for section in t.get("sections", []):
            for m in section.get("metadata", []) or []:
                if m.get("title") == "Album":
                    album = m.get("text", "")
        images = t.get("images", {}) or {}
        matches = out.get("matches") or []
        offset = matches[0].get("offset") if matches else None
        track = Track(
            offset=float(offset) if isinstance(offset, (int, float)) else None,
            title=t.get("title", ""),
            artist=t.get("subtitle", ""),
            album=album,
            art_url=images.get("coverarthq") or images.get("coverart") or "",
            shazam_id=str(t.get("key", "")),
        )
        better = await self._itunes_art(track)
        if better:
            track.art_url = better
        return track

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._http

    async def fetch(self, url: str) -> bytes | None:
        try:
            async with self._session().get(url) as r:
                return await r.read() if r.status == 200 else None
        except Exception as exc:
            log.warning("Art download failed: %s", exc)
            return None

    async def search_art(self, term: str) -> list[dict]:
        """Album cover candidates for the 'fix art' picker."""
        params = {"term": term, "entity": "album", "limit": "12"}
        try:
            async with self._session().get("https://itunes.apple.com/search", params=params) as r:
                data = await r.json(content_type=None)
        except Exception as exc:
            log.warning("Art search failed: %s", exc)
            return []
        return [
            {"album": x.get("collectionName", ""), "artist": x.get("artistName", ""),
             "year": (x.get("releaseDate") or "")[:4],
             "art": x["artworkUrl100"].replace("100x100bb", "600x600bb")}
            for x in data.get("results", []) if x.get("artworkUrl100")
        ]

    # --- MusicBrainz (track lists) and Cover Art Archive ---------------------------
    async def _mb(self, path: str, params: dict) -> dict | None:
        """MusicBrainz allows one request per second and wants a descriptive User-Agent."""
        async with self._mb_lock:
            wait = 1.1 - (time.monotonic() - self._mb_last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self._session().get(
                        f"https://musicbrainz.org/ws/2/{path}", params={**params, "fmt": "json"},
                        headers={"User-Agent": f"VinylID/{VERSION} (https://github.com/b2dmx/vinyl-id)"}) as r:
                    return await r.json(content_type=None) if r.status == 200 else None
            except Exception as exc:
                log.warning("MusicBrainz lookup failed: %s", exc)
                return None
            finally:
                self._mb_last = time.monotonic()

    async def search_releases(self, term: str) -> list[dict]:
        """Pressings matching a search, vinyl first, for the 'add album' picker."""
        data = await self._mb("release", {"query": term, "limit": "25"})
        out = []
        for r in (data or {}).get("releases", []):
            media = r.get("media") or []
            fmt = ", ".join(sorted({m.get("format", "") for m in media if m.get("format")}))
            out.append({
                "mbid": r["id"], "album": r.get("title", ""),
                "artist": "".join(c.get("name", "") + c.get("joinphrase", "")
                                  for c in r.get("artist-credit", [])),
                "year": (r.get("date") or "")[:4], "country": r.get("country", ""),
                "format": fmt, "tracks": r.get("track-count", 0),
                "label": next((li["label"]["name"] for li in r.get("label-info", [])
                               if li.get("label")), ""),
            })
        out.sort(key=lambda x: "vinyl" not in x["format"].lower())
        return out

    async def release(self, mbid: str) -> dict | None:
        data = await self._mb(f"release/{mbid}", {"inc": "recordings+artist-credits+media"})
        return parse_release(data) if data else None

    async def front_cover(self, mbid: str) -> bytes | None:
        return await self.fetch(f"https://coverartarchive.org/release/{mbid}/front-500")

    async def _itunes_art(self, track: Track) -> str:
        """Album cover from iTunes (better match for the LP than Shazam's single art)."""
        if not track.album:
            return ""
        params = {"term": f"{track.artist} {track.album}", "entity": "album", "limit": "5"}
        try:
            async with self._session().get("https://itunes.apple.com/search", params=params) as r:
                data = await r.json(content_type=None)
        except Exception as exc:
            log.debug("iTunes lookup failed: %s", exc)
            return ""
        want = track.album.lower()
        for res in data.get("results", []):
            if want in res.get("collectionName", "").lower() and res.get("artworkUrl100"):
                return res["artworkUrl100"].replace("100x100bb", "600x600bb")
        return ""
