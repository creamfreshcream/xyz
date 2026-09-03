"""Fan-out of one encoded stream to many listeners."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: ICY metadata is sent every this many bytes of audio.
ICY_METAINT = 16000


@dataclass
class Listener:
    id: str
    username: str
    remote: str
    user_agent: str
    connected_at: float = field(default_factory=time.time)
    queue: asyncio.Queue[bytes | None] = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    dropped_chunks: int = 0

    def info(self) -> dict[str, object]:
        return {
            "id": self.id,
            "username": self.username,
            "remote": self.remote,
            "user_agent": self.user_agent[:120],
            "connected_seconds": round(time.time() - self.connected_at, 1),
        }


class Broadcaster:
    """Distributes encoded audio chunks to connected listeners.

    A listener that cannot keep up loses the oldest queued chunk rather than
    stalling the station - one slow client must never hold up the broadcast.
    """

    def __init__(self, prebuffer_bytes: int = 96_000) -> None:
        self._listeners: dict[str, Listener] = {}
        self._prebuffer: deque[bytes] = deque()
        self._prebuffer_bytes = prebuffer_bytes
        self._prebuffer_size = 0
        self._lock = asyncio.Lock()

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    def listeners(self) -> list[dict[str, object]]:
        return [listener.info() for listener in self._listeners.values()]

    async def add(self, username: str, remote: str, user_agent: str) -> Listener:
        listener = Listener(str(uuid.uuid4()), username, remote, user_agent)
        async with self._lock:
            # Hand over the recent audio so playback starts immediately instead
            # of after the player has buffered from scratch.
            for chunk in self._prebuffer:
                try:
                    listener.queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    break
            self._listeners[listener.id] = listener
        log.info("listener %s connected (%s, %s)", listener.id[:8], username, remote)
        return listener

    async def remove(self, listener: Listener) -> None:
        async with self._lock:
            self._listeners.pop(listener.id, None)
        try:
            listener.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        log.info(
            "listener %s disconnected after %.0fs (%d dropped chunks)",
            listener.id[:8],
            time.time() - listener.connected_at,
            listener.dropped_chunks,
        )

    def publish(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._prebuffer.append(chunk)
        self._prebuffer_size += len(chunk)
        while self._prebuffer_size > self._prebuffer_bytes and self._prebuffer:
            self._prebuffer_size -= len(self._prebuffer.popleft())

        for listener in list(self._listeners.values()):
            try:
                listener.queue.put_nowait(chunk)
            except asyncio.QueueFull:
                try:
                    listener.queue.get_nowait()
                    listener.queue.put_nowait(chunk)
                    listener.dropped_chunks += 1
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def close(self) -> None:
        async with self._lock:
            listeners = list(self._listeners.values())
            self._listeners.clear()
        for listener in listeners:
            try:
                listener.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._prebuffer.clear()
        self._prebuffer_size = 0


def icy_metadata_block(title: str) -> bytes:
    """An ICY metadata block: one length byte + padded payload."""
    payload = f"StreamTitle='{title}';".encode("utf-8", "replace")
    padding = (16 - len(payload) % 16) % 16
    payload += b"\x00" * padding
    return bytes([len(payload) // 16]) + payload


async def icy_wrap(
    source, title_getter, metaint: int = ICY_METAINT
):
    """Interleave ICY title metadata into an audio byte stream.

    Players that send ``Icy-MetaData: 1`` expect a metadata block after every
    ``metaint`` bytes of audio; that is how the track title reaches the radio.
    """
    counter = 0
    last_title = None
    async for chunk in source:
        offset = 0
        while offset < len(chunk):
            space = metaint - counter
            piece = chunk[offset : offset + space]
            yield piece
            counter += len(piece)
            offset += len(piece)
            if counter >= metaint:
                counter = 0
                title = title_getter()
                if title != last_title:
                    last_title = title
                    yield icy_metadata_block(title)
                else:
                    yield b"\x00"  # "nothing changed"
