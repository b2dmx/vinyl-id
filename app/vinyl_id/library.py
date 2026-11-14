"""Your record library: songs, albums, saved covers and local fingerprints (SQLite)."""

from __future__ import annotations

import functools
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

MAX_HASHES_PER_SONG = 20000
MIN_MATCH = 40          # aligned hashes needed to trust a local match
MIN_RATIO = 2.0         # best match must beat the runner-up by this much
LEFTOVER_DAYS = 30      # covers of removed albums are kept this long, then deleted

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
  id INTEGER PRIMARY KEY, artist TEXT NOT NULL, album TEXT NOT NULL,
  art_file TEXT, art_locked INTEGER NOT NULL DEFAULT 0,
  UNIQUE (artist COLLATE NOCASE, album COLLATE NOCASE));
CREATE TABLE IF NOT EXISTS songs (
  id INTEGER PRIMARY KEY, shazam_id TEXT UNIQUE, title TEXT NOT NULL, artist TEXT NOT NULL,
  album_id INTEGER REFERENCES albums(id), plays INTEGER NOT NULL DEFAULT 0,
  last_played REAL, created REAL NOT NULL, hashes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY, album_id INTEGER NOT NULL, n INTEGER NOT NULL,
  disc INTEGER NOT NULL DEFAULT 1, side TEXT NOT NULL, pos INTEGER NOT NULL, title TEXT NOT NULL,
  length REAL, song_id INTEGER);
