"""Local audio fingerprints (spectrogram peak pairs, Shazam-style).

Each hash = (peak freq, paired peak freq, time gap). A sample matches a song
when many of its hashes line up at one consistent time offset.
"""

from __future__ import annotations

import numpy as np

from .config import SAMPLE_RATE

N_FFT = 1024
HOP = 256           # 16 ms per frame
FREQ_BINS = 512     # bins kept (0–8 kHz)
PEAK_T, PEAK_F = 10, 12   # neighbourhood half-size for a local maximum
FAN_OUT = 5
PEAKS_PER_SECOND = 10  # keep only the strongest peaks: smaller library, ignores surface noise
MAX_DT = 63
FRAMES_PER_SECOND = SAMPLE_RATE / HOP
_WINDOW = np.hanning(N_FFT).astype(np.float32)   # float32 throughout: half the memory on long clips


def _spectrogram(pcm: np.ndarray) -> np.ndarray:
    x = pcm.astype(np.float32) / 32768.0
    if len(x) < N_FFT:
        return np.zeros((0, FREQ_BINS), np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, N_FFT)[::HOP]
    spec = np.abs(np.fft.rfft(frames * _WINDOW, axis=1))[:, :FREQ_BINS]
    return 20 * np.log10(spec + 1e-6)


def _local_max(s: np.ndarray) -> np.ndarray:
    """Boolean mask of points that are the max of their neighbourhood."""
    m = s.copy()
    for axis, r in ((0, PEAK_T), (1, PEAK_F)):
        padded = np.pad(m, [(r, r) if a == axis else (0, 0) for a in range(2)], constant_values=-np.inf)
        win = np.lib.stride_tricks.sliding_window_view(padded, 2 * r + 1, axis=axis)
        m = win.max(axis=-1)
    return (s == m) & (s > np.median(s) + 10)


def hashes(pcm: np.ndarray) -> list[tuple[int, int]]:
    """Return [(hash, frame)] for a mono 16 kHz int16 clip."""
    s = _spectrogram(pcm)
    if not len(s):
        return []
    t_idx, f_idx = np.nonzero(_local_max(s))
    # strongest peaks per ~1 s window
    win = int(FRAMES_PER_SECOND)
    keep = []
    for w0 in range(0, s.shape[0], win):
        sel = np.nonzero((t_idx >= w0) & (t_idx < w0 + win))[0]
        if len(sel) > PEAKS_PER_SECOND:
            sel = sel[np.argsort(s[t_idx[sel], f_idx[sel]])[-PEAKS_PER_SECOND:]]
        keep.append(sel)
    sel = np.concatenate(keep) if keep else np.array([], int)
    t_idx, f_idx = t_idx[sel], f_idx[sel]
    order = np.argsort(t_idx, kind="stable")
    t_idx, f_idx = t_idx[order], f_idx[order]
    out: list[tuple[int, int]] = []
    n = len(t_idx)
    for i in range(n):
        t1, f1, taken = t_idx[i], f_idx[i], 0
        for j in range(i + 1, n):
            dt = t_idx[j] - t1
            if dt == 0:
                continue
            if dt > MAX_DT or taken >= FAN_OUT:
                break
            out.append(((int(f1) << 15) | (int(f_idx[j]) << 6) | int(dt), int(t1)))
            taken += 1
    return out
