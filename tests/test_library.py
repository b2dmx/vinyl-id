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


def test_disc_cover_and_loose_titles(tmp_path):
    lib = Library(tmp_path)
    tracks = [{"n": 1, "disc": 1, "side": "A", "pos": 1, "title": "The Harbour"},
              {"n": 2, "disc": 2, "side": "C", "pos": 1, "title": "Ruins"}]
    album = lib.add_album_from_release("Composer One & Composer Two", "Box", "mb", tracks)
    assert lib.find_track("Harbour", "Composer Two")["side"] == "A"      # no "The", one of the artists
    assert lib.disc_art_file(album, 2) is None and lib.discs_with_art(album) == []
    lib.set_disc_art(album, 2, b"\xff\xd8\xffdisc2")
    assert lib.discs_with_art(album) == [2]
    assert lib.art_file_bytes(lib.disc_art_file(album, 2)) == b"\xff\xd8\xffdisc2"


def test_lp_covers_can_be_switched_off(tmp_path):
    lib = Library(tmp_path)
    album = lib.add_album("Band", "Box")
    assert lib.album(album)["lp_covers"] == 1
    lib.set_lp_covers(album, False)
    assert lib.album(album)["lp_covers"] == 0 and lib.albums()[0]["lp_covers"] == 0


def test_shazam_title_inside_the_sleeve_title_matches(tmp_path):
    lib = Library(tmp_path)
    tracks = [{"n": 1, "side": "A", "pos": 1, "title": "Closing Credits"},
              {"n": 2, "side": "A", "pos": 2, "title": "Battle (Day)"},
              {"n": 3, "side": "A", "pos": 3, "title": "Battle (Night)"},
              {"n": 4, "side": "B", "pos": 1, "title": "Boat Race"},
              {"n": 5, "side": "B", "pos": 2, "title": "Boat Race: Win"}]
    lib.add_album_from_release("Composer", "Box", "mb", tracks)
    assert lib.find_track("Credits", "Composer")["title"] == "Closing Credits"
    assert lib.find_track("Boat Race", "Composer")["pos"] == 1          # exact beats contained
    assert lib.find_track("Race", "Composer") is None                        # too short to mean anything
    assert lib.find_track("Seal Race", "Composer") is None                   # fits two tracks: no guess


def test_covers_of_a_removed_album_are_kept_and_can_go_back(tmp_path):
    lib = Library(tmp_path)
    album = lib.add_album("Band", "Gone LP")
    song = lib.add_song(album, "Song", "Band")
    lib.set_art(album, b"\xff\xd8\xffcover", locked=True)
    lib.forget(song)                                   # the album had nothing else: it goes
    assert lib.albums() == []
    [left] = lib.leftovers()
    assert (left["artist"], left["album"], left["days_left"]) == ("Band", "Gone LP", 30)
    assert lib.leftover_bytes(left["id"]) == b"\xff\xd8\xffcover"

    other = lib.add_album("Band", "Kept LP")           # may get the removed album's id: it must not inherit its cover
    assert lib.art_bytes(other) is None
    assert lib.restore_leftover(left["id"], other) and lib.art_bytes(other) == b"\xff\xd8\xffcover"
    assert lib.leftovers() == []


def test_old_leftovers_are_deleted(tmp_path):
    lib = Library(tmp_path)
    album = lib.add_album("Band", "Gone LP")
    song = lib.add_song(album, "Song", "Band")
    lib.set_art(album, b"\xff\xd8\xffcover", locked=True)
    lib.forget(song)
    lib.db.execute("UPDATE leftovers SET removed_at = removed_at - 31 * 86400")
    assert lib.leftovers() == [] and list(lib.leftover_dir.iterdir()) == []


def test_a_full_library_stops_learning(tmp_path):
    lib = Library(tmp_path)
    sid, _ = lib.upsert("T", "A", "Al", "id1")
    lib.max_bytes = 1                                  # already over
    lib.learn(sid, [(1, 0), (2, 1)], 0)
    assert lib.full and lib.hash_count(sid) == 0
    lib.max_bytes = 0
    lib.learn(sid, [(1, 0), (2, 1)], 0)
    assert lib.hash_count(sid) == 2


def test_putting_a_leftover_on_an_album_sets_its_old_cover_aside(tmp_path):
    lib = Library(tmp_path)
    gone = lib.add_album("Band", "Gone LP")
    song = lib.add_song(gone, "Song", "Band")
    lib.set_art(gone, b"\xff\xd8\xffold", locked=True)
    lib.forget(song)
    kept = lib.add_album("Band", "Kept LP")
    lib.add_song(kept, "Other", "Band")
    lib.set_art(kept, b"\xff\xd8\xffkept", locked=True)
    [left] = lib.leftovers()
    assert lib.restore_leftover(left["id"], kept) and lib.art_bytes(kept) == b"\xff\xd8\xffold"
    [aside] = lib.leftovers()
    assert aside["album"] == "Kept LP" and lib.leftover_bytes(aside["id"]) == b"\xff\xd8\xffkept"


