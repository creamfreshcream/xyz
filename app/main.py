"""Application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import routes_auth, routes_hub, routes_stations, routes_stream
from app.audiomuse import AudioMuseClient
from app.auth import RevocationStore, UserStore
from app.config import get_settings
from app.jellyfin import JellyfinClient
from app.library import MusicLibrary
from app.manager import StationManager
from app.store import StationStore

log = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("%s %s starting", settings.app_name, __version__)

    if settings.secret_key.startswith("insecure-") or settings.secret_key == "change-me-openssl-rand-hex-32":
        log.warning(
            "RADIO_SECRET_KEY is not set - sessions and stream tokens are not secure. "
            "Generate one with: openssl rand -hex 32"
        )
    if not settings.jellyfin_api_key:
        log.warning("RADIO_JELLYFIN_API_KEY is not set - no music can be loaded")

    users = UserStore(settings.users_file)
    users.bootstrap_admin(settings.admin_username, settings.admin_password)

    jellyfin = JellyfinClient(settings)
    audiomuse = AudioMuseClient(settings)
    library = MusicLibrary(jellyfin, audiomuse)
    store = StationStore(settings.stations_file)
    manager = StationManager(store, library, settings)

    app.state.settings = settings
    app.state.users = users
    app.state.revocations = RevocationStore(settings.revocations_file)
    app.state.jellyfin = jellyfin
    app.state.audiomuse = audiomuse
    app.state.library = library
    app.state.store = store
    app.state.manager = manager

    await manager.startup()
    try:
        yield
    finally:
        log.info("shutting down")
        await manager.shutdown()
        await audiomuse.aclose()
        await jellyfin.aclose()


app = FastAPI(
    title="Jellyfin Radio",
    description="A self-hosted internet radio station on top of Jellyfin and AudioMuse.",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(routes_auth.router)
app.include_router(routes_stations.router)
app.include_router(routes_stream.router)
app.include_router(routes_hub.router)

_static_dir = Path(__file__).resolve().parent / "web" / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, Any]:
    """Liveness only - no auth, no upstream calls."""
    return {"status": "ok", "version": __version__}


@app.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    """Readiness: are Jellyfin and AudioMuse actually reachable?"""
    library: MusicLibrary = request.app.state.library
    manager: StationManager = request.app.state.manager
    upstream = await library.health()
    return {
        "status": "ok" if upstream["jellyfin"].get("ok") else "degraded",
        "version": __version__,
        **upstream,
        "stations": len(request.app.state.store.list()),
        "running": [
            station_id
            for station_id in (s.id for s in request.app.state.store.list())
            if (engine := manager.engine(station_id)) and engine.running
        ],
    }


@app.exception_handler(404)
async def not_found(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": "not found", "path": request.url.path}, status_code=404)
