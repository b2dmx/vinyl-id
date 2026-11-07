"""Song identification (Shazam) and album-art lookup (iTunes, then Shazam's art)."""

from __future__ import annotations

import io
import logging
import wave
from dataclasses import dataclass

import aiohttp
import numpy as np
from shazamio import Shazam

from .config import SAMPLE_RATE

log = logging.getLogger(__name__)


@dataclass
class Track:
    title: str
    artist: str
    album: str = ""
    art_url: str = ""
    shazam_id: str = ""

    @property
    def key(self) -> str:
        return (self.shazam_id or f"{self.artist}|{self.title}").lower()


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
        track = Track(
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
