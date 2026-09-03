"""Rotation rules and flow."""

from __future__ import annotations

import random

import pytest

from app.models import RotationSpec, Track, TrackAnalysis
from app.presets import quick_library
from app.scheduler import Scheduler


def make_pool(count: int, artists: int = 8, albums: int = 6) -> list[Track]:
    return [
        Track(
            id=f"id{i}",
            name=f"Track {i}",
            artist=f"Artist {i % artists}",
            album_id=f"album{i % albums}",
            duration_seconds=200,
            analysis=TrackAnalysis(
                bpm=110 + (i % 5) * 5,
                camelot=f"{(i % 12) + 1}A",
                energy=(i % 10) / 10,
                source="audiomuse",
            ),
        )
        for i in range(count)
    ]


def station(**rotation):
    spec = quick_library("Test", station_id="test")
    spec.rotation = RotationSpec(**{"no_repeat_minutes": 0, **rotation})
    return spec


@pytest.fixture(autouse=True)
def _seed():
    random.seed(42)


def test_no_repeat_window_is_respected():
    scheduler = Scheduler(station(flow="smart", no_repeat_tracks=20, no_repeat_artist_tracks=3))
    played = [scheduler.next_track(make_pool(60)).track.id for _ in range(40)]
    for index, track_id in enumerate(played):
        assert track_id not in played[max(0, index - 20) : index]


def test_artist_spacing_is_respected_across_the_lookahead_queue():
    scheduler = Scheduler(station(flow="smart", no_repeat_artist_tracks=4, queue_depth=6))
    artists = [scheduler.next_track(make_pool(60)).track.artist for _ in range(30)]
    for index, artist in enumerate(artists):
        assert artist not in artists[max(0, index - 4) : index]


def test_hourly_artist_cap_is_enforced():
    scheduler = Scheduler(
        station(flow="random", max_per_artist_per_hour=2, no_repeat_tracks=0, no_repeat_artist_tracks=0)
    )
    pool = make_pool(60, artists=10)
    played = [scheduler.next_track(pool).track.artist for _ in range(20)]
    for artist in set(played):
        assert played.count(artist) <= 2


def test_a_tiny_pool_still_alternates_instead_of_looping_one_track():
    scheduler = Scheduler(station(flow="random", no_repeat_tracks=50))
    pool = make_pool(2)
    played = [scheduler.next_track(pool).track.id for _ in range(6)]
    # Rules cannot be met, but consecutive repeats must still be avoided.
    assert all(played[i] != played[i + 1] for i in range(len(played) - 1))


def test_a_single_track_pool_does_not_deadlock():
    scheduler = Scheduler(station(flow="random"))
    pool = make_pool(1)
    assert [scheduler.next_track(pool).track.id for _ in range(3)] == ["id0"] * 3


def test_empty_pool_returns_nothing():
    assert Scheduler(station()).next_track([]) is None


def test_smart_flow_prefers_musically_close_tracks():
    """With the rotation rules out of the way, flow should follow the music."""
    spec = station(
        flow="smart",
        temperature=0.02,
        no_repeat_tracks=0,
        no_repeat_artist_tracks=0,
        max_per_artist_per_hour=60,
        max_per_album_per_hour=60,
    )
    scheduler = Scheduler(spec)
    anchor = Track(
        id="anchor", name="Anchor", artist="Anchor", duration_seconds=200,
        analysis=TrackAnalysis(bpm=124, camelot="8A", energy=0.5, source="audiomuse"),
    )
    # Ten tracks that sit right next to the anchor, ten that clash with it.
    near = [
        Track(
            id=f"near{i}", name=f"Near {i}", artist=f"Near {i}", duration_seconds=200,
            analysis=TrackAnalysis(bpm=124, camelot="8A", energy=0.5, source="audiomuse"),
        )
        for i in range(10)
    ]
    far = [
        Track(
            id=f"far{i}", name=f"Far {i}", artist=f"Far {i}", duration_seconds=200,
            analysis=TrackAnalysis(bpm=180, camelot="2B", energy=0.98, source="audiomuse"),
        )
        for i in range(10)
    ]
    scheduler.note_played(anchor)
    picks = [scheduler.next_track(near + far).track.id for _ in range(10)]
    assert sum(1 for pick in picks if pick.startswith("near")) >= 8


def test_energy_curve_flow_follows_the_daypart_target():
    from datetime import time as clock

    from app.models import Daypart

    spec = station(flow="energy_curve", temperature=0.02, no_repeat_tracks=0, no_repeat_artist_tracks=0)
    spec.dayparts = [Daypart(name="all day", start=clock(0, 0), end=clock(23, 59), energy_target=0.9)]
    scheduler = Scheduler(spec)
    pool = make_pool(20)
    energies = [scheduler.next_track(pool).track.analysis.energy for _ in range(8)]
    assert sum(energies) / len(energies) > 0.6


def test_queue_lookahead_matches_what_is_played():
    scheduler = Scheduler(station(flow="smart", queue_depth=4))
    pool = make_pool(40)
    scheduler.fill_queue(pool)
    upcoming = [track.id for track in scheduler.upcoming()]
    assert scheduler.next_track(pool).track.id == upcoming[0]
