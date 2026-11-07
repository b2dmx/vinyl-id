"""Your record library: songs, albums, saved covers and local fingerprints (SQLite)."""

from __future__ import annotations

import functools
import re
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .fingerprint import FRAMES_PER_SECOND

MAX_HASHES_PER_SONG = 20000
MIN_MATCH = 40          # aligned hashes needed to trust a local match
MIN_RATIO = 2.0         # best match must beat the runner-up by this much

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
  id INTEGER PRIMARY KEY, artist TEXT NOT NULL, album TEXT NOT NULL,
  art_file TEXT, art_locked INTEGER NOT NULL DEFAULT 0,
  UNIQUE (artist COLLATE NOCASE, album COLLATE NOCASE));
CREATE TABLE IF NOT EXISTS songs (
  id INTEGER PRIMARY KEY, shazam_id TEXT UNIQUE, title TEXT NOT NULL, artist TEXT NOT NULL,
  album_id INTEGER REFERENCES albums(id), plays INTEGER NOT NULL DEFAULT 0,
  last_played REAL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY, album_id INTEGER NOT NULL, n INTEGER NOT NULL,
  disc INTEGER NOT NULL DEFAULT 1, side TEXT NOT NULL, pos INTEGER NOT NULL, title TEXT NOT NULL,
  length REAL, song_id INTEGER);
CREATE INDEX IF NOT EXISTS tracks_album ON tracks (album_id, n);
CREATE TABLE IF NOT EXISTS hashes (
  hash INTEGER NOT NULL, song_id INTEGER NOT NULL, t INTEGER NOT NULL,
  PRIMARY KEY (hash, song_id, t)) WITHOUT ROWID;
"""


def locked(fn):
    """The library is used from the main loop and worker threads; serialize access."""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)
    return wrapper


@dataclass
class Song:
    id: int
    title: str
    artist: str
    album_id: int | None
    album: str
    art_file: str | None


def _norm(s: str) -> str:
    """Compare titles loosely: no case, no '(Remastered)'-style suffix, no punctuation."""
    s = re.sub(r"[\(\[].*?[\)\]]", "", s.lower())
    s = re.sub(r"\s-\s.*(remaster|mix|version|edit|mono|stereo).*$", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


class TrackMixin:
    """Track lists for pre-loaded albums (side, position, title, length)."""

    @locked
    def add_tracks(self, album_id: int, mbid: str, tracks: list[dict]) -> None:
        """Store (replace) an album's track list. Links to songs already learned are kept by title."""
        old = {_norm(r["title"]): r["song_id"] for r in self.db.execute(
            "SELECT title, song_id FROM tracks WHERE album_id=?", (album_id,)) if r["song_id"]}
        self.db.execute("DELETE FROM tracks WHERE album_id=?", (album_id,))
        for t in tracks:
            self.db.execute(
                "INSERT INTO tracks (album_id, n, disc, side, pos, title, length, song_id) VALUES (?,?,?,?,?,?,?,?)",
                (album_id, t["n"], t.get("disc", 1), t["side"], t["pos"], t["title"], t.get("length"),
                 old.get(_norm(t["title"]))))
        self.db.execute("UPDATE albums SET mbid=? WHERE id=?", (mbid, album_id))
        self.db.commit()

    @locked
    def album_tracks(self, album_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, album_id, n, disc, side, pos, title, length, song_id FROM tracks "
            "WHERE album_id=? ORDER BY n", (album_id,)).fetchall()
        return [dict(r) for r in rows]

    @locked
    def song_for_track(self, track_id: int) -> int | None:
        """The song row behind a track, created (and linked) the first time it's needed."""
        t = self.db.execute(
            "SELECT t.title, t.album_id, t.song_id, a.artist FROM tracks t "
            "JOIN albums a ON a.id = t.album_id WHERE t.id=?", (track_id,)).fetchone()
        if not t:
            return None
        if t["song_id"] and self.song(t["song_id"]):
            return t["song_id"]
        cur = self.db.execute(
            "INSERT INTO songs (title, artist, album_id, created) VALUES (?,?,?,?)",
            (t["title"], t["artist"], t["album_id"], time.time()))
        self.db.execute("UPDATE tracks SET song_id=? WHERE id=?", (cur.lastrowid, track_id))
        self.db.commit()
        return cur.lastrowid

    @locked
    def track_in_album(self, album_id: int, song_id: int) -> dict | None:
        """The track of this album that a song is (by link, else by title)."""
        tracks = self.album_tracks(album_id)
        for t in tracks:
            if t["song_id"] == song_id:
                return t
        s = self.song(song_id)
        if s:
            for t in tracks:
                if _norm(t["title"]) == _norm(s.title):
                    return t
        return None

    @locked
    def find_track(self, title: str, artist: str) -> dict | None:
        """A track on any pre-loaded album matching this song (most recently played album first)."""
        rows = self.db.execute(
            "SELECT t.id, t.album_id, t.title, a.artist, MAX(s.last_played) lp FROM tracks t "
            "JOIN albums a ON a.id = t.album_id LEFT JOIN songs s ON s.album_id = a.id "
            "GROUP BY t.id ORDER BY lp DESC NULLS LAST").fetchall()
        want, who = _norm(title), _norm(artist)
        for r in rows:
            have = _norm(r["artist"])
            if _norm(r["title"]) == want and (not who or not have or who in have or have in who):
                return next(t for t in self.album_tracks(r["album_id"]) if t["id"] == r["id"])
        return None

    @locked
    def attach_song(self, song_id: int, track: dict) -> int:
        """Make a song this track. Returns the song id to use (the track's own, if it had one)."""
        row = self.db.execute("SELECT song_id FROM tracks WHERE id=?", (track["id"],)).fetchone()
        if row and row["song_id"] and row["song_id"] != song_id and self.song(row["song_id"]):
            return row["song_id"]
        self.db.execute("UPDATE tracks SET song_id=? WHERE id=?", (song_id, track["id"]))
        self.db.execute("UPDATE songs SET album_id=? WHERE id=?", (track["album_id"], song_id))
        self._gc_albums()
        self.db.commit()
        return song_id

    @locked
    def add_album_from_release(self, artist: str, album: str, mbid: str, tracks: list[dict]) -> int:
        album_id = self.add_album(artist, album)
        self.add_tracks(album_id, mbid, tracks)
        return album_id

    @locked
    def album_has_art(self, album_id: int) -> bool:
        r = self.db.execute("SELECT art_file FROM albums WHERE id=?", (album_id,)).fetchone()
        return bool(r and r[0])


