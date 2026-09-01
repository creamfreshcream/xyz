"""Per-guild audio player: queue, playback loop and voice-state handling."""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from collections import deque

import discord

from .embeds import now_playing_embed
from .jellyfin import JellyfinClient, Track

log = logging.getLogger(__name__)

FFMPEG_BEFORE = (
    "-nostdin -reconnect 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 5 -analyzeduration 0 -loglevel error"
)
FFMPEG_OPTIONS = "-vn"
PROGRESS_REPORT_INTERVAL = 15


class LoopMode(enum.Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


class PlayerError(RuntimeError):
    """Raised for user-facing player problems."""


class GuildPlayer:
    """Owns the queue and the playback task for a single guild."""

    def __init__(
        self,
        bot: discord.Client,
        guild: discord.Guild,
        jellyfin: JellyfinClient,
        *,
        volume: float = 0.5,
        idle_timeout: int = 300,
        max_queue_length: int = 500,
        report_playback: bool = True,
    ) -> None:
        self.bot = bot
        self.guild = guild
        self.jellyfin = jellyfin
        self.volume = volume
        self.idle_timeout = idle_timeout
        self.max_queue_length = max_queue_length
        self.report_playback = report_playback

        self.queue: deque[Track] = deque()
        self.history: deque[Track] = deque(maxlen=25)
        self.current: Track | None = None
        self.loop_mode = LoopMode.OFF
        self.text_channel: discord.abc.Messageable | None = None

        self._queue_event = asyncio.Event()
        self._track_done = asyncio.Event()
        self._audio: discord.PCMVolumeTransformer | None = None
        self._skip_requested = False
        self._stopped = False
        self._pending_seek: float | None = None
        self._started_at = 0.0
        self._paused_at: float | None = None
        self._task: asyncio.Task | None = None
        self._closed = False

    # ---------------------------------------------------------------- helpers

    @property
    def voice(self) -> discord.VoiceClient | None:
        vc = self.guild.voice_client
        return vc if isinstance(vc, discord.VoiceClient) else None

    @property
    def connected(self) -> bool:
        vc = self.voice
        return bool(vc and vc.is_connected())

    @property
    def is_playing(self) -> bool:
        vc = self.voice
        return bool(vc and (vc.is_playing() or vc.is_paused()))

    @property
    def is_paused(self) -> bool:
        vc = self.voice
        return bool(vc and vc.is_paused())

    @property
    def position(self) -> float:
        if self.current is None or not self._started_at:
            return 0.0
        now = self._paused_at if self._paused_at is not None else time.monotonic()
        elapsed = max(0.0, now - self._started_at)
        if self.current.duration:
            return min(elapsed, self.current.duration)
        return elapsed

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._runner(), name=f"player-{self.guild.id}"
            )

    # ------------------------------------------------------------- queue edits

    def add(self, tracks: list[Track]) -> int:
        """Append tracks, respecting the queue cap. Returns how many were added."""
        free = max(0, self.max_queue_length - len(self.queue))
        accepted = tracks[:free]
        self.queue.extend(accepted)
        if accepted:
            self._queue_event.set()
        return len(accepted)

    def add_next(self, tracks: list[Track]) -> int:
        free = max(0, self.max_queue_length - len(self.queue))
        accepted = tracks[:free]
        for track in reversed(accepted):
            self.queue.appendleft(track)
        if accepted:
            self._queue_event.set()
        return len(accepted)

    def shuffle(self) -> None:
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)

    def clear(self) -> None:
        self.queue.clear()

    def remove(self, index: int) -> Track:
        """Remove the 1-based queue entry at ``index``."""
        if not 1 <= index <= len(self.queue):
            raise PlayerError(f"Queue position {index} does not exist.")
        items = list(self.queue)
        track = items.pop(index - 1)
        self.queue = deque(items)
        return track

    def move(self, source: int, dest: int) -> Track:
        if not 1 <= source <= len(self.queue):
            raise PlayerError(f"Queue position {source} does not exist.")
        dest = max(1, min(dest, len(self.queue)))
        items = list(self.queue)
        track = items.pop(source - 1)
        items.insert(dest - 1, track)
        self.queue = deque(items)
        return track

    # ------------------------------------------------------------- transport

    def skip(self, count: int = 1) -> None:
        if self.current is None and not self.queue:
            raise PlayerError("Nothing is playing.")
        for _ in range(max(0, count - 1)):
            if self.queue:
                self.queue.popleft()
        self._skip_requested = True
        vc = self.voice
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        else:
            self._queue_event.set()

    def pause(self) -> None:
        vc = self.voice
        if not vc or not vc.is_playing():
            raise PlayerError("Nothing is playing.")
        vc.pause()
        self._paused_at = time.monotonic()

    def resume(self) -> None:
        vc = self.voice
        if not vc or not vc.is_paused():
            raise PlayerError("Playback is not paused.")
        if self._paused_at is not None:
            self._started_at += time.monotonic() - self._paused_at
            self._paused_at = None
        vc.resume()

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(2.0, volume))
        if self._audio is not None:
            self._audio.volume = self.volume

    def seek(self, position: float) -> None:
        if self.current is None:
            raise PlayerError("Nothing is playing.")
        if self.current.duration and position >= self.current.duration:
            raise PlayerError("That position is past the end of the track.")
        self._pending_seek = max(0.0, position)
        vc = self.voice
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    def stop(self) -> None:
        """Clear the queue and stop playback, staying connected."""
        self._stopped = True
        self.queue.clear()
        self.loop_mode = LoopMode.OFF
        vc = self.voice
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    # -------------------------------------------------------------- lifecycle

    async def connect_to(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        vc = self.voice
        if vc and vc.is_connected():
            if vc.channel and vc.channel.id != channel.id:
                await vc.move_to(channel)
            return
        await channel.connect(self_deaf=True)

    async def disconnect(self) -> None:
        vc = self.voice
        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception:  # pragma: no cover - best effort
                log.debug("Voice disconnect failed", exc_info=True)

    async def close(self) -> None:
        self._closed = True
        self.queue.clear()
        self.current = None
        task, self._task = self._task, None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.disconnect()

    # ------------------------------------------------------------ playback loop

    def _after_playback(self, error: Exception | None) -> None:
        if error:
            log.warning("Playback error in guild %s: %s", self.guild.id, error)
        self.bot.loop.call_soon_threadsafe(self._track_done.set)

    async def _next_track(self) -> Track | None:
        """Return the next track, or None when the player timed out while idle."""
        while not self._closed:
            if self._skip_requested:
                self._skip_requested = False
            elif (
                self.loop_mode is LoopMode.TRACK
                and self.current is not None
                and not self._stopped
            ):
                return self.current

            self._stopped = False

            if self.queue:
                return self.queue.popleft()

            self.current = None
            self._queue_event.clear()
            try:
                await asyncio.wait_for(self._queue_event.wait(), timeout=self.idle_timeout)
            except asyncio.TimeoutError:
                return None
        return None

    async def _runner(self) -> None:
        try:
            while not self._closed:
                track = await self._next_track()
                if track is None:
                    await self._announce("Nothing queued for a while — leaving the voice channel. 👋")
                    await self.disconnect()
                    return

                if not self.connected:
                    log.info("Voice gone in guild %s, stopping player", self.guild.id)
                    return

                if self.current is not track and self.current is not None:
                    self.history.appendleft(self.current)
                self.current = track
                start_at = 0.0

                while True:
                    try:
                        await self._play_once(track, start_at)
                    except Exception as exc:  # pragma: no cover - runtime safety
                        log.exception("Failed to play %s", track.label)
                        await self._announce(f"⚠️ Could not play **{track.label}**: {exc}")
                        break

                    if self._pending_seek is not None:
                        start_at = self._pending_seek
                        self._pending_seek = None
                        continue
                    break

                if (
                    self.loop_mode is LoopMode.QUEUE
                    and not self._stopped
                    and self.current is not None
                ):
                    self.queue.append(self.current)
                    self._queue_event.set()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # pragma: no cover - runtime safety
            log.exception("Player task crashed for guild %s", self.guild.id)

    async def _play_once(self, track: Track, start: float) -> None:
        vc = self.voice
        if vc is None or not vc.is_connected():
            raise PlayerError("Not connected to a voice channel.")

        before = FFMPEG_BEFORE
        if start > 0:
            before = f"-ss {start:.2f} " + before

        source = discord.FFmpegPCMAudio(
            track.stream_url, before_options=before, options=FFMPEG_OPTIONS
        )
        self._audio = discord.PCMVolumeTransformer(source, volume=self.volume)

        self._track_done.clear()
        self._paused_at = None
        self._started_at = time.monotonic() - start

        vc.play(self._audio, after=self._after_playback)

        if start == 0:
            await self._announce_now_playing(track)

        reporter = None
        if self.report_playback:
            await self.jellyfin.report_start(track)
            reporter = asyncio.create_task(self._report_progress_loop(track))

        try:
            await self._track_done.wait()
        finally:
            if reporter:
                reporter.cancel()
            position = self.position
            self._audio = None
            if self.report_playback and self._pending_seek is None:
                await self.jellyfin.report_stop(track, position)

    async def _report_progress_loop(self, track: Track) -> None:
        try:
            while True:
                await asyncio.sleep(PROGRESS_REPORT_INTERVAL)
                await self.jellyfin.report_progress(track, self.position, self.is_paused)
        except asyncio.CancelledError:  # pragma: no cover - normal teardown
            pass

    # ----------------------------------------------------------- announcements

    async def _announce(self, content: str) -> None:
        if self.text_channel is None:
            return
        try:
            await self.text_channel.send(content)
        except discord.HTTPException:  # pragma: no cover - best effort
            log.debug("Could not post to text channel", exc_info=True)

    async def _announce_now_playing(self, track: Track) -> None:
        if self.text_channel is None:
            return
        try:
            await self.text_channel.send(embed=now_playing_embed(self, track, compact=True))
        except discord.HTTPException:  # pragma: no cover - best effort
            log.debug("Could not post now-playing embed", exc_info=True)


class PlayerManager:
    """Keeps one :class:`GuildPlayer` per guild."""

    def __init__(self, bot: discord.Client, jellyfin: JellyfinClient, config) -> None:
        self.bot = bot
        self.jellyfin = jellyfin
        self.config = config
        self._players: dict[int, GuildPlayer] = {}

    def get(self, guild: discord.Guild) -> GuildPlayer:
        player = self._players.get(guild.id)
        if player is None:
            player = GuildPlayer(
                self.bot,
                guild,
                self.jellyfin,
                volume=self.config.default_volume,
                idle_timeout=self.config.idle_timeout,
                max_queue_length=self.config.max_queue_length,
                report_playback=self.config.report_playback,
            )
            self._players[guild.id] = player
        player.start()
        return player

    def existing(self, guild: discord.Guild) -> GuildPlayer | None:
        return self._players.get(guild.id)

    async def destroy(self, guild: discord.Guild) -> None:
        player = self._players.pop(guild.id, None)
        if player:
            await player.close()

    async def close_all(self) -> None:
        for guild_id in list(self._players):
            player = self._players.pop(guild_id)
            await player.close()