def test_lengths_for_sides_that_are_numbers(tmp_path):
    lib = Library(tmp_path)
    tracks = [{"n": 1, "side": "1", "pos": 1, "title": "One"}, {"n": 2, "side": "1", "pos": 2, "title": "Two"}]
    album = lib.add_album_from_release("Band", "CD", "mb", tracks)
    assert lib.set_track_lengths(album, {"1|1": 100, "1|2": 200, "2|1": 5}) == ["2|1"]
    assert [t["length"] for t in lib.album_tracks(album)] == [100, 200]


def test_nothing_is_learned_for_a_forgotten_song_and_space_comes_back(tmp_path):
    lib = Library(tmp_path)
    sid, _ = lib.upsert("T", "A", "Al", "id1")
    lib.forget(sid)
    lib.learn(sid, [(1, 0), (2, 1)], 0)                # a learn job that finished after the forget
    assert lib.db.execute("SELECT COUNT(*) FROM hashes").fetchone()[0] == 0
    lib.compact()                                      # must not fail, with or without anything to free


def test_an_album_you_just_added_is_not_swept_away(tmp_path):
    lib = Library(tmp_path)
    empty = lib.add_album("Band", "Just Added")
    lib.set_art(empty, b"\xff\xd8\xffmine", locked=True)
    sid, _ = lib.upsert("T", "A", "Al", "id1")
    lib.forget(sid)                                         # its own album goes, the empty one stays
    assert [a["album"] for a in lib.albums()] == ["Just Added"] and lib.leftovers() == []


def test_two_tracks_with_the_same_name_apart_from_brackets(tmp_path):
    lib = Library(tmp_path)
    tracks = [{"n": 1, "side": "A", "pos": 1, "title": "Battle (Day)"},
              {"n": 2, "side": "A", "pos": 2, "title": "Battle (Night)"}]
    album = lib.add_album_from_release("Band", "LP", "mb", tracks)
    assert lib.title_in_album(album, "battle (night)")["pos"] == 2
    assert lib.title_in_album(album, "Battle (Day)")["pos"] == 1
    assert lib.find_track("Battle (Night)", "Band")["pos"] == 2


def test_shazam_id_is_not_tied_to_a_track_by_someone_else(tmp_path):
    lib = Library(tmp_path)
    album = lib.add_album_from_release("The Band", "LP", "mb", [{"n": 1, "side": "A", "pos": 1, "title": "Home"}])
    own = lib.song_for_track(lib.album_tracks(album)[0]["id"])
    lib.tag_shazam(own, "other-home", "Someone Else")
    assert lib.shazam_song("other-home") is None
    lib.tag_shazam(own, "their-home", "Band")
    assert lib.shazam_song("their-home") == own


def test_measured_lengths_and_what_replaces_what(tmp_path):
    from vinyl_id.library import Q_GAP, Q_HEARD, Q_STOP

    lib = Library(tmp_path)
    tracks = [{"n": 1, "side": "A", "pos": 1, "title": "One", "length": 100},
              {"n": 2, "side": "A", "pos": 2, "title": "Two"}]
    album = lib.add_album_from_release("Band", "LP", "mb", tracks)
    one, two = (t["id"] for t in lib.album_tracks(album))
    assert [t["source"] for t in lib.album_tracks(album)] == ["listed", None]

    assert not lib.set_measured(one, 20, Q_GAP)              # a fifth of the listed length: a false gap
    assert not lib.set_measured(one, 170, Q_GAP)             # far longer: two songs taken for one
    assert lib.set_measured(one, 92.04, Q_HEARD)
    assert not lib.set_measured(one, 80, Q_HEARD)            # no surer than what is there
    assert not lib.set_measured(one, 80, Q_STOP)
    assert lib.set_measured(one, 91, Q_GAP)                  # surer: replaces it
    assert lib.set_measured(two, 40, Q_STOP) and lib.set_measured(two, 55, Q_STOP)   # of two stops, the longer
    assert not lib.set_measured(two, 50, Q_STOP) and not lib.set_measured(two, 2, Q_GAP)
    got = lib.album_tracks(album)
    assert [(t["length"], t["listed"], t["source"]) for t in got] == [(91, 100, "measured"), (55, None, "measured")]

    lib.set_track_lengths(album, {"A1": 95})                 # typed by you: wins, and stays
    assert not lib.set_measured(one, 90, Q_GAP) or lib.album_tracks(album)[0]["length"] == 95
    assert lib.album_tracks(album)[0]["source"] == "typed"
    lib.set_track_lengths(album, {"A1": None})               # taken away: measured afresh
    assert (lib.album_tracks(album)[0]["length"], lib.album_tracks(album)[0]["source"]) == (100, "listed")
    lib.set_track_lengths(album, {"A2": 60}, placeholder=True)
    assert (lib.album_tracks(album)[1]["length"], lib.album_tracks(album)[1]["listed"]) == (55, 60)

    lib.add_tracks(album, "mb", tracks)                      # the track list fetched again: measurements stay
    assert lib.album_tracks(album)[1]["length"] == 55
