"""Owns the engines: starts them when someone listens, stops them when nobody does."""

from __future__ import annotations

import asyncio
import logging

from app.audio.engine import StationEngine
from app.config import Settings
from app.library import MusicLibrary
from app.models import NowPlaying, StationSpec
from app.store import StationStore

log = logging.getLogger(__name__)

SUPERVISOR_INTERVAL = 15.0


class StationManager:
    def __init__(self, store: StationStore, library: MusicLibrary, settings: Settings) -> None:
        self.store = store
        self.library = library
        self.settings = settings
        self._engines: dict[str, StationEngine] = {}
        self._idle_since: dict[str, float] = {}
        self._supervisor: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def startup(self) -> None:
        self._supervisor = asyncio.create_task(self._supervise(), name="station-supervisor")
        for spec in self.store.list():
            if spec.enabled and spec.stream.always_on:
                await self.ensure_running(spec.id)

    async def shutdown(self) -> None:
        if self._supervisor:
            self._supervisor.cancel()
            self._supervisor = None
        await asyncio.gather(
            *(engine.stop() for engine in list(self._engines.values())), return_exceptions=True
        )
        self._engines.clear()

    # -- engines -----------------------------------------------------------

    def engine(self, station_id: str) -> StationEngine | None:
        return self._engines.get(station_id)

    async def ensure_running(self, station_id: str) -> StationEngine:
        spec = self.store.get(station_id)
        if not spec.enabled:
            raise RuntimeError(f"station '{station_id}' is disabled")
        async with self._lock:
            engine = self._engines.get(station_id)
            if engine is None:
                engine = StationEngine(spec, self.library, self.settings)
                self._engines[station_id] = engine
            if not engine.running:
                await engine.start()
            self._idle_since.pop(station_id, None)
        return engine

    async def stop_station(self, station_id: str) -> None:
        async with self._lock:
            engine = self._engines.pop(station_id, None)
        if engine:
            await engine.stop()
        self._idle_since.pop(station_id, None)

    async def reload_station(self, spec: StationSpec) -> None:
        """Apply an edited spec: restart if the stream format changed."""
        engine = self._engines.get(spec.id)
        if engine is None:
            return
        stream_changed = engine.spec.stream != spec.stream
        if stream_changed or not spec.enabled:
            await self.stop_station(spec.id)
            if spec.enabled and spec.stream.always_on:
                await self.ensure_running(spec.id)
            return
        engine.update_spec(spec)
        self.library.invalidate(spec.id)

    def snapshot(self, station_id: str) -> NowPlaying:
        engine = self._engines.get(station_id)
        if engine is None:
            return NowPlaying(station_id=station_id, state="stopped")
        return engine.snapshot()

    def listener_count(self, station_id: str) -> int:
        engine = self._engines.get(station_id)
        return engine.broadcaster.listener_count if engine else 0

    # -- supervisor --------------------------------------------------------

    async def _supervise(self) -> None:
        """Stop idle stations, keep always-on ones alive, restart crashed ones."""
        while True:
            try:
                await asyncio.sleep(SUPERVISOR_INTERVAL)
                loop_time = asyncio.get_running_loop().time()
                for station_id, engine in list(self._engines.items()):
                    try:
                        spec = self.store.get(station_id)
                    except KeyError:
                        await self.stop_station(station_id)
                        continue

                    if engine.state == "error" and spec.enabled and spec.stream.always_on:
                        log.warning("station %s: restarting after error", station_id)
                        await self.stop_station(station_id)
                        await self.ensure_running(station_id)
                        continue

                    if spec.stream.always_on or not engine.running:
                        self._idle_since.pop(station_id, None)
                        continue

                    if engine.broadcaster.listener_count > 0:
                        self._idle_since.pop(station_id, None)
                        continue

                    since = self._idle_since.setdefault(station_id, loop_time)
                    if loop_time - since >= spec.stream.idle_timeout_seconds:
                        log.info("station %s: idle, stopping engine", station_id)
                        await self.stop_station(station_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the supervisor must never die
                log.exception("supervisor iteration failed")
