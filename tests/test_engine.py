"""End-to-end playout: real ffmpeg, real crossfade, real encoded output.

Generates short test tracks, runs the engine against them through a fake
Jellyfin, and checks the broadcast stream: continuous, correctly paced, and
containing an actual blend where the tracks meet.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time

import numpy as np
import pytest

from app.audio.engine import StationEngine
from app.config import get_settings
from app.library import MusicLibrary
from app.models import Track, TrackAnalysis
from app.presets import quick_library

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg is required for the playout test",
)

SR = 44100
TONES = {"a": 300, "b": 700}


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """Two 8 s tracks on distinct tones, each padded with silence."""
    directory = tmp_path_factory.mktemp("media")
    for name, hz in TONES.items():
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", f"sine=frequency={hz}:duration=7",
                "-af", "adelay=500|500,apad=pad_dur=0.5,volume=0.4",
                "-ar", str(SR), "-ac", "2", str(directory / f"{name}.flac"),
            ],
            check=True,
        )
    return directory


def make_tracks() -> list[Track]:
    return [
        Track(
            id="a", name="Track A", artist="Alpha", album_id="al1", duration_seconds=8.0,
            analysis=TrackAnalysis(bpm=124, camelot="8A", energy=0.5, lufs=-14, source="audiomuse"),
        ),
        Track(
            id="b", name="Track B", artist="Beta", album_id="al2", duration_seconds=8.0,
            analysis=TrackAnalysis(bpm=124, camelot="9A", energy=0.55, lufs=-14, source="audiomuse"),
        ),
    ]


class FakeLibrary(MusicLibrary):
    def __init__(self, media_dir):
        self._tracks = make_tracks()
        self.jellyfin = _FakeJellyfin(media_dir)
        self.audiomuse = _FakeAudioMuse()

    async def pool_for(self, spec, force_refresh=False):
        return list(self._tracks)

    def invalidate(self, station_id):
        pass


class _FakeJellyfin:
    def __init__(self, media_dir):
        self._dir = media_dir

    def stream_url(self, item_id):
        return str(self._dir / f"{item_id}.flac")

    async def report_playback(self, item_id, station_name):
        pass


class _FakeAudioMuse:
    enabled = False
    available = False

    async def analyse_many(self, item_ids, concurrency=8):
        return {}


def band_energy(pcm: np.ndarray, hz: float) -> float:
    window = pcm * np.hanning(pcm.size)
    spectrum = np.abs(np.fft.rfft(window))
    freqs = np.fft.rfftfreq(pcm.size, 1 / SR)
    return float(spectrum[(freqs > hz - 25) & (freqs < hz + 25)].max())


@pytest.mark.asyncio
async def test_engine_broadcasts_a_continuous_crossfaded_stream(media, _environment):
    settings = get_settings()
    spec = quick_library("Playout Test", station_id="playout-test")
    spec.stream = spec.stream.model_copy(update={"bitrate": 128})
    spec.crossfade = spec.crossfade.model_copy(
        update={"default_seconds": 3.0, "min_seconds": 2.0, "max_seconds": 4.0, "beat_align": False}
    )
    spec.rotation = spec.rotation.model_copy(
        update={"no_repeat_tracks": 1, "no_repeat_artist_tracks": 1,
                "max_per_artist_per_hour": 60, "max_per_album_per_hour": 60}
    )

    engine = StationEngine(spec, FakeLibrary(media), settings)
    await engine.start()
    listener = await engine.broadcaster.add("tester", "127.0.0.1", "pytest")

    received = bytearray()
    titles: list[str] = []

    async def collect():
        while True:
            chunk = await listener.queue.get()
            if chunk is None:
                break
            received.extend(chunk)

    collector = asyncio.create_task(collect())
    deadline = time.monotonic() + 16
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        snapshot = engine.snapshot()
        if snapshot.track and (not titles or titles[-1] != snapshot.track.title):
            titles.append(snapshot.track.title)

    transition = engine.snapshot().transition
    await engine.stop()
    collector.cancel()

    # It played, it moved on, and it told us how it blended.
    assert len(titles) >= 2, f"expected a track change, saw {titles}"
    assert transition is not None and transition.overlap_seconds >= 2.0
    assert transition.key_matched  # 8A -> 9A are neighbours on the wheel
    assert "tempo matched" in transition.reason

    # Roughly real-time: 128 kbit/s = 16 kB/s.
    seconds_of_audio = len(received) / 16_000
    assert 10 < seconds_of_audio < 20, f"{seconds_of_audio:.1f}s of audio for a ~16 s run"

    # Decode the broadcast and look for a window where both tones sound at once.
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-"],
        input=bytes(received), capture_output=True, check=True,
    ).stdout
    samples = np.frombuffer(decoded, dtype="<f4")
    assert samples.size > SR * 8

    window = SR // 2
    blended = silent = 0
    for start in range(0, samples.size - window, window):
        segment = samples[start : start + window]
        levels = {name: band_energy(segment, hz) for name, hz in TONES.items()}
        if sum(levels.values()) < 1.0:
            silent += 1
        if sum(1 for value in levels.values() if value > 10) >= 2:
            blended += 1

    assert blended >= 2, "no window contained both tracks - the crossfade did not happen"
    assert silent == 0, "the stream dropped to silence between tracks"
