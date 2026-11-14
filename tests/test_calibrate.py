"""The quiet level finding itself."""

import math

from vinyl_id.calibrate import WINDOW, Calibrator


def feed(cal, level, seconds, playing=False, recognized=False, wobble=1.0):
    """`seconds` of sound around `level`, moving by `wobble` dB. Returns every level it proposed."""
    out = []
    for i in range(int(seconds * 4)):
        got = cal.feed(level + wobble * math.sin(i / 3), playing, recognized)
        if got is not None:
            out.append(got)
    return out


def test_the_first_steady_sound_is_taken_and_a_quieter_one_later_is_not():
    cal = Calibrator(-50, calibrated=False)
    assert feed(cal, -62, 20) == []                          # not long enough yet
    assert feed(cal, -62, 20)[0] == -62
    cal.threshold, cal.calibrated = -62, True
    assert feed(cal, -80, 120) == []                         # the turntable switched off: nothing changes


def test_a_louder_hum_raises_it():
    cal = Calibrator(-62, calibrated=True)
    assert feed(cal, -61, 60) == []                          # about the same
    assert feed(cal, -55, 60)[0] == -55


def test_music_and_no_input_are_never_taken_for_hum():
    cal = Calibrator(-50, calibrated=False)
    assert feed(cal, -45, 120, wobble=8) == []               # it moves: music
    assert feed(cal, -20, 120) == []                         # steady, but far too loud for hum
    assert feed(cal, -90, 120) == []                         # nothing arriving at all


def test_a_record_that_was_flat_from_its_first_second_is_hum():
    cal = Calibrator(-80, calibrated=True)                   # set while the turntable was off
    assert feed(cal, -62, 40, playing=True)[0] == -62        # "started", never moved, nothing recognized


def test_a_real_record_is_left_alone():
    cal = Calibrator(-62, calibrated=True)
    assert feed(cal, -45, 60, playing=True, wobble=6) == []                 # it moves
    assert feed(cal, -45, 120, playing=True, recognized=True) == []         # steady, but a song was recognized
    cal = Calibrator(-62, calibrated=True)
    feed(cal, -30, 20, playing=True, wobble=8)                              # a loud opening ...
    assert feed(cal, -48, 120, playing=True) == []                          # ... then a long quiet drone: still a record


def test_window_is_thirty_seconds():
    assert WINDOW == 120
