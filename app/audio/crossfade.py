"""Smart crossfade: decide the transition, then render it.

The plan is built from four inputs, in this order of influence:

    tempo   - matching tempos get long, beat-aligned blends; clashing tempos
              get short ones so the collision is over quickly
    energy  - a big jump up becomes a short slam, a drop becomes a long letdown
    key     - harmonically compatible keys may overlap longer; clashing keys
              are cut short and get a bass swap
    audio   - measured silence and self-fades at the track edges, so the fade
              always sits on music instead of on nothing

Rendering applies the gain curves, an optional rolling high-pass on the
outgoing track (bass swap) and loudness matching.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from app.audiomuse import camelot_distance
from app.models import CrossfadeSpec, TrackAnalysis


@dataclass
class CrossfadePlan:
    """The decision. All times in seconds."""

    overlap_seconds: float
    curve: str = "equal_power"
    #: Silence trimmed from the end of the outgoing / start of the incoming track.
    a_trim_end: float = 0.0
    b_trim_start: float = 0.0
    gap_seconds: float = 0.0
    a_gain_db: float = 0.0
    b_gain_db: float = 0.0
    bass_swap: bool = False
    bass_swap_hz: int = 180
    beat_matched: bool = False
    key_matched: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _tempo_relation(bpm_a: float | None, bpm_b: float | None) -> float | None:
    """Tempo difference in octaves, folded over the 2:1 harmonic (0 = same)."""
    if not bpm_a or not bpm_b:
        return None
    ratio = bpm_b / bpm_a
    while ratio < 0.75:
        ratio *= 2
    while ratio > 1.5:
        ratio /= 2
    return abs(math.log2(ratio))


def _loudness_gain(analysis: TrackAnalysis, spec: CrossfadeSpec) -> float:
    if not spec.match_loudness or analysis.lufs is None:
        return 0.0
    gain = spec.target_lufs - analysis.lufs
    return max(-spec.max_gain_db, min(spec.max_gain_db, gain))


def plan_crossfade(
    a: TrackAnalysis,
    b: TrackAnalysis,
    spec: CrossfadeSpec,
    a_duration: float,
    b_duration: float,
) -> CrossfadePlan:
    """Decide how track A becomes track B."""
    plan = CrossfadePlan(overlap_seconds=0.0, curve=spec.curve, bass_swap_hz=spec.bass_swap_hz)
    plan.a_gain_db = _loudness_gain(a, spec)
    plan.b_gain_db = _loudness_gain(b, spec)

    if spec.trim_silence:
        plan.a_trim_end = min(a.outro_silence, max(0.0, a_duration * 0.25))
        plan.b_trim_start = min(b.intro_silence, max(0.0, b_duration * 0.25))

    if not spec.enabled or spec.mode == "cut":
        # Still fade over ~120 ms so the splice does not click.
        plan.overlap_seconds = 0.0 if not spec.enabled else 0.12
        plan.gap_seconds = spec.gap_seconds
        plan.curve = "linear"
        plan.reasons.append("hard cut")
        return plan

    if spec.mode == "fixed":
        plan.overlap_seconds = spec.default_seconds
        plan.gap_seconds = spec.gap_seconds
        plan.reasons.append(f"fixed {spec.default_seconds:.1f}s")
        return plan

    # ---- smart -----------------------------------------------------------
    overlap = spec.default_seconds

    tempo_delta = _tempo_relation(a.bpm, b.bpm)
    tempo_matched = tempo_delta is not None and tempo_delta <= spec.tempo_tolerance
    if tempo_delta is None:
        plan.reasons.append("tempo unknown")
    elif tempo_matched:
        overlap *= 1.3
        plan.reasons.append(f"tempo matched ({a.bpm:.0f}->{b.bpm:.0f})")
    elif tempo_delta <= 0.12:
        plan.reasons.append(f"tempo close ({a.bpm:.0f}->{b.bpm:.0f})")
    else:
        overlap *= 0.55
        plan.reasons.append(f"tempo clash ({a.bpm:.0f}->{b.bpm:.0f}), shortened")

    if a.energy is not None and b.energy is not None:
        delta = b.energy - a.energy
        if delta > spec.energy_jump_cut:
            overlap *= 0.45
            plan.curve = "exponential"
            plan.reasons.append(f"energy jump +{delta:.2f}, slam")
        elif delta < -spec.energy_jump_cut:
            overlap *= 1.35
            plan.curve = "s_curve"
            plan.reasons.append(f"energy drop {delta:.2f}, long blend")

    distance = camelot_distance(a.camelot, b.camelot)
    if distance is None:
        plan.reasons.append("key unknown")
    elif distance == 0:
        overlap *= 1.15
        plan.key_matched = True
        plan.reasons.append(f"same key ({a.camelot})")
    elif distance == 1:
        overlap *= 1.05
        plan.key_matched = True
        plan.reasons.append(f"harmonic ({a.camelot}->{b.camelot})")
    else:
        overlap *= spec.key_clash_factor
        plan.reasons.append(f"key clash ({a.camelot}->{b.camelot}), shortened")

    # A track that already fades itself should not be overlapped past its fade.
    if a.outro_fade:
        limited = max(spec.min_seconds, min(overlap, a.outro_fade))
        if limited < overlap:
            plan.reasons.append(f"outgoing self-fade {a.outro_fade:.1f}s")
        overlap = limited

    # Never eat more than a third of the shorter track.
    usable_a = max(0.0, a_duration - plan.a_trim_end)
    usable_b = max(0.0, b_duration - plan.b_trim_start)
    ceiling = max(0.5, min(usable_a, usable_b) / 3.0)
    overlap = min(overlap, ceiling)
    overlap = max(spec.min_seconds, min(overlap, spec.max_seconds))

    if spec.beat_align and tempo_matched and a.bpm and b.bpm:
        beat_seconds = 60.0 / ((a.bpm + b.bpm) / 2.0)
        candidates = [
            (beats, beats * beat_seconds)
            for beats in sorted(spec.beat_lengths)
            if spec.min_seconds <= beats * beat_seconds <= min(spec.max_seconds, ceiling)
        ]
        if candidates:
            beats, length = min(candidates, key=lambda item: abs(item[1] - overlap))
            overlap = length
            plan.beat_matched = True
            plan.reasons.append(f"{beats} beats")

    plan.overlap_seconds = round(overlap, 3)
    plan.gap_seconds = spec.gap_seconds
    plan.bass_swap = bool(
        spec.bass_swap and plan.overlap_seconds >= 3.0 and (distance is None or distance > 0)
    )
    if plan.bass_swap:
        plan.reasons.append(f"bass swap @{spec.bass_swap_hz} Hz")
    return plan


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def gain_curves(curve: str, length: int) -> tuple[np.ndarray, np.ndarray]:
    """Gain arrays (outgoing, incoming) of ``length`` samples."""
    if length <= 0:
        empty = np.zeros(0, dtype=np.float32)
        return empty, empty
    t = np.linspace(0.0, 1.0, length, dtype=np.float32)

    if curve == "linear":
        out, incoming = 1.0 - t, t
    elif curve == "s_curve":
        smooth = t * t * (3.0 - 2.0 * t)
        out, incoming = 1.0 - smooth, smooth
    elif curve == "exponential":
        # Incoming arrives fast, outgoing drops away quickly: the slam.
        out, incoming = (1.0 - t) ** 2, np.sqrt(t)
    elif curve == "logarithmic":
        out, incoming = np.sqrt(1.0 - t), t**2
    else:  # equal_power - constant perceived loudness through the blend
        out, incoming = np.cos(t * np.pi / 2.0), np.sin(t * np.pi / 2.0)

    return out.astype(np.float32), incoming.astype(np.float32)


def rolling_highpass(pcm: np.ndarray, sample_rate: int, target_hz: int) -> np.ndarray:
    """High-pass whose cutoff rises from ~20 Hz to ``target_hz`` across the block.

    Implemented as an overlap-add STFT filter: cheap, phase-linear and, unlike a
    per-sample IIR, fast enough in numpy to run inside the playout loop.
    """
    frames = pcm.shape[0]
    if frames < 1024 or target_hz <= 20:
        return pcm

    window_size = 2048
    hop = window_size // 2
    window = np.hanning(window_size).astype(np.float32)
    freqs = np.fft.rfftfreq(window_size, 1.0 / sample_rate)

    padded = np.pad(pcm, ((0, window_size), (0, 0)), mode="constant")
    output = np.zeros_like(padded)
    normalisation = np.zeros(padded.shape[0], dtype=np.float32)

    for start in range(0, frames, hop):
        block = padded[start : start + window_size]
        if block.shape[0] < window_size:
            break
        progress = min(1.0, start / max(1, frames))
        # Sweep the cutoff geometrically - that is how it is heard.
        cutoff = 20.0 * (target_hz / 20.0) ** progress
        # One-pole-ish magnitude response, smooth enough to avoid ringing.
        response = (freqs / cutoff) / np.sqrt(1.0 + (freqs / cutoff) ** 2)
        response = response.astype(np.float32)

        windowed = block * window[:, None]
        spectrum = np.fft.rfft(windowed, axis=0) * response[:, None]
        output[start : start + window_size] += np.fft.irfft(spectrum, n=window_size, axis=0).astype(
            np.float32
        )
        normalisation[start : start + window_size] += window * window

    normalisation = np.maximum(normalisation, 1e-6)
    return (output / normalisation[:, None])[:frames].astype(np.float32)


def render_crossfade(
    tail: np.ndarray, head: np.ndarray, plan: CrossfadePlan, sample_rate: int
) -> np.ndarray:
    """Mix the outgoing tail and the incoming head into the overlap block.

    ``tail`` and ``head`` must both be exactly the overlap length.
    """
    from app.audio.analysis import apply_gain, soft_limit

    length = min(tail.shape[0], head.shape[0])
    if length == 0:
        return np.zeros((0, tail.shape[1] if tail.ndim > 1 else 2), dtype=np.float32)

    tail = apply_gain(tail[:length].astype(np.float32), plan.a_gain_db)
    head = apply_gain(head[:length].astype(np.float32), plan.b_gain_db)

    if plan.bass_swap:
        tail = rolling_highpass(tail, sample_rate, plan.bass_swap_hz)

    out_gain, in_gain = gain_curves(plan.curve, length)
    mixed = tail * out_gain[:, None] + head * in_gain[:, None]
    return soft_limit(mixed)
