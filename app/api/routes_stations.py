"""Station CRUD, quick-setup structs, control and live status."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from app.api.deps import CurrentUser, Library, Manager, Station, Store
from app.auth import User, create_stream_token, get_revocations, require_admin, user_may_access
from app.config import Settings, get_settings
from app.models import (
    AccessSpec,
    CrossfadeSpec,
    Daypart,
    RotationSpec,
    StationSpec,
    StationUpdate,
    StreamSpec,
    TrackFilters,
)
from app.presets import TEMPLATES, QUICK_BUILDERS
from app.security import TokenError, verify_token
from app.store import StationExists, StationPersistError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["stations"])


# ---------------------------------------------------------------------------
# Quick setup - the fast path to a new station
# ---------------------------------------------------------------------------


class QuickStation(BaseModel):
    """Create a station from a genre, mood, artist or seed list in one call."""

    kind: Literal["genre", "mood", "artist", "similar", "library"]
    name: str = Field(min_length=1, max_length=120)
    #: Genres, moods, artist names or seed tracks, depending on ``kind``.
    values: list[str] = Field(default_factory=list)
    template: str = "radio"
    station_id: str | None = None
    description: str = ""
    #: Widen an artist station to similar artists.
    include_similar: bool = True
    #: Any StationSpec field may be overridden here (crossfade, access, ...).
    overrides: dict[str, Any] = Field(default_factory=dict)


def _station_payload(spec: StationSpec, manager: Manager, base_url: str) -> dict[str, Any]:
    snapshot = manager.snapshot(spec.id)
    return {
        **spec.model_dump(mode="json"),
        "mount": spec.mount(),
        "stream_url": f"{base_url.rstrip('/')}{spec.mount()}",
        "now_playing": snapshot.model_dump(mode="json"),
    }


@router.get("/templates")
async def list_templates(user: CurrentUser) -> dict[str, Any]:
    """The station templates and source kinds available for quick setup."""
    return {
        "templates": [tpl.as_dict() for tpl in TEMPLATES.values()],
        "kinds": [
            {"kind": "genre", "label": "Genre", "values": "one or more genre names"},
            {"kind": "mood", "label": "Mood", "values": "AudioMuse mood names"},
            {"kind": "artist", "label": "Artist", "values": "artist names"},
            {"kind": "similar", "label": "Sounds like", "values": "seed tracks"},
            {"kind": "library", "label": "Whole library", "values": "optional search term"},
        ],
        "schema": {
            "station": StationSpec.model_json_schema(),
            "crossfade": CrossfadeSpec.model_json_schema(),
            "rotation": RotationSpec.model_json_schema(),
            "filters": TrackFilters.model_json_schema(),
            "stream": StreamSpec.model_json_schema(),
            "access": AccessSpec.model_json_schema(),
            "daypart": Daypart.model_json_schema(),
        },
    }


@router.post("/stations/quick", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
async def quick_create(payload: QuickStation, store: Store, manager: Manager) -> dict[str, Any]:
    builder = QUICK_BUILDERS[payload.kind]
    kwargs: dict[str, Any] = {
        "template": payload.template,
        "station_id": payload.station_id,
        "description": payload.description,
        **payload.overrides,
    }
    try:
        if payload.kind == "library":
            spec = builder(payload.name, search=payload.values[0] if payload.values else None, **kwargs)
        elif payload.kind == "artist":
            _require_values(payload)
            spec = builder(
                payload.name, payload.values, include_similar=payload.include_similar, **kwargs
            )
        else:
            _require_values(payload)
            spec = builder(payload.name, payload.values, **kwargs)
    except (ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    try:
        store.create(spec)
    except StationExists as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except StationPersistError as exc:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc)) from exc
    return _station_payload(spec, manager, str(manager.settings.base_url))


def _require_values(payload: QuickStation) -> None:
    if not payload.values:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"'{payload.kind}' stations need at least one value"
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("/stations")
async def list_stations(user: CurrentUser, store: Store, manager: Manager) -> list[dict[str, Any]]:
    base_url = manager.settings.base_url
    visible = [
        spec
        for spec in store.list()
        if user_may_access(user, spec) and (spec.access.visibility != "private" or user.role == "admin")
    ]
    return [_station_payload(spec, manager, base_url) for spec in visible]


@router.get("/stations/{station_id}")
async def get_station_detail(
    user: CurrentUser, station: Station, manager: Manager
) -> dict[str, Any]:
    _assert_access(user, station)
    return _station_payload(station, manager, manager.settings.base_url)


@router.post("/stations", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
async def create_station(spec: StationSpec, store: Store, manager: Manager) -> dict[str, Any]:
    try:
        store.create(spec)
    except StationExists as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except StationPersistError as exc:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc)) from exc
    return _station_payload(spec, manager, manager.settings.base_url)


@router.patch("/stations/{station_id}", dependencies=[Depends(require_admin)])
async def update_station(
    station_id: str, payload: StationUpdate, store: Store, manager: Manager, library: Library
) -> dict[str, Any]:
    if not store.exists(station_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no station '{station_id}'")
    try:
        spec = store.update(station_id, payload)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except StationPersistError as exc:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc)) from exc
    library.invalidate(spec.id)
    await manager.reload_station(spec)
    return _station_payload(spec, manager, manager.settings.base_url)


@router.delete(
    "/stations/{station_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(require_admin)],
)
async def delete_station(station_id: str, store: Store, manager: Manager) -> Response:
    if not store.exists(station_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no station '{station_id}'")
    await manager.stop_station(station_id)
    try:
        store.delete(station_id)
    except StationPersistError as exc:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Control and status
# ---------------------------------------------------------------------------


@router.post("/stations/{station_id}/start", dependencies=[Depends(require_admin)])
async def start_station(station: Station, manager: Manager) -> dict[str, Any]:
    engine = await manager.ensure_running(station.id)
    return engine.snapshot().model_dump(mode="json")


@router.post("/stations/{station_id}/stop", dependencies=[Depends(require_admin)])
async def stop_station(station: Station, manager: Manager) -> dict[str, str]:
    await manager.stop_station(station.id)
    return {"status": "stopped"}


@router.post("/stations/{station_id}/skip", dependencies=[Depends(require_admin)])
async def skip_track(station: Station, manager: Manager) -> dict[str, str]:
    engine = manager.engine(station.id)
    if engine is None or not engine.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "station is not playing")
    engine.skip()
    return {"status": "skipping"}


@router.post("/stations/{station_id}/refresh", dependencies=[Depends(require_admin)])
async def refresh_pool(station: Station, library: Library) -> dict[str, Any]:
    """Re-query Jellyfin/AudioMuse for this station's candidate tracks."""
    pool = await library.pool_for(station, force_refresh=True)
    unique = {t.id for t in pool}
    analysed = sum(1 for t in {t.id: t for t in pool}.values() if t.analysis.source != "none")
    return {"tracks": len(unique), "analysed": analysed}


