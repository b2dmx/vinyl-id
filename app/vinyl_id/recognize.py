"""Song identification (Shazam) and album-art lookup (iTunes, then Shazam's art)."""

from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import re
import time
import wave
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp
import numpy as np
from shazamio import Shazam

from .config import SAMPLE_RATE, VERSION
from .library import _norm, _pick, _same_artist

log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 10 * 1024 * 1024   # a cover larger than this is not downloaded
ART_SECONDS = 4                      # how long a cover lookup may hold up showing the song
MAX_REDIRECTS = 5
MB_BUSY_PAUSES = (2, 4)              # seconds waited before asking a busy MusicBrainz again
MAX_PRESSINGS = 3                    # track lists read while looking for the one with the song heard
_STOP = {"a", "an", "and", "the", "of", "in", "on", "to", "for", "at", "by", "is", "it"}   # MusicBrainz ignores these


@dataclass
class Track:
    title: str
    artist: str
    album: str = ""
    art_url: str = ""
    shazam_id: str = ""
    offset: float | None = None   # seconds into the song where the sample started (Shazam's match offset)


def _credit(x: dict) -> str:
    """MusicBrainz artist credit ('A feat. B') as one string."""
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in x.get("artist-credit", []))


def _big(itunes_album: dict) -> str:
    """iTunes hands out a 100 px cover; the same address serves 600 px."""
    return itunes_album["artworkUrl100"].replace("100x100bb", "600x600bb")


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
        "artist": _credit(data),
        "year": (data.get("date") or "")[:4], "tracks": tracks,
    }


def _phrase(text: str) -> str:
    """Words to be found together in a MusicBrainz search."""
    return '"' + " ".join(re.sub(r'["\\]', " ", text).split()) + '"'


def usual_pressings(releases: list[dict], artist: str, album: str) -> list[dict]:
    """Of a MusicBrainz search's releases, the vinyl pressings of this album that are worth
    reading, best first: one for each number of tracks, the number most pressings have
    leading (the standard edition, not a box set or a one-off), and of those the oldest."""
    found = []
    for r in releases:
        formats = [(m.get("format") or "").lower() for m in r.get("media") or []]
        if (not formats or not all("vinyl" in f for f in formats)
                or r.get("status") in ("Promotion", "Bootleg", "Pseudo-Release")
                or _norm(r.get("title", "")) != _norm(album) or not _same_artist(_credit(r), artist)):
            continue
        found.append(r)
    often = Counter(r.get("track-count", 0) for r in found)
    found.sort(key=lambda r: (-often[r.get("track-count", 0)], r.get("date") or "9999"))
    first: dict[int, dict] = {}
    for r in found:
        first.setdefault(r.get("track-count", 0), r)
    return list(first.values())


