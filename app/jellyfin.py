"""Jellyfin client - the music library and the audio source."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.config import Settings
from app.models import Track

log = logging.getLogger(__name__)

ITEM_FIELDS = "Genres,RunTimeTicks,ProductionYear,AlbumArtist,ArtistItems,Path,CommunityRating,AlbumId"
TICKS_PER_SECOND = 10_000_000


class JellyfinError(RuntimeError):
    pass


def _to_track(item: dict[str, Any]) -> Track:
    artists = [a for a in (item.get("Artists") or []) if a]
    if not artists:
        artists = [a.get("Name", "") for a in (item.get("ArtistItems") or []) if a.get("Name")]
    return Track(
        id=item.get("Id", ""),
        name=item.get("Name", "Unknown"),
        artist=artists[0] if artists else (item.get("AlbumArtist") or "Unknown Artist"),
        artists=artists,
        album=item.get("Album", "") or "",
        album_id=item.get("AlbumId", "") or "",
        album_artist=item.get("AlbumArtist", "") or "",
        genres=[g for g in (item.get("Genres") or []) if g],
        duration_seconds=float(item.get("RunTimeTicks") or 0) / TICKS_PER_SECOND,
        year=item.get("ProductionYear"),
        community_rating=item.get("CommunityRating"),
    )


class JellyfinClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base = settings.jellyfin_url.rstrip("/")
        self._user_id = settings.jellyfin_user_id or ""
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=settings.jellyfin_timeout_seconds,
            headers={
                "X-Emby-Token": settings.jellyfin_api_key,
                "Accept": "application/json",
                "X-Emby-Authorization": (
                    'MediaBrowser Client="Jellyfin Radio", Device="radio", '
                    'DeviceId="jellyfin-radio", Version="1.0.0"'
                ),
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- plumbing ----------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.get(path, params=params or {})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise JellyfinError(
                f"Jellyfin {exc.response.status_code} on {path}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise JellyfinError(f"Jellyfin unreachable ({path}): {exc}") from exc
        if not response.content:
            return {}
        return response.json()

    async def user_id(self) -> str:
        if self._user_id:
            return self._user_id
        users = await self._get("/Users")
        if not users:
            raise JellyfinError("no Jellyfin users visible with this API key")
        self._user_id = users[0]["Id"]
        log.info("using Jellyfin user '%s' (%s)", users[0].get("Name"), self._user_id)
        return self._user_id

    async def ping(self) -> dict[str, Any]:
        info = await self._get("/System/Info/Public")
        return {
            "ok": True,
            "server": info.get("ServerName"),
            "version": info.get("Version"),
            "user_id": await self.user_id(),
        }

    # -- queries -----------------------------------------------------------

    async def _items(self, params: dict[str, Any]) -> list[Track]:
        base: dict[str, Any] = {
            "userId": await self.user_id(),
            "IncludeItemTypes": "Audio",
            "Recursive": "true",
            "Fields": ITEM_FIELDS,
            "EnableTotalRecordCount": "false",
        }
        if self._settings.jellyfin_library_id:
            base["ParentId"] = self._settings.jellyfin_library_id
        base.update(params)
        data = await self._get("/Items", base)
        return [_to_track(item) for item in data.get("Items", [])]

    async def by_genres(self, genres: list[str], limit: int, match_all: bool = False) -> list[Track]:
        if match_all:
            # Jellyfin's Genres filter is OR; intersect client-side for AND.
            wanted = {g.lower() for g in genres}
            pool = await self._items({"Genres": "|".join(genres), "Limit": limit * 4, "SortBy": "Random"})
            return [t for t in pool if wanted <= {g.lower() for g in t.genres}][:limit]
        return await self._items({"Genres": "|".join(genres), "Limit": limit, "SortBy": "Random"})

    async def by_artists(self, artists: list[str], limit: int, include_appearances: bool = True) -> list[Track]:
        key = "Artists" if include_appearances else "AlbumArtists"
        return await self._items({key: "|".join(artists), "Limit": limit, "SortBy": "Random"})

    async def by_ids(self, item_ids: list[str]) -> list[Track]:
        if not item_ids:
            return []
        tracks: list[Track] = []
        # Keep URLs sane on very large id lists.
        for start in range(0, len(item_ids), 100):
            chunk = item_ids[start : start + 100]
            tracks.extend(await self._items({"Ids": ",".join(chunk), "Limit": len(chunk)}))
        return tracks

    async def search(self, term: str, limit: int) -> list[Track]:
        return await self._items({"searchTerm": term, "Limit": limit, "SortBy": "Random"})

    async def all_tracks(self, limit: int) -> list[Track]:
        return await self._items({"Limit": limit, "SortBy": "Random"})

    async def similar_items(self, item_id: str, limit: int = 25) -> list[Track]:
        data = await self._get(
            f"/Items/{quote(item_id)}/Similar",
            {"userId": await self.user_id(), "limit": limit, "Fields": ITEM_FIELDS},
        )
        return [_to_track(item) for item in data.get("Items", [])]

    async def genres(self, limit: int = 500) -> list[str]:
        params: dict[str, Any] = {
            "userId": await self.user_id(),
            "IncludeItemTypes": "Audio",
            "Recursive": "true",
            "Limit": limit,
            "SortBy": "SortName",
        }
        if self._settings.jellyfin_library_id:
            params["ParentId"] = self._settings.jellyfin_library_id
        data = await self._get("/Genres", params)
        return [item.get("Name", "") for item in data.get("Items", []) if item.get("Name")]

    async def search_artists(self, term: str = "", limit: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"userId": await self.user_id(), "Limit": limit, "SortBy": "SortName"}
        if term:
            params["searchTerm"] = term
        if self._settings.jellyfin_library_id:
            params["ParentId"] = self._settings.jellyfin_library_id
        data = await self._get("/Artists", params)
        return [{"id": i.get("Id"), "name": i.get("Name")} for i in data.get("Items", [])]

    async def similar_artists(self, artist_name: str, limit: int = 25) -> list[str]:
        matches = await self.search_artists(artist_name, limit=1)
        if not matches:
            return []
        data = await self._get(
            f"/Artists/{quote(str(matches[0]['id']))}/Similar",
            {"userId": await self.user_id(), "limit": limit},
        )
        return [i.get("Name", "") for i in data.get("Items", []) if i.get("Name")]

    async def playlists(self) -> list[dict[str, Any]]:
        data = await self._get(
            "/Items",
            {
                "userId": await self.user_id(),
                "IncludeItemTypes": "Playlist",
                "Recursive": "true",
                "SortBy": "SortName",
            },
        )
        return [{"id": i.get("Id"), "name": i.get("Name")} for i in data.get("Items", [])]

    async def playlist_tracks(self, playlist: str, limit: int = 500) -> list[Track]:
        playlist_id = playlist
        if not _looks_like_id(playlist):
            for entry in await self.playlists():
                if str(entry["name"]).lower() == playlist.lower():
                    playlist_id = str(entry["id"])
                    break
            else:
                log.warning("playlist '%s' not found", playlist)
                return []
        data = await self._get(
            f"/Playlists/{quote(playlist_id)}/Items",
            {"userId": await self.user_id(), "Fields": ITEM_FIELDS, "Limit": limit},
        )
        return [_to_track(item) for item in data.get("Items", [])]

    # -- playback ----------------------------------------------------------

    def stream_url(self, item_id: str) -> str:
        """Direct, untranscoded stream of the original file - ffmpeg decodes it."""
        query = urlencode(
            {"static": "true", "api_key": self._settings.jellyfin_api_key, "deviceId": "jellyfin-radio"}
        )
        return f"{self._base}/Audio/{quote(item_id)}/stream?{query}"

    def artwork_url(self, item_id: str, max_height: int = 480) -> str:
        query = urlencode({"maxHeight": max_height, "quality": 90})
        return f"{self._base}/Items/{quote(item_id)}/Images/Primary?{query}"

    async def report_playback(self, item_id: str, station_name: str) -> None:
        """Best-effort play reporting so Jellyfin play counts stay meaningful."""
        if not self._settings.jellyfin_report_playback:
            return
        try:
            await self._client.post(
                "/Sessions/Playing",
                json={
                    "ItemId": item_id,
                    "PlayMethod": "DirectStream",
                    "PositionTicks": 0,
                    "CanSeek": False,
                    "IsPaused": False,
                    "PlaySessionId": f"radio-{station_name}",
                },
            )
        except httpx.HTTPError as exc:
            log.debug("playback report failed for %s: %s", item_id, exc)


def _looks_like_id(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value)
