"""Autonomous radio DJ.

Picks a mood target for the current daypart, bridges to it from whatever the
Icecast stream is currently playing using AudioMuse-AI's sonic-path data, and
pushes the resulting tracks one at a time onto Liquidsoap's `request.queue`
over its telnet control socket. There is no playlist file and nothing is ever
swapped out from under the stream — the DJ only ever appends, so every
target change is reached by a run of sonically-connected tracks rather than a
jump. See ../liquidsoap/radio.liq for the queue side of this.

Two small AudioMuse-AI HTTP endpoints do the actual musical judgement calls
(both are exposed through the Jellyfin plugin, so they're reached the same
way as any other Jellyfin API call, with the same api_key):

  * /AudioMuseAI/find_path?start_song_id=..&end_song_id=..&max_steps=..
      A sequence of tracks that sonically connects two songs (used today by
      hand for the "ultradespair -> iPod Touch" journey; the DJ automates
      picking both endpoints of that call).
  * /AudioMuseAI/similar_tracks?item_id=..&n=..&eliminate_duplicates=true
      Tracks near a single point, used to dwell in a target's neighbourhood
      for a while instead of immediately moving on again.

Candidate target *selection* (which track best matches the current
daypart's mood) isn't exposed as an endpoint, so this reads AudioMuse-AI's
`score` table directly: `other_features` stores exactly the six-dimension
mood vector (happy/sad/aggressive/party/relaxed/danceable) the AudioMuse-AI
backend itself logs as "mood centroids" at startup, per track.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import MISSING, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("dj")

USER_AGENT = "jellyfin-radio-dj/1.0"
MOOD_DIMENSIONS = ("happy", "sad", "aggressive", "party", "relaxed", "danceable")
ALBUM_AWARE_FIELDS = "Artists,Album,AlbumId,ParentIndexNumber,IndexNumber,RunTimeTicks"
PRELUDE_MAX_SECONDS = 60.0
PRELUDE_CHANCE = 0.5


# --------------------------------------------------------------------- config

class ConfigError(Exception):
    """A configuration problem the user has to fix."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Config:
    jellyfin_url: str
    api_key: str
    user_id: str
    stream_mode: str
    max_bitrate: int
    transcode_codec: str

    pg_host: str
    pg_port: int
    pg_user: str
    pg_password: str
    pg_db: str

    liquidsoap_host: str
    liquidsoap_port: int
    queue_id: str

    schedule_file: str
    state_file: str
    status_port: int

    lookahead_tracks: int
    loop_interval: float
    bridge_max_tracks: int
    themed_bridge_max_tracks: int
    dwell_tracks: int
    recent_track_window_hours: float
    recent_artist_window_minutes: float

    playlist_name: str
    playlist_sync_minutes: float
    playlist_max_tracks: int

    @classmethod
    def from_env(cls) -> "Config":
        jellyfin_url = _env("JELLYFIN_URL").rstrip("/")
        api_key = _env("JELLYFIN_API_KEY")
        if not jellyfin_url or not api_key:
            raise ConfigError("JELLYFIN_URL and JELLYFIN_API_KEY are required")
        pg_password = _env("POSTGRES_PASSWORD")
        if not pg_password:
            raise ConfigError("POSTGRES_PASSWORD is required")
        return cls(
            jellyfin_url=jellyfin_url,
            api_key=api_key,
            user_id=_env("JELLYFIN_USER_ID"),
            stream_mode=_env("RADIO_STREAM_MODE", "direct").lower(),
            max_bitrate=_env_int("RADIO_MAX_BITRATE", 320000),
            transcode_codec=_env("RADIO_TRANSCODE_CODEC", "mp3"),
            pg_host=_env("POSTGRES_HOST", "postgres"),
            pg_port=_env_int("POSTGRES_PORT", 5432),
            pg_user=_env("POSTGRES_USER", "audiomuse"),
            pg_password=pg_password,
            pg_db=_env("POSTGRES_DB", "audiomusedb"),
            liquidsoap_host=_env("LIQUIDSOAP_HOST", "liquidsoap"),
            liquidsoap_port=_env_int("LIQUIDSOAP_TELNET_PORT", 1234),
            queue_id=_env("LIQUIDSOAP_QUEUE_ID", "dj_queue"),
            schedule_file=_env("DJ_SCHEDULE_FILE", "/dj/schedule.json"),
            state_file=_env("DJ_STATE_FILE", "/state/dj_state.json"),
            status_port=_env_int("DJ_STATUS_PORT", 9090),
            lookahead_tracks=_env_int("DJ_LOOKAHEAD_TRACKS", 6),
            loop_interval=_env_float("DJ_LOOP_INTERVAL", 60.0),
            bridge_max_tracks=_env_int("DJ_BRIDGE_MAX_TRACKS", 14),
            # A themed daypart (artists/genres/tags) is time-boxed -- e.g. a
            # 60-minute theme hour -- so it needs to actually reach its own
            # content quickly. The full bridge_max_tracks (up to 14 sonic
            # steps) can eat most or all of a themed slot on its own before
            # a single on-theme track plays; found live in a Friday "80s"
            # hour still nowhere near 80s territory 40 minutes in.
            themed_bridge_max_tracks=_env_int("DJ_THEMED_BRIDGE_MAX_TRACKS", 4),
            dwell_tracks=_env_int("DJ_DWELL_TRACKS", 6),
            recent_track_window_hours=_env_float("DJ_RECENT_TRACK_HOURS", 3.0),
            recent_artist_window_minutes=_env_float("DJ_RECENT_ARTIST_MINUTES", 45.0),
            playlist_name=_env("DJ_PLAYLIST_NAME", "DJ: on air now"),
            playlist_sync_minutes=_env_float("DJ_PLAYLIST_SYNC_MINUTES", 10.0),
            playlist_max_tracks=_env_int("DJ_PLAYLIST_MAX_TRACKS", 30),
        )


# ------------------------------------------------------------------- Jellyfin

