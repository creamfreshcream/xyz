"""Station structs, presets, filters and YAML persistence."""

from __future__ import annotations

from datetime import time as clock

import pytest
import yaml
from pydantic import ValidationError

from app.library import apply_filters
from app.models import Daypart, StationSpec, StationUpdate, Track, TrackAnalysis, TrackFilters
from app.presets import TEMPLATES, quick_artist, quick_genre, quick_library, quick_mood
from app.store import StationExists, StationNotFound, StationStore


def test_quick_builders_produce_valid_stations():
    genre = quick_genre("Deep House Nights", ["Deep House", "House"], template="club")
    assert genre.id == "deep-house-nights"
    assert genre.sources[0].kind == "genre"
    assert genre.crossfade.beat_align and genre.crossfade.bass_swap

    mood = quick_mood("Sunday Morning", ["calm", "warm"])
    assert mood.sources[0].moods == ["calm", "warm"]
    assert mood.filters.energy_max == 0.55  # the 'chill' template's ceiling

    artist = quick_artist("Bowie Radio", ["David Bowie"], include_similar=True)
    assert artist.sources[0].include_similar

    everything = quick_library("Shuffle")
    assert everything.sources[0].kind == "library"


def test_templates_are_internally_consistent():
    for template in TEMPLATES.values():
        crossfade = template.crossfade
        assert crossfade.min_seconds <= crossfade.default_seconds <= crossfade.max_seconds


def test_station_ids_are_validated():
    with pytest.raises(ValidationError):
        StationSpec(id="Not A Slug!", name="x", sources=[{"kind": "library"}])
    assert StationSpec(id="Valid-ID_1", name="x", sources=[{"kind": "library"}]).id == "valid-id_1"


def test_a_station_needs_at_least_one_source():
    with pytest.raises(ValidationError):
        StationSpec(id="empty", name="x", sources=[])


def test_unknown_fields_are_rejected_so_typos_do_not_pass_silently():
    with pytest.raises(ValidationError):
        StationSpec(id="typo", name="x", sources=[{"kind": "library"}], crossfad3={"mode": "smart"})


def test_dayparts_including_one_that_wraps_midnight():
    night = Daypart(name="night", start=clock(22, 0), end=clock(6, 0), energy_target=0.2)
    day = Daypart(name="day", start=clock(6, 0), end=clock(22, 0), energy_target=0.7)
    assert night.contains(clock(23, 30)) and night.contains(clock(3, 0))
    assert not night.contains(clock(12, 0))
    assert day.contains(clock(12, 0))

    spec = quick_library("Test", station_id="test", dayparts=[night, day])
    assert spec.active_daypart(clock(2, 0)).name == "night"
    assert spec.effective_crossfade(clock(2, 0)).default_seconds == spec.crossfade.default_seconds


def test_daypart_can_override_the_crossfade_length():
    spec = quick_library(
        "Test",
        station_id="test",
        dayparts=[Daypart(name="night", start=clock(0, 0), end=clock(23, 59), crossfade_seconds=15)],
    )
    assert spec.effective_crossfade(clock(3, 0)).default_seconds == 15


def test_crossfade_default_is_clamped_into_its_own_range():
    spec = quick_library("Test", station_id="test")
    spec.crossfade = spec.crossfade.model_copy(update={"min_seconds": 8.0, "max_seconds": 20.0})
    assert StationSpec(**spec.model_dump()).crossfade.default_seconds >= 8.0


def track(**kwargs) -> Track:
    base = {"id": "x", "name": "Song", "artist": "Artist", "duration_seconds": 200}
    analysis = kwargs.pop("analysis", None)
    return Track(**{**base, **kwargs}, analysis=analysis or TrackAnalysis())


def test_filters_drop_what_they_should():
    tracks = [
        track(id="ok", genres=["Jazz"], year=2001),
        track(id="short", duration_seconds=10),
        track(id="long", duration_seconds=5000),
        track(id="old", year=1950),
        track(id="excluded-genre", genres=["Metal"]),
        track(id="excluded-artist", artist="Nickelback", artists=["Nickelback"]),
        track(id="live-take", name="Song (Live at Wembley)"),
    ]
    filters = TrackFilters(
        year_min=1990,
        exclude_genres=["Metal"],
        exclude_artists=["Nickelback"],
        exclude_title_keywords=["live at"],
    )
    assert {t.id for t in apply_filters(tracks, filters)} == {"ok"}


def test_analysis_filters_require_analysis_data():
    analysed = track(id="analysed", analysis=TrackAnalysis(bpm=128, energy=0.8, source="audiomuse"))
    slow = track(id="slow", analysis=TrackAnalysis(bpm=70, energy=0.8, source="audiomuse"))
    unknown = track(id="unknown")

    kept = apply_filters([analysed, slow, unknown], TrackFilters(bpm_min=100, bpm_max=150))
    assert {t.id for t in kept} == {"analysed"}

    kept = apply_filters([analysed, unknown], TrackFilters(require_analysis=True))
    assert {t.id for t in kept} == {"analysed"}


def test_contradictory_filter_ranges_are_rejected():
    with pytest.raises(ValidationError):
        TrackFilters(bpm_min=180, bpm_max=90)
    with pytest.raises(ValidationError):
        TrackFilters(year_min=2020, year_max=1990)


def test_store_round_trips_through_yaml(tmp_path):
    path = tmp_path / "stations.yaml"
    store = StationStore(path)  # writes the examples on first use
    assert store.list()

    store.create(quick_genre("Techno Bunker", ["Techno"], template="club", station_id="techno"))
    with pytest.raises(StationExists):
        store.create(quick_genre("Techno Bunker", ["Techno"], station_id="techno"))

    store.update("techno", StationUpdate(description="Hard and fast"))
    assert store.get("techno").description == "Hard and fast"

    # Everything survives a reload from disk.
    reloaded = StationStore(path)
    assert reloaded.get("techno").description == "Hard and fast"
    assert reloaded.get("techno").crossfade.beat_align

    reloaded.delete("techno")
    with pytest.raises(StationNotFound):
        StationStore(path).get("techno")


def test_store_skips_invalid_entries_instead_of_failing_to_start(tmp_path, caplog):
    path = tmp_path / "stations.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "stations": [
                    {"id": "good", "name": "Good", "sources": [{"kind": "library"}]},
                    {"id": "bad", "name": "Bad"},  # no sources
                    {"id": "Bad Slug!", "name": "Worse", "sources": [{"kind": "library"}]},
                ]
            }
        )
    )
    store = StationStore(path)
    assert [s.id for s in store.list()] == ["good"]


def test_the_shipped_example_config_is_valid():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "config" / "stations.example.yaml"
    data = yaml.safe_load(example.read_text("utf-8"))
    specs = [StationSpec(**entry) for entry in data["stations"]]
    assert {s.id for s in specs} >= {"deep-house", "sunday-morning", "miles-radio", "shuffle"}


def test_an_unwritable_config_dir_reports_instead_of_silently_dropping_changes(tmp_path, monkeypatch):
    """A failed write must surface and must not desync memory from disk."""
    from pathlib import Path

    from app.store import StationPersistError

    path = tmp_path / "stations.yaml"
    store = StationStore(path)
    before = {s.id for s in store.list()}

    def deny(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", deny)
    with pytest.raises(StationPersistError, match="cannot write"):
        store.create(quick_genre("Nope", ["Pop"], station_id="nope"))
    monkeypatch.undo()

    # Rolled back: memory still matches what is actually on disk.
    assert {s.id for s in store.list()} == before
    assert {s.id for s in StationStore(path).list()} == before
