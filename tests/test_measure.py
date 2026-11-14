"""The track-length timer, by itself."""

from vinyl_id.library import Q_GAP, Q_HEARD, Q_STOP
from vinyl_id.measure import TrackTimer
from vinyl_id.spin import Spin

LP = [{"id": 1, "side": "A", "pos": 1, "title": "One", "length": 60},
      {"id": 2, "side": "A", "pos": 2, "title": "Two", "length": 60},
      {"id": 3, "side": "B", "pos": 1, "title": "Three", "length": 60}]


def spin(i=0):
    return Spin(1, [dict(t) for t in LP], i)


def test_gap_to_gap_then_confirmed():
    sp, t = spin(), TrackTimer()
    t.begin(1, 0.0)
    sp.i = 1
    assert t.gap(sp, 50.0, 3.0, late=False, moved=True, was=0) == []      # held until something confirms it
    assert t.settle(True) == [(1, 50.0, Q_GAP)]
    assert t.open.track_id == 2 and t.open.began == 50.0


def test_an_unconfirmed_gap_counts_for_less():
    sp, t = spin(), TrackTimer()
    t.begin(1, 0.0)
    sp.i = 1
    t.gap(sp, 50.0, 3.0, late=False, moved=True, was=0)
    assert t.stopped(sp, 200.0, 45.0, lost=False) == [(1, 50.0, Q_HEARD), (2, 105.0, Q_STOP)]


def test_songs_that_run_together_are_timed_by_recognition():
    sp, t = spin(), TrackTimer()
    t.begin(1, 0.0)
    assert t.moved_on() == []                         # the listed length ran out: no gap was heard
    sp.i = 1
    assert t.heard(sp, 57.0) == [(1, 57.0, Q_HEARD)]  # track 2 recognized, and when it began
    assert t.open.track_id == 2 and t.open.q == Q_HEARD


def test_a_jump_back_drops_the_gap_that_caused_it():
    sp, t = spin(), TrackTimer()
    t.begin(1, 0.0)
    sp.i = 1
    t.gap(sp, 20.0, 3.0, late=False, moved=True, was=0)      # a quiet passage taken for a gap
    t.jumped(sp, 1, 0)                                       # recognition: still track 1
    assert t.settle(True) == [] and t.open is None


def test_a_flip_is_timed_up_to_the_quiet_and_lost_sound_ends_nothing():
    sp, t = spin(1), TrackTimer()
    t.begin(2, 100.0)
    sp.i = 2
    t.gap(sp, 180.0, 20.0, late=False, moved=True, was=1)
    assert t.settle(True) == [(2, 60.0, Q_GAP)]
    assert t.stopped(sp, 300.0, 45.0, lost=True) == [] and t.open is None


def test_a_song_that_ends_in_several_silences_is_timed_to_the_last():
    sp, t = spin(), TrackTimer()
    t.begin(1, 0.0)
    sp.i = 1
    t.gap(sp, 44.0, 2.5, late=False, moved=True, was=0)       # the first silence of the ending
    assert t.gap(sp, 48.0, 2.5, late=True, moved=True, was=1) == []
    assert t.gap(sp, 60.0, 5.5, late=True, moved=True, was=1) == []      # the real gap
    assert t.settle(True) == [(1, 60.0, Q_GAP)] and t.open.began == 60.0
