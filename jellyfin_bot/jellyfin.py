"""Minimal async Jellyfin client covering everything the music bot needs."""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any, Sequence

import aiohttp

log = logging.getLogger(__name__)

CLIENT_NAME = "Discord Jellyfin Music Bot"
CLIENT_VERSION = "1.0.0"
TICKS_PER_SECOND = 10_000_000

AUDIO_FIELDS = (
    "Id,Name,RunTimeTicks,Album,AlbumId,AlbumArtist,Artists,ArtistItems,"
    "IndexNumber,ParentIndexNumber,ImageTags,AlbumPrimaryImageTag,MediaSources"
)


class JellyfinError(RuntimeError):
    """Raised when Jellyfin returns something unusable."""


@dataclass(slots=True)
class Track:
    """A single playable audio item."""

    id: str
    name: str
    artist: str
    album: str
    duration: float  # seconds; 0 when unknown
    stream_url: str
    art_url: str | None = None
    requester_id: int | None = None
    requester_name: str | None = None

    @property
    def label(self) -> str:
        return f"{self.artist} — {self.name}" if self.artist else self.name


@dataclass(slots=True)
class Item:
    """A non-audio Jellyfin item (album, artist, playlist)."""

    id: str
    name: str
    type: str
    subtitle: str = ""


def ticks_to_seconds(ticks: Any) -> float:
    try:
        return max(0.0, float(ticks) / TICKS_PER_SECOND)
    except (TypeError, ValueError):
        return 0.0


def seconds_to_ticks(seconds: float) -> int:
    return int(max(0.0, seconds) * TICKS_PER_SECOND)


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _join_artists(data: dict[str, Any]) -> str:
    artists = data.get("Artists") or []
    if artists:
        return ", ".join(str(a) for a in artists if a)
    return str(data.get("AlbumArtist") or "")


