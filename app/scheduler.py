"""Track sequencing: what plays next, and why.

Rotation rules (no repeats, artist and album spacing) are hard constraints.
Within what is left, the ``flow`` setting decides the order:

    random        - shuffle
    sorted        - pool order (album stations, chronological programmes)
    energy_curve  - steer towards the current daypart's energy target
    smart         - similarity walk: prefer tracks that sit close to the one
                    just played in tempo, key and energy, softened by a
                    randomness temperature so it never loops
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass

from app.audiomuse import camelot_distance
from app.models import QueuedTrack, StationSpec, Track

log = logging.getLogger(__name__)


@dataclass
class PlayRecord:
    track_id: str
    artist: str
    album_id: str
    played_at: float


class Scheduler:
    """Per-station rotation state."""

    def __init__(self, spec: StationSpec) -> None:
        self.spec = spec
        self._history: deque[PlayRecord] = deque(maxlen=2000)
        self._last: Track | None = None
        self._queue: list[QueuedTrack] = []

    def update_spec(self, spec: StationSpec) -> None:
        self.spec = spec
        self._queue.clear()

    @property
    def history(self) -> list[PlayRecord]:
        return list(self._history)

    def note_played(self, track: Track) -> None:
        self._history.append(
            PlayRecord(track.id, track.artist.lower(), track.album_id, time.time())
        )
        self._last = track

    # -- rotation rules ----------------------------------------------------

    def _violations(self, track: Track, sequence: list[PlayRecord]) -> set[str]:
        """Which rotation rules this track would break right now."""
        rotation = self.spec.rotation
        now = time.time()
        broken: set[str] = set()

        # A window of 0 means the rule is switched off.
        if rotation.no_repeat_tracks and any(
            record.track_id == track.id for record in sequence[-rotation.no_repeat_tracks :]
        ):
            broken.add("track")
        if rotation.no_repeat_minutes:
            cutoff = now - rotation.no_repeat_minutes * 60
            if any(r.track_id == track.id and r.played_at >= cutoff for r in sequence):
                broken.add("track")

        artist = track.artist.lower()
        if rotation.no_repeat_artist_tracks and any(
            r.artist == artist for r in sequence[-rotation.no_repeat_artist_tracks :]
        ):
            broken.add("artist")

        hour_ago = now - 3600
        in_hour = [r for r in sequence if r.played_at >= hour_ago]
        if sum(1 for r in in_hour if r.artist == artist) >= rotation.max_per_artist_per_hour:
            broken.add("artist_hour")
        if track.album_id and (
            sum(1 for r in in_hour if r.album_id == track.album_id)
            >= rotation.max_per_album_per_hour
        ):
            broken.add("album_hour")
        return broken

    #: Rules dropped at each relaxation level, in the order they are given up.
    #: The no-repeat window is never dropped here - if even that cannot be
    #: satisfied, the final fallback below keeps as much spacing as the pool
    #: physically allows.
    _RELAXATION: tuple[set[str], ...] = (
        set(),
        {"album_hour"},
        {"album_hour", "artist_hour"},
        {"album_hour", "artist_hour", "artist"},
    )

    def _candidates(self, pool: list[Track], planned: list[PlayRecord] | None = None) -> list[Track]:
        """Tracks that may play next.

        A pool smaller than the rotation rules assume would otherwise leave no
        candidate at all, so the rules are given up one at a time - hourly caps
        first, the no-repeat window last - instead of all at once.
        """
        unique: dict[str, Track] = {}
        for track in pool:
            unique.setdefault(track.id, track)
        sequence = list(self._history) + list(planned or [])

        broken_by = {track.id: self._violations(track, sequence) for track in unique.values()}
        for level, ignored in enumerate(self._RELAXATION):
            allowed = [t for t in unique.values() if not (broken_by[t.id] - ignored)]
            if allowed:
                if level:
                    log.debug(
                        "station %s: pool too small, ignoring %s",
                        self.spec.id,
                        ", ".join(sorted(ignored)),
                    )
                return allowed

        # Even the no-repeat window cannot be met (a handful of tracks in the
        # pool): space them out as far as the pool allows.
        spacing = max(1, min(3, len(unique) - 1))
        recent = {r.track_id for r in sequence[-spacing:]}
        remaining = [t for t in unique.values() if t.id not in recent]
        return remaining or list(unique.values())

    # -- scoring -----------------------------------------------------------

    def _similarity(self, previous: Track, candidate: Track) -> float:
        """0..1, higher = flows better after ``previous``."""
        score, weight = 0.0, 0.0
        a, b = previous.analysis, candidate.analysis

        if a.bpm and b.bpm:
            ratio = b.bpm / a.bpm
            while ratio < 0.75:
                ratio *= 2
            while ratio > 1.5:
                ratio /= 2
            score += max(0.0, 1.0 - abs(math.log2(ratio)) / 0.25) * 2.0
            weight += 2.0

        distance = camelot_distance(a.camelot, b.camelot)
        if distance is not None:
            score += max(0.0, 1.0 - distance / 3.0) * 1.5
            weight += 1.5

        if a.energy is not None and b.energy is not None:
            score += max(0.0, 1.0 - abs(a.energy - b.energy) / 0.5)
            weight += 1.0

        if a.moods and b.moods:
            shared = set(a.moods) & set(b.moods)
            if shared:
                score += sum(min(a.moods[m], b.moods[m]) for m in shared) / len(shared)
            weight += 1.0

        if set(g.lower() for g in previous.genres) & set(g.lower() for g in candidate.genres):
            score += 0.75
        weight += 0.75

        return score / weight if weight else 0.5

    def _energy_score(self, candidate: Track, target: float) -> float:
        energy = candidate.analysis.energy
        if energy is None:
            return 0.5
        return max(0.0, 1.0 - abs(energy - target))

    def _pick(self, candidates: list[Track], previous: Track | None) -> tuple[Track, str]:
        rotation = self.spec.rotation
        flow = rotation.flow

        if flow == "sorted":
            return candidates[0], "sorted"
        if flow == "random" or (flow == "smart" and previous is None):
            return random.choice(candidates), "random"

        if flow == "energy_curve":
            daypart = self.spec.active_daypart()
            target = daypart.energy_target if daypart and daypart.energy_target is not None else 0.5
            scored = [(self._energy_score(t, target), t) for t in candidates]
            reason = f"energy target {target:.2f}"
        else:  # smart
            assert previous is not None
            scored = [(self._similarity(previous, t), t) for t in candidates]
            reason = f"smart flow after {previous.name}"

        # Softmax-style sampling: temperature 0 picks the best, 1 is a shuffle.
        temperature = max(0.02, rotation.temperature)
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[: max(3, int(len(scored) * temperature) + 3)]
        weights = [math.exp(score / temperature) for score, _ in top]
        total = sum(weights) or 1.0
        choice = random.choices(top, weights=[w / total for w in weights], k=1)[0]
        return choice[1], f"{reason} (match {choice[0]:.2f})"

    # -- public ------------------------------------------------------------

    def fill_queue(self, pool: list[Track]) -> list[QueuedTrack]:
        """Plan ahead up to ``queue_depth`` tracks."""
        if not pool:
            return []
        depth = self.spec.rotation.queue_depth
        now = time.time()
        planned = [
            PlayRecord(q.track.id, q.track.artist.lower(), q.track.album_id, now)
            for q in self._queue
        ]
        guard = 0
        while len(self._queue) < depth and guard < depth * 20:
            guard += 1
            candidates = self._candidates(pool, planned)
            if not candidates:
                break
            previous = self._queue[-1].track if self._queue else self._last
            track, reason = self._pick(candidates, previous)
            self._queue.append(QueuedTrack(track=track, reason=reason))
            planned.append(PlayRecord(track.id, track.artist.lower(), track.album_id, now))
        return list(self._queue)

    def next_track(self, pool: list[Track]) -> QueuedTrack | None:
        self.fill_queue(pool)
        if not self._queue:
            return None
        queued = self._queue.pop(0)
        self.note_played(queued.track)
        return queued

    def upcoming(self) -> list[Track]:
        return [q.track for q in self._queue]