class Jellyfin:
    """Just enough of the Jellyfin + AudioMuse-AI-plugin HTTP API for the DJ."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def get(self, path: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        params = dict(params or {})
        params["api_key"] = self.cfg.api_key
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        url = f"{self.cfg.jellyfin_url}{path}?{query}"
        request = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path: str, body: dict[str, Any], timeout: float = 30.0) -> Any:
        query = urllib.parse.urlencode({"api_key": self.cfg.api_key})
        url = f"{self.cfg.jellyfin_url}{path}?{query}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None

    def delete_item(self, item_id: str, timeout: float = 30.0) -> None:
        query = urllib.parse.urlencode({"api_key": self.cfg.api_key})
        url = f"{self.cfg.jellyfin_url}/Items/{item_id}?{query}"
        request = urllib.request.Request(url, method="DELETE", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout):
            pass

    def set_favorite(self, item_id: str, favorite: bool = True, timeout: float = 30.0) -> None:
        query = urllib.parse.urlencode({"api_key": self.cfg.api_key})
        url = f"{self.cfg.jellyfin_url}/Users/{self.cfg.user_id}/FavoriteItems/{item_id}?{query}"
        request = urllib.request.Request(
            url, method="POST" if favorite else "DELETE", headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=timeout):
            pass

    def create_playlist(self, name: str, item_ids: list[str]) -> str | None:
        if not item_ids:
            return None
        body = {"Name": name, "Ids": item_ids, "MediaType": "Audio"}
        if self.cfg.user_id:
            body["UserId"] = self.cfg.user_id
        result = self.post("/Playlists", body)
        return result.get("Id") if result else None

    def search_track(self, query: str) -> dict[str, Any] | None:
        data = self.get(
            "/Items",
            {"SearchTerm": query, "IncludeItemTypes": "Audio", "Recursive": "true", "Limit": 5},
        )
        items = data.get("Items", [])
        return items[0] if items else None

    def tracks_by_artist(self, name: str, limit: int = 50) -> list[dict[str, Any]]:
        # /Items?SearchTerm= doesn't reliably match on the Artists field (it
        # missed real, well-stocked artists in practice) -- resolve the
        # artist to an id via /Artists first, then filter by ArtistIds,
        # which Jellyfin does properly.
        artists = self.get("/Artists", {"searchTerm": name, "Recursive": "true", "Limit": 10})
        needle = name.strip().lower()
        match = next(
            (a for a in artists.get("Items", []) if a.get("Name", "").strip().lower() == needle),
            None,
        )
        if not match:
            return []
        data = self.get(
            "/Items",
            {
                "ArtistIds": match["Id"],
                "IncludeItemTypes": "Audio",
                "Recursive": "true",
                "Fields": "Artists",
                "Limit": limit,
            },
        )
        return data.get("Items", [])

    def tracks_by_genre(self, genre: str, limit: int = 200) -> list[dict[str, Any]]:
        data = self.get(
            "/Items",
            {
                "Genres": genre,
                "IncludeItemTypes": "Audio",
                "Recursive": "true",
                "Fields": "Artists",
                "Limit": limit,
            },
        )
        return data.get("Items", [])

    def items_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        data = self.get(
            "/Items", {"Ids": ",".join(ids), "Fields": "Artists", "Recursive": "true"}
        )
        return data.get("Items", [])

    def item(self, item_id: str) -> dict[str, Any] | None:
        data = self.get(
            "/Items",
            {"Ids": item_id, "Fields": ALBUM_AWARE_FIELDS, "Recursive": "true"},
        )
        items = data.get("Items", [])
        return items[0] if items else None

    def album_tracks(self, album_id: str) -> list[dict[str, Any]]:
        data = self.get(
            "/Items",
            {
                "ParentId": album_id,
                "IncludeItemTypes": "Audio",
                "Recursive": "true",
                "Fields": ALBUM_AWARE_FIELDS,
            },
        )
        return data.get("Items", [])

    def find_path(self, start_id: str, end_id: str, max_steps: int = 300) -> list[dict[str, Any]]:
        data = self.get(
            "/AudioMuseAI/find_path",
            {"start_song_id": start_id, "end_song_id": end_id, "max_steps": max_steps},
            timeout=60.0,
        )
        return data.get("path", [])

    def similar_tracks(self, item_id: str, n: int = 10) -> list[dict[str, Any]]:
        data = self.get(
            "/AudioMuseAI/similar_tracks",
            {"item_id": item_id, "n": n, "eliminate_duplicates": "true"},
            timeout=30.0,
        )
        return data if isinstance(data, list) else data.get("similar_tracks", data.get("tracks", []))

    def stream_url(self, item_id: str) -> str:
        if self.cfg.stream_mode == "transcode":
            query = urllib.parse.urlencode(
                {
                    "api_key": self.cfg.api_key,
                    "audioCodec": self.cfg.transcode_codec,
                    "maxStreamingBitrate": self.cfg.max_bitrate,
                    "container": self.cfg.transcode_codec,
                }
            )
            return f"{self.cfg.jellyfin_url}/Audio/{item_id}/universal?{query}"
        query = urllib.parse.urlencode({"static": "true", "api_key": self.cfg.api_key})
        return f"{self.cfg.jellyfin_url}/Audio/{item_id}/stream?{query}"


class AlbumIndex:
    """How the artist actually sequenced each album, read from Jellyfin and
    cached per album (album track order doesn't change once analysed, so
    there's no need to ever invalidate this within a process's lifetime).

    Backs three pieces of album-awareness in the DJ:
      * "double feature" -- when the mood/similarity algorithm happens to
        pick two tracks by the same artist back to back, trust that call but
        swap the second one for whichever track actually follows the first
        on its own album, if there is one.
      * the "prelude table" -- whether a track is preceded on its album by a
        short (<60s) track, e.g. a spoken intro or skit meant to lead
        straight into it.
      * no-crossfade tagging for any pair that's genuinely album-consecutive
        (naturally, via the double-feature fix, or via a prelude), since a
        blended transition would work against how the album was authored.
    """

    def __init__(self, jf: Jellyfin) -> None:
        self.jf = jf
        self._by_album: dict[str, list[dict[str, Any]]] = {}

    def _tracks(self, album_id: str) -> list[dict[str, Any]]:
        if album_id not in self._by_album:
            try:
                tracks = self.jf.album_tracks(album_id)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.warning("Album lookup failed for %s (%s)", album_id, exc)
                tracks = []
            tracks.sort(key=lambda t: (t.get("ParentIndexNumber") or 1, t.get("IndexNumber") or 0))
            self._by_album[album_id] = tracks
        return self._by_album[album_id]

    def _position(self, item: dict[str, Any]) -> tuple[list[dict[str, Any]], int] | None:
        album_id = item.get("AlbumId")
        if not album_id:
            return None
        tracks = self._tracks(album_id)
        pos = next((i for i, t in enumerate(tracks) if t.get("Id") == item.get("Id")), None)
        return (tracks, pos) if pos is not None else None

    def next_track(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """The track that actually follows `item` on its own album/disc."""
        found = self._position(item)
        if not found:
            return None
        tracks, pos = found
        if pos + 1 >= len(tracks):
            return None
        nxt = tracks[pos + 1]
        disc = item.get("ParentIndexNumber") or 1
        return nxt if (nxt.get("ParentIndexNumber") or 1) == disc else None

    def is_album_consecutive(self, a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
        if not a or not b:
            return False
        nxt = self.next_track(a)
        return bool(nxt and nxt.get("Id") == b.get("Id"))

    def prelude_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """A short (<60s) track immediately preceding `item` on the same
        album/disc -- an intro/skit meant to lead straight into it."""
        found = self._position(item)
        if not found:
            return None
        tracks, pos = found
        if pos == 0:
            return None
        prev = tracks[pos - 1]
        disc = item.get("ParentIndexNumber") or 1
        if (prev.get("ParentIndexNumber") or 1) != disc:
            return None
        return prev if self._is_short(prev) else None

    def main_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """The reverse of prelude_of(): if `item` is itself short enough
        (<60s) to be a prelude, the track it leads into on the album."""
        return self.next_track(item) if self._is_short(item) else None

    @staticmethod
    def _is_short(item: dict[str, Any]) -> bool:
        seconds = (item.get("RunTimeTicks") or 0) / 10_000_000
        return 0 < seconds < PRELUDE_MAX_SECONDS


def _escape(value: str) -> str:
    cleaned = " ".join(str(value).split())
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def annotate_uri(item: dict[str, Any], jf: Jellyfin) -> str | None:
    item_id = item.get("Id") or item.get("item_id")
    if not item_id:
        return None
    artist = item.get("author") or ""
    if not artist:
        artists = item.get("Artists") or []
        artist = artists[0] if artists else ""
    title = item.get("Name") or item.get("title") or "Unknown track"
    album = item.get("Album") or item.get("album") or ""
    fields = {"title": title, "artist": artist, "album": album}
    if item.get("segue"):
        # Read by radio.liq's smart_transition to force a hard cut instead
        # of the usual dB-based crossfade decision -- set when this track is
        # genuinely album-consecutive with whatever precedes it.
        fields["segue"] = "true"
    annotations = ",".join(f'{k}="{_escape(v)}"' for k, v in fields.items() if v)
    return f"annotate:{annotations}:{jf.stream_url(item_id)}"


# -------------------------------------------------------------------- Postgres

def pg_connect(cfg: Config):
    import psycopg2  # imported lazily so --help/tests don't need it installed

    return psycopg2.connect(
        host=cfg.pg_host, port=cfg.pg_port, user=cfg.pg_user,
        password=cfg.pg_password, dbname=cfg.pg_db, connect_timeout=10,
    )


def parse_feature_string(raw: str | None) -> dict[str, float]:
    """Parse AudioMuse-AI's "label:value,label:value" feature columns."""
    result: dict[str, float] = {}
    if not raw:
        return result
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        key, _, value = pair.partition(":")
        try:
            result[key.strip()] = float(value)
        except ValueError:
            continue
    return result


