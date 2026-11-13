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


def test_soft_music_with_short_quiet_spells_is_one_song():
    """Ambient piano: notes at -37 dB with 1.5 s near-silences in between."""
    d = Detector(Settings(signal_threshold_db=-65))
    ev = run(d, -37, 30)
    for _ in range(20):
        ev += run(d, -62, 1.5) + run(d, -37, 3)
    assert ev == [Event.STARTED]



def test_jingle_between_gaps_is_its_own_track():
    """A 9-second jingle between two songs still gets its own track change."""
    d = Detector(Settings())
    ev = run(d, -20, 60) + run(d, -60, 3) + run(d, -20, 9) + run(d, -60, 3) + run(d, -20, 30)
    assert ev == [Event.STARTED, Event.TRACK_CHANGE, Event.TRACK_CHANGE]


def test_hum_at_the_threshold_is_not_a_record():
    """An idle turntable hums right around the threshold: that must never start a record."""
    d = Detector(Settings(signal_threshold_db=-65))
    ev = []
    for _ in range(120):                     # 5 minutes of hum wandering between -72 and -61
        ev += run(d, -63, 1) + run(d, -70, 1) + run(d, -61, 0.5)
    assert ev == []


def test_record_stops_when_only_hum_is_left():
    d = Detector(Settings(signal_threshold_db=-65))
    ev = run(d, -35, 60)
    for _ in range(60):                      # the side has ended; the hum hovers at the threshold
        ev += run(d, -63, 1) + run(d, -70, 1)
    assert ev == [Event.STARTED, Event.STOPPED]


def test_lost_audio_is_not_a_gap_but_groove_noise_is():
    """Over a network receiver, lost audio arrives as exact silence; a real gap is the groove's own noise."""
    d = Detector(Settings(signal_threshold_db=-65, audio_input_format="pulse"))
    ev = run(d, -35, 30) + run(d, -90, 6) + run(d, -35, 30) + run(d, -66, 3) + run(d, -35, 10)
    assert ev == [Event.STARTED, Event.TRACK_CHANGE]


def test_soft_passage_is_not_a_gap():
    d = Detector(Settings(signal_threshold_db=-65))
    ev = run(d, -35, 30) + run(d, -50, 8) + run(d, -35, 30) + run(d, -56, 60) + run(d, -35, 5)
    assert ev == [Event.STARTED]          # quiet music keeps the record going and changes nothing


def test_a_click_in_the_gap_does_not_hide_the_gap():
    d = Detector(Settings(signal_threshold_db=-65))
    ev = run(d, -35, 30) + run(d, -68, 1.5) + run(d, -40, 0.25) + run(d, -68, 1.5) + run(d, -35, 30)
    assert ev == [Event.STARTED, Event.TRACK_CHANGE]


def test_pops_in_the_run_out_groove_do_not_keep_the_record_going():
    d = Detector(Settings(signal_threshold_db=-65))
    ev = run(d, -35, 30)
    for _ in range(40):                      # the needle sits in the run-out groove: a pop every two seconds
        ev += run(d, -68, 1.75) + run(d, -45, 0.25)
    assert ev == [Event.STARTED, Event.STOPPED]