@router.get("/stations/{station_id}/nowplaying")
async def now_playing(user: CurrentUser, station: Station, manager: Manager) -> dict[str, Any]:
    _assert_access(user, station)
    return manager.snapshot(station.id).model_dump(mode="json")


@router.get("/stations/{station_id}/queue")
async def queue(user: CurrentUser, station: Station, manager: Manager, library: Library) -> dict[str, Any]:
    _assert_access(user, station)
    engine = manager.engine(station.id)
    if engine is None:
        return {"queue": [], "pool_size": len(await library.pool_for(station))}
    pool = await library.pool_for(station)
    return {
        "queue": [
            {"track": q.track.model_dump(mode="json"), "reason": q.reason}
            for q in engine.scheduler.fill_queue(pool)
        ],
        "pool_size": len({t.id for t in pool}),
    }


@router.get("/stations/{station_id}/listeners", dependencies=[Depends(require_admin)])
async def listeners(station: Station, manager: Manager) -> dict[str, Any]:
    engine = manager.engine(station.id)
    return {"listeners": engine.broadcaster.listeners() if engine else []}


@router.get("/stations/{station_id}/events")
async def station_events(
    request: Request, user: CurrentUser, station: Station, manager: Manager
) -> StreamingResponse:
    """Server-sent events with the live now-playing state (used by the hub)."""
    _assert_access(user, station)

    async def stream() -> Any:
        previous: str | None = None
        while not await request.is_disconnected():
            payload = json.dumps(manager.snapshot(station.id).model_dump(mode="json"), default=str)
            if payload != previous:
                previous = payload
                yield f"data: {payload}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Stream tokens
