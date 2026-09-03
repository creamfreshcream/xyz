"""Station definitions on disk (``/config/stations.yaml``).

The file is the source of truth and is meant to be edited by hand as well as
through the API - which is why it is written back as readable YAML.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.models import StationSpec, StationUpdate, utcnow
from app.presets import example_stations

log = logging.getLogger(__name__)


class StationNotFound(KeyError):
    pass


class StationExists(ValueError):
    pass


class StationPersistError(RuntimeError):
    """The station file could not be written - usually a permissions problem."""


class StationStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._stations: dict[str, StationSpec] = {}
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self._path.exists():
            log.info("no station config at %s, writing examples", self._path)
            self._stations = {s.id: s for s in example_stations()}
            try:
                self._save()
            except StationPersistError as exc:
                # Serve the examples from memory rather than refusing to start.
                log.error("%s", exc)
            return
        try:
            raw = yaml.safe_load(self._path.read_text("utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.error("could not read %s: %s", self._path, exc)
            return

        entries = raw.get("stations", raw if isinstance(raw, list) else [])
        loaded: dict[str, StationSpec] = {}
        for entry in entries:
            try:
                spec = StationSpec(**entry)
            except ValidationError as exc:
                log.error("invalid station %r: %s", (entry or {}).get("id", "?"), exc)
                continue
            loaded[spec.id] = spec
        self._stations = loaded
        log.info("loaded %d stations from %s", len(loaded), self._path)

    def _save(self) -> None:
        payload = {
            "stations": [
                spec.model_dump(mode="json", exclude_defaults=False)
                for spec in self._stations.values()
            ]
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100), "utf-8"
            )
            tmp.replace(self._path)
        except OSError as exc:
            log.error("could not write %s: %s", self._path, exc)
            raise StationPersistError(
                f"cannot write {self._path}: {exc}. The config directory must be writable by "
                f"the container user (uid 10001), e.g. 'chown -R 10001:10001 ./config'."
            ) from exc

    def _save_or_restore(self, snapshot: dict[str, StationSpec]) -> None:
        """Persist, and roll the in-memory state back if the write fails.

        Without this a failed write would leave the running app disagreeing
        with the file on disk until the next restart.
        """
        try:
            self._save()
        except StationPersistError:
            self._stations = snapshot
            raise

    # -- access ------------------------------------------------------------

    def list(self) -> list[StationSpec]:
        return sorted(self._stations.values(), key=lambda s: s.name.lower())

    def get(self, station_id: str) -> StationSpec:
        try:
            return self._stations[station_id.strip().lower()]
        except KeyError as exc:
            raise StationNotFound(station_id) from exc

    def exists(self, station_id: str) -> bool:
        return station_id.strip().lower() in self._stations

    def create(self, spec: StationSpec) -> StationSpec:
        with self._lock:
            if spec.id in self._stations:
                raise StationExists(f"station '{spec.id}' already exists")
            snapshot = dict(self._stations)
            self._stations[spec.id] = spec
            self._save_or_restore(snapshot)
        log.info("created station '%s'", spec.id)
        return spec

    def update(self, station_id: str, update: StationUpdate) -> StationSpec:
        with self._lock:
            spec = self.get(station_id)
            changes: dict[str, Any] = update.model_dump(exclude_unset=True, exclude_none=True)
            if not changes:
                return spec
            merged = spec.model_dump()
            merged.update(changes)
            merged["updated_at"] = utcnow()
            new_spec = StationSpec(**merged)
            snapshot = dict(self._stations)
            self._stations[new_spec.id] = new_spec
            self._save_or_restore(snapshot)
        log.info("updated station '%s' (%s)", station_id, ", ".join(sorted(changes)))
        return new_spec

    def delete(self, station_id: str) -> None:
        with self._lock:
            spec = self.get(station_id)
            snapshot = dict(self._stations)
            del self._stations[spec.id]
            self._save_or_restore(snapshot)
        log.info("deleted station '%s'", station_id)

    def replace_all(self, specs: list[StationSpec]) -> None:
        with self._lock:
            snapshot = dict(self._stations)
            self._stations = {spec.id: spec for spec in specs}
            self._save_or_restore(snapshot)