async def is_public(url: str) -> bool:
    """Whether a web address leads out to the internet: http(s), and every address its name
    stands for is a public one (not this machine, the home network or Home Assistant's own)."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return False
    try:
        found = await asyncio.get_running_loop().getaddrinfo(parts.hostname, None)
        return bool(found) and all(ipaddress.ip_address(f[4][0].split("%")[0]).is_global for f in found)
    except (OSError, ValueError):
        return False


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
        self.reachable = True              # whether the last Shazam lookup got an answer at all
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
            self.reachable = False
            return None
        self.reachable = True
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
        return track

    async def art_for(self, track: Track) -> str:
        """Where the song's cover is: the album's from iTunes if that answers soon, else Shazam's."""
        try:
            found = await asyncio.wait_for(self._itunes_art(track), ART_SECONDS)
        except Exception as exc:               # too slow, or no answer: Shazam's own will do
            log.info("No cover from iTunes (%s)", type(exc).__name__)
            found = ""
        return found or track.art_url

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._http

    async def fetch(self, url: str) -> bytes | None:
        """An image from the web, or None. Never more than MAX_IMAGE_BYTES, and only from the
        internet: an address on this machine or the home network is not fetched, also not when
        a web address forwards there."""
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                if not await is_public(url):
                    log.warning("Art download refused: %s is not an address on the internet", url)
                    return None
                async with self._session().get(url, allow_redirects=False) as r:
                    if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                        url = urljoin(url, r.headers["Location"])
                        continue
                    if r.status != 200:
                        return None
                    data = bytearray()
                    async for chunk in r.content.iter_chunked(65536):
                        data += chunk
                        if len(data) > MAX_IMAGE_BYTES:
                            log.warning("Art download stopped: larger than %d MB", MAX_IMAGE_BYTES >> 20)
                            return None
                    return bytes(data)
            log.warning("Art download stopped: forwarded too many times")
            return None
        except Exception as exc:
            log.warning("Art download failed: %s", exc)
            return None

    async def search_art(self, term: str) -> list[dict]:
        """Album cover candidates for the 'fix art' picker."""
        return [{"album": x.get("collectionName", ""), "artist": x.get("artistName", ""),
                 "year": (x.get("releaseDate") or "")[:4], "art": _big(x)}
                for x in await self._itunes_albums(term, 12)]

    async def _itunes_albums(self, term: str, limit: int) -> list[dict]:
        """iTunes albums that have a cover."""
        params = {"term": term, "entity": "album", "limit": str(limit)}
        try:
            async with self._session().get("https://itunes.apple.com/search", params=params) as r:
                data = await r.json(content_type=None)
        except Exception as exc:
            log.warning("iTunes search failed: %s", exc)
            return []
        return [x for x in data.get("results", []) if x.get("artworkUrl100")]

    # --- MusicBrainz (track lists) and Cover Art Archive ---------------------------
    async def _mb(self, path: str, params: dict) -> dict | None:
        """MusicBrainz allows one request per second and wants a descriptive User-Agent."""
        async with self._mb_lock:
            for pause in (*MB_BUSY_PAUSES, None):
                wait = 1.1 - (time.monotonic() - self._mb_last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    async with self._session().get(
                            f"https://musicbrainz.org/ws/2/{path}", params={**params, "fmt": "json"},
                            headers={"User-Agent": f"VinylID/{VERSION} (https://github.com/b2dmx/vinyl-id)"}) as r:
                        if r.status == 200:
                            return await r.json(content_type=None)
                        if r.status != 503 or pause is None:   # 503 = busy, or asked too often
                            log.warning("MusicBrainz answered %s for %s", r.status, path)
                            return None
                except Exception as exc:
                    log.warning("MusicBrainz lookup failed: %s", exc)
                    return None
                finally:
                    self._mb_last = time.monotonic()
                await asyncio.sleep(pause)

    async def search_releases(self, term: str) -> list[dict]:
        """Pressings matching a search, vinyl first, for the 'add album' picker."""
        if ":" not in term:      # plain words: every word must be in the album or the artist
            words = [w for w in (re.sub(r'["\\]', "", w) for w in term.split()) if w]
            words = [w for w in words if w.lower() not in _STOP] or words
            term = " AND ".join(f'(release:"{w}" OR artist:"{w}")' for w in words) or term
        data = await self._mb("release", {"query": term, "limit": "25"})
        out = []
        for r in (data or {}).get("releases", []):
            media = r.get("media") or []
            fmt = ", ".join(sorted({m.get("format", "") for m in media if m.get("format")}))
            out.append({
                "mbid": r["id"], "album": r.get("title", ""),
                "artist": _credit(r),
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

    async def vinyl_release(self, artist: str, album: str, title: str) -> dict | bool | None:
        """The track list of the usual vinyl pressing of an album, which must have the song that
        was heard. False: MusicBrainz has none. None: it could not be asked; try again later."""
        name = re.sub(r"\s*[\(\[].*?[\)\]]", "", album).strip() or album   # "(Remastered)" and the like
        who = re.split(r"\s+(?:feat\.?|ft\.|featuring)\s+", artist, flags=re.IGNORECASE)[0].strip()
        if not _norm(name) or not _norm(who):
            return False
        data = await self._mb("release", {
            "query": f"release:{_phrase(name)} AND artist:{_phrase(who)} AND format:*Vinyl*", "limit": "100"})
        if data is None:
            return None
        for found in usual_pressings(data.get("releases", []), who, name)[:MAX_PRESSINGS]:
            rel = await self.release(found["id"])
            if rel is None:
                return None
            if _pick(rel["tracks"], title):
                return rel
        return False

    async def front_cover(self, mbid: str) -> bytes | None:
        return await self.fetch(f"https://coverartarchive.org/release/{mbid}/front-500")

    async def _itunes_art(self, track: Track) -> str:
        """Album cover from iTunes (better match for the LP than Shazam's single art)."""
        if not track.album:
            return ""
        want = track.album.lower()
        return next((_big(x) for x in await self._itunes_albums(f"{track.artist} {track.album}", 5)
                     if want in x.get("collectionName", "").lower()), "")
