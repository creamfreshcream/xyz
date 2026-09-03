"""The station *structs*.

Everything a station is made of is declared here as a typed struct, so a new
station is one small object (or one YAML block, or one API call) away.

A station has:

    sources   - WHERE tracks come from (genre / mood / artist / similar / ...)
    filters   - which of those tracks are actually allowed on air
    rotation  - how they are sequenced and how often they may repeat
    crossfade - how one track becomes the next
    stream    - codec, bitrate, listener limits
    access    - who may listen
    dayparts  - optional time-of-day overrides
"""

from __future__ import annotations

import re
from datetime import datetime, time, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Curve = Literal["equal_power", "linear", "s_curve", "exponential", "logarithmic"]
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return (slug or "station")[:64]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Struct(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------------------
# Sources - where the music comes from
# ---------------------------------------------------------------------------


class _BaseSource(Struct):
    #: Relative pull weight when a station mixes several sources.
    weight: float = Field(default=1.0, gt=0, le=100)
    #: Cap on how many tracks this source contributes to the candidate pool.
    limit: int = Field(default=500, ge=1, le=5000)


class GenreSource(_BaseSource):
    """Everything tagged with one or more genres."""

    kind: Literal["genre"] = "genre"
    genres: list[str] = Field(min_length=1)
    match: Literal["any", "all"] = "any"


class MoodSource(_BaseSource):
    """Tracks whose AudioMuse mood vector scores high on the given moods.

    Requires AudioMuse; without it the source contributes nothing and the
    station falls back to its other sources.
    """

    kind: Literal["mood"] = "mood"
    moods: list[str] = Field(min_length=1)
    min_score: float = Field(default=0.5, ge=0.0, le=1.0)
    match: Literal["any", "all"] = "any"


class ArtistSource(_BaseSource):
    """One or more artists, optionally widened to sonically similar artists."""

    kind: Literal["artist"] = "artist"
    artists: list[str] = Field(min_length=1)
    #: Pull in AudioMuse-similar artists too (classic "artist radio").
    include_similar: bool = False
    similar_limit: int = Field(default=25, ge=1, le=200)
    #: Include albums the artist only appears on (features, compilations).
    include_appearances: bool = True


class PlaylistSource(_BaseSource):
    """A Jellyfin playlist, by name or id."""

    kind: Literal["playlist"] = "playlist"
    playlists: list[str] = Field(min_length=1)


class SimilarSource(_BaseSource):
    """AudioMuse similarity around seed tracks - "sounds like this"."""

    kind: Literal["similar"] = "similar"
    seeds: list[str] = Field(min_length=1, description="Jellyfin item ids or 'Artist - Title'")
    radius: float = Field(default=0.6, ge=0.0, le=1.0, description="0 = near clones, 1 = loose")


class LibrarySource(_BaseSource):
    """The whole music library, optionally narrowed by a search term."""

    kind: Literal["library"] = "library"
    search: str | None = None


Source = Annotated[
    GenreSource | MoodSource | ArtistSource | PlaylistSource | SimilarSource | LibrarySource,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TrackFilters(Struct):
    """Applied to every candidate track after the sources produced it."""

    year_min: int | None = Field(default=None, ge=1900, le=2200)
    year_max: int | None = Field(default=None, ge=1900, le=2200)
    bpm_min: float | None = Field(default=None, ge=20, le=300)
    bpm_max: float | None = Field(default=None, ge=20, le=300)
    energy_min: float | None = Field(default=None, ge=0.0, le=1.0)
    energy_max: float | None = Field(default=None, ge=0.0, le=1.0)
    duration_min_seconds: float = Field(default=45.0, ge=0)
    duration_max_seconds: float = Field(default=900.0, ge=10)
    exclude_genres: list[str] = Field(default_factory=list)
    exclude_artists: list[str] = Field(default_factory=list)
    #: Drop tracks whose title matches any of these (case-insensitive substring).
    exclude_title_keywords: list[str] = Field(default_factory=lambda: ["live at", "interlude"])
    #: Only play tracks AudioMuse has analysed (guarantees beat-matched fades).
    require_analysis: bool = False
    min_community_rating: float | None = Field(default=None, ge=0, le=10)

    @model_validator(mode="after")
    def _check_ranges(self) -> "TrackFilters":
        for lo, hi, label in (
            (self.year_min, self.year_max, "year"),
            (self.bpm_min, self.bpm_max, "bpm"),
            (self.energy_min, self.energy_max, "energy"),
        ):
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"{label}_min must not be greater than {label}_max")
        if self.duration_min_seconds > self.duration_max_seconds:
            raise ValueError("duration_min_seconds must not be greater than duration_max_seconds")
        return self


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


class RotationSpec(Struct):
    """How the scheduler picks the next track."""

    #: random          - straight shuffle inside the pool
    #: smart           - AudioMuse similarity walk with a randomness temperature
    #: energy_curve    - follow the daypart's energy target
    #: sorted          - library order (useful for album/chronological stations)
    flow: Literal["random", "smart", "energy_curve", "sorted"] = "smart"
    #: 0 = always take the best match, 1 = basically random.
    temperature: float = Field(default=0.35, ge=0.0, le=1.0)
    no_repeat_tracks: int = Field(default=80, ge=0, le=5000)
    no_repeat_artist_tracks: int = Field(default=12, ge=0, le=500)
    no_repeat_minutes: int = Field(default=180, ge=0, le=10080)
    max_per_artist_per_hour: int = Field(default=2, ge=1, le=60)
    max_per_album_per_hour: int = Field(default=2, ge=1, le=60)
    #: How many tracks to plan ahead (shown as "up next" in the hub).
    queue_depth: int = Field(default=5, ge=1, le=25)


# ---------------------------------------------------------------------------
# Crossfade
# ---------------------------------------------------------------------------


class CrossfadeSpec(Struct):
    """Smart crossfade tuning.

    In ``smart`` mode the engine starts from ``default_seconds`` and then
    reshapes the transition using tempo, musical key, energy delta and the
    actual audio at the edges of both tracks.
    """

    enabled: bool = True
    mode: Literal["smart", "fixed", "cut"] = "smart"
    default_seconds: float = Field(default=6.0, ge=0.1, le=60)
    min_seconds: float = Field(default=1.5, ge=0.1, le=60)
    max_seconds: float = Field(default=14.0, ge=0.5, le=60)
    curve: Curve = "equal_power"

    #: Snap the overlap to a whole number of beats when both tempos match.
    beat_align: bool = True
    beat_lengths: list[int] = Field(default_factory=lambda: [8, 16, 32])
    #: Max relative tempo difference (in octaves) still counted as "matched".
    tempo_tolerance: float = Field(default=0.06, ge=0.0, le=0.5)

    #: Roll a high-pass over the outgoing track so the two basslines never
    #: fight each other - the single biggest quality win in a long blend.
    bass_swap: bool = True
    bass_swap_hz: int = Field(default=180, ge=40, le=600)

    #: Energy jump (0..1) above which the blend becomes a short, punchy slam.
    energy_jump_cut: float = Field(default=0.35, ge=0.05, le=1.0)
    #: Overlap multiplier applied when the musical keys clash.
    key_clash_factor: float = Field(default=0.5, ge=0.1, le=1.0)

    #: Trim leading/trailing silence so fades sit on the music, not on nothing.
    trim_silence: bool = True
    silence_threshold_db: float = Field(default=-45.0, ge=-90, le=-20)
    #: Extra gap between tracks (negative overlap), e.g. for talk stations.
    gap_seconds: float = Field(default=0.0, ge=0.0, le=10.0)

    #: Normalise every track towards this loudness before mixing.
    match_loudness: bool = True
    target_lufs: float = Field(default=-14.0, ge=-30.0, le=-5.0)
    max_gain_db: float = Field(default=9.0, ge=0.0, le=24.0)

    @model_validator(mode="after")
    def _check(self) -> "CrossfadeSpec":
        if self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds must not be greater than max_seconds")
        clamped = min(max(self.default_seconds, self.min_seconds), self.max_seconds)
        # Written through __dict__: a normal assignment would re-run this
        # validator (validate_assignment is on) and recurse.
        self.__dict__["default_seconds"] = clamped
        return self


# ---------------------------------------------------------------------------
# Stream / access / dayparts
# ---------------------------------------------------------------------------


class StreamSpec(Struct):
    format: Literal["mp3", "aac", "opus"] = "mp3"
    bitrate: int = Field(default=192, ge=48, le=512)
    sample_rate: int = Field(default=44100, ge=22050, le=48000)
    channels: Literal[1, 2] = 2
    #: Keep the station playing even with zero listeners (continuous programme).
    always_on: bool = False
    #: Stop the engine this many seconds after the last listener left.
    idle_timeout_seconds: int = Field(default=300, ge=0, le=86400)
    max_listeners: int | None = Field(default=None, ge=1, le=10000)
    #: Send ICY title metadata to players that ask for it.
    icy_metadata: bool = True

    @property
    def content_type(self) -> str:
        return {"mp3": "audio/mpeg", "aac": "audio/aac", "opus": "audio/ogg"}[self.format]

    @property
    def file_extension(self) -> str:
        return {"mp3": "mp3", "aac": "aac", "opus": "opus"}[self.format]


class AccessSpec(Struct):
    """Who may listen.

    Streams are authenticated by default. ``public`` is an explicit opt-out and
    the only setting that serves audio without credentials.
    """

    visibility: Literal["private", "listed", "public"] = "listed"
    allowed_roles: list[Literal["admin", "listener"]] = Field(
        default_factory=lambda: ["admin", "listener"]
    )
    #: Empty = every user whose role is allowed.
    allowed_users: list[str] = Field(default_factory=list)

    @property
    def requires_auth(self) -> bool:
        return self.visibility != "public"


class Daypart(Struct):
    """Time-of-day override. Times are local to the container's timezone."""

    name: str
    start: time
    end: time
    energy_target: float | None = Field(default=None, ge=0.0, le=1.0)
    moods: list[str] = Field(default_factory=list)
    crossfade_seconds: float | None = Field(default=None, ge=0.1, le=60)

    def contains(self, moment: time) -> bool:
        if self.start <= self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end  # wraps midnight


class SweeperSpec(Struct):
    """Station IDs / jingles dropped in every N tracks."""

    enabled: bool = False
    playlist: str | None = None
    item_ids: list[str] = Field(default_factory=list)
    every_tracks: int = Field(default=8, ge=1, le=100)
    crossfade_seconds: float = Field(default=1.0, ge=0.0, le=10)


# ---------------------------------------------------------------------------
# The station
# ---------------------------------------------------------------------------


class StationSpec(Struct):
    """A complete radio station."""

    id: str
    name: str
    description: str = ""
    #: Free text shown as ICY genre in players.
    genre_tag: str = ""
    artwork_url: str | None = None
    enabled: bool = True

    sources: list[Source] = Field(min_length=1)
    filters: TrackFilters = Field(default_factory=TrackFilters)
    rotation: RotationSpec = Field(default_factory=RotationSpec)
    crossfade: CrossfadeSpec = Field(default_factory=CrossfadeSpec)
    stream: StreamSpec = Field(default_factory=StreamSpec)
    access: AccessSpec = Field(default_factory=AccessSpec)
    dayparts: list[Daypart] = Field(default_factory=list)
    sweepers: SweeperSpec = Field(default_factory=SweeperSpec)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        value = value.strip().lower()
        if not SLUG_RE.match(value):
            raise ValueError(
                "id must be lowercase a-z, 0-9, '-' or '_', starting with a letter or digit"
            )
        return value

    def active_daypart(self, moment: time | None = None) -> Daypart | None:
        moment = moment or datetime.now().time()
        for part in self.dayparts:
            if part.contains(moment):
                return part
        return None

    def effective_crossfade(self, moment: time | None = None) -> CrossfadeSpec:
        part = self.active_daypart(moment)
        if part is None or part.crossfade_seconds is None:
            return self.crossfade
        return self.crossfade.model_copy(update={"default_seconds": part.crossfade_seconds})

    def mount(self) -> str:
        return f"/stream/{self.id}.{self.stream.file_extension}"


class StationUpdate(BaseModel):
    """Partial update payload - every field optional."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    genre_tag: str | None = None
    artwork_url: str | None = None
    enabled: bool | None = None
    sources: list[Source] | None = None
    filters: TrackFilters | None = None
    rotation: RotationSpec | None = None
    crossfade: CrossfadeSpec | None = None
    stream: StreamSpec | None = None
    access: AccessSpec | None = None
    dayparts: list[Daypart] | None = None
    sweepers: SweeperSpec | None = None


# ---------------------------------------------------------------------------
# Runtime structs (library / playout)
# ---------------------------------------------------------------------------


class TrackAnalysis(BaseModel):
    """Everything the crossfade planner knows about a track."""

    model_config = ConfigDict(extra="ignore")

    bpm: float | None = None
    key: str | None = None
    camelot: str | None = None
    energy: float | None = None
    danceability: float | None = None
    valence: float | None = None
    moods: dict[str, float] = Field(default_factory=dict)
    lufs: float | None = None
    #: Measured from the audio itself, filled in by the engine.
    intro_silence: float = 0.0
    outro_silence: float = 0.0
    outro_fade: float | None = None
    source: Literal["audiomuse", "local", "mixed", "none"] = "none"

    def merged_with(self, other: "TrackAnalysis") -> "TrackAnalysis":
        """Overlay ``other`` on top of self for fields self does not have."""
        data = self.model_dump()
        for field, value in other.model_dump().items():
            if field == "source":
                continue
            if field == "moods":
                if not data.get("moods"):
                    data["moods"] = value
                continue
            if data.get(field) in (None, 0.0) and value not in (None,):
                data[field] = value
        merged = TrackAnalysis(**data)
        merged.source = "mixed" if self.source != other.source else self.source
        return merged


class Track(BaseModel):
    """A playable item from Jellyfin."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    artist: str = "Unknown Artist"
    artists: list[str] = Field(default_factory=list)
    album: str = ""
    album_id: str = ""
    album_artist: str = ""
    genres: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
    year: int | None = None
    community_rating: float | None = None
    analysis: TrackAnalysis = Field(default_factory=TrackAnalysis)

    @property
    def title(self) -> str:
        return f"{self.artist} - {self.name}"


class QueuedTrack(BaseModel):
    track: Track
    reason: str = ""


class TransitionInfo(BaseModel):
    """What the engine actually did on the last transition (shown in the hub)."""

    overlap_seconds: float = 0.0
    curve: str = "equal_power"
    beat_matched: bool = False
    key_matched: bool = False
    bass_swap: bool = False
    gain_db: float = 0.0
    reason: str = ""


class NowPlaying(BaseModel):
    station_id: str
    track: Track | None = None
    started_at: datetime | None = None
    elapsed_seconds: float = 0.0
    duration_seconds: float = 0.0
    next_up: list[Track] = Field(default_factory=list)
    listeners: int = 0
    state: str = "stopped"
    transition: TransitionInfo | None = None
