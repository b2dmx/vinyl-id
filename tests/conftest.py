import pytest

import vinyl_id.recognize as rec_mod


@pytest.fixture(autouse=True)
def no_cover_lookups(monkeypatch):
    """Tests never ask iTunes for a cover."""
    async def none(self, track):
        return ""

    monkeypatch.setattr(rec_mod.Recognizer, "_itunes_art", none)


@pytest.fixture(autouse=True)
def no_track_list_lookups(monkeypatch):
    """Tests never ask MusicBrainz for a track list by themselves (it has none, as far as they know)."""
    async def none(self, artist, album, title):
        return False

    monkeypatch.setattr(rec_mod.Recognizer, "vinyl_release", none)
