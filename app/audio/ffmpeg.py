"""ffmpeg subprocess wrappers: URL -> float PCM, and PCM -> encoded stream."""

from __future__ import annotations

import asyncio
import logging
from collections import deque

import numpy as np

log = logging.getLogger(__name__)

BYTES_PER_SAMPLE = 4  # f32le


class FFmpegError(RuntimeError):
    pass


class _Process:
    """Shared plumbing: spawn ffmpeg and keep its last stderr lines around."""

    def __init__(self, binary: str) -> None:
        self._binary = binary
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr: deque[str] = deque(maxlen=15)
        self._stderr_task: asyncio.Task[None] | None = None

    async def _spawn(self, args: list[str], stdin: int | None, stdout: int | None) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._binary,
                *args,
                stdin=stdin,
                stdout=stdout,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise FFmpegError(f"ffmpeg binary '{self._binary}' not found") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        # Must be drained continuously, otherwise ffmpeg blocks on a full pipe.
        assert self._proc is not None and self._proc.stderr is not None
        try:
            async for line in self._proc.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._stderr.append(text)
        except (asyncio.CancelledError, ValueError):
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("stderr drain ended: %s", exc)

    @property
    def stderr_tail(self) -> str:
        return " | ".join(self._stderr)

    async def close(self) -> None:
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None


class PcmDecoder(_Process):
    """Decodes any input URL/file to interleaved float32 PCM."""

    def __init__(
        self,
        url: str,
        sample_rate: int,
        channels: int,
        start_offset: float = 0.0,
        binary: str = "ffmpeg",
    ) -> None:
        super().__init__(binary)
        self.url = url
        self.sample_rate = sample_rate
        self.channels = channels
        self.start_offset = max(0.0, start_offset)
        self.frames_read = 0
        self.eof = False

    async def open(self) -> None:
        args = ["-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.url.startswith("http"):
            # Survive a brief Jellyfin hiccup instead of dropping the track.
            args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if self.start_offset > 0:
            args += ["-ss", f"{self.start_offset:.3f}"]
        args += [
            "-i", self.url,
            "-vn",
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "pipe:1",
        ]
        await self._spawn(args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE)

    async def read(self, frames: int) -> np.ndarray:
        """Read up to ``frames`` frames. A short result means end of track."""
        if self._proc is None or self._proc.stdout is None or self.eof:
            return np.zeros((0, self.channels), dtype=np.float32)

        wanted = frames * self.channels * BYTES_PER_SAMPLE
        chunks: list[bytes] = []
        collected = 0
        while collected < wanted:
            chunk = await self._proc.stdout.read(wanted - collected)
            if not chunk:
                self.eof = True
                break
            chunks.append(chunk)
            collected += len(chunk)

        raw = b"".join(chunks)
        # Guard against a truncated final frame.
        usable = len(raw) - (len(raw) % (self.channels * BYTES_PER_SAMPLE))
        if usable <= 0:
            return np.zeros((0, self.channels), dtype=np.float32)
        # frombuffer is read-only and the mixer writes in place, so copy.
        pcm = np.nan_to_num(np.frombuffer(raw[:usable], dtype="<f4").reshape(-1, self.channels))
        self.frames_read += pcm.shape[0]
        return pcm

    async def read_seconds(self, seconds: float) -> np.ndarray:
        return await self.read(int(seconds * self.sample_rate))

    async def check_started(self) -> None:
        """Raise if ffmpeg died immediately (bad URL, unsupported codec, 401)."""
        if self._proc is None:
            raise FFmpegError("decoder not opened")
        await asyncio.sleep(0.2)
        if self._proc.returncode not in (None, 0):
            raise FFmpegError(f"ffmpeg could not open input: {self.stderr_tail or 'unknown error'}")


class PcmEncoder(_Process):
    """Encodes float32 PCM into the station's broadcast format."""

    CODECS = {
        "mp3": ("libmp3lame", "mp3"),
        "aac": ("aac", "adts"),
        "opus": ("libopus", "ogg"),
    }

    def __init__(
        self,
        fmt: str,
        bitrate: int,
        sample_rate: int,
        channels: int,
        binary: str = "ffmpeg",
    ) -> None:
        super().__init__(binary)
        self.format = fmt
        self.bitrate = bitrate
        self.sample_rate = sample_rate
        self.channels = channels

    async def open(self) -> None:
        codec, container = self.CODECS.get(self.format, self.CODECS["mp3"])
        args = [
            "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "f32le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-i", "pipe:0",
            "-c:a", codec,
            "-b:a", f"{self.bitrate}k",
            "-f", container,
        ]
        if container == "mp3":
            # No Xing/LAME header: this is an endless stream, not a file.
            args += ["-write_xing", "0", "-id3v2_version", "0"]
        args.append("pipe:1")
        await self._spawn(args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)

    async def write(self, pcm: np.ndarray) -> None:
        if self._proc is None or self._proc.stdin is None or pcm.size == 0:
            return
        try:
            self._proc.stdin.write(np.ascontiguousarray(pcm, dtype="<f4").tobytes())
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise FFmpegError(f"encoder pipe closed: {self.stderr_tail or exc}") from exc

    async def read(self, size: int = 8192) -> bytes:
        if self._proc is None or self._proc.stdout is None:
            return b""
        return await self._proc.stdout.read(size)


async def probe_duration(url: str, binary: str = "ffmpeg") -> float | None:
    """Container duration in seconds via ffprobe, or None if unavailable."""
    probe = binary.replace("ffmpeg", "ffprobe")
    try:
        proc = await asyncio.create_subprocess_exec(
            probe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return float(stdout.decode().strip())
    except (FileNotFoundError, ValueError, asyncio.TimeoutError):
        return None
