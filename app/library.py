"""Turns station *sources* into a pool of playable, analysed tracks."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from app.audiomuse import AudioMuseClient
from app.jellyfin import JellyfinClient, JellyfinError
from app.models import StationSpec, Track, TrackFilters

log = logging.getLogger(__name__)

POOL_TTL_SECONDS = 900


class MusicLibrary:
    """Jellyfin + AudioMuse behind one interface, with a per-station pool cache."""

    def __init__(self, jellyfin: JellyfinClient, audiomuse: AudioMuseClient) -> None:
        self.jellyfin = jellyfin
        self.audiomuse = audiomuse
        self._pools: dict[str, tuple[float, list[Track]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- pool --------------------------------------------------------------

    async def pool_for(self, spec: StationSpec, force_refresh: bool = False) -> list[Track]:
        lock = self._locks.setdefault(spec.id, asyncio.Lock())
        async with lock:
            cached = self._pools.get(spec.id)
            if cached and not force_refresh and time.time() - cached[0] < POOL_TTL_SECONDS:
                return cached[1]
            tracks = await self._build_pool(spec)
            self._pools[spec.id] = (time.time(), tracks)
            return tracks

    def invalidate(self, station_id: str) -> None:
        self._pools.pop(station_id, None)

    async def _build_pool(self, spec: StationSpec) -> list[Track]:
        collected: dict[str, Track] = {}
        weights: dict[str, float] = {}

        results = await asyncio.gather(
            *(self._resolve_source(source) for source in spec.sources), return_exceptions=True
        )
        for source, result in zip(spec.sources, results):
            if isinstance(result, BaseException):
                log.error("station %s: source %s failed: %s", spec.id, source.kind, result)
                continue
            for track in result:
                collected.setdefault(track.id, track)
                weights[track.id] = max(weights.get(track.id, 0.0), source.weight)

        tracks = list(collected.values())
        log.info("station %s: %d candidates from %d sources", spec.id, len(tracks), len(spec.sources))

        await self.enrich(tracks)
        filtered = apply_filters(tracks, spec.filters)
        log.info("station %s: %d tracks after filters", spec.id, len(filtered))

        # Weighted sources are honoured by duplicating the higher-weighted
        # entries in the pool; the scheduler samples from it.
        expanded: list[Track] = []
        for track in filtered:
            copies = max(1, int(round(weights.get(track.id, 1.0))))
            expanded.extend([track] * min(copies, 5))
        random.shuffle(expanded)
        return expanded

    async def _resolve_source(self, source: Any) -> list[Track]:
        kind = source.kind
        if kind == "genre":
            return await self.jellyfin.by_genres(source.genres, source.limit, source.match == "all")

        if kind == "artist":
            artists = list(source.artists)
            if source.include_similar:
                found = await asyncio.gather(
                    *(self.jellyfin.similar_artists(a, source.similar_limit) for a in source.artists),
                    return_exceptions=True,
                )
                for entry in found:
                    if isinstance(entry, list):
                        artists.extend(entry)
            return await self.jellyfin.by_artists(
                list(dict.fromkeys(artists)), source.limit, source.include_appearances
            )

        if kind == "playlist":
            tracks: list[Track] = []
            for playlist in source.playlists:
                tracks.extend(await self.jellyfin.playlist_tracks(playlist, source.limit))
            return tracks

        if kind == "mood":
            if not self.audiomuse.available:
                log.warning("mood source needs AudioMuse, which is unavailable")
                return []
            ids = await self.audiomuse.search_by_mood(source.moods, source.limit, source.min_score)
            if not ids:
                return await self._mood_fallback(source)
            return await self.jellyfin.by_ids(ids)

        if kind == "similar":
            return await self._resolve_similar(source)

        if kind == "library":
            if source.search:
                return await self.jellyfin.search(source.search, source.limit)
            return await self.jellyfin.all_tracks(source.limit)

        log.warning("unknown source kind %r", kind)
        return []

    async def _mood_fallback(self, source: Any) -> list[Track]:
        """AudioMuse has no mood search endpoint here: scan and score locally."""
        candidates = await self.jellyfin.all_tracks(min(source.limit * 4, 2000))
        await self.enrich(candidates)
        wanted = [m.lower() for m in source.moods]
        matches = []
        for track in candidates:
            scores = [track.analysis.moods.get(mood, 0.0) for mood in wanted]
            if not scores:
                continue
            hit = all(s >= source.min_score for s in scores) if source.match == "all" else any(
                s >= source.min_score for s in scores
            )
            if hit:
                matches.append(track)
        return matches[: source.limit]

    async def _resolve_similar(self, source: Any) -> list[Track]:
        seed_ids: list[str] = []
        for seed in source.seeds:
            if len(seed) == 32 and " " not in seed:
                seed_ids.append(seed)
                continue
            found = await self.jellyfin.search(seed, limit=1)
            if found:
                seed_ids.append(found[0].id)

        item_ids: list[str] = []
        for seed_id in seed_ids:
            if self.audiomuse.available:
                item_ids.extend(
                    await self.audiomuse.similar_tracks(seed_id, source.limit, source.radius)
                )
            else:
                item_ids.extend(t.id for t in await self.jellyfin.similar_items(seed_id, source.limit))

        tracks = await self.jellyfin.by_ids(list(dict.fromkeys(item_ids))[: source.limit])
        if not tracks:
            tracks = await self.jellyfin.by_ids(seed_ids)
        return tracks

    # -- analysis ----------------------------------------------------------

    async def enrich(self, tracks: list[Track]) -> None:
        """Attach AudioMuse analysis to tracks, in place."""
        if not tracks or not self.audiomuse.enabled:
            return
        pending = [t.id for t in tracks if t.analysis.source == "none"]
        if not pending:
            return
        analyses = await self.audiomuse.analyse_many(pending)
        for track in tracks:
            found = analyses.get(track.id)
            if found is not None:
                track.analysis = found

    async def health(self) -> dict[str, Any]:
        try:
            jellyfin = await self.jellyfin.ping()
        except JellyfinError as exc:
            jellyfin = {"ok": False, "reason": str(exc)}
        return {"jellyfin": jellyfin, "audiomuse": await self.audiomuse.ping()}


def apply_filters(tracks: list[Track], filters: TrackFilters) -> list[Track]:
    excluded_genres = {g.lower() for g in filters.exclude_genres}
    excluded_artists = {a.lower() for a in filters.exclude_artists}
    keywords = [k.lower() for k in filters.exclude_title_keywords if k.strip()]

    kept: list[Track] = []
    for track in tracks:
        duration = track.duration_seconds
        if duration and not (
            filters.duration_min_seconds <= duration <= filters.duration_max_seconds
        ):
            continue
        if filters.year_min and (track.year or 0) < filters.year_min:
            continue
        if filters.year_max and (track.year or 9999) > filters.year_max:
            continue
        if excluded_genres & {g.lower() for g in track.genres}:
            continue
        if excluded_artists & {a.lower() for a in track.artists + [track.artist]}:
            continue
        title = track.name.lower()
        if any(keyword in title for keyword in keywords):
            continue
        if filters.min_community_rating is not None and (
            track.community_rating is None or track.community_rating < filters.min_community_rating
        ):
            continue

        analysis = track.analysis
        if filters.require_analysis and analysis.source == "none":
            continue
        if filters.bpm_min is not None and (analysis.bpm is None or analysis.bpm < filters.bpm_min):
            continue
        if filters.bpm_max is not None and (analysis.bpm is None or analysis.bpm > filters.bpm_max):
            continue
        if filters.energy_min is not None and (
            analysis.energy is None or analysis.energy < filters.energy_min
        ):
            continue
        if filters.energy_max is not None and (
            analysis.energy is None or analysis.energy > filters.energy_max
        ):
            continue
        kept.append(track)
    return kept
