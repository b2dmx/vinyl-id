from vinyl_id.config import BLOCK_SECONDS, Settings
from vinyl_id.detector import Detector, Event


def run(det, db, seconds):
    events = []
    for _ in range(int(seconds / BLOCK_SECONDS)):
        e = det.feed(db)
        if e:
            events.append(e)
    return events


def test_full_side():
    d = Detector(Settings())
    ev = []
    ev += run(d, -90, 5)        # needle up
    ev += run(d, -58, 3)        # needle drop: surface noise only, below threshold
    ev += run(d, -20, 180)      # song 1
    ev += run(d, -45, 3)        # lead-in groove between songs (above threshold, big dip)
    ev += run(d, -21, 200)      # song 2
    ev += run(d, -22, 0.5)      # brief quiet passage inside a song: not a gap
    ev += run(d, -40, 0.75)
    ev += run(d, -22, 60)
    ev += run(d, -90, 60)       # side ends, arm lifts
    assert ev == [Event.STARTED, Event.TRACK_CHANGE, Event.STOPPED]


def test_side_flip_within_stop_window_stays_playing():
    d = Detector(Settings())
    ev = run(d, -20, 60) + run(d, -90, 30) + run(d, -20, 60)
    assert ev == [Event.STARTED, Event.TRACK_CHANGE]  # flip counts as a new track, not a stop