# ---------------------------------------------------------------------------


class TokenRequest(BaseModel):
    #: None = a key valid for every station this user may listen to.
    station_id: str | None = None
    ttl_hours: int | None = Field(default=None, ge=1, le=87600)


@router.post("/tokens")
async def mint_token(
    payload: TokenRequest,
    user: CurrentUser,
    store: Store,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Mint a tuner URL token for a radio player."""
    if payload.station_id:
        station = store.get(payload.station_id)
        _assert_access(user, station)
        url = f"{settings.base_url.rstrip('/')}{station.mount()}"
    else:
        url = None
    token = create_stream_token(user, settings, payload.station_id, payload.ttl_hours)
    return {
        "token": token,
        "station_id": payload.station_id,
        "stream_url": f"{url}?token={token}" if url else None,
        "expires_in_hours": payload.ttl_hours or settings.stream_token_ttl_hours,
    }


@router.delete("/tokens")
async def revoke_token(
    request: Request,
    payload: dict[str, str],
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Revoke a previously minted stream token."""
    token = payload.get("token", "")
    try:
        claims = verify_token(token, settings.secret_key)
    except TokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"not a valid token: {exc}") from exc
    if claims.get("sub") != user.username and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "that token belongs to another user")
    get_revocations(request).revoke(str(claims.get("jti")), {"exp": claims.get("exp")})
    return {"status": "revoked"}


# ---------------------------------------------------------------------------
# Library helpers (autocomplete for the station editor)
# ---------------------------------------------------------------------------


@router.get("/library/genres")
async def library_genres(user: CurrentUser, library: Library) -> list[str]:
    return await library.jellyfin.genres()


@router.get("/library/artists")
async def library_artists(user: CurrentUser, library: Library, q: str = "", limit: int = 50) -> list[dict[str, Any]]:
    return await library.jellyfin.search_artists(q, limit)


@router.get("/library/playlists")
async def library_playlists(user: CurrentUser, library: Library) -> list[dict[str, Any]]:
    return await library.jellyfin.playlists()


@router.get("/library/moods")
async def library_moods(user: CurrentUser, library: Library) -> list[str]:
    """Mood names seen in the analysis cache, plus the usual AudioMuse set."""
    known = {
        "calm", "warm", "dark", "energetic", "happy", "sad", "aggressive",
        "relaxed", "party", "acoustic", "electronic", "melancholic", "uplifting",
    }
    for entry in getattr(library.audiomuse.cache, "_entries", {}).values():
        known.update((entry.get("analysis") or {}).get("moods", {}).keys())
    return sorted(known)


def _assert_access(user: User, station: StationSpec) -> None:
    if not user_may_access(user, station):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you may not access this station")


@router.get("/artwork/{item_id}")
async def artwork(user: CurrentUser, library: Library, item_id: str) -> Any:
    """Proxy album art from Jellyfin so browsers need no Jellyfin access."""
    from fastapi.responses import Response as RawResponse

    import httpx

    url = library.jellyfin.artwork_url(item_id)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            upstream = await client.get(url)
        if upstream.status_code != 200:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no artwork")
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"artwork unavailable: {exc}") from exc
    return RawResponse(
        content=upstream.content,
        media_type=upstream.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=86400"},
    )
