"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.auth import User, current_user
from app.library import MusicLibrary
from app.manager import StationManager
from app.models import StationSpec
from app.store import StationNotFound, StationStore


def get_store(request: Request) -> StationStore:
    return request.app.state.store


def get_manager(request: Request) -> StationManager:
    return request.app.state.manager


def get_library(request: Request) -> MusicLibrary:
    return request.app.state.library


def get_station(
    station_id: str, store: Annotated[StationStore, Depends(get_store)]
) -> StationSpec:
    try:
        return store.get(station_id)
    except StationNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no station '{station_id}'") from exc


CurrentUser = Annotated[User, Depends(current_user)]
Store = Annotated[StationStore, Depends(get_store)]
Manager = Annotated[StationManager, Depends(get_manager)]
Library = Annotated[MusicLibrary, Depends(get_library)]
Station = Annotated[StationSpec, Depends(get_station)]
