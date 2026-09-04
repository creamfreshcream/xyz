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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("dj")

USER_AGENT = "jellyfin-radio-dj/1.0"
MOOD_DIMENSIONS = ("happy", "sad", "aggressive", "party", "relaxed", "danceable")


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
            "/Items", {"Ids": item_id, "Fields": "Artists,Album", "Recursive": "true"}
        )
        items = data.get("Items", [])
        return items[0] if items else None

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
        self._loaded_at = time.time()
        log.info("Catalogue refreshed: %d candidate tracks", len(self._tracks))

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
        self, target_mood: dict[str, float], exclude_ids: set[str], exploration: str = "medium"
    ) -> dict[str, Any] | None:
        if not self._tracks:
            self.refresh()
        candidates = [t for t in self._tracks if t["item_id"] not in exclude_ids]
        if not candidates:
            candidates = self._tracks
        if not candidates:
            return None
        ranked = sorted(candidates, key=lambda t: mood_distance(t["mood"], target_mood))
        pool_size = {"low": 5, "medium": 15, "high": 40}.get(exploration, 15)
        pool = ranked[: max(1, min(pool_size, len(ranked)))]
        return random.choice(pool)


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
    banned_track_ids: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "DjState":
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            return cls(**{k: raw.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()

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
        if item_id in self.banned_track_ids:
            self.banned_track_ids.remove(item_id)

    def ban(self, item_id: str) -> None:
        if item_id not in self.banned_track_ids:
            self.banned_track_ids.append(item_id)
        if item_id in self.liked_track_ids:
            self.liked_track_ids.remove(item_id)

    def recent_artists(self, minutes: float) -> set[str]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        return {h["artist"] for h in self.history if h.get("artist") and datetime.fromisoformat(h["at"]) > cutoff}


# ------------------------------------------------------------------ schedule

def load_schedule(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["dayparts"]


def current_daypart(schedule: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    hour = now.hour
    # Monday=0 .. Sunday=6, matching Python's own datetime.weekday().
    weekday = now.weekday()
    prev_weekday = (weekday - 1) % 7

    def day_ok(days: list[int] | None, d: int) -> bool:
        return days is None or d in days

    for daypart in schedule:
        days = daypart.get("days")
        start, end = daypart["hours"]
        if start <= end:
            if day_ok(days, weekday) and start <= hour < end:
                return daypart
        else:
            # Wraps past midnight, e.g. Fri/Sat 20:00-02:00 -- the evening
            # part belongs to `weekday`, but the early-morning tail belongs
            # to whichever day's evening started it, i.e. `prev_weekday`.
            # Without this split, "Friday 01:00" (prev_weekday=Thursday,
            # not in a Fri/Sat rule) would wrongly inherit Saturday's window
            # a full day early.
            evening = day_ok(days, weekday) and hour >= start
            morning = day_ok(days, prev_weekday) and hour < end
            if evening or morning:
                return daypart
    return schedule[0]


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
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.jf = Jellyfin(cfg)
        self.catalogue = Catalogue(cfg)
        self.schedule = load_schedule(cfg.schedule_file)
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
            }

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
            else:
                self.state.ban(item_id)
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
            return manual

        daypart = current_daypart(self.schedule)
        self.state.daypart = daypart["name"]
        exclude = self.state.recent_track_ids(self.cfg.recent_track_window_hours) | set(self.state.banned_track_ids)

        preferred = self._pick_preferred(daypart, exclude)
        if preferred:
            return preferred

        self.catalogue.refresh()
        exploration = daypart.get("exploration", "medium")
        candidate = self.catalogue.pick_target(daypart["mood"], exclude, exploration)
        if not candidate:
            raise RuntimeError("No candidate tracks available at all")
        item = self.jf.item(candidate["item_id"])
        return {
            "item_id": candidate["item_id"],
            "title": item.get("Name") if item else None,
            "author": ", ".join(item.get("Artists") or []) if item else None,
        }

    def _pick_preferred(self, daypart: dict[str, Any], exclude: set[str]) -> dict[str, Any] | None:
        """A daypart can name specific `artists`, Jellyfin `genres`, and/or
        AudioMuse-AI classifier `tags` it wants (e.g. a themed hour) instead
        of, or alongside, a mood target. `artists`/`genres` are pulled
        straight from Jellyfin; `tags` go through AudioMuse-AI's own
        genre/vocal-type classifier (see Catalogue.tag_pool) -- that's how a
        themed hour can mean "this vibe, whoever's actually in the library"
        rather than a fixed, hand-picked artist list."""
        artists = daypart.get("artists") or []
        genres = daypart.get("genres") or []
        tags = daypart.get("tags") or []
        if not artists and not genres and not tags:
            return None
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
        pool = [item for item in pool if item.get("Id") not in exclude]
        if not pool:
            return None
        item = random.choice(pool)
        return {
            "item_id": item["Id"],
            "title": item.get("Name"),
            "author": ", ".join(item.get("Artists") or []),
        }

    def _refill_plan(self) -> None:
        target = self._next_target()
        self.state.target = target
        self.state.phase = "transitioning"
        log.info("New target: %s - %s (%s)", target.get("author"), target.get("title"), self.state.daypart)

        bridge = build_bridge(self.jf, self.state.last_item_id, target["item_id"], self.cfg.bridge_max_tracks)
        if not bridge:
            bridge = [target]
        exclude = (
            self.state.recent_track_ids(self.cfg.recent_track_window_hours)
            | set(self.state.banned_track_ids)
            | {target["item_id"]}
        )
        dwell = build_dwell(self.jf, target["item_id"], self.cfg.dwell_tracks, exclude)

        self.state.plan = bridge + dwell
        self.state.phase = "dwelling" if dwell else "transitioning"

    def _tick(self) -> None:
        try:
            current_len = queue_length(self.cfg)
        except (OSError, LiquidsoapError) as exc:
            log.warning("Could not reach Liquidsoap telnet (%s), will retry", exc)
            return

        while current_len < self.cfg.lookahead_tracks:
            if not self.state.plan:
                self._refill_plan()
                if not self.state.plan:
                    break
            item = self.state.plan.pop(0)
            uri = annotate_uri(item, self.jf)
            if not uri:
                continue
            queue_push(self.cfg, uri)
            self.state.record_played(item)
            current_len += 1
            log.info(
                "Pushed: %s - %s (%d queued, %d planned)",
                item.get("author"), item.get("title"), current_len, len(self.state.plan),
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
