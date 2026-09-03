"""Ready-made station structs.

The point of this module: setting up a new station should be one line.

    spec = quick_genre("Deep House Nights", ["Deep House", "House"])
    spec = quick_mood("Sunday Morning", ["calm", "warm"])
    spec = quick_artist("Miles Davis Radio", ["Miles Davis"], include_similar=True)

Each builder starts from a *template* - a tuned bundle of rotation, crossfade
and filter settings for that flavour of station - and every field can be
overridden through ``**overrides``.
"""

from __future__ import annotations

from typing import Any

from app.models import (
    AccessSpec,
    ArtistSource,
    CrossfadeSpec,
    Daypart,
    GenreSource,
    LibrarySource,
    MoodSource,
    RotationSpec,
    SimilarSource,
    StationSpec,
    StreamSpec,
    TrackFilters,
    slugify,
)


class Template:
    """A named bundle of defaults for a kind of station."""

    def __init__(
        self,
        key: str,
        label: str,
        description: str,
        crossfade: CrossfadeSpec,
        rotation: RotationSpec,
        filters: TrackFilters | None = None,
        stream: StreamSpec | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.description = description
        self.crossfade = crossfade
        self.rotation = rotation
        self.filters = filters or TrackFilters()
        self.stream = stream or StreamSpec()

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "crossfade": self.crossfade.model_dump(mode="json"),
            "rotation": self.rotation.model_dump(mode="json"),
            "filters": self.filters.model_dump(mode="json"),
            "stream": self.stream.model_dump(mode="json"),
        }


TEMPLATES: dict[str, Template] = {
    # Long, beat-matched blends. The default for dance/electronic genres.
    "club": Template(
        key="club",
        label="Club / DJ mix",
        description="Long beat-matched blends, bass swap, key aware. For house, techno, disco.",
        crossfade=CrossfadeSpec(
            mode="smart",
            default_seconds=10.0,
            min_seconds=4.0,
            max_seconds=20.0,
            curve="linear",
            beat_align=True,
            beat_lengths=[16, 32],
            bass_swap=True,
            energy_jump_cut=0.45,
        ),
        rotation=RotationSpec(flow="smart", temperature=0.25, no_repeat_tracks=120),
        filters=TrackFilters(duration_min_seconds=120.0),
    ),
    # The all-rounder: pleasant blends, works for anything.
    "radio": Template(
        key="radio",
        label="Classic radio",
        description="Balanced 5-8 s blends, wide rotation. Good default for any genre.",
        crossfade=CrossfadeSpec(mode="smart", default_seconds=6.0, min_seconds=2.0, max_seconds=12.0),
        rotation=RotationSpec(flow="smart", temperature=0.4),
    ),
    # Short, tight transitions that keep vocals intact.
    "vocal": Template(
        key="vocal",
        label="Songs / vocal",
        description="Short 2-4 s fades that never talk over a vocal intro or outro.",
        crossfade=CrossfadeSpec(
            mode="smart",
            default_seconds=3.0,
            min_seconds=1.0,
            max_seconds=6.0,
            curve="equal_power",
            beat_align=False,
            bass_swap=False,
        ),
        rotation=RotationSpec(flow="smart", temperature=0.45),
    ),
    "chill": Template(
        key="chill",
        label="Ambient / chill",
        description="Very long, soft dissolves. Low energy ceiling, no bangers.",
        crossfade=CrossfadeSpec(
            mode="smart",
            default_seconds=12.0,
            min_seconds=6.0,
            max_seconds=24.0,
            curve="s_curve",
            beat_align=False,
            bass_swap=False,
            energy_jump_cut=0.25,
        ),
        rotation=RotationSpec(flow="smart", temperature=0.3),
        filters=TrackFilters(energy_max=0.55),
    ),
    "workout": Template(
        key="workout",
        label="Workout",
        description="High energy only, punchy slams, tight tempo window.",
        crossfade=CrossfadeSpec(
            mode="smart",
            default_seconds=5.0,
            min_seconds=2.0,
            max_seconds=10.0,
            curve="exponential",
            beat_align=True,
            energy_jump_cut=0.6,
        ),
        rotation=RotationSpec(flow="energy_curve", temperature=0.35),
        filters=TrackFilters(energy_min=0.6, bpm_min=110, bpm_max=180),
    ),
    "sleep": Template(
        key="sleep",
        label="Sleep",
        description="Minimal energy, 20 s dissolves, nothing loud, nothing fast.",
        crossfade=CrossfadeSpec(
            mode="smart",
            default_seconds=18.0,
            min_seconds=10.0,
            max_seconds=30.0,
            curve="s_curve",
            beat_align=False,
            bass_swap=False,
            target_lufs=-20.0,
        ),
        rotation=RotationSpec(flow="smart", temperature=0.3),
        filters=TrackFilters(energy_max=0.35, bpm_max=100),
    ),
    "talk": Template(
        key="talk",
        label="Spoken word",
        description="Hard cuts with a short gap. For audiobooks, podcasts, comedy.",
        crossfade=CrossfadeSpec(mode="cut", gap_seconds=0.8, match_loudness=True, target_lufs=-16.0),
        rotation=RotationSpec(flow="sorted", no_repeat_tracks=500),
        filters=TrackFilters(duration_min_seconds=10.0, duration_max_seconds=7200.0),
    ),
}