class Library(TrackMixin):
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir
        self.art_dir = data_dir / "art"
        self.art_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "library.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if "mbid" not in {r[1] for r in self.db.execute("PRAGMA table_info(albums)")}:
            self.db.execute("ALTER TABLE albums ADD COLUMN mbid TEXT")
        if "disc" not in {r[1] for r in self.db.execute("PRAGMA table_info(tracks)")}:
            self.db.execute("ALTER TABLE tracks ADD COLUMN disc INTEGER NOT NULL DEFAULT 1")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()

    # --- lookups -----------------------------------------------------------
    @locked
    def song(self, song_id: int) -> Song | None:
        r = self.db.execute(
            "SELECT s.id, s.title, s.artist, s.album_id, COALESCE(a.album,'') album, a.art_file "
            "FROM songs s LEFT JOIN albums a ON a.id = s.album_id WHERE s.id = ?", (song_id,)
        ).fetchone()
        return Song(**dict(r)) if r else None

    @locked
    def size(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM songs").fetchone()[0]

    @locked
    def hash_count(self, song_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM hashes WHERE song_id=?", (song_id,)).fetchone()[0]

    @locked
    def match(self, sample: list[tuple[int, int]]) -> tuple[int, int, int] | None:
        """Best (song_id, aligned hash count, frames into the song where the sample starts),
        or None if not confident. The position is right when the song was learned from its start."""
        if not sample:
            return None
        by_hash: dict[int, list[int]] = {}
        for h, t in sample:
            by_hash.setdefault(h, []).append(t)
        votes: Counter[tuple[int, int]] = Counter()
        keys = list(by_hash)
        for i in range(0, len(keys), 900):
            chunk = keys[i:i + 900]
            q = f"SELECT hash, song_id, t FROM hashes WHERE hash IN ({','.join('?' * len(chunk))})"
            for h, sid, t_db in self.db.execute(q, chunk):
                for t_s in by_hash[h]:
                    votes[(sid, (t_db - t_s) // 4)] += 1   # 4-frame (~64 ms) offset bins
        if not votes:
            return None
        best: dict[int, tuple[int, int]] = {}
        for (sid, b), n in votes.items():
            if n > best.get(sid, (0, 0))[0]:
                best[sid] = (n, b)
        ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
        sid, (n, b) = ranked[0]
        runner = ranked[1][1][0] if len(ranked) > 1 else 0
        if n >= MIN_MATCH and n >= MIN_RATIO * max(runner, 1):
            return sid, n, max(0, b * 4)
        return None

    # --- writes ------------------------------------------------------------
    @locked
    def upsert(self, title: str, artist: str, album: str, shazam_id: str) -> tuple[int, bool]:
        """Return (song_id, album_needs_art)."""
        album_id = None
        if album:
            self.db.execute("INSERT OR IGNORE INTO albums (artist, album) VALUES (?, ?)", (artist, album))
            album_id = self.db.execute(
                "SELECT id FROM albums WHERE artist = ? COLLATE NOCASE AND album = ? COLLATE NOCASE",
                (artist, album)).fetchone()[0]
        row = self.db.execute("SELECT id FROM songs WHERE shazam_id = ?", (shazam_id,)).fetchone() if shazam_id else None
        if row:
            sid = row[0]
        else:
            cur = self.db.execute(
                "INSERT INTO songs (shazam_id, title, artist, album_id, created) VALUES (?,?,?,?,?)",
                (shazam_id or None, title, artist, album_id, time.time()))
            sid = cur.lastrowid
        self.db.commit()
        s = self.song(sid)
        return sid, bool(s and s.album_id and not s.art_file)

    @locked
    def learn(self, song_id: int, hs: list[tuple[int, int]], t0: int) -> None:
        room = MAX_HASHES_PER_SONG - self.hash_count(song_id)
        if room <= 0 or not hs:
            return
        self.db.executemany("INSERT OR IGNORE INTO hashes VALUES (?,?,?)", [(h, song_id, t + t0) for h, t in hs[:room]])
        self.db.commit()

    @locked
    def played(self, song_id: int) -> None:
        self.db.execute("UPDATE songs SET plays = plays + 1, last_played = ? WHERE id = ?", (time.time(), song_id))
        self.db.commit()

    @locked
    def set_art(self, album_id: int, data: bytes, locked: bool) -> None:
        name = f"{album_id}.jpg"
        (self.art_dir / name).write_bytes(data)
        self.db.execute("UPDATE albums SET art_file=?, art_locked=? WHERE id=?", (name, int(locked), album_id))
        self.db.commit()

    @locked
    def art_bytes(self, album_id: int) -> bytes | None:
        r = self.db.execute("SELECT art_file FROM albums WHERE id=?", (album_id,)).fetchone()
        if r and r[0] and (self.art_dir / r[0]).exists():
            return (self.art_dir / r[0]).read_bytes()
        return None

    @locked
    def forget(self, song_id: int) -> None:
        self.db.execute("DELETE FROM hashes WHERE song_id=?", (song_id,))
        self.db.execute("DELETE FROM songs WHERE id=?", (song_id,))
        self.db.execute("UPDATE tracks SET song_id=NULL WHERE song_id=?", (song_id,))
        self._gc_albums()
        self.db.commit()

    def _gc_albums(self) -> None:
        """Drop albums with no songs and no track list (pre-loaded albums stay)."""
        self.db.execute(
            "DELETE FROM albums WHERE id NOT IN (SELECT album_id FROM songs WHERE album_id IS NOT NULL) "
            "AND id NOT IN (SELECT album_id FROM tracks)")

    # --- editing (used by the web UI) ---------------------------------------
    @locked
    def albums(self, q: str = "") -> list[dict]:
        like = f"%{q}%"
        rows = self.db.execute(
            "SELECT a.id, a.artist, a.album, a.art_file IS NOT NULL has_art, a.art_locked, "
            "COUNT(s.id) songs, MAX(s.last_played) last_played, "
            "(SELECT COUNT(*) FROM tracks t WHERE t.album_id = a.id) tracks FROM albums a "
            "LEFT JOIN songs s ON s.album_id = a.id "
            "WHERE a.artist LIKE ? OR a.album LIKE ? GROUP BY a.id ORDER BY last_played DESC NULLS LAST",
            (like, like)).fetchall()
        return [dict(r) for r in rows]

    @locked
    def album_songs(self, album_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, title, artist, plays, last_played FROM songs WHERE album_id=? ORDER BY title",
            (album_id,)).fetchall()
        return [dict(r) for r in rows]

    @locked
    def add_album(self, artist: str, album: str) -> int:
        """Manually add an album (or return the existing one with that name)."""
        self.db.execute("INSERT OR IGNORE INTO albums (artist, album) VALUES (?, ?)", (artist, album))
        self.db.commit()
        return self.db.execute(
            "SELECT id FROM albums WHERE artist = ? COLLATE NOCASE AND album = ? COLLATE NOCASE",
            (artist, album)).fetchone()[0]

    @locked
    def add_song(self, album_id: int | None, title: str, artist: str) -> int:
        """Manually add a song; it gets a fingerprint the next time it plays and is labelled."""
        cur = self.db.execute(
            "INSERT INTO songs (title, artist, album_id, created) VALUES (?,?,?,?)",
            (title, artist, album_id, time.time()))
        self.db.commit()
        return cur.lastrowid

    @locked
    def edit_album(self, album_id: int, artist: str, album: str) -> None:
        self.db.execute("UPDATE albums SET artist=?, album=? WHERE id=?", (artist, album, album_id))
        self.db.commit()

    @locked
    def edit_song(self, song_id: int, title: str, artist: str) -> None:
        self.db.execute("UPDATE songs SET title=?, artist=? WHERE id=?", (title, artist, song_id))
        self.db.commit()


def seconds_to_frames(sec: float) -> int:
    return int(sec * FRAMES_PER_SECOND)
