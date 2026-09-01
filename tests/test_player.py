"""Exercises the playback loop with a fake voice client (no Discord, no ffmpeg)."""

from __future__ import annotations

import asyncio

import pytest

from jellyfin_bot import player as player_module
from jellyfin_bot.jellyfin import Track
from jellyfin_bot.player import GuildPlayer, LoopMode


class FakeSource:
    def __init__(self, *args, **kwargs) -> None:
        self.volume = 1.0

    def cleanup(self) -> None:  # pragma: no cover - parity with discord.py
        pass


class FakeVoiceClient:
    """Plays every track instantly and records what it was asked to play."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.played: list[str] = []
        self._playing = False
        self._paused = False
        self._after = None
        self.channel = None

    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return self._playing

    def is_paused(self) -> bool:
        return self._paused

    def play(self, source, *, after=None) -> None:
        self._playing = True
        self._after = after
        self.played.append(getattr(source, "track_id", "?"))
        self.loop.call_later(0.01, self._finish)

    def _finish(self) -> None:
        if not self._playing:
            return
        self._playing = False
        self._paused = False
        if self._after:
            self._after(None)

    def stop(self) -> None:
        self._finish()

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    async def disconnect(self, *, force: bool = False) -> None:
        self._playing = False


class FakeGuild:
    def __init__(self, voice_client) -> None:
        self.id = 1
        self.voice_client = voice_client


class FakeBot:
    def __init__(self, loop) -> None:
        self.loop = loop


def make_track(name: str) -> Track:
    return Track(
        id=name,
        name=name,
        artist="Tester",
        album="Album",
        duration=180.0,
        stream_url=f"http://example/{name}",
    )


@pytest.fixture()
def fake_player(monkeypatch):
    loop = asyncio.get_event_loop_policy().new_event_loop()
    asyncio.set_event_loop(loop)

    def fake_ffmpeg(url, **kwargs):
        source = FakeSource()
        source.track_id = url.rsplit("/", 1)[-1]
        return source

    def fake_transformer(source, volume=1.0):
        source.volume = volume
        return source

    monkeypatch.setattr(player_module.discord, "FFmpegPCMAudio", fake_ffmpeg)
    monkeypatch.setattr(player_module.discord, "PCMVolumeTransformer", fake_transformer)
    # isinstance(vc, discord.VoiceClient) must accept the fake
    monkeypatch.setattr(player_module.discord, "VoiceClient", FakeVoiceClient)

    vc = FakeVoiceClient(loop)
    guild = FakeGuild(vc)
    player = GuildPlayer(FakeBot(loop), guild, jellyfin=None, report_playback=False)
    player.idle_timeout = 0.05

    yield player, vc, loop

    loop.run_until_complete(player.close())
    loop.close()


async def drain(vc, expected: int, timeout: float = 2.0):
    """Wait until ``expected`` tracks have been handed to the voice client."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(vc.played) < expected and loop.time() < deadline:
        await asyncio.sleep(0.01)


def run(loop, coro_fn):
    """Run an async test body on the fixture's event loop."""
    loop.run_until_complete(coro_fn())


def test_queue_plays_in_order(fake_player):
    player, vc, loop = fake_player

    async def body():
        player.start()
        player.add([make_track("a"), make_track("b"), make_track("c")])
        await drain(vc, 3)
        assert vc.played == ["a", "b", "c"]

    run(loop, body)


def test_loop_track_repeats(fake_player):
    player, vc, loop = fake_player

    async def body():
        player.start()
        player.loop_mode = LoopMode.TRACK
        player.add([make_track("a")])
        await drain(vc, 3)
        assert vc.played[:3] == ["a", "a", "a"]
        player.loop_mode = LoopMode.OFF

    run(loop, body)


def test_loop_queue_recycles(fake_player):
    player, vc, loop = fake_player

    async def body():
        player.start()
        player.loop_mode = LoopMode.QUEUE
        player.add([make_track("a"), make_track("b")])
        await drain(vc, 4)
        assert vc.played[:4] == ["a", "b", "a", "b"]
        player.loop_mode = LoopMode.OFF

    run(loop, body)


def test_skip_advances_past_tracks(fake_player):
    player, vc, loop = fake_player

    async def body():
        player.start()
        player.add([make_track("a"), make_track("b"), make_track("c")])
        await drain(vc, 1)
        player.skip(2)  # drop "b" and skip out of "a"
        await drain(vc, 2)
        assert vc.played == ["a", "c"]

    run(loop, body)


def test_stop_clears_queue(fake_player):
    player, vc, loop = fake_player

    async def body():
        player.start()
        player.add([make_track("a"), make_track("b"), make_track("c")])
        await drain(vc, 1)
        player.stop()
        await asyncio.sleep(0.1)
        assert vc.played == ["a"]
        assert len(player.queue) == 0
        assert player.current is None

    run(loop, body)


def test_queue_editing():
    track_a, track_b, track_c = make_track("a"), make_track("b"), make_track("c")
    player = GuildPlayer(FakeBot(None), FakeGuild(None), jellyfin=None)

    assert player.add([track_a, track_b]) == 2
    assert player.add_next([track_c]) == 1
    assert [t.id for t in player.queue] == ["c", "a", "b"]

    assert player.move(1, 3).id == "c"
    assert [t.id for t in player.queue] == ["a", "b", "c"]

    assert player.remove(2).id == "b"
    assert [t.id for t in player.queue] == ["a", "c"]

    with pytest.raises(player_module.PlayerError):
        player.remove(9)

    player.clear()
    assert not player.queue


def test_queue_cap_is_enforced():
    player = GuildPlayer(FakeBot(None), FakeGuild(None), jellyfin=None, max_queue_length=2)
    assert player.add([make_track(str(i)) for i in range(5)]) == 2
    assert len(player.queue) == 2
