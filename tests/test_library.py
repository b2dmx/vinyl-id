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
