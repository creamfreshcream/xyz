"""The station engine: one continuous, crossfaded stream per station.

Playout works on buffered edges rather than a rolling mixer. For every
transition the engine holds the outgoing track's tail and the incoming track's
head in memory, measures them, decides the blend (see ``crossfade.py``) and
renders the overlap in one go. That is what makes the fade *smart*: the plan is
made with the actual audio of both sides in hand, not from metadata alone.

Output is paced against the wall clock, encoded by ffmpeg and handed to the
broadcaster, which fans it out to the listeners.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from app.audio import analysis as an
from app.audio.broadcast import Broadcaster
from app.audio.crossfade import plan_crossfade, render_crossfade
from app.audio.ffmpeg import FFmpegError, PcmDecoder, PcmEncoder, probe_duration
from app.config import Settings
from app.library import MusicLibrary
from app.models import NowPlaying, StationSpec, Track, TrackAnalysis, TransitionInfo
from app.scheduler import Scheduler

log = logging.getLogger(__name__)

#: Give up on a station after this many consecutive unplayable tracks.
MAX_CONSECUTIVE_FAILURES = 8
#: How far ahead of the wall clock the engine is allowed to run.
LEAD_SECONDS = 1.0


@dataclass
class Deck:
    """One loaded track: its decoder, its analysis and its buffered head."""

    track: Track
    decoder: PcmDecoder
    analysis: TrackAnalysis
    duration: float
    gain_db: float = 0.0
    buffer: np.ndarray | None = None
    frames_emitted: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    async def read(self, frames: int) -> np.ndarray:
        """Read from the buffered head first, then from the decoder."""
        parts: list[np.ndarray] = []
        remaining = frames

        if self.buffer is not None and self.buffer.shape[0]:
            take = min(remaining, self.buffer.shape[0])
            parts.append(self.buffer[:take])
            self.buffer = self.buffer[take:]
            remaining -= take
        if remaining > 0:
            parts.append(await self.decoder.read(remaining))

        if not parts:
            return np.zeros((0, self.decoder.channels), dtype=np.float32)
        return parts[0] if len(parts) == 1 else np.concatenate(parts, axis=0)

    async def close(self) -> None:
        await self.decoder.close()


class StationEngine:
    """Plays one station."""

    def __init__(
        self,
        spec: StationSpec,
        library: MusicLibrary,
        settings: Settings,
    ) -> None:
        self.spec = spec
        self.library = library
        self.settings = settings
        self.scheduler = Scheduler(spec)
        self.broadcaster = Broadcaster(
            prebuffer_bytes=int(settings.prebuffer_seconds * spec.stream.bitrate * 1000 / 8)
        )

        self.sample_rate = spec.stream.sample_rate
        self.channels = spec.stream.channels
        self.state = "stopped"
        self.error: str | None = None

        self._task: asyncio.Task[None] | None = None
        self._pump: asyncio.Task[None] | None = None
        self._encoder: PcmEncoder | None = None
        self._stop = asyncio.Event()
        self._skip = asyncio.Event()

        self._current: Deck | None = None
        self._transition: TransitionInfo | None = None
        self._frames_emitted = 0
        self._clock_origin = 0.0
        self._tracks_played = 0
        self._sweeper_ids: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.error = None
        self.state = "starting"
        self._task = asyncio.create_task(self._run(), name=f"engine:{self.spec.id}")
        log.info("station %s: engine starting", self.spec.id)

    async def stop(self) -> None:
        if not self.running:
            return
        log.info("station %s: engine stopping", self.spec.id)
        self._stop.set()
        self._skip.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        await self._teardown()
        self.state = "stopped"

    def skip(self) -> None:
        """Start the transition to the next track immediately."""
        self._skip.set()

    def update_spec(self, spec: StationSpec) -> None:
        self.spec = spec
        self.scheduler.update_spec(spec)

    async def _teardown(self) -> None:
        if self._pump:
            self._pump.cancel()
            self._pump = None
        if self._current:
            await self._current.close()
            self._current = None
        if self._encoder:
            await self._encoder.close()
            self._encoder = None
        await self.broadcaster.close()

    # -- status ------------------------------------------------------------

    def snapshot(self) -> NowPlaying:
        deck = self._current
        elapsed = 0.0
        if deck is not None:
            elapsed = deck.frames_emitted / self.sample_rate
        return NowPlaying(
            station_id=self.spec.id,
            track=deck.track if deck else None,
            started_at=deck.started_at if deck else None,
            elapsed_seconds=round(elapsed, 1),
            duration_seconds=round(deck.duration, 1) if deck else 0.0,
            next_up=self.scheduler.upcoming(),
            listeners=self.broadcaster.listener_count,
            state=self.state,
            transition=self._transition,
        )

    def current_title(self) -> str:
        deck = self._current
        return deck.track.title if deck else self.spec.name

    # -- main loop ---------------------------------------------------------

    async def _run(self) -> None:
        try:
            self._encoder = PcmEncoder(
                self.spec.stream.format,
                self.spec.stream.bitrate,
                self.sample_rate,
                self.channels,
                binary=self.settings.ffmpeg_binary,
            )
            await self._encoder.open()
            self._pump = asyncio.create_task(self._pump_encoder(), name=f"pump:{self.spec.id}")
            self._clock_origin = time.monotonic()
            self._frames_emitted = 0
            self.state = "playing"
            await self._playout()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a station must report, not vanish
            log.exception("station %s: engine crashed", self.spec.id)
            self.error = str(exc)
            self.state = "error"
        finally:
            await self._teardown()

    async def _pump_encoder(self) -> None:
        """Encoded bytes -> listeners."""
        assert self._encoder is not None
        try:
            while not self._stop.is_set():
                chunk = await self._encoder.read(8192)
                if not chunk:
                    break
                self.broadcaster.publish(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("station %s: encoder pump failed: %s", self.spec.id, exc)

    async def _playout(self) -> None:
        current = await self._load_next()
        if current is None:
            return

        self._current = current
        await self._report_playback(current.track)

        while not self._stop.is_set():
            # 1. Play the body of the current track, keeping its tail back.
            tail = await self._stream_body(current)

            # 2. Load the next track; its head is buffered and analysed.
            nxt = await self._load_next()
            if nxt is None:
                await self._emit(np.zeros((self.sample_rate, self.channels), dtype=np.float32))
                if self._stop.is_set():
                    break
                continue

            # 3. Decide and render the transition.
            try:
                await self._transition_to(current, nxt, tail)
            except (FFmpegError, OSError) as exc:
                log.error("station %s: transition failed: %s", self.spec.id, exc)
                await nxt.close()
                continue

            await current.close()
            current = nxt
            self._current = current
            self._tracks_played += 1
            await self._report_playback(current.track)

    # -- track loading -----------------------------------------------------

    async def _next_track(self) -> Track | None:
        """The scheduler's next pick, with sweepers injected on schedule."""
        sweepers = self.spec.sweepers
        if (
            sweepers.enabled
            and self._tracks_played
            and self._tracks_played % sweepers.every_tracks == 0
        ):
            sweeper = await self._pick_sweeper()
            if sweeper is not None:
                return sweeper

        pool = await self.library.pool_for(self.spec)
        if not pool:
            log.warning("station %s: no tracks match its sources", self.spec.id)
            return None
        queued = self.scheduler.next_track(pool)
        return queued.track if queued else None

    async def _pick_sweeper(self) -> Track | None:
        if not self._sweeper_ids:
            spec = self.spec.sweepers
            if spec.item_ids:
                self._sweeper_ids = list(spec.item_ids)
            elif spec.playlist:
                tracks = await self.library.jellyfin.playlist_tracks(spec.playlist, limit=50)
                self._sweeper_ids = [t.id for t in tracks]
        if not self._sweeper_ids:
            return None
        item_id = self._sweeper_ids[self._tracks_played % len(self._sweeper_ids)]
        tracks = await self.library.jellyfin.by_ids([item_id])
        return tracks[0] if tracks else None

    async def _load_next(self) -> Deck | None:
        """Pick and open the next track, skipping over unplayable ones."""
        attempts = 0
        while not self._stop.is_set() and attempts < MAX_CONSECUTIVE_FAILURES:
            attempts += 1
            track = await self._next_track()
            if track is None:
                self.state = "idle"
                self.error = "no tracks available for this station"
                await asyncio.sleep(min(30, 2 * attempts))
                continue
            try:
                deck = await self._open_deck(track)
                self.state = "playing"
                self.error = None
                return deck
            except (FFmpegError, OSError) as exc:
                log.error("station %s: cannot play '%s': %s", self.spec.id, track.title, exc)
                await asyncio.sleep(0.5)

        self.error = "too many unplayable tracks in a row"
        self.state = "error"
        return None

    async def _open_deck(self, track: Track) -> Deck:
        url = self.library.jellyfin.stream_url(track.id)
        decoder = PcmDecoder(
            url,
            self.sample_rate,
            self.channels,
            binary=self.settings.ffmpeg_binary,
        )
        await decoder.open()
        try:
            await decoder.check_started()
        except FFmpegError:
            await decoder.close()
            raise

        duration = track.duration_seconds
        if duration <= 0:
            duration = await probe_duration(url, self.settings.ffmpeg_binary) or 0.0

        # Buffer enough of the head to analyse it and to render the longest
        # blend this station allows.
        head_seconds = self.spec.crossfade.max_seconds + 2.0
        head = await decoder.read_seconds(head_seconds)
        if head.shape[0] == 0:
            await decoder.close()
            raise FFmpegError(f"no audio decoded from '{track.title}'")

        analysis = self._analyse_head(track, head)
        deck = Deck(track=track, decoder=decoder, analysis=analysis, duration=duration)
        deck.gain_db = self._gain_for(analysis)

        # Drop the intro silence so the fade lands on the first note.
        if self.spec.crossfade.trim_silence and analysis.intro_silence > 0.05:
            trim = min(int(analysis.intro_silence * self.sample_rate), head.shape[0] - 1)
            head = head[trim:]
            deck.duration = max(0.0, deck.duration - analysis.intro_silence)
        deck.buffer = head
        return deck

    def _analyse_head(self, track: Track, head: np.ndarray) -> TrackAnalysis:
        """AudioMuse data, topped up with measurements of the audio itself."""
        analysis = track.analysis.model_copy(deep=True)
        if not self.settings.local_analysis_enabled:
            return analysis

        local = TrackAnalysis(source="local")
        local.intro_silence = an.leading_silence(
            head, self.sample_rate, self.spec.crossfade.silence_threshold_db
        )
        local.lufs = an.estimate_lufs(head)
        if analysis.bpm is None:
            local.bpm = an.estimate_bpm(head, self.sample_rate)

        merged = analysis.merged_with(local)
        # Measurements always win over metadata for the edges.
        merged.intro_silence = local.intro_silence
        if analysis.lufs is None:
            merged.lufs = local.lufs
        return merged

    def _gain_for(self, analysis: TrackAnalysis) -> float:
        spec = self.spec.crossfade
        if not spec.match_loudness or analysis.lufs is None:
            return 0.0
        return max(-spec.max_gain_db, min(spec.max_gain_db, spec.target_lufs - analysis.lufs))

    # -- streaming ---------------------------------------------------------

    async def _stream_body(self, deck: Deck) -> np.ndarray:
        """Play the track up to its tail, then return the tail for blending."""
        tail_seconds = self.spec.crossfade.max_seconds + 2.0
        tail_frames = int(tail_seconds * self.sample_rate)
        block = self.settings.block_frames

        total_frames = int(deck.duration * self.sample_rate) if deck.duration > 0 else 0
        self._skip.clear()

        while not self._stop.is_set() and not self._skip.is_set():
            if total_frames:
                remaining = total_frames - deck.frames_emitted
                if remaining <= tail_frames:
                    break
                to_read = min(block, remaining - tail_frames)
            else:
                to_read = block

            pcm = await deck.read(to_read)
            if pcm.shape[0] == 0:
                # Ended earlier than its metadata claimed - nothing to blend.
                return np.zeros((0, self.channels), dtype=np.float32)
            deck.frames_emitted += pcm.shape[0]
            await self._emit(an.apply_gain(pcm, deck.gain_db))

        if self._skip.is_set():
            log.info("station %s: skipping '%s'", self.spec.id, deck.track.title)

        tail = await deck.read(tail_frames)
        deck.frames_emitted += tail.shape[0]
        return tail

    async def _transition_to(self, current: Deck, nxt: Deck, tail: np.ndarray) -> None:
        """Blend ``current``'s tail into ``nxt``'s head and emit the result."""
        spec = self.spec.effective_crossfade()
        sample_rate = self.sample_rate

        if tail.shape[0]:
            silence = an.trailing_silence(tail, sample_rate, spec.silence_threshold_db)
            current.analysis.outro_silence = silence
            # Trim before measuring the self-fade, otherwise the trailing
            # silence reads as a fade and shortens the blend twice.
            if spec.trim_silence and silence > 0.05:
                drop = min(int(silence * sample_rate), tail.shape[0] - 1)
                tail = tail[: tail.shape[0] - drop]
            current.analysis.outro_fade = an.outro_fade_length(tail, sample_rate)
            if current.analysis.bpm is None:
                current.analysis.bpm = an.estimate_bpm(tail, sample_rate)

        plan = plan_crossfade(current.analysis, nxt.analysis, spec, current.duration, nxt.duration)
        # The decks already know their own gain; keep the blend consistent.
        plan.a_gain_db = current.gain_db
        plan.b_gain_db = nxt.gain_db

        # plan.a_trim_end is already removed from ``tail`` above.
        overlap = int(plan.overlap_seconds * sample_rate)
        overlap = min(overlap, tail.shape[0])
        if overlap <= 0 or tail.shape[0] == 0:
            # No usable tail (very short track, or a hard cut): straight splice.
            await self._emit(an.apply_gain(tail, current.gain_db))
            plan.overlap_seconds = 0.0
        else:
            pre = tail[: tail.shape[0] - overlap]
            if pre.shape[0]:
                await self._emit(an.apply_gain(pre, current.gain_db))

            head = await nxt.read(overlap)
            if head.shape[0] < overlap:
                # Incoming track is shorter than the planned blend.
                overlap = head.shape[0]
                plan.overlap_seconds = overlap / sample_rate
            if overlap > 0:
                mixed = render_crossfade(tail[tail.shape[0] - overlap :], head[:overlap], plan, sample_rate)
                nxt.frames_emitted += overlap
                await self._emit(mixed)

        if plan.gap_seconds > 0:
            await self._emit(
                np.zeros((int(plan.gap_seconds * sample_rate), self.channels), dtype=np.float32)
            )

        self._transition = TransitionInfo(
            overlap_seconds=round(plan.overlap_seconds, 2),
            curve=plan.curve,
            beat_matched=plan.beat_matched,
            key_matched=plan.key_matched,
            bass_swap=plan.bass_swap,
            gain_db=round(plan.b_gain_db, 1),
            reason=plan.reason,
        )
        log.info(
            "station %s: '%s' -> '%s' [%.1fs %s%s%s]",
            self.spec.id,
            current.track.title,
            nxt.track.title,
            plan.overlap_seconds,
            plan.curve,
            ", beat-matched" if plan.beat_matched else "",
            ", bass swap" if plan.bass_swap else "",
        )
        nxt.started_at = datetime.now(timezone.utc)

    async def _emit(self, pcm: np.ndarray) -> None:
        """Encode PCM, paced against the wall clock."""
        if pcm.size == 0 or self._encoder is None:
            return
        block = self.settings.block_frames
        for start in range(0, pcm.shape[0], block):
            if self._stop.is_set():
                return
            chunk = an.soft_limit(pcm[start : start + block])
            await self._encoder.write(chunk)
            self._frames_emitted += chunk.shape[0]

            # Stay at most LEAD_SECONDS ahead of real time.
            ahead = (self._clock_origin + self._frames_emitted / self.sample_rate) - time.monotonic()
            if ahead > LEAD_SECONDS:
                await asyncio.sleep(ahead - LEAD_SECONDS)

    async def _report_playback(self, track: Track) -> None:
        try:
            await self.library.jellyfin.report_playback(track.id, self.spec.name)
        except Exception as exc:  # noqa: BLE001 - reporting must never stop playout
            log.debug("playback report failed: %s", exc)
