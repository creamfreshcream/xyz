"""The actual audio endpoints.

Authenticated by default: a listener needs a stream token in the URL, a session
cookie, a bearer token or HTTP Basic credentials. Only a station explicitly set
to ``visibility: public`` serves audio without any of them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import Manager, Store
from app.audio.broadcast import ICY_METAINT, icy_wrap
from app.auth import (
    RevocationStore,
    User,
    UserStore,
    authenticate_stream_request,
    get_revocations,
    get_user_store,
    user_may_access,
)
from app.config import Settings, get_settings
from app.models import StationSpec
from app.store import StationNotFound

log = logging.getLogger(__name__)

router = APIRouter(tags=["stream"])


def _resolve(store: Store, station_id: str, extension: str) -> StationSpec:
    try:
        station = store.get(station_id)
    except StationNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no station '{station_id}'") from exc
    if not station.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, f"station '{station_id}' is disabled")
    if extension != station.stream.file_extension:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"station '{station_id}' broadcasts as .{station.stream.file_extension}",
        )
    return station


def _authorise(
    request: Request,
    station: StationSpec,
    settings: Settings,
    users: UserStore,
    revocations: RevocationStore,
) -> User | None:
    if not station.access.requires_auth:
        return None
    user = authenticate_stream_request(request, station.id, settings, users, revocations)
    if not user_may_access(user, station):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you may not listen to this station")
    return user


def _headers(station: StationSpec, with_metadata: bool) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
        "Connection": "close",
        "icy-name": station.name,
        "icy-description": station.description or station.name,
        "icy-genre": station.genre_tag or "Various",
        "icy-br": str(station.stream.bitrate),
        "icy-pub": "0",
        "Accept-Ranges": "none",
    }
    if with_metadata:
        headers["icy-metaint"] = str(ICY_METAINT)
    return headers


@router.head("/stream/{station_id}.{extension}")
async def stream_head(
    station_id: str,
    extension: str,
    request: Request,
    store: Store,
    settings: Annotated[Settings, Depends(get_settings)],
    users: Annotated[UserStore, Depends(get_user_store)],
    revocations: Annotated[RevocationStore, Depends(get_revocations)],
) -> Response:
    """Players probe with HEAD before opening the stream."""
    station = _resolve(store, station_id, extension)
    _authorise(request, station, settings, users, revocations)
    return Response(status_code=200, media_type=station.stream.content_type, headers=_headers(station, False))


@router.get("/stream/{station_id}.{extension}")
async def stream_station(
    station_id: str,
    extension: str,
    request: Request,
    store: Store,
    manager: Manager,
    settings: Annotated[Settings, Depends(get_settings)],
    users: Annotated[UserStore, Depends(get_user_store)],
    revocations: Annotated[RevocationStore, Depends(get_revocations)],
) -> StreamingResponse:
    station = _resolve(store, station_id, extension)
    user = _authorise(request, station, settings, users, revocations)
    username = user.username if user else "anonymous"

    limit = station.stream.max_listeners or settings.max_listeners_per_station
    if manager.listener_count(station.id) >= limit:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "station is at its listener limit")

    try:
        engine = await manager.ensure_running(station.id)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    wants_metadata = station.stream.icy_metadata and request.headers.get("icy-metadata") == "1"
    listener = await engine.broadcaster.add(
        username,
        request.client.host if request.client else "unknown",
        request.headers.get("user-agent", ""),
    )

    async def audio() -> AsyncIterator[bytes]:
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(listener.queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    # The engine went quiet - stop rather than hold the socket.
                    log.warning("station %s: no audio for 30 s, dropping listener", station.id)
                    break
                if chunk is None:
                    break
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            raise
        finally:
            await engine.broadcaster.remove(listener)

    source: AsyncIterator[bytes] = audio()
    if wants_metadata:
        source = icy_wrap(source, engine.current_title)

    return StreamingResponse(
        source,
        media_type=station.stream.content_type,
        headers=_headers(station, wants_metadata),
    )
