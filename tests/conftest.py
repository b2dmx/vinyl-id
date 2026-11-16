import pytest

import vinyl_id.recognize as rec_mod


@pytest.fixture(autouse=True)
def no_cover_lookups(monkeypatch):
    """Tests never ask iTunes for a cover."""
    async def none(self, track):
        return ""

    monkeypatch.setattr(rec_mod.Recognizer, "_itunes_art", none)
