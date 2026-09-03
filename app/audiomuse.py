"""AudioMuse-AI client - the DJ brain.

AudioMuse supplies tempo, musical key, energy and mood vectors per track plus
sonic similarity between tracks. The crossfade planner and the "smart flow"
scheduler both run on that data.

Deployments expose slightly different routes and field names, so this client is
deliberately tolerant: endpoint paths come from settings, responses are
normalised, and every failure degrades to "no analysis" rather than taking a
station off the air (the engine then falls back to measuring the audio itself).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.models import TrackAnalysis

log = logging.getLogger(__name__)

# Camelot wheel: (pitch class, mode) -> code. Mode 1 = major (B), 0 = minor (A).
_CAMELOT_MAJOR = {
    "B": "1B", "F#": "2B", "Gb": "2B", "Db": "3B", "C#": "3B", "Ab": "4B", "G#": "4B",
    "Eb": "5B", "D#": "5B", "Bb": "6B", "A#": "6B", "F": "7B", "C": "8B", "G": "9B",
    "D": "10B", "A": "11B", "E": "12B",
}
_CAMELOT_MINOR = {
    "Ab": "1A", "G#": "1A", "Eb": "2A", "D#": "2A", "Bb": "3A", "A#": "3A", "F": "4A",
    "C": "5A", "G": "6A", "D": "7A", "A": "8A", "E": "9A", "B": "10A", "F#": "11A",
    "Gb": "11A", "Db": "12A", "C#": "12A",
}

_BPM_KEYS = ("bpm", "tempo", "estimated_bpm", "tempo_bpm")
_ENERGY_KEYS = ("energy", "energy_level", "arousal")
_DANCE_KEYS = ("danceability", "dance")
_VALENCE_KEYS = ("valence", "happiness", "positivity")
_LUFS_KEYS = ("lufs", "loudness_lufs", "integrated_loudness", "loudness")
_MOOD_KEYS = ("moods", "mood_vector", "mood_scores", "tags", "mood")


def to_camelot(key: str | None, scale: str | None = None) -> str | None:
    """Normalise a key name ("F# minor", "Gbm", "11A") to a Camelot code."""
    if not key:
        return None
    raw = str(key).strip()
    if not raw:
        return None

    upper = raw.upper().replace(" ", "")
    if len(upper) in (2, 3) and upper[-1] in "AB" and upper[:-1].isdigit():
        number = int(upper[:-1])
        if 1 <= number <= 12:
            return f"{number}{upper[-1]}"

    text = raw.replace("-", " ").replace("_", " ")
    parts = text.split()
    root = parts[0]
    mode = (scale or (parts[1] if len(parts) > 1 else "")).lower()

    if not mode and root and root[-1] in "mM" and len(root) > 1 and root[-1] == "m":
        mode, root = "minor", root[:-1]

    root = root[0].upper() + root[1:].replace("S", "#")
    minor = mode.startswith("min") or mode.startswith("m") and not mode.startswith("maj")
    table = _CAMELOT_MINOR if minor else _CAMELOT_MAJOR
    return table.get(root)


def camelot_distance(a: str | None, b: str | None) -> int | None:
    """Distance on the Camelot wheel. 0 = same, 1 = harmonically compatible."""
    if not a or not b:
        return None
    try:
        num_a, mode_a = int(a[:-1]), a[-1]
        num_b, mode_b = int(b[:-1]), b[-1]
    except (ValueError, IndexError):
        return None
    if a == b:
        return 0
    ring = min((num_a - num_b) % 12, (num_b - num_a) % 12)
    if mode_a == mode_b:
        return ring
    return 1 if ring == 0 else ring + 1


def _first_number(data: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _normalise_unit(value: float | None) -> float | None:
    """Accept 0..1 or 0..100 and return 0..1."""
    if value is None:
        return None
    if value > 1.0:
        value = value / 100.0
    return max(0.0, min(1.0, value))


def _extract_moods(data: dict[str, Any]) -> dict[str, float]:
    for key in _MOOD_KEYS:
        value = data.get(key)
        if isinstance(value, dict):
            out = {}
            for name, score in value.items():
                if isinstance(score, (int, float)) and not isinstance(score, bool):
                    out[str(name).lower()] = _normalise_unit(float(score)) or 0.0
            if out:
                return out
        if isinstance(value, list) and value:
            if all(isinstance(v, str) for v in value):
                return {str(v).lower(): 1.0 for v in value}
            out = {}
            for entry in value:
                if isinstance(entry, dict):
                    name = entry.get("name") or entry.get("mood") or entry.get("label")
                    score = _first_number(entry, ("score", "value", "confidence", "weight"))
                    if name:
                        out[str(name).lower()] = _normalise_unit(score) if score is not None else 1.0
            if out:
                return out
        if isinstance(value, str) and value:
            return {value.lower(): 1.0}
    return {}


def parse_analysis(payload: dict[str, Any]) -> TrackAnalysis:
    """Normalise whatever AudioMuse returned into a TrackAnalysis."""
    data = dict(payload)
    for nested in ("analysis", "features", "attributes", "data"):
        inner = data.get(nested)
        if isinstance(inner, dict):
            merged = dict(inner)
            merged.update({k: v for k, v in data.items() if k != nested})
            data = merged

    key = data.get("key") or data.get("musical_key") or data.get("camelot")
    scale = data.get("scale") or data.get("mode")
    bpm = _first_number(data, _BPM_KEYS)

    return TrackAnalysis(
        bpm=bpm if bpm and 20 <= bpm <= 300 else None,
        key=str(key) if key else None,
        camelot=to_camelot(str(key) if key else None, str(scale) if scale else None),
        energy=_normalise_unit(_first_number(data, _ENERGY_KEYS)),
        danceability=_normalise_unit(_first_number(data, _DANCE_KEYS)),
        valence=_normalise_unit(_first_number(data, _VALENCE_KEYS)),
        moods=_extract_moods(data),
        lufs=_first_number(data, _LUFS_KEYS),
        source="audiomuse",
    )


class AnalysisCache:
    """Disk-backed cache so a restart does not re-query the whole library."""

    def __init__(self, path: Path, ttl_seconds: int = 30 * 24 * 3600) -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._entries: dict[str, dict[str, Any]] = {}
        self._dirty = 0
        if path.exists():
            try:
                self._entries = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                self._entries = {}

    def get(self, item_id: str) -> TrackAnalysis | None:
        entry = self._entries.get(item_id)
        if not entry or time.time() - entry.get("ts", 0) > self._ttl:
            return None
        try:
            return TrackAnalysis(**entry["analysis"])
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, item_id: str, analysis: TrackAnalysis) -> None:
        self._entries[item_id] = {"ts": time.time(), "analysis": analysis.model_dump(mode="json")}
        self._dirty += 1
        if self._dirty >= 25:
            self.flush()

    def flush(self) -> None:
        if not self._dirty:
            return
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._entries), "utf-8")
            tmp.replace(self._path)
            self._dirty = 0
        except OSError as exc:
            log.warning("could not persist analysis cache: %s", exc)


class AudioMuseClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.enabled = settings.audiomuse_enabled and bool(settings.audiomuse_url)
        headers = {"Accept": "application/json"}
        if settings.audiomuse_api_key:
            headers["Authorization"] = f"Bearer {settings.audiomuse_api_key}"
            headers["X-API-Key"] = settings.audiomuse_api_key
        self._client = httpx.AsyncClient(
            base_url=settings.audiomuse_url.rstrip("/"),
            timeout=settings.audiomuse_timeout_seconds,
            headers=headers,
        )
        self.cache = AnalysisCache(settings.analysis_cache_file)
        self._unavailable_until = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        self.cache.flush()
        await self._client.aclose()

    @property
    def available(self) -> bool:
        return self.enabled and time.time() >= self._unavailable_until

    def _mark_unavailable(self, exc: Exception) -> None:
        # Back off for a minute so a dead AudioMuse does not slow down playout.
        if time.time() >= self._unavailable_until:
            log.warning("AudioMuse unavailable, backing off for 60 s: %s", exc)
        self._unavailable_until = time.time() + 60

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.available:
            return None
        try:
            response = await self._client.request(method, path, **kwargs)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                self._mark_unavailable(exc)
            return None
        except (httpx.HTTPError, ValueError) as exc:
            self._mark_unavailable(exc)
            return None

    async def ping(self) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "reason": "disabled"}
        for path in ("/api/health", "/health", "/healthz", "/"):
            try:
                response = await self._client.get(path)
                if response.status_code < 400:
                    return {"ok": True, "endpoint": path}
            except httpx.HTTPError:
                continue
        return {"ok": False, "reason": "unreachable"}

    async def analysis(self, item_id: str) -> TrackAnalysis | None:
        cached = self.cache.get(item_id)
        if cached is not None:
            return cached
        if not self.available:
            return None
        payload = await self._request("GET", self._settings.audiomuse_track_path.format(id=item_id))
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get("results"), list) and payload["results"]:
            payload = payload["results"][0]
        analysis = parse_analysis(payload)
        self.cache.put(item_id, analysis)
        return analysis

    async def analyse_many(self, item_ids: list[str], concurrency: int = 8) -> dict[str, TrackAnalysis]:
        """Fetch analysis for many tracks, bounded in parallel."""
        out: dict[str, TrackAnalysis] = {}
        pending = []
        for item_id in item_ids:
            cached = self.cache.get(item_id)
            if cached is not None:
                out[item_id] = cached
            else:
                pending.append(item_id)
        if not pending or not self.available:
            return out

        semaphore = asyncio.Semaphore(concurrency)

        async def fetch(item_id: str) -> None:
            async with semaphore:
                result = await self.analysis(item_id)
                if result is not None:
                    out[item_id] = result

        await asyncio.gather(*(fetch(i) for i in pending), return_exceptions=True)
        self.cache.flush()
        return out

    async def similar_tracks(self, item_id: str, limit: int = 50, radius: float = 0.6) -> list[str]:
        """Item ids of tracks that sound like ``item_id``, best match first."""
        payload = await self._request(
            "GET",
            self._settings.audiomuse_similar_path,
            params={"item_id": item_id, "id": item_id, "n": limit, "limit": limit, "radius": radius},
        )
        return _extract_ids(payload, limit)

    async def search_by_mood(self, moods: list[str], limit: int = 200, min_score: float = 0.5) -> list[str]:
        payload = await self._request(
            "GET",
            self._settings.audiomuse_search_path,
            params={
                "mood": ",".join(moods),
                "moods": ",".join(moods),
                "limit": limit,
                "n": limit,
                "min_score": min_score,
            },
        )
        return _extract_ids(payload, limit)


def _extract_ids(payload: Any, limit: int) -> list[str]:
    """Pull Jellyfin item ids out of any of the shapes AudioMuse may return."""
    if payload is None:
        return []
    if isinstance(payload, dict):
        for key in ("results", "items", "tracks", "data", "similar", "songs"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return []

    ids: list[str] = []
    for entry in payload:
        if isinstance(entry, str):
            ids.append(entry)
        elif isinstance(entry, dict):
            for key in ("item_id", "itemId", "id", "jellyfin_id", "track_id", "Id"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    ids.append(value)
                    break
        if len(ids) >= limit:
            break
    return ids
