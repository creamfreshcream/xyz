"""Build a Liquidsoap playlist out of a Jellyfin music library.

Every entry is an ``annotate:`` URI, so Liquidsoap gets title/artist/album for the
Icecast metadata without having to probe the remote file first:

    annotate:title="Blue Monday",artist="New Order":http://jellyfin:8096/Audio/<id>/stream?...

The script runs as a sidecar next to Liquidsoap: it rewrites the playlist file every
``RADIO_REFRESH_INTERVAL`` seconds and Liquidsoap picks it up via ``reload_mode="watch"``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

log = logging.getLogger("playlist-sync")

USER_AGENT = "jellyfin-radio-playlist-sync/1.0"
FIELDS = "Genres,RunTimeTicks,ProductionYear,AlbumArtist,Album"


class ConfigError(Exception):
    """Raised for a configuration problem the user has to fix."""


@dataclass(frozen=True)
class Config:
    jellyfin_url: str
    api_key: str
    user_id: str
    playlist_file: str
    limit: int
    interval: int
    favorites_only: bool
    playlist_name: str
    genres: tuple[str, ...]
    years: str
    stream_mode: str
    max_bitrate: int
    transcode_codec: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env

        url = env.get("JELLYFIN_URL", "").strip().rstrip("/")
        if not url:
            raise ConfigError("JELLYFIN_URL is required")
        api_key = env.get("JELLYFIN_API_KEY", "").strip()
        if not api_key:
            raise ConfigError("JELLYFIN_API_KEY is required")

        stream_mode = env.get("RADIO_STREAM_MODE", "direct").strip().lower()
        if stream_mode not in ("direct", "transcode"):
            raise ConfigError("RADIO_STREAM_MODE must be 'direct' or 'transcode'")

        genres = tuple(
            g.strip() for g in env.get("RADIO_GENRES", "").split(",") if g.strip()
        )

        return cls(
            jellyfin_url=url,
            api_key=api_key,
            user_id=env.get("JELLYFIN_USER_ID", "").strip(),
            playlist_file=env.get("RADIO_PLAYLIST_FILE", "/playlists/radio.m3u"),
            limit=_int(env, "RADIO_LIMIT", 2000),
            interval=_int(env, "RADIO_REFRESH_INTERVAL", 3600),
            favorites_only=_bool(env, "RADIO_FAVORITES_ONLY", False),
            playlist_name=env.get("RADIO_JELLYFIN_PLAYLIST", "").strip(),
            genres=genres,
            years=env.get("RADIO_YEARS", "").strip(),
            stream_mode=stream_mode,
            max_bitrate=_int(env, "RADIO_MAX_BITRATE", 320000),
            transcode_codec=env.get("RADIO_TRANSCODE_CODEC", "mp3").strip(),
        )


def _int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class Jellyfin:
    """Just enough of the Jellyfin HTTP API to list audio items."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "", [])}
        )
        url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"
        request = urllib.request.Request(
            url,
            headers={
                "X-Emby-Token": self.api_key,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def resolve_playlist_id(self, name: str) -> str:
        """Look up a Jellyfin playlist by (case-insensitive) name."""
        data = self.get(
            "/Items",
            {
                "IncludeItemTypes": "Playlist",
                "Recursive": "true",
                "SearchTerm": name,
                "Limit": 50,
            },
        )
        wanted = name.casefold()
        for item in data.get("Items", []):
            if item.get("Name", "").casefold() == wanted:
                return item["Id"]
        raise ConfigError(f"No Jellyfin playlist named {name!r}")

    def tracks(self, config: Config) -> list[dict[str, Any]]:
        if config.playlist_name:
            playlist_id = self.resolve_playlist_id(config.playlist_name)
            data = self.get(
                f"/Playlists/{playlist_id}/Items",
                {
                    "userId": config.user_id,
                    "Fields": FIELDS,
                    "Limit": config.limit,
                },
            )
            return [i for i in data.get("Items", []) if i.get("Type") == "Audio"]

        params: dict[str, Any] = {
            "IncludeItemTypes": "Audio",
            "Recursive": "true",
            "SortBy": "Random",
            "Fields": FIELDS,
            "Limit": config.limit,
            "Genres": "|".join(config.genres),
            "Years": config.years,
        }
        if config.favorites_only:
            if not config.user_id:
                raise ConfigError(
                    "RADIO_FAVORITES_ONLY needs JELLYFIN_USER_ID (favourites are per user)"
                )
            params["Filters"] = "IsFavorite"
        if config.user_id:
            params["userId"] = config.user_id

        data = self.get("/Items", params)
        return data.get("Items", [])


def stream_url(item_id: str, config: Config) -> str:
    """Absolute URL Liquidsoap will hand to ffmpeg."""
    if config.stream_mode == "transcode":
        query = urllib.parse.urlencode(
            {
                "api_key": config.api_key,
                "audioCodec": config.transcode_codec,
                "maxStreamingBitrate": config.max_bitrate,
                "container": config.transcode_codec,
            }
        )
        return f"{config.jellyfin_url}/Audio/{item_id}/universal?{query}"

    query = urllib.parse.urlencode({"static": "true", "api_key": config.api_key})
    return f"{config.jellyfin_url}/Audio/{item_id}/stream?{query}"


def _escape(value: str) -> str:
    """Quote a value for a Liquidsoap ``annotate:`` field."""
    cleaned = " ".join(str(value).split())
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def annotate_uri(item: dict[str, Any], config: Config) -> str | None:
    item_id = item.get("Id")
    if not item_id:
        return None

    artist = item.get("AlbumArtist") or ""
    if not artist:
        artists = item.get("Artists") or []
        artist = artists[0] if artists else ""

    fields = {
        "title": item.get("Name") or "Unknown track",
        "artist": artist,
        "album": item.get("Album") or "",
        "year": str(item.get("ProductionYear") or ""),
    }
    annotations = ",".join(
        f'{key}="{_escape(value)}"' for key, value in fields.items() if value
    )
    return f"annotate:{annotations}:{stream_url(item_id, config)}"


def render_playlist(items: Iterable[dict[str, Any]], config: Config) -> str:
    lines = [
        "#EXTM3U",
        f"# generated by playlist-sync at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
    ]
    lines.extend(uri for uri in (annotate_uri(i, config) for i in items) if uri)
    return "\n".join(lines) + "\n"


def write_atomic(path: str, content: str) -> None:
    """Replace the playlist in one step so Liquidsoap never reads a half-written file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp, path)


def sync_once(client: Jellyfin, config: Config) -> int:
    items = client.tracks(config)
    if not items:
        log.warning("Jellyfin returned no tracks — keeping the previous playlist")
        return 0
    write_atomic(config.playlist_file, render_playlist(items, config))
    log.info("Wrote %d tracks to %s", len(items), config.playlist_file)
    return len(items)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    client = Jellyfin(config.jellyfin_url, config.api_key)
    once = "--once" in argv

    while True:
        try:
            sync_once(client, config)
        except ConfigError as exc:
            log.error("%s", exc)
            return 2
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.error("Playlist refresh failed: %s", exc)
            if once:
                return 1
        if once:
            return 0
        time.sleep(config.interval)


if __name__ == "__main__":
    raise SystemExit(main())
