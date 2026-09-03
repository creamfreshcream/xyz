"""Analysis performed on the decoded audio itself.

Used to fill the gaps AudioMuse leaves (or to run the station without AudioMuse
at all): where the music actually starts and ends, how loud it is, and a tempo
estimate from the outgoing track's tail.

Everything here works on float32 PCM shaped ``(frames, channels)`` in -1..1.
"""

from __future__ import annotations

import numpy as np

EPS = 1e-9


def to_mono(pcm: np.ndarray) -> np.ndarray:
    return pcm if pcm.ndim == 1 else pcm.mean(axis=1)


def rms_db(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return -120.0
    value = float(np.sqrt(np.mean(np.square(to_mono(pcm), dtype=np.float64)) + EPS))
    return 20.0 * float(np.log10(max(value, EPS)))


def peak(pcm: np.ndarray) -> float:
    return float(np.max(np.abs(pcm))) if pcm.size else 0.0


def _envelope(mono: np.ndarray, sample_rate: int, hop_ms: float = 20.0) -> tuple[np.ndarray, int]:
    """Short-term RMS envelope in dB plus the hop size in frames."""
    hop = max(1, int(sample_rate * hop_ms / 1000.0))
    usable = (mono.size // hop) * hop
    if usable == 0:
        return np.array([-120.0], dtype=np.float32), hop
    blocks = mono[:usable].reshape(-1, hop)
    rms = np.sqrt(np.mean(np.square(blocks, dtype=np.float64), axis=1) + EPS)
    return (20.0 * np.log10(np.maximum(rms, EPS))).astype(np.float32), hop


def leading_silence(pcm: np.ndarray, sample_rate: int, threshold_db: float = -45.0) -> float:
    """Seconds of silence before the music starts."""
    envelope, hop = _envelope(to_mono(pcm), sample_rate)
    loud = np.flatnonzero(envelope > threshold_db)
    if loud.size == 0:
        return float(pcm.shape[0]) / sample_rate
    return float(loud[0] * hop) / sample_rate


def trailing_silence(pcm: np.ndarray, sample_rate: int, threshold_db: float = -45.0) -> float:
    """Seconds of silence after the music ends."""
    envelope, hop = _envelope(to_mono(pcm), sample_rate)
    loud = np.flatnonzero(envelope > threshold_db)
    if loud.size == 0:
        return float(pcm.shape[0]) / sample_rate
    tail_blocks = envelope.size - 1 - int(loud[-1])
    return float(tail_blocks * hop) / sample_rate


def outro_fade_length(pcm: np.ndarray, sample_rate: int, drop_db: float = 12.0) -> float:
    """How long the track has been fading out at its end.

    A track that already fades itself needs a shorter overlap - otherwise the
    blend happens over near-silence and sounds like a gap.
    """
    envelope, hop = _envelope(to_mono(pcm), sample_rate, hop_ms=50.0)
    if envelope.size < 4:
        return 0.0
    reference = float(np.percentile(envelope, 90))
    last = float(envelope[-1])
    if reference - last < drop_db:
        return 0.0
    # Walk back to where the level was still within 3 dB of the reference.
    threshold = reference - 3.0
    index = envelope.size - 1
    while index > 0 and envelope[index] < threshold:
        index -= 1
    return float((envelope.size - 1 - index) * hop) / sample_rate


def estimate_bpm(
    pcm: np.ndarray, sample_rate: int, bpm_min: float = 60.0, bpm_max: float = 190.0
) -> float | None:
    """Tempo estimate via autocorrelation of the onset envelope.

    A fallback for tracks AudioMuse has not analysed. Accurate enough to decide
    whether two tracks share a tempo and to snap an overlap to whole beats.
    """
    mono = to_mono(pcm)
    if mono.size < sample_rate * 4:
        return None

    envelope, hop = _envelope(mono, sample_rate, hop_ms=10.0)
    if envelope.size < 64:
        return None
    # Spectral-flux-ish: positive differences of the level envelope.
    onset = np.diff(envelope)
    onset = np.maximum(onset, 0.0)
    onset -= onset.mean()
    if not np.any(onset):
        return None

    envelope_rate = sample_rate / hop  # envelope samples per second
    min_lag = int(envelope_rate * 60.0 / bpm_max)
    max_lag = int(envelope_rate * 60.0 / bpm_min)
    if max_lag >= onset.size or min_lag < 1:
        return None

    correlation = np.correlate(onset, onset, mode="full")[onset.size - 1 :]
    window = correlation[min_lag : max_lag + 1]
    if window.size == 0 or not np.any(window > 0):
        return None

    lag = int(np.argmax(window)) + min_lag
    # Parabolic interpolation around the peak for sub-bin resolution.
    if 0 < lag < correlation.size - 1:
        left, centre, right = correlation[lag - 1], correlation[lag], correlation[lag + 1]
        denominator = left - 2 * centre + right
        if abs(denominator) > EPS:
            lag += float(0.5 * (left - right) / denominator)

    bpm = 60.0 * envelope_rate / max(lag, EPS)
    while bpm < bpm_min:
        bpm *= 2
    while bpm > bpm_max:
        bpm /= 2
    return round(float(bpm), 1)


def estimate_lufs(pcm: np.ndarray) -> float:
    """Rough loudness estimate.

    Not a certified EBU R128 measurement - it is an RMS reading offset to land
    close to an integrated LUFS value, which is all the gain matching needs.
    """
    return rms_db(pcm) - 3.0


def apply_gain(pcm: np.ndarray, gain_db: float) -> np.ndarray:
    if abs(gain_db) < 0.05:
        return pcm
    return pcm * float(10.0 ** (gain_db / 20.0))


def soft_limit(pcm: np.ndarray, ceiling: float = 0.98) -> np.ndarray:
    """Tanh-style knee that only engages above the ceiling."""
    over = np.abs(pcm) > ceiling
    if not np.any(over):
        return pcm
    out = pcm.copy()
    excess = np.abs(out[over]) - ceiling
    out[over] = np.sign(out[over]) * (ceiling + (1.0 - ceiling) * np.tanh(excess / (1.0 - ceiling)))
    return out