class JellyfinClient:
    """Thin wrapper around the Jellyfin HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        user_id: str | None = None,
        device_id: str = "discord-jellyfin-bot",
        direct_stream: bool = True,
        max_bitrate: int = 320_000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = api_key
        self.username = username
        self.password = password
        self.user_id = user_id
        self.device_id = device_id
        self.direct_stream = direct_stream
        self.max_bitrate = max_bitrate
        self.server_name: str | None = None
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------ setup

    async def connect(self) -> None:
        """Open the HTTP session, authenticate if needed and resolve the user."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                raise_for_status=False,
            )

        if not self.token:
            await self._authenticate_by_name()

        info = await self._request("GET", "/System/Info/Public", authed=False)
        self.server_name = (info or {}).get("ServerName")

        if not self.user_id:
            self.user_id = await self._resolve_user_id()

        log.info(
            "Connected to Jellyfin %r as user %s",
            self.server_name or self.base_url,
            self.user_id,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _authenticate_by_name(self) -> None:
        if not self.username:
            raise JellyfinError("No API key and no username configured")
        payload = {"Username": self.username, "Pw": self.password or ""}
        data = await self._request(
            "POST", "/Users/AuthenticateByName", json=payload, authed=False
        )
        token = (data or {}).get("AccessToken")
        if not token:
            raise JellyfinError("Jellyfin did not return an access token")
        self.token = token
        self.user_id = self.user_id or (data.get("User") or {}).get("Id")

    async def _resolve_user_id(self) -> str:
        # With an admin API key /Users lists everyone; with a user token
        # /Users/Me is the reliable endpoint.
        me = await self._request("GET", "/Users/Me", allow_error=True)
        if isinstance(me, dict) and me.get("Id"):
            return str(me["Id"])

        users = await self._request("GET", "/Users", allow_error=True)
        if isinstance(users, list) and users:
            return str(users[0]["Id"])

        raise JellyfinError(
            "Could not determine a Jellyfin user id; set JELLYFIN_USER_ID explicitly"
        )

    # ---------------------------------------------------------------- plumbing

    def _auth_header(self) -> str:
        parts = [
            f'Client="{CLIENT_NAME}"',
            'Device="Discord"',
            f'DeviceId="{self.device_id}"',
            f'Version="{CLIENT_VERSION}"',
        ]
        if self.token:
            parts.append(f'Token="{self.token}"')
        return "MediaBrowser " + ", ".join(parts)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        authed: bool = True,
        allow_error: bool = False,
    ) -> Any:
        if self._session is None or self._session.closed:
            raise JellyfinError("Jellyfin client is not connected")

        headers = {"Accept": "application/json"}
        if authed:
            headers["Authorization"] = self._auth_header()

        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )
        url = f"{self.base_url}{path}"

        async with self._session.request(
            method, url, params=clean_params, json=json, headers=headers
        ) as resp:
            if resp.status >= 400:
                body = (await resp.text())[:300]
                if allow_error:
                    log.debug("Jellyfin %s %s -> %s: %s", method, path, resp.status, body)
                    return None
                raise JellyfinError(f"Jellyfin {method} {path} failed ({resp.status}): {body}")
            if resp.status == 204 or not resp.content_length:
                # Some endpoints (playback reporting) answer with an empty body.
                text = await resp.text()
                if not text.strip():
                    return None
                return _loads(text)
            return _loads(await resp.text())

    # ------------------------------------------------------------------ lookup

    async def search(
        self,
        term: str,
        *,
        item_types: Sequence[str] = ("Audio",),
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            "/Items",
            params={
                "userId": self.user_id,
                "searchTerm": term,
                "includeItemTypes": ",".join(item_types),
                "recursive": "true",
                "limit": limit,
                "fields": AUDIO_FIELDS,
                "enableTotalRecordCount": "false",
            },
        )
        return list((data or {}).get("Items") or [])

    async def search_tracks(self, term: str, *, limit: int = 25) -> list[Track]:
        return [self.to_track(i) for i in await self.search(term, limit=limit)]

    async def search_items(
        self, term: str, item_type: str, *, limit: int = 25
    ) -> list[Item]:
        raw = await self.search(term, item_types=(item_type,), limit=limit)
        return [
            Item(
                id=str(i["Id"]),
                name=str(i.get("Name") or "Unknown"),
                type=str(i.get("Type") or item_type),
                subtitle=str(i.get("AlbumArtist") or _join_artists(i) or ""),
            )
            for i in raw
            if i.get("Id")
        ]

    async def get_item(self, item_id: str) -> dict[str, Any] | None:
        data = await self._request(
            "GET",
            "/Items",
            params={
                "userId": self.user_id,
                "ids": item_id,
                "fields": AUDIO_FIELDS,
            },
            allow_error=True,
        )
        items = (data or {}).get("Items") or []
        return items[0] if items else None

    async def _items(self, params: dict[str, Any]) -> list[Track]:
        base = {
            "userId": self.user_id,
            "includeItemTypes": "Audio",
            "recursive": "true",
            "fields": AUDIO_FIELDS,
            "enableTotalRecordCount": "false",
            "limit": 500,
        }
        base.update(params)
        data = await self._request("GET", "/Items", params=base)
        return [self.to_track(i) for i in (data or {}).get("Items") or []]

    async def album_tracks(self, album_id: str) -> list[Track]:
        return await self._items(
            {"parentId": album_id, "sortBy": "ParentIndexNumber,IndexNumber,SortName"}
        )

    async def artist_tracks(self, artist_id: str, *, limit: int = 200) -> list[Track]:
        return await self._items(
            {
                "artistIds": artist_id,
                "sortBy": "Album,ParentIndexNumber,IndexNumber",
                "limit": limit,
            }
        )

    async def playlist_tracks(self, playlist_id: str) -> list[Track]:
        data = await self._request(
            "GET",
            f"/Playlists/{playlist_id}/Items",
            params={"userId": self.user_id, "fields": AUDIO_FIELDS, "limit": 1000},
        )
        return [
            self.to_track(i)
            for i in (data or {}).get("Items") or []
            if (i.get("Type") or "Audio") == "Audio"
        ]

    async def favorite_tracks(self, *, limit: int = 200) -> list[Track]:
        return await self._items(
            {"filters": "IsFavorite", "sortBy": "Random", "limit": limit}
        )

    async def instant_mix(self, item_id: str, *, limit: int = 50) -> list[Track]:
        data = await self._request(
            "GET",
            f"/Items/{item_id}/InstantMix",
            params={"userId": self.user_id, "limit": limit, "fields": AUDIO_FIELDS},
        )
        return [self.to_track(i) for i in (data or {}).get("Items") or []]

    async def random_tracks(self, *, limit: int = 50) -> list[Track]:
        return await self._items({"sortBy": "Random", "limit": limit})

    # ------------------------------------------------------------------ tracks

    def to_track(self, data: dict[str, Any]) -> Track:
        item_id = str(data["Id"])
        return Track(
            id=item_id,
            name=str(data.get("Name") or "Unknown title"),
            artist=_join_artists(data),
            album=str(data.get("Album") or ""),
            duration=ticks_to_seconds(data.get("RunTimeTicks")),
            stream_url=self.stream_url(item_id),
            art_url=self.image_url(data),
        )

    def stream_url(self, item_id: str) -> str:
        """Build a URL ffmpeg can read directly."""
        if self.direct_stream:
            query = {"static": "true", "api_key": self.token or ""}
            return f"{self.base_url}/Audio/{item_id}/stream?" + urllib.parse.urlencode(query)

        query = {
            "UserId": self.user_id or "",
            "DeviceId": self.device_id,
            "api_key": self.token or "",
            "Container": "opus,mp3,aac,m4a,flac,alac,wav,ogg,webm",
            "AudioCodec": "aac",
            "TranscodingContainer": "ts",
            "TranscodingProtocol": "http",
            "MaxStreamingBitrate": str(self.max_bitrate),
            "EnableRedirection": "true",
        }
        return f"{self.base_url}/Audio/{item_id}/universal?" + urllib.parse.urlencode(query)

    def image_url(self, data: dict[str, Any], *, max_size: int = 300) -> str | None:
        tags = data.get("ImageTags") or {}
        if tags.get("Primary"):
            item_id = data.get("Id")
            tag = tags["Primary"]
        elif data.get("AlbumPrimaryImageTag") and data.get("AlbumId"):
            item_id = data["AlbumId"]
            tag = data["AlbumPrimaryImageTag"]
        else:
            return None
        query = urllib.parse.urlencode(
            {"tag": tag, "maxHeight": max_size, "maxWidth": max_size, "quality": 90}
        )
        return f"{self.base_url}/Items/{item_id}/Images/Primary?{query}"

    # -------------------------------------------------------- playback reports

    def _play_method(self) -> str:
        return "DirectStream" if self.direct_stream else "Transcode"

    async def report_start(self, track: Track) -> None:
        await self._report(
            "/Sessions/Playing",
            {
                "ItemId": track.id,
                "PositionTicks": 0,
                "IsPaused": False,
                "CanSeek": True,
                "PlayMethod": self._play_method(),
            },
        )

    async def report_progress(self, track: Track, position: float, paused: bool) -> None:
        await self._report(
            "/Sessions/Playing/Progress",
            {
                "ItemId": track.id,
                "PositionTicks": seconds_to_ticks(position),
                "IsPaused": paused,
                "CanSeek": True,
                "PlayMethod": self._play_method(),
            },
        )

    async def report_stop(self, track: Track, position: float) -> None:
        await self._report(
            "/Sessions/Playing/Stopped",
            {"ItemId": track.id, "PositionTicks": seconds_to_ticks(position)},
        )

    async def _report(self, path: str, payload: dict[str, Any]) -> None:
        try:
            await self._request("POST", path, json=payload, allow_error=True)
        except Exception:  # pragma: no cover - reporting is best effort
            log.debug("Playback report to %s failed", path, exc_info=True)


def _loads(text: str) -> Any:
    import json as _json

    try:
        return _json.loads(text)
    except ValueError as exc:
        raise JellyfinError(f"Jellyfin returned invalid JSON: {text[:200]}") from exc
