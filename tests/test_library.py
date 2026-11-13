import numpy as np

from vinyl_id.fingerprint import hashes
from vinyl_id.library import Library

SR = 16000


def fake_song(seed: int, seconds: int = 60) -> np.ndarray:
    """Random melody with harmonics: enough structure to fingerprint."""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(int(seconds / 0.25)):
        t = np.arange(int(SR * 0.25)) / SR
        f = 110 * 2 ** (rng.integers(0, 36) / 12)
        note = sum(np.sin(2 * np.pi * f * k * t) / k for k in (1, 2, 3))
        out.append(note * np.hanning(len(t)))
    x = np.concatenate(out)
    return (x / np.abs(x).max() * 12000).astype(np.int16)


def degrade(x: np.ndarray, seed: int) -> np.ndarray:
    """Quieter, plus vinyl-ish hiss and crackle."""
    rng = np.random.default_rng(seed)
    y = x.astype(np.float64) * 0.6 + rng.normal(0, 600, len(x))
    y[rng.integers(0, len(x), 300)] += rng.normal(0, 8000, 300)
    return np.clip(y, -32768, 32767).astype(np.int16)


def test_learned_song_matches_locally(tmp_path):
    lib = Library(tmp_path)
    songs = {}
    for seed in (1, 2, 3):
        sid, _ = lib.upsert(f"Song {seed}", "Artist", "LP", f"shz{seed}")
        lib.learn(sid, hashes(fake_song(seed)), 0)
        songs[seed] = sid

    clip = degrade(fake_song(2)[SR * 30:SR * 42], seed=9)  # 12 s from the middle
    hit = lib.match(hashes(clip))
    assert hit and hit[0] == songs[2]

    unknown = degrade(fake_song(7)[SR * 10:SR * 22], seed=4)
    assert lib.match(hashes(unknown)) is None


def test_forget_and_album_art(tmp_path):
    lib = Library(tmp_path)
    sid, needs_art = lib.upsert("A", "B", "C", "x1")
    assert needs_art
    album_id = lib.song(sid).album_id
    lib.set_art(album_id, b"jpg", locked=True)
    assert lib.art_bytes(album_id) == b"jpg"
    sid2, needs_art2 = lib.upsert("A2", "b", "c", "x2")   # same album, different case
    assert lib.song(sid2).album_id == album_id and not needs_art2
    lib.forget(sid)
    lib.forget(sid2)
    assert lib.size() == 0 and lib.albums() == []


def _rows(lib, sid):
    return lib.db.execute("SELECT COUNT(*) FROM hashes WHERE song_id=?", (sid,)).fetchone()[0]


def test_hash_count_is_kept_in_step(tmp_path):
    lib = Library(tmp_path)
    sid, _ = lib.upsert("A", "B", "C", "x1")
    hs = hashes(fake_song(4, 30))
    lib.learn(sid, hs, 0)
    lib.learn(sid, hs, 0)                      # the same audio again adds nothing
    assert lib.hash_count(sid) == _rows(lib, sid) > 0
    assert lib.hash_count(999) == 0


def test_old_library_gets_its_hash_counts(tmp_path):
    lib = Library(tmp_path)
    sid, _ = lib.upsert("A", "B", "C", "x1")
    lib.learn(sid, hashes(fake_song(4, 30)), 0)
    n = lib.hash_count(sid)
    lib.db.execute("ALTER TABLE songs DROP COLUMN hashes")   # as saved by 0.4.4 and earlier
    lib.db.commit()
    lib.db.close()
    assert Library(tmp_path).hash_count(sid) == n


def test_track_lengths_can_be_set_by_track_number(tmp_path):
    lib = Library(tmp_path)
    tracks = [{"n": i, "side": s, "pos": p, "title": f"T{i}"}
              for i, (s, p) in enumerate([("A", 1), ("A", 2), ("B", 1)], start=1)]
    album = lib.add_album_from_release("Band", "LP", "mb", tracks)
    assert lib.set_track_lengths(album, {"A1": 121, "b1": 9, "C4": 30}) == ["C4"]
    assert [t["length"] for t in lib.album_tracks(album)] == [121, None, 9]