CREATE INDEX IF NOT EXISTS tracks_album ON tracks (album_id, n);
CREATE TABLE IF NOT EXISTS leftovers (
  id INTEGER PRIMARY KEY, file TEXT NOT NULL, artist TEXT NOT NULL, album TEXT NOT NULL,
  disc INTEGER, removed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS hashes (
  hash INTEGER NOT NULL, song_id INTEGER NOT NULL, t INTEGER NOT NULL,
  PRIMARY KEY (hash, song_id, t)) WITHOUT ROWID;
"""


def as_jpeg(data: bytes) -> bytes:
    """Covers are stored and served as JPEG, the one format every display reads.
    PNG, WebP and GIF are converted with ffmpeg; if that fails the image is kept as it is."""
    if data[:3] == b"\xff\xd8\xff":
        return data
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-frames:v", "1",
             "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "pipe:1"],
            input=data, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return data
    return out.stdout if out.returncode == 0 and out.stdout[:3] == b"\xff\xd8\xff" else data


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
    s = re.sub(r"^\s*(the|a|an)\s+", "", s)          # Shazam says "Harbour", the sleeve "The Harbour"
    return re.sub(r"[\W_]+", "", s)          # letters and digits of any script


def _same_title(a: str, b: str) -> bool:
    """Titles already through _norm. Shazam says "Credits" where the sleeve says "Closing Credits":
    one inside the other counts, unless it is too short to mean anything."""
    return a == b or (min(len(a), len(b)) >= 5 and (a in b or b in a))


def _same_artist(a: str, b: str) -> bool:
    """Loosely: one name inside the other, or one of them unknown."""
    a, b = _norm(a), _norm(b)
    return not a or not b or a in b or b in a


def _pick(tracks: list, title: str):
    """The track with this title: an exact one, else the only one that contains it (or is contained)."""
    want = _norm(title)
    if not want:                              # nothing left to compare (a title of symbols only)
        return None
    exact = [t for t in tracks if _norm(t["title"]) == want]
    if exact:
        # "Battle (Day)" and "Battle (Night)" compare equal: the one spelled as asked comes first
        same = [t for t in exact if t["title"].strip().lower() == title.strip().lower()]
        return (same or exact)[0]
    close = [t for t in tracks if _same_title(_norm(t["title"]), want)]
    return close[0] if len(close) == 1 else None


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
    def set_track_lengths(self, album_id: int, lengths: dict[str, float]) -> list[str]:
        """Lengths in seconds by track number ('A1', 'b10'). Returns the numbers not on the album."""
        unknown = []
        for key, secs in lengths.items():
            # "A1", or "side|position" for releases whose sides are numbers
            m = re.match(r"^\s*([^|]+?)\s*\|\s*(\d+)\s*$", key) or re.match(r"^\s*([A-Za-z]+)\s*(\d+)\s*$", key)
            cur = self.db.execute("UPDATE tracks SET length=? WHERE album_id=? AND UPPER(side)=? AND pos=?",
                                  (secs, album_id, m.group(1).upper(), int(m.group(2)))) if m else None
            if cur is None or cur.rowcount == 0:
                unknown.append(key)
        self.db.commit()
        return unknown

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
            return _pick(tracks, s.title)
        return None

    @locked
    def find_track(self, title: str, artist: str) -> dict | None:
        """A track on any pre-loaded album matching this song (most recently played album first)."""
        rows = self.db.execute(
            "SELECT t.id, t.album_id, t.title, a.artist, MAX(s.last_played) lp FROM tracks t "
            "JOIN albums a ON a.id = t.album_id LEFT JOIN songs s ON s.album_id = a.id "
            "GROUP BY t.id ORDER BY lp DESC NULLS LAST").fetchall()
        r = _pick([r for r in rows if _same_artist(r["artist"], artist)], title)
        return dict(self.db.execute(
            "SELECT id, album_id, n, disc, side, pos, title, length, song_id FROM tracks WHERE id=?",
            (r["id"],)).fetchone()) if r else None

    @locked
    def title_in_album(self, album_id: int, title: str) -> dict | None:
        """The track of this album with this title."""
        return _pick(self.album_tracks(album_id), title)

    @locked
    def attach_song(self, song_id: int, track: dict) -> int:
        """Make a song this track. Returns the song id to use (the track's own, if it had one:
        the other song is then the same song a second time, and is folded into it)."""
        row = self.db.execute("SELECT song_id FROM tracks WHERE id=?", (track["id"],)).fetchone()
        if row and row["song_id"] and row["song_id"] != song_id and (own := self.song(row["song_id"])):
            mine = self.song(song_id)
            if mine and _same_artist(mine.artist, own.artist):   # same title by someone else stays its own song
                self._merge(song_id, row["song_id"])
            return row["song_id"]
        was = self.db.execute("SELECT album_id FROM songs WHERE id=?", (song_id,)).fetchone()
        self.db.execute("UPDATE tracks SET song_id=NULL WHERE song_id=?", (song_id,))   # one track per song
        self.db.execute("UPDATE tracks SET song_id=? WHERE id=?", (song_id, track["id"]))
        self.db.execute("UPDATE songs SET album_id=? WHERE id=?", (track["album_id"], song_id))
        self._gc_album(was and was["album_id"], keep_fetched=False)
        self.db.commit()
        return song_id

    def _merge(self, src: int, dst: int) -> None:
        """Fold song `src` into `dst`: what was learned, its Shazam id and its play count move over."""
        old = self.db.execute("SELECT shazam_id, plays, album_id FROM songs WHERE id=?", (src,)).fetchone()
        if not old:
            return
        moved = self.db.execute("UPDATE OR IGNORE hashes SET song_id=? WHERE song_id=?", (dst, src)).rowcount
        self.db.execute("DELETE FROM hashes WHERE song_id=?", (src,))
        self.db.execute("UPDATE tracks SET song_id=NULL WHERE song_id=?", (src,))
        self.db.execute("DELETE FROM songs WHERE id=?", (src,))
        self.db.execute("UPDATE songs SET shazam_id=COALESCE(shazam_id, ?), plays=plays+?, hashes=hashes+? WHERE id=?",
                        (old["shazam_id"], old["plays"], moved, dst))
        self._gc_album(old["album_id"], keep_fetched=False)
        self.db.commit()

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
        self.leftover_dir = data_dir / "leftover-covers"
        self.leftover_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = 0                       # 0 = no limit (set from the settings)
        self._size: tuple[float, int] = (0.0, 0)  # (when measured, bytes)
        self.db = sqlite3.connect(data_dir / "library.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, far fewer disk flushes
        self._migrate()
        self.lock = threading.RLock()
        self._purge_leftovers()

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Columns added after the first release."""
        if "mbid" not in self._columns("albums"):
            self.db.execute("ALTER TABLE albums ADD COLUMN mbid TEXT")
        if "disc" not in self._columns("tracks"):
            self.db.execute("ALTER TABLE tracks ADD COLUMN disc INTEGER NOT NULL DEFAULT 1")
        if "lp_covers" not in self._columns("albums"):   # box sets: show each LP's own cover
            self.db.execute("ALTER TABLE albums ADD COLUMN lp_covers INTEGER NOT NULL DEFAULT 1")
        if "hashes" not in self._columns("songs"):   # a kept count: counting the rows scans every song's hashes
            self.db.execute("ALTER TABLE songs ADD COLUMN hashes INTEGER NOT NULL DEFAULT 0")
            counts = self.db.execute("SELECT COUNT(*), song_id FROM hashes GROUP BY song_id").fetchall()
            self.db.executemany("UPDATE songs SET hashes=? WHERE id=?", [tuple(c) for c in counts])
        self.db.commit()

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
    def compact(self) -> None:
        """Give freed space back to the disk (after songs were forgotten)."""
        self.db.commit()
        self.db.execute("VACUUM")
        self._size = (0.0, 0)

    def size_bytes(self) -> int:
        """Everything the library keeps on disk. Measured at most once a minute."""
        at, size = self._size
        if time.monotonic() - at > 60 or not at:
            size = 0
            for p in self.dir.rglob("*"):
                try:                              # a file can vanish while this looks (a cover being replaced)
                    size += p.stat().st_size if p.is_file() and p.name != "spin.json" else 0
                except OSError:
                    pass
            self._size = (time.monotonic(), size)
        return size

    @property
    def full(self) -> bool:
        """The size limit is reached: nothing more is learned (covers and edits are still saved)."""
        return bool(self.max_bytes) and self.size_bytes() >= self.max_bytes

    @locked
    def can_learn(self, song_id: int) -> bool:
        return not self.full and self.hash_count(song_id) < MAX_HASHES_PER_SONG

    @locked
    def set_song_album(self, song_id: int, album_id: int) -> None:
        was = self.db.execute("SELECT album_id FROM songs WHERE id=?", (song_id,)).fetchone()
        self.db.execute("UPDATE songs SET album_id=? WHERE id=?", (album_id, song_id))
        self._gc_album(was and was["album_id"])
        self.db.commit()

    @locked
    def hash_count(self, song_id: int) -> int:
        r = self.db.execute("SELECT hashes FROM songs WHERE id=?", (song_id,)).fetchone()
        return r[0] if r else 0

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

    @locked
    def shazam_song(self, shazam_id: str) -> int | None:
        """The song already saved under this Shazam id."""
        row = self.db.execute("SELECT id FROM songs WHERE shazam_id=?", (shazam_id,)).fetchone() if shazam_id else None
        return row[0] if row else None

    @locked
    def tag_shazam(self, song_id: int, shazam_id: str, artist: str) -> None:
        """Remember Shazam's id for a song, so the next time it is found without comparing titles.
        Not when the artists differ: the same title by someone else must stay a title match only."""
        song = self.song(song_id)
        if shazam_id and song and _same_artist(song.artist, artist):
            self.db.execute("UPDATE OR IGNORE songs SET shazam_id=? WHERE id=? AND shazam_id IS NULL",
                            (shazam_id, song_id))
            self.db.commit()

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
        gone = self.db.execute("SELECT 1 FROM songs WHERE id=?", (song_id,)).fetchone() is None
        if room <= 0 or not hs or self.full or gone:      # gone: forgotten while this was being worked out
            return
        cur = self.db.executemany("INSERT OR IGNORE INTO hashes VALUES (?,?,?)",
                                  [(h, song_id, t + t0) for h, t in hs[:room]])
        self.db.execute("UPDATE songs SET hashes = hashes + ? WHERE id=?", (cur.rowcount, song_id))
        self.db.commit()

    @locked
    def played(self, song_id: int) -> None:
        self.db.execute("UPDATE songs SET plays = plays + 1, last_played = ? WHERE id = ?", (time.time(), song_id))
        self.db.commit()

    def set_art(self, album_id: int, data: bytes, locked: bool) -> None:
        data = as_jpeg(data)            # before taking the lock: converting can take seconds
        name = f"{album_id}.jpg"
        with self.lock:
            (self.art_dir / name).write_bytes(data)
            self.db.execute("UPDATE albums SET art_file=?, art_locked=? WHERE id=?", (name, int(locked), album_id))
            self.db.commit()

    @locked
    def art_bytes(self, album_id: int) -> bytes | None:
        r = self.db.execute("SELECT art_file FROM albums WHERE id=?", (album_id,)).fetchone()
        if r and r[0] and (self.art_dir / r[0]).exists():
            return (self.art_dir / r[0]).read_bytes()
        return None

    # --- a cover per disc of a box set (falls back to the album's) ---------------------
    @staticmethod
    def _disc_art_name(album_id: int, disc: int) -> str:
        return f"{album_id}-disc{disc}.jpg"

    def set_disc_art(self, album_id: int, disc: int, data: bytes) -> None:
        (self.art_dir / self._disc_art_name(album_id, disc)).write_bytes(as_jpeg(data))

    def disc_art_file(self, album_id: int, disc: int) -> str | None:
        name = self._disc_art_name(album_id, disc)
        return name if (self.art_dir / name).exists() else None

    def discs_with_art(self, album_id: int) -> list[int]:
        return sorted(int(p.stem.split("-disc")[1]) for p in self.art_dir.glob(f"{album_id}-disc*.jpg"))

    def art_file_bytes(self, name: str) -> bytes | None:
        p = self.art_dir / name
        return p.read_bytes() if p.exists() else None

    def art_version(self, art_file: str) -> int:
        """Changes only when the cover does, so displays re-download it only then."""
        try:
            return int((self.art_dir / art_file).stat().st_mtime)
        except OSError:
            return 0

    @locked
    def forget(self, song_id: int) -> None:
        was = self.db.execute("SELECT album_id FROM songs WHERE id=?", (song_id,)).fetchone()
        self.db.execute("DELETE FROM hashes WHERE song_id=?", (song_id,))
        self.db.execute("DELETE FROM songs WHERE id=?", (song_id,))
        self.db.execute("UPDATE tracks SET song_id=NULL WHERE song_id=?", (song_id,))
        self._gc_album(was and was["album_id"])
        self.db.commit()

    def _gc_album(self, album_id: int | None, keep_fetched: bool = True) -> None:
        """A song just left this album: drop the album if it now has no songs and no track list.
        (Only this one: an album you added and have not filled yet is left alone.)
        Its covers are set aside for a while instead of deleted, so a slip can be undone.
        keep_fetched=False: the album only emptied because its song turned out to be a track of
        another album, so a cover that was fetched for it (not one you chose) is just removed."""
        gone = self.db.execute(
            "SELECT id, artist, album, art_locked FROM albums WHERE id=? AND id NOT IN "
            "(SELECT album_id FROM songs WHERE album_id IS NOT NULL) "
            "AND id NOT IN (SELECT album_id FROM tracks)", (album_id,)).fetchall()
        for a in gone:
            if keep_fetched or a["art_locked"]:
                self._set_aside(self.art_dir / f"{a['id']}.jpg", a["artist"], a["album"], None)
            else:
                (self.art_dir / f"{a['id']}.jpg").unlink(missing_ok=True)
            for p in self.art_dir.glob(f"{a['id']}-disc*.jpg"):
                self._set_aside(p, a["artist"], a["album"], int(p.stem.split("-disc")[1]))
            self.db.execute("DELETE FROM albums WHERE id=?", (a["id"],))

    def _set_aside(self, path: Path, artist: str, album: str, disc: int | None) -> None:
        """Move a cover file to the leftovers instead of losing it."""
        if path.exists():
            name = f"{uuid.uuid4().hex}.jpg"
            path.replace(self.leftover_dir / name)
            self.db.execute("INSERT INTO leftovers (file, artist, album, disc, removed_at) VALUES (?,?,?,?,?)",
                            (name, artist, album, disc, time.time()))

    # --- covers of removed albums --------------------------------------------
    def _drop_leftover(self, row) -> None:
        (self.leftover_dir / row["file"]).unlink(missing_ok=True)
        self.db.execute("DELETE FROM leftovers WHERE id=?", (row["id"],))

    @locked
    def _purge_leftovers(self) -> None:
        old = self.db.execute("SELECT id, file FROM leftovers WHERE removed_at < ?",
                              (time.time() - LEFTOVER_DAYS * 86400,)).fetchall()
        for row in old:
            self._drop_leftover(row)
        if old:
            self.db.commit()

    @locked
    def leftovers(self) -> list[dict]:
        self._purge_leftovers()
        rows = self.db.execute(
            "SELECT id, artist, album, disc, removed_at FROM leftovers ORDER BY removed_at DESC").fetchall()
        return [dict(r) | {"days_left": max(0, round(LEFTOVER_DAYS - (time.time() - r["removed_at"]) / 86400))}
                for r in rows]

    @locked
    def leftover_bytes(self, leftover_id: int) -> bytes | None:
        row = self.db.execute("SELECT file FROM leftovers WHERE id=?", (leftover_id,)).fetchone()
        path = self.leftover_dir / row["file"] if row else None
        return path.read_bytes() if path and path.exists() else None

    @locked
    def delete_leftover(self, leftover_id: int) -> bool:
        row = self.db.execute("SELECT id, file FROM leftovers WHERE id=?", (leftover_id,)).fetchone()
        if not row:
            return False
        self._drop_leftover(row)
        self.db.commit()
        return True

    @locked
    def restore_leftover(self, leftover_id: int, album_id: int) -> bool:
        """Put a set-aside cover on an album: as that LP's cover if the album has such an LP,
        otherwise as the album cover."""
        row = self.db.execute("SELECT id, file, disc FROM leftovers WHERE id=?", (leftover_id,)).fetchone()
        data = self.leftover_bytes(leftover_id)
        target = self.album(album_id)
        if not row or data is None or target is None:
            return False
        self._drop_leftover(row)
        # the cover it replaces is set aside in turn, so nothing is lost by a wrong pick
        if row["disc"] and any(t["disc"] == row["disc"] for t in self.album_tracks(album_id)):
            self._set_aside(self.art_dir / self._disc_art_name(album_id, row["disc"]),
                            target["artist"], target["album"], row["disc"])
            self.set_disc_art(album_id, row["disc"], data)
        else:
            self._set_aside(self.art_dir / f"{album_id}.jpg", target["artist"], target["album"], None)
            self.set_art(album_id, data, locked=True)
        self.db.commit()
        return True

    # --- editing (used by the web UI) ---------------------------------------
    @locked
    def albums(self, q: str = "") -> list[dict]:
        like = f"%{q}%"
        rows = self.db.execute(
            "SELECT a.id, a.artist, a.album, a.art_file IS NOT NULL has_art, a.art_locked, a.lp_covers, "
            "COUNT(s.id) songs, MAX(s.last_played) last_played, "
            "(SELECT COUNT(*) FROM tracks t WHERE t.album_id = a.id) tracks FROM albums a "
            "LEFT JOIN songs s ON s.album_id = a.id "
            "WHERE a.artist LIKE ? OR a.album LIKE ? GROUP BY a.id ORDER BY last_played DESC NULLS LAST",
            (like, like)).fetchall()
        # art_v changes with the cover, so a page never shows a removed album's cover for a new one
        return [dict(r) | {"art_v": self.art_version(f"{r['id']}.jpg") if r["has_art"] else 0} for r in rows]

    @locked
    def album(self, album_id: int) -> dict | None:
        r = self.db.execute("SELECT id, artist, album, lp_covers FROM albums WHERE id=?", (album_id,)).fetchone()
        return dict(r) if r else None

    @locked
    def set_lp_covers(self, album_id: int, on: bool) -> None:
        """Whether a box set shows each LP's own cover (when it has one) or always the album's."""
        self.db.execute("UPDATE albums SET lp_covers=? WHERE id=?", (int(on), album_id))
        self.db.commit()

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
