"""The crossfade planner: does it make the right call?"""

from __future__ import annotations

import numpy as np
import pytest

from app.audio.analysis import estimate_bpm, leading_silence, trailing_silence
from app.audio.crossfade import gain_curves, plan_crossfade, render_crossfade
from app.audiomuse import camelot_distance, to_camelot
from app.models import CrossfadeSpec, TrackAnalysis

SR = 44100


def analysis(**kwargs) -> TrackAnalysis:
    return TrackAnalysis(**{"source": "audiomuse", **kwargs})


def test_matched_tempo_and_key_blends_longer_than_a_clash():
    spec = CrossfadeSpec(default_seconds=6, min_seconds=1.5, max_seconds=14)
    matched = plan_crossfade(
        analysis(bpm=124, camelot="8A"), analysis(bpm=124.4, camelot="9A"), spec, 300, 300
    )
    clashing = plan_crossfade(
        analysis(bpm=124, camelot="8A"), analysis(bpm=176, camelot="3B"), spec, 300, 300
    )
    assert matched.overlap_seconds > clashing.overlap_seconds
    assert matched.key_matched and not clashing.key_matched


def test_beat_alignment_snaps_to_whole_beats():
    spec = CrossfadeSpec(default_seconds=8, min_seconds=2, max_seconds=20, beat_align=True,
                         beat_lengths=[8, 16, 32])
    plan = plan_crossfade(analysis(bpm=120), analysis(bpm=120), spec, 300, 300)
    assert plan.beat_matched
    # 120 BPM -> 0.5 s per beat, so the overlap must be a whole number of beats.
    beats = plan.overlap_seconds / 0.5
    assert beats == pytest.approx(round(beats))
    assert round(beats) in (8, 16, 32)


def test_energy_jump_becomes_a_short_slam():
    spec = CrossfadeSpec(default_seconds=8, min_seconds=1, max_seconds=16, energy_jump_cut=0.3)
    slam = plan_crossfade(analysis(energy=0.3), analysis(energy=0.9), spec, 300, 300)
    drop = plan_crossfade(analysis(energy=0.9), analysis(energy=0.3), spec, 300, 300)
    assert slam.overlap_seconds < 8 < drop.overlap_seconds
    assert slam.curve == "exponential"


def test_overlap_never_exceeds_a_third_of_the_shorter_track():
    spec = CrossfadeSpec(default_seconds=14, min_seconds=1, max_seconds=20)
    plan = plan_crossfade(analysis(bpm=120), analysis(bpm=120), spec, 300, 12)
    assert plan.overlap_seconds <= 12 / 3 + 1e-6


def test_cut_mode_leaves_only_a_declick_fade():
    plan = plan_crossfade(analysis(), analysis(), CrossfadeSpec(mode="cut", gap_seconds=1.0), 300, 300)
    assert plan.overlap_seconds < 0.3
    assert plan.gap_seconds == 1.0


def test_missing_analysis_falls_back_to_the_default():
    spec = CrossfadeSpec(default_seconds=6, min_seconds=2, max_seconds=12)
    plan = plan_crossfade(TrackAnalysis(), TrackAnalysis(), spec, 300, 300)
    assert plan.overlap_seconds == pytest.approx(6.0)
    assert "tempo unknown" in plan.reason


def test_loudness_matching_is_capped():
    spec = CrossfadeSpec(match_loudness=True, target_lufs=-14, max_gain_db=6)
    plan = plan_crossfade(analysis(lufs=-30), analysis(lufs=-5), spec, 300, 300)
    assert plan.a_gain_db == 6.0
    assert plan.b_gain_db == -6.0


@pytest.mark.parametrize("curve", ["equal_power", "linear", "s_curve", "exponential", "logarithmic"])
def test_every_curve_starts_and_ends_fully_swapped(curve):
    out, incoming = gain_curves(curve, 512)
    assert out[0] == pytest.approx(1.0, abs=1e-3)
    assert incoming[0] == pytest.approx(0.0, abs=1e-3)
    assert out[-1] == pytest.approx(0.0, abs=1e-3)
    assert incoming[-1] == pytest.approx(1.0, abs=1e-3)


def test_equal_power_holds_loudness_through_the_blend():
    out, incoming = gain_curves("equal_power", 512)
    power = out**2 + incoming**2
    assert np.allclose(power, 1.0, atol=1e-3)


def test_rendered_blend_swaps_the_tracks_without_clipping():
    frames = SR * 4
    t = np.linspace(0, 4, frames, dtype=np.float32)
    tail = np.stack([0.5 * np.sin(2 * np.pi * 200 * t)] * 2, axis=1)
    head = np.stack([0.5 * np.sin(2 * np.pi * 900 * t)] * 2, axis=1)

    spec = CrossfadeSpec(default_seconds=4, min_seconds=4, max_seconds=4, bass_swap=True)
    plan = plan_crossfade(analysis(bpm=120), analysis(bpm=120), spec, 300, 300)
    mixed = render_crossfade(tail, head, plan, SR)

    assert mixed.shape == (frames, 2)
    assert np.max(np.abs(mixed)) <= 1.0
    # The outgoing tone dominates at the start, the incoming one at the end.
    def energy_at(pcm, hz):
        spectrum = np.abs(np.fft.rfft(pcm[:, 0] * np.hanning(pcm.shape[0])))
        freqs = np.fft.rfftfreq(pcm.shape[0], 1 / SR)
        return float(spectrum[(freqs > hz - 20) & (freqs < hz + 20)].max())

    start, end = mixed[: SR // 2], mixed[-SR // 2 :]
    assert energy_at(start, 200) > energy_at(start, 900)
    assert energy_at(end, 900) > energy_at(end, 200)


def test_camelot_conversion_and_distance():
    assert to_camelot("A", "minor") == "8A"
    assert to_camelot("F# minor") == "11A"
    assert to_camelot("Am") == "8A"
    assert to_camelot("8A") == "8A"
    assert to_camelot("") is None
    assert camelot_distance("8A", "8A") == 0
    assert camelot_distance("8A", "9A") == 1   # neighbour on the wheel
    assert camelot_distance("8A", "8B") == 1   # relative major
    assert camelot_distance("8A", "2A") > 1    # clash
    assert camelot_distance("8A", None) is None


def test_edge_detection_finds_silence_and_tempo():
    silence = np.zeros((SR * 2, 2), dtype=np.float32)
    t = np.linspace(0, 3, SR * 3, dtype=np.float32)
    tone = np.stack([0.4 * np.sin(2 * np.pi * 440 * t)] * 2, axis=1)
    padded = np.concatenate([silence, tone, silence])
    assert leading_silence(padded, SR) == pytest.approx(2.0, abs=0.1)
    assert trailing_silence(padded, SR) == pytest.approx(2.0, abs=0.1)

    clicks = np.zeros(SR * 8, dtype=np.float32)
    for beat in range(16):  # 120 BPM
        start = int(beat * 0.5 * SR)
        clicks[start : start + 800] = np.hanning(800)
    assert estimate_bpm(np.stack([clicks] * 2, axis=1), SR) == pytest.approx(120, abs=2)