def mood_distance(mood: dict[str, float], target: dict[str, float]) -> float:
    """Euclidean distance over the target's dimensions only (missing track
    dimensions count as 0, matching how AudioMuse-AI's own zero-valued
    dimensions are represented)."""
    total = 0.0
    for key, weight in target.items():
        total += (mood.get(key, 0.0) - weight) ** 2
    return math.sqrt(total)


class Catalogue:
    """Cached view of the Jellyfin-side track mood data from Postgres."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._jellyfin_server_id: str | None = None
        self._tracks: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, float]] = {}
        self._loaded_at: float = 0.0

    def _server_id(self, cur) -> str:
        if self._jellyfin_server_id is None:
            cur.execute("SELECT server_id FROM music_servers WHERE server_type = 'jellyfin' LIMIT 1")
            row = cur.fetchone()
            if not row:
                raise ConfigError("No Jellyfin server registered in AudioMuse-AI's music_servers table")
            self._jellyfin_server_id = row[0]
        return self._jellyfin_server_id

    def refresh(self) -> None:
        """Reload the candidate pool. Cheap enough (a few thousand rows) to
        call every loop iteration -- this is also how newly-analysed tracks
        become selectable without any special-cased "new music" handling."""
        conn = pg_connect(self.cfg)
        try:
            cur = conn.cursor()
            server_id = self._server_id(cur)
            cur.execute(
                """
                SELECT tsm.provider_track_id, s.other_features
                FROM track_server_map tsm
                JOIN score s ON s.item_id = tsm.item_id
                WHERE tsm.server_id = %s AND s.other_features IS NOT NULL
                """,
                (server_id,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        self._tracks = [
            {"item_id": jf_id, "mood": parse_feature_string(feat)}
            for jf_id, feat in rows
            if jf_id
        ]
        self._by_id = {t["item_id"]: t["mood"] for t in self._tracks}
        self._loaded_at = time.time()
        log.info("Catalogue refreshed: %d candidate tracks", len(self._tracks))

    def meets_minimums(self, item_id: str, min_dims: dict[str, float]) -> bool:
        """True when item_id has no known mood data (nothing to filter on,
        so it's kept rather than blocked) or clears every floor in
        min_dims -- lets a time-window floor (e.g. a minimum danceability)
        apply to pools that don't otherwise rank by mood, like
        Dj._pick_preferred's artist/genre/tag pool."""
        if not min_dims:
            return True
        mood = self._by_id.get(item_id)
        if mood is None:
            return True
        return all(mood.get(dim, 0.0) >= floor for dim, floor in min_dims.items())

    def tag_pool(self, tags: list[str], exclude_ids: set[str], min_score: float = 0.3) -> list[str]:
        """Jellyfin item_ids matching any of `tags` in AudioMuse-AI's own
        genre/vocal-type classifier -- confusingly stored in the `mood_vector`
        column, not `other_features` (the actual 6-dimension mood vector used
        by pick_target). Its vocabulary mixes genre-ish labels ("Hip-Hop",
        "electronic") with vocal-type ones ("female vocalists"), each track
        carrying its top handful with a confidence score -- this is what the
        AudioMuse-AI song map colors points by."""
        conn = pg_connect(self.cfg)
        try:
            cur = conn.cursor()
            server_id = self._server_id(cur)
            cur.execute(
                """
                SELECT tsm.provider_track_id, s.mood_vector
                FROM track_server_map tsm
                JOIN score s ON s.item_id = tsm.item_id
                WHERE tsm.server_id = %s AND s.mood_vector IS NOT NULL
                """,
                (server_id,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        wanted = {t.strip().lower() for t in tags}
        matches = []
        for jf_id, raw in rows:
            if not jf_id or jf_id in exclude_ids:
                continue
            scores = parse_feature_string(raw)
            if any(v >= min_score for k, v in scores.items() if k.strip().lower() in wanted):
                matches.append(jf_id)
        return matches

    def pick_target(
        self,
        target_mood: dict[str, float],
        exclude_ids: set[str],
        exploration: str = "medium",
        min_dims: dict[str, float] | None = None,
    ) -> dict[str, Any] | None:
        if not self._tracks:
            self.refresh()
        candidates = [t for t in self._tracks if t["item_id"] not in exclude_ids]
        if not candidates:
            candidates = self._tracks
        if not candidates:
            return None
        if min_dims:
            # A time-window floor (e.g. minimum danceability) is applied as
            # a soft filter: prefer candidates that clear it, but an
            # atmospheric daypart shouldn't lose every candidate just
            # because the floor is active -- fall back to the unfiltered
            # pool rather than ever returning nothing.
            floored = [
                t for t in candidates
                if all(t["mood"].get(dim, 0.0) >= floor for dim, floor in min_dims.items())
            ]
            if floored:
                candidates = floored
        ranked = sorted(candidates, key=lambda t: mood_distance(t["mood"], target_mood))
        pool_size = {"low": 5, "medium": 15, "high": 40}.get(exploration, 15)
        pool = ranked[: max(1, min(pool_size, len(ranked)))]
        return random.choice(pool)


class SongMap:
    """Cached view of AudioMuse-AI's 2D sonic map projection -- lets the web
    player show "you are here" against the pre-rendered static image of the
    whole map (song_map.png). Reloaded rarely: the projection only changes
    when AudioMuse-AI recomputes it, not on every track."""

    REFRESH_SECONDS = 3600

    def __init__(self, cfg: Config, index_name: str = "main_map") -> None:
        self.cfg = cfg
        self.index_name = index_name
        self._positions: dict[str, tuple[float, float]] = {}
        self._bounds: dict[str, float] | None = None
        self._loaded_at: float = 0.0

    def refresh(self) -> None:
        conn = pg_connect(self.cfg)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT projection_data, id_map_json FROM map_projection_data WHERE index_name = %s",
                (self.index_name,),
            )
            row = cur.fetchone()
            if not row:
                self._loaded_at = time.time()
                return
            proj_blob, id_map_json = row
            id_map = json.loads(id_map_json)
            floats = struct.unpack(f"<{len(proj_blob) // 4}f", proj_blob)

            cur.execute("SELECT server_id FROM music_servers WHERE server_type = 'jellyfin' LIMIT 1")
            srv_row = cur.fetchone()
            fp_to_jf: dict[str, str] = {}
            if srv_row:
                cur.execute(
                    "SELECT item_id, provider_track_id FROM track_server_map WHERE server_id = %s",
                    (srv_row[0],),
                )
                fp_to_jf = dict(cur.fetchall())
        finally:
            conn.close()

        positions: dict[str, tuple[float, float]] = {}
        xs: list[float] = []
        ys: list[float] = []
        for i, fp_id in enumerate(id_map):
            x, y = floats[2 * i], floats[2 * i + 1]
            xs.append(x)
            ys.append(y)
            jf_id = fp_to_jf.get(fp_id)
            if jf_id:
                positions[jf_id] = (x, y)

        self._positions = positions
        if xs and ys:
            self._bounds = {"xmin": min(xs), "xmax": max(xs), "ymin": min(ys), "ymax": max(ys)}
        self._loaded_at = time.time()
        log.info("Song map refreshed: %d positions", len(positions))

    def position(self, item_id: str) -> dict[str, Any] | None:
        if time.time() - self._loaded_at > self.REFRESH_SECONDS:
            try:
                self.refresh()
            except Exception:
                log.exception("Song map refresh failed")
        point = self._positions.get(item_id)
        if not point or not self._bounds:
            return None
        return {"x": point[0], "y": point[1], "bounds": self._bounds}


# --------------------------------------------------------------------- state

@dataclass
class DjState:
    last_item_id: str | None = None
    last_pushed_at: str | None = None
    phase: str = "starting"
    daypart: str | None = None
    target: dict[str, Any] | None = None
    plan: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    manual_target: dict[str, Any] | None = None
    synced_playlist_id: str | None = None
    last_playlist_sync: str | None = None
    liked_track_ids: list[str] = field(default_factory=list)
    penalties: dict[str, dict[str, Any]] = field(default_factory=dict)  # item_id -> {strikes, until}
    like_events: list[str] = field(default_factory=list)  # UTC timestamps, one per up-vote action

    @classmethod
    def load(cls, path: str) -> "DjState":
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
        # Only pass through keys the saved file actually has, and only when
        # not null, so the dataclass's own field defaults (default_factory
        # included) apply instead -- a state file from before a field was
        # added, or one a prior buggy load() already wrote a literal `null`
        # into, must not override an empty-list default with None.
        by_name = {f.name: f for f in fields(cls)}
        kwargs = {}
        for k, v in raw.items():
            f = by_name.get(k)
            if f is None or (v is None and f.default_factory is not MISSING):
                continue
            kwargs[k] = v
        return cls(**kwargs)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.__dict__, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def record_played(self, item: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.last_item_id = item.get("item_id") or item.get("Id")
        self.last_pushed_at = now
        self.history.append(
            {
                "item_id": self.last_item_id,
                "artist": item.get("author") or ",".join(item.get("Artists") or []),
                "title": item.get("title") or item.get("Name"),
                "at": now,
            }
        )
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        self.history = [h for h in self.history if datetime.fromisoformat(h["at"]) > cutoff]

    def recent_track_ids(self, hours: float) -> set[str]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return {h["item_id"] for h in self.history if datetime.fromisoformat(h["at"]) > cutoff}

    def like(self, item_id: str) -> None:
        if item_id not in self.liked_track_ids:
            self.liked_track_ids.append(item_id)
        self.penalties.pop(item_id, None)

    def penalize(self, item_id: str, daypart: str | None = None) -> None:
        # Escalating temporary ban rather than a permanent one: 1st downvote
        # -> 14 days out, 2nd -> 28, and so on. A later like() clears it.
        # Scoped to the daypart it happened in -- a downvote during "night"
        # says this track doesn't fit that segment, not that it's unwanted
        # everywhere, so it stays eligible for other dayparts/theme hours.
        # daypart=None (e.g. an older state file) means unscoped/global.
        strikes = self.penalties.get(item_id, {}).get("strikes", 0) + 1
        until = datetime.now(timezone.utc) + timedelta(days=14 * strikes)
        self.penalties[item_id] = {"strikes": strikes, "until": until.isoformat(), "daypart": daypart}
        if item_id in self.liked_track_ids:
            self.liked_track_ids.remove(item_id)

    def active_penalty_ids(self, daypart: str | None = None) -> set[str]:
        now = datetime.now(timezone.utc)
        return {
            item_id
            for item_id, entry in self.penalties.items()
            if datetime.fromisoformat(entry["until"]) > now
            and entry.get("daypart") in (None, daypart)
        }

    def record_like_event(self) -> None:
        self.like_events.append(datetime.now(timezone.utc).isoformat())
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        self.like_events = [t for t in self.like_events if datetime.fromisoformat(t) > cutoff]

    def likes_today(self) -> int:
        today = datetime.now().date()
        count = 0
        for ts in self.like_events:
            try:
                local = datetime.fromisoformat(ts).astimezone()
            except ValueError:
                continue
            if local.date() == today:
                count += 1
        return count

    def recent_artists(self, minutes: float) -> set[str]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        return {h["artist"] for h in self.history if h.get("artist") and datetime.fromisoformat(h["at"]) > cutoff}


# ------------------------------------------------------------------ schedule

def load_schedule(path: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["dayparts"], data.get("constraints", [])


def _window_matches(entry: dict[str, Any], now: datetime) -> bool:
    """Shared hours/days window check for both dayparts and constraints."""
    hour = now.hour
    # Monday=0 .. Sunday=6, matching Python's own datetime.weekday().
    weekday = now.weekday()
    prev_weekday = (weekday - 1) % 7
    days = entry.get("days")

    def day_ok(d: int) -> bool:
        return days is None or d in days

    start, end = entry["hours"]
    if start <= end:
        return day_ok(weekday) and start <= hour < end
    # Wraps past midnight, e.g. Fri/Sat 20:00-02:00 -- the evening part
    # belongs to `weekday`, but the early-morning tail belongs to whichever
    # day's evening started it, i.e. `prev_weekday`. Without this split,
    # "Friday 01:00" (prev_weekday=Thursday, not in a Fri/Sat rule) would
    # wrongly inherit Saturday's window a full day early.
    evening = day_ok(weekday) and hour >= start
    morning = day_ok(prev_weekday) and hour < end
    return evening or morning


def current_daypart(schedule: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    for daypart in schedule:
        if _window_matches(daypart, now):
            return daypart
    return schedule[0]


def active_min_dims(constraints: list[dict[str, Any]], now: datetime | None = None) -> dict[str, float]:
    """Hard floors active right now (e.g. a minimum danceability during
    daytime hours), merged across every matching window -- the highest floor
    wins per dimension if more than one constraint applies. This is a global
    overlay on top of whichever daypart is active, not a daypart itself:
    atmospheric/ambient dayparts still get picked during these hours, their
    candidates just can't dip below the floor (and if the floor would empty
    the pool entirely, the floor loses rather than the daypart -- see
    Catalogue.pick_target/Dj._pick_preferred)."""
    now = now or datetime.now()
    merged: dict[str, float] = {}
    for constraint in constraints:
        if _window_matches(constraint, now):
            for dim, value in (constraint.get("min") or {}).items():
                merged[dim] = max(merged.get(dim, value), value)
    return merged


# ---------------------------------------------------------------------- Liquidsoap telnet

class LiquidsoapError(Exception):
    pass


def telnet_command(host: str, port: int, command: str, timeout: float = 10.0) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall((command + "\n").encode("utf-8"))
        sock.settimeout(timeout)
        buf = b""
        while not buf.rstrip().endswith(b"END"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        try:
            sock.sendall(b"quit\n")
        except OSError:
            pass
    text = buf.decode("utf-8", errors="replace")
    return text.rsplit("END", 1)[0].strip()


def queue_length(cfg: Config) -> int:
    # `<id>.queue` is request.queue's actual telnet command (there is no
    # `.length`) -- it replies with the queued-but-not-yet-playing request
    # ids on one space-separated line, e.g. "2 3", or blank when empty.
    reply = telnet_command(cfg.liquidsoap_host, cfg.liquidsoap_port, f"{cfg.queue_id}.queue")
    return len(reply.split())


def queue_push(cfg: Config, uri: str) -> None:
    # The URI itself never contains literal newlines (annotate_uri escapes
    # them via _escape's whitespace collapsing), so a single-line command is
    # safe. Reply is the new request's id (e.g. "1"), not an "OK" of any kind.
    reply = telnet_command(cfg.liquidsoap_host, cfg.liquidsoap_port, f'{cfg.queue_id}.push {uri}')
    if not reply.strip().isdigit():
        log.warning("Liquidsoap push may have failed: %s", reply)


# --------------------------------------------------------------- bridging logic

def build_bridge(jf: Jellyfin, from_id: str | None, to_id: str, max_tracks: int) -> list[dict[str, Any]]:
    """A sonic path from the last queued track to the new target. Falls back
    to just the target itself if there's no known "from" yet (first run) or
    the path lookup fails."""
    if not from_id or from_id == to_id:
        return []
    try:
        path = jf.find_path(from_id, to_id, max_steps=300)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("find_path failed (%s), jumping straight to target instead", exc)
        return []
    # Drop the first hop -- it's `from_id` itself, already playing/queued.
    path = path[1:]
    if not path:
        return []
    if len(path) > max_tracks:
        # Evenly subsample rather than truncate, so the far end of the path
        # is still reached instead of cut off -- always keep the actual last
        # hop rather than re-deriving it, since find_path's own path doesn't
        # necessarily land exactly on `to_id` (e.g. when max_steps truncates).
        step = len(path) / max_tracks
        sampled = [path[int(i * step)] for i in range(max_tracks - 1)]
        sampled.append(path[-1])
        path = sampled
    return path


def build_dwell(jf: Jellyfin, target_id: str, n: int, exclude_ids: set[str]) -> list[dict[str, Any]]:
    try:
        similar = jf.similar_tracks(target_id, n=n + len(exclude_ids))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("similar_tracks failed (%s), skipping dwell phase", exc)
        return []
    picked = [t for t in similar if t.get("item_id") not in exclude_ids][:n]
    return picked


# --------------------------------------------------------------------- status API

class StatusHandler(BaseHTTPRequestHandler):
    dj: "Dj" = None  # set by serve_status()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        log.debug("status api: " + format, *args)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        if self.path.rstrip("/") == "/status":
            self._json(200, self.dj.status_payload())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        path = self.path.rstrip("/")
        if path not in ("/target", "/feedback"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return
        try:
            if path == "/target":
                resolved = self.dj.set_manual_target(body)
                self._json(202, {"accepted": resolved})
            else:
                result = self.dj.record_feedback(body)
                self._json(200, result)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})


def serve_status(dj: "Dj", port: int) -> None:
    StatusHandler.dj = dj
    server = ThreadingHTTPServer(("0.0.0.0", port), StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Status API listening on :%d", port)


# ------------------------------------------------------------------------- Dj

class Dj:
    MAP_TRAIL_LENGTH = 8  # how many recent tracks to draw as a fading path on the song map

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.jf = Jellyfin(cfg)
        self.catalogue = Catalogue(cfg)
        self.song_map = SongMap(cfg)
        self.album_index = AlbumIndex(self.jf)
        self._item_cache: dict[str, dict[str, Any]] = {}
        self._current_daypart: dict[str, Any] | None = None
        self.schedule, self.constraints = load_schedule(cfg.schedule_file)
        self.state = DjState.load(cfg.state_file)
        # RLock, not Lock: run()'s main loop holds this for the whole tick,
        # and _next_target() (called from inside that tick) also takes it to
        # read/clear a manual override -- a plain Lock would self-deadlock.
        self._lock = threading.RLock()

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "daypart": self.state.daypart,
                "phase": self.state.phase,
                "target": self.state.target,
                "last_track": self.state.history[-1] if self.state.history else None,
                "since": self.state.last_pushed_at,
                "queued_ahead": len(self.state.plan),
                "likes_today": self.state.likes_today(),
                "map_position": self.song_map.position(self.state.last_item_id) if self.state.last_item_id else None,
                "map_trail": self._map_trail(),
            }

    def _map_trail(self) -> list[dict[str, float]]:
        """Positions of the last MAP_TRAIL_LENGTH played tracks, oldest first,
        ending at the current track -- lets the web player draw a fading path
        showing how the DJ wandered across the sonic map to get here."""
        recent = self.state.history[-self.MAP_TRAIL_LENGTH :]
        trail = []
        for h in recent:
            item_id = h.get("item_id")
            if not item_id:
                continue
            pos = self.song_map.position(item_id)
            if pos:
                trail.append({"x": pos["x"], "y": pos["y"]})
        return trail

    def set_manual_target(self, body: dict[str, Any]) -> dict[str, Any]:
        if "item_id" in body:
            item = self.jf.item(body["item_id"])
        elif "query" in body:
            item = self.jf.search_track(body["query"])
        else:
            raise ValueError("Provide either 'item_id' or 'query'")
        if not item:
            raise ValueError("No matching track found")
        resolved = {
            "item_id": item["Id"],
            "title": item.get("Name"),
            "author": ", ".join(item.get("Artists") or []),
        }
        with self._lock:
            self.state.manual_target = resolved
            self.state.save(self.cfg.state_file)
        log.info("Manual target set: %s - %s", resolved["author"], resolved["title"])
        return resolved

    def record_feedback(self, body: dict[str, Any]) -> dict[str, Any]:
        vote = body.get("vote")
        if vote not in ("up", "down"):
            raise ValueError("'vote' must be 'up' or 'down'")
        with self._lock:
            item_id = body.get("item_id") or self.state.last_item_id
            if not item_id:
                raise ValueError("No current track to vote on")
            is_current = item_id == self.state.last_item_id
            if vote == "up":
                self.state.like(item_id)
                self.state.record_like_event()
            else:
                self.state.penalize(item_id, self.state.daypart)
            self.state.save(self.cfg.state_file)

        if vote == "up":
            try:
                self.jf.set_favorite(item_id, True)
            except (urllib.error.URLError, OSError) as exc:
                log.warning("Feedback: could not favorite %s (%s)", item_id, exc)
        elif is_current:
            # Thumbs-down on whatever's currently on air -- skip it right
            # away rather than making the listener sit through it anyway.
            try:
                telnet_command(self.cfg.liquidsoap_host, self.cfg.liquidsoap_port, f"{self.cfg.queue_id}.skip")
            except (OSError, LiquidsoapError) as exc:
                log.warning("Feedback: could not skip after downvote (%s)", exc)

        log.info("Feedback: %s for %s", vote, item_id)
        return {"item_id": item_id, "vote": vote}

    def _next_target(self) -> dict[str, Any]:
        with self._lock:
            manual = self.state.manual_target
            self.state.manual_target = None
        if manual:
            # No daypart context for a manual override -- _refill_plan falls
            # back to build_dwell's sonic-similarity picks for the dwell
            # phase rather than a themed pool.
            self._current_daypart = None
            return manual

        daypart = current_daypart(self.schedule)
        self._current_daypart = daypart
        self.state.daypart = daypart["name"]
        exclude = self.state.recent_track_ids(self.cfg.recent_track_window_hours) | self.state.active_penalty_ids(
            daypart["name"]
        )
        min_dims = active_min_dims(self.constraints)

        preferred = self._pick_preferred(daypart, exclude, min_dims)
        if preferred:
            return preferred

        self.catalogue.refresh()
        exploration = daypart.get("exploration", "medium")
        candidate = self.catalogue.pick_target(daypart["mood"], exclude, exploration, min_dims=min_dims)
        if not candidate:
            raise RuntimeError("No candidate tracks available at all")
        item = self.jf.item(candidate["item_id"])
        return {
            "item_id": candidate["item_id"],
            "title": item.get("Name") if item else None,
            "author": ", ".join(item.get("Artists") or []) if item else None,
        }

    def _preference_pool(
        self, daypart: dict[str, Any], exclude: set[str], min_dims: dict[str, float] | None = None
    ) -> list[dict[str, Any]]:
        """The combined artists/genres/tags candidate pool a themed daypart
        draws from -- deduplicated and filtered by exclude/min_dims. Shared
        by _pick_preferred (one pick, for a fresh target) and
        _pick_preferred_many (several picks, to keep a themed dwell phase
        actually on-theme -- see _refill_plan)."""
        artists = daypart.get("artists") or []
        genres = daypart.get("genres") or []
        tags = daypart.get("tags") or []
        if not artists and not genres and not tags:
            return []
        pool: list[dict[str, Any]] = []
        for artist in artists:
            pool.extend(self.jf.tracks_by_artist(artist))
        for genre in genres:
            pool.extend(self.jf.tracks_by_genre(genre))
        if tags:
            ids = self.catalogue.tag_pool(tags, exclude)
            if len(ids) > 150:
                ids = random.sample(ids, 150)
            pool.extend(self.jf.items_by_ids(ids))
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for item in pool:
            item_id = item.get("Id")
            if item_id and item_id not in exclude and item_id not in seen:
                seen.add(item_id)
                unique.append(item)
        if min_dims:
            # Same soft-floor behaviour as Catalogue.pick_target: prefer
            # tracks that clear it, but a themed pool full of atmospheric
            # tracks shouldn't be emptied out entirely by it.
            floored = [item for item in unique if self.catalogue.meets_minimums(item["Id"], min_dims)]
            if floored:
                unique = floored
        return unique

    def _pick_preferred(
        self, daypart: dict[str, Any], exclude: set[str], min_dims: dict[str, float] | None = None
    ) -> dict[str, Any] | None:
        """A daypart can name specific `artists`, Jellyfin `genres`, and/or
        AudioMuse-AI classifier `tags` it wants (e.g. a themed hour) instead
        of, or alongside, a mood target. `artists`/`genres` are pulled
        straight from Jellyfin; `tags` go through AudioMuse-AI's own
        genre/vocal-type classifier (see Catalogue.tag_pool) -- that's how a
        themed hour can mean "this vibe, whoever's actually in the library"
        rather than a fixed, hand-picked artist list."""
        pool = self._preference_pool(daypart, exclude, min_dims)
        if not pool:
            return None
        item = random.choice(pool)
        return {
            "item_id": item["Id"],
            "title": item.get("Name"),
            "author": ", ".join(item.get("Artists") or []),
        }

    def _pick_preferred_many(
        self, daypart: dict[str, Any], exclude: set[str], n: int, min_dims: dict[str, float] | None = None
    ) -> list[dict[str, Any]]:
        """Several distinct picks from the same themed pool _pick_preferred
        draws from -- used for the dwell phase of a themed daypart instead
        of build_dwell's pure sonic-similarity picks. build_dwell doesn't
        know about a daypart's artists/genres/tags at all (it just asks
        AudioMuse-AI for tracks near the target's *sound*), so a themed
        hour's dwell phase could drift off-theme within a track or two of
        arriving -- found live: a Friday "80s" hour was still audibly
        nowhere near 80s territory 40 minutes in, well past the single
        target track this replaces."""
        pool = self._preference_pool(daypart, exclude, min_dims)
        random.shuffle(pool)
        picked = pool[:n]
        return [
            {"item_id": item["Id"], "title": item.get("Name"), "author": ", ".join(item.get("Artists") or [])}
            for item in picked
        ]

    def _refill_plan(self) -> None:
        target = self._next_target()
        daypart = self._current_daypart
        self.state.target = target
        self.state.phase = "transitioning"
        log.info("New target: %s - %s (%s)", target.get("author"), target.get("title"), self.state.daypart)

        is_themed = bool(daypart and (daypart.get("artists") or daypart.get("genres") or daypart.get("tags")))
        bridge_max = self.cfg.themed_bridge_max_tracks if is_themed else self.cfg.bridge_max_tracks
        bridge = build_bridge(self.jf, self.state.last_item_id, target["item_id"], bridge_max)
        if not bridge:
            bridge = [target]
        exclude = (
            self.state.recent_track_ids(self.cfg.recent_track_window_hours)
            | self.state.active_penalty_ids(self.state.daypart)
            | {target["item_id"]}
        )
        if is_themed:
            # Keep a themed daypart's dwell phase actually on-theme instead
            # of build_dwell's sonic-similarity picks, which don't know
            # about the daypart's artists/genres/tags at all -- see
            # _pick_preferred_many's docstring.
            dwell = self._pick_preferred_many(
                daypart, exclude, self.cfg.dwell_tracks, active_min_dims(self.constraints)
            )
        else:
            dwell = build_dwell(self.jf, target["item_id"], self.cfg.dwell_tracks, exclude)

        self.state.plan = bridge + dwell
        self.state.phase = "dwelling" if dwell else "transitioning"

    def _full_item(self, ref: dict[str, Any] | str | None) -> dict[str, Any] | None:
        """Resolves a plan entry (which may only carry the minimal
        item_id/title/author fields build_bridge/build_dwell/etc. produce)
        to the full Jellyfin item, including the album fields album-awareness
        needs. Cached per process since album track order is effectively
        static."""
        if not ref:
            return None
        item_id = ref if isinstance(ref, str) else (ref.get("Id") or ref.get("item_id"))
        if not item_id:
            return None
        if item_id not in self._item_cache:
            try:
                item = self.jf.item(item_id)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.warning("Could not resolve item %s (%s)", item_id, exc)
                item = None
            if item:
                self._item_cache[item_id] = item
        return self._item_cache.get(item_id)

    @staticmethod
    def _same_artist(a: dict[str, Any], b: dict[str, Any]) -> bool:
        a_artists = set(a.get("Artists") or [])
        b_artists = set(b.get("Artists") or [])
        return bool(a_artists & b_artists)

    def _apply_album_awareness(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        """Runs right before a track is pushed, with the actual previously
        pushed track (self.state.last_item_id) as ground truth for what
        comes right before it:

          * "double feature": an accidental same-artist back-to-back gets
            corrected to whatever really follows the previous track on its
            own album, unless that track is itself excluded -- or is the
            deliberately mood/tag-picked target track (self.state.target),
            which build_bridge/build_dwell already led into and around, and
            which /status is telling listeners the DJ is heading for. Only
            candidates picked along the way are fair game for the swap.
          * a short prelude preceding this track on its album completes it
            (gets prepended) with PRELUDE_CHANCE odds -- and the reverse:
            if this track IS such a short prelude, its own main track
            completes it (gets appended) with the same odds.
          * every resulting pair that's genuinely album-consecutive is
            tagged 'segue' so radio.liq hard-cuts instead of crossfading.

        Falls back to the untouched item (no swap/prelude/segue) whenever
        Jellyfin lookups fail, rather than blocking playback on it."""
        full = self._full_item(item)
        if not full:
            return [item]

        previous = self._full_item(self.state.last_item_id)
        target_id = (self.state.target or {}).get("item_id")
        is_target = target_id is not None and full.get("Id") == target_id
        if previous and not is_target and self._same_artist(previous, full) and not self.album_index.is_album_consecutive(
            previous, full
        ):
            successor = self.album_index.next_track(previous)
            excluded = self.state.recent_track_ids(self.cfg.recent_track_window_hours) | self.state.active_penalty_ids(
                self.state.daypart
            )
            if successor and successor.get("Id") not in excluded:
                resolved = self._full_item(successor)
                if resolved:
                    log.info(
                        "Double feature fix: %s -> album successor %s",
                        full.get("Name"), resolved.get("Name"),
                    )
                    full = resolved

        sequence = [full]
        prelude = self.album_index.prelude_of(full)
        if prelude and random.random() < PRELUDE_CHANCE:
            resolved_prelude = self._full_item(prelude)
            if resolved_prelude:
                sequence.insert(0, resolved_prelude)

        main = self.album_index.main_of(full)
        if main and random.random() < PRELUDE_CHANCE:
            resolved_main = self._full_item(main)
            if resolved_main:
                sequence.append(resolved_main)

        prior = previous
        for track in sequence:
            track["segue"] = self.album_index.is_album_consecutive(prior, track)
            prior = track
        return sequence

    def _discard_plan_on_daypart_change(self) -> None:
        """Without this, a daypart boundary is only noticed once the current
        plan happens to run dry -- found live: a themed hour's start (and,
        symmetrically, its end) could sit unnoticed behind a long leftover
        bridge/dwell backlog from the previous daypart for most of the hour.
        Discarding the not-yet-pushed plan here forces _refill_plan to pick
        a fresh target for the new daypart on the very next _tick loop
        iteration, rather than whenever the old plan happens to run out.
        Tracks already pushed to Liquidsoap's own queue still finish playing
        -- this doesn't cut anything off mid-stream."""
        if self.state.manual_target:
            return
        fresh_daypart = current_daypart(self.schedule)["name"]
        if self.state.daypart is not None and fresh_daypart != self.state.daypart and self.state.plan:
            log.info("Daypart boundary: %s -> %s, replanning now", self.state.daypart, fresh_daypart)
            self.state.plan = []

    def _tick(self) -> None:
        try:
            current_len = queue_length(self.cfg)
        except (OSError, LiquidsoapError) as exc:
            log.warning("Could not reach Liquidsoap telnet (%s), will retry", exc)
            return

        self._discard_plan_on_daypart_change()

        while current_len < self.cfg.lookahead_tracks:
            if not self.state.plan:
                self._refill_plan()
                if not self.state.plan:
                    break
            item = self.state.plan.pop(0)
            for push_item in self._apply_album_awareness(item):
                uri = annotate_uri(push_item, self.jf)
                if not uri:
                    continue
                queue_push(self.cfg, uri)
                self.state.record_played(push_item)
                current_len += 1
                log.info(
                    "Pushed: %s - %s (%d queued, %d planned)%s",
                    push_item.get("author") or ", ".join(push_item.get("Artists") or []),
                    push_item.get("title") or push_item.get("Name"),
                    current_len, len(self.state.plan),
                    " [segue]" if push_item.get("segue") else "",
                )

        self._sync_playlist()
        self.state.save(self.cfg.state_file)

    def _sync_playlist(self) -> None:
        """Mirror recent playback into a real Jellyfin playlist so it shows
        up as a normal, browsable thing rather than only living in this
        service's own /status. Runs on a coarser cadence than the queue
        top-up (DJ_PLAYLIST_SYNC_MINUTES) -- there's no update-in-place for a
        playlist's item list without a user auth token (only api_key here),
        so each sync deletes the previous one and creates a fresh one under
        the same fixed name, keeping exactly one live rather than piling up."""
        last = self.state.last_playlist_sync
        if last:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
            if elapsed < self.cfg.playlist_sync_minutes:
                return

        item_ids = [h["item_id"] for h in self.state.history if h.get("item_id")]
        item_ids = item_ids[-self.cfg.playlist_max_tracks :]
        if not item_ids:
            return

        old_id = self.state.synced_playlist_id
        new_id = self.jf.create_playlist(self.cfg.playlist_name, item_ids)
        if not new_id:
            log.warning("Playlist sync: failed to create %r", self.cfg.playlist_name)
            return
        if old_id and old_id != new_id:
            try:
                self.jf.delete_item(old_id)
            except (urllib.error.URLError, OSError) as exc:
                log.warning("Playlist sync: could not remove previous playlist %s (%s)", old_id, exc)

        self.state.synced_playlist_id = new_id
        self.state.last_playlist_sync = datetime.now(timezone.utc).isoformat()
        log.info("Playlist sync: %r updated with %d tracks", self.cfg.playlist_name, len(item_ids))

    def run(self) -> None:
        serve_status(self, self.cfg.status_port)
        log.info("DJ loop starting (lookahead=%d, interval=%.0fs)", self.cfg.lookahead_tracks, self.cfg.loop_interval)
        while True:
            with self._lock:
                try:
                    self._tick()
                except Exception:  # noqa: BLE001 - keep the loop alive no matter what
                    log.exception("Unhandled error in DJ tick")
            time.sleep(self.cfg.loop_interval)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    Dj(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