DEFAULT_TEMPLATE = "radio"


def _build(
    name: str,
    sources: list[Any],
    template: str,
    station_id: str | None,
    description: str,
    genre_tag: str,
    overrides: dict[str, Any],
) -> StationSpec:
    tpl = TEMPLATES.get(template, TEMPLATES[DEFAULT_TEMPLATE])
    data: dict[str, Any] = {
        "id": station_id or slugify(name),
        "name": name,
        "description": description,
        "genre_tag": genre_tag,
        "sources": sources,
        "crossfade": tpl.crossfade.model_copy(deep=True),
        "rotation": tpl.rotation.model_copy(deep=True),
        "filters": tpl.filters.model_copy(deep=True),
        "stream": tpl.stream.model_copy(deep=True),
        "access": AccessSpec(),
    }
    data.update({k: v for k, v in overrides.items() if v is not None})
    return StationSpec(**data)


def quick_genre(
    name: str,
    genres: list[str],
    *,
    template: str = "radio",
    match: str = "any",
    station_id: str | None = None,
    description: str = "",
    **overrides: Any,
) -> StationSpec:
    """A station that plays one or more genres."""
    return _build(
        name,
        [GenreSource(genres=genres, match=match)],  # type: ignore[arg-type]
        template,
        station_id,
        description or f"Non-stop {', '.join(genres)}.",
        ", ".join(genres),
        overrides,
    )


def quick_mood(
    name: str,
    moods: list[str],
    *,
    template: str = "chill",
    min_score: float = 0.5,
    station_id: str | None = None,
    description: str = "",
    **overrides: Any,
) -> StationSpec:
    """A station driven by AudioMuse mood scores."""
    return _build(
        name,
        [MoodSource(moods=moods, min_score=min_score)],
        template,
        station_id,
        description or f"Everything that feels {', '.join(moods)}.",
        ", ".join(moods),
        overrides,
    )


def quick_artist(
    name: str,
    artists: list[str],
    *,
    template: str = "radio",
    include_similar: bool = True,
    station_id: str | None = None,
    description: str = "",
    **overrides: Any,
) -> StationSpec:
    """Artist radio - the artists themselves plus sonically similar company."""
    return _build(
        name,
        [ArtistSource(artists=artists, include_similar=include_similar)],
        template,
        station_id,
        description
        or (
            f"{', '.join(artists)} and friends." if include_similar else f"Only {', '.join(artists)}."
        ),
        ", ".join(artists),
        overrides,
    )


def quick_similar(
    name: str,
    seeds: list[str],
    *,
    template: str = "radio",
    radius: float = 0.6,
    station_id: str | None = None,
    description: str = "",
    **overrides: Any,
) -> StationSpec:
    """"Sounds like these tracks" - AudioMuse similarity around seed tracks."""
    return _build(
        name,
        [SimilarSource(seeds=seeds, radius=radius)],
        template,
        station_id,
        description or "Tracks that sound like the seeds.",
        "",
        overrides,
    )


def quick_library(
    name: str,
    *,
    template: str = "radio",
    search: str | None = None,
    station_id: str | None = None,
    description: str = "",
    **overrides: Any,
) -> StationSpec:
    """Everything in the library, shuffled."""
    return _build(
        name,
        [LibrarySource(search=search)],
        template,
        station_id,
        description or "The whole library on shuffle.",
        "",
        overrides,
    )


QUICK_BUILDERS = {
    "genre": quick_genre,
    "mood": quick_mood,
    "artist": quick_artist,
    "similar": quick_similar,
    "library": quick_library,
}


def example_stations() -> list[StationSpec]:
    """Stations shipped as the example config."""
    return [
        quick_genre(
            "Deep House Nights",
            ["Deep House", "House", "Nu Disco"],
            template="club",
            station_id="deep-house",
        ),
        quick_mood(
            "Sunday Morning",
            ["calm", "warm", "acoustic"],
            template="chill",
            station_id="sunday-morning",
        ),
        quick_artist(
            "Miles Davis Radio",
            ["Miles Davis"],
            template="vocal",
            include_similar=True,
            station_id="miles-radio",
        ),
        quick_library(
            "Shuffle Everything",
            station_id="shuffle",
            dayparts=[
                Daypart(name="night", start="22:00", end="06:00", energy_target=0.3),  # type: ignore[arg-type]
                Daypart(name="day", start="06:00", end="22:00", energy_target=0.6),  # type: ignore[arg-type]
            ],
        ),
    ]
