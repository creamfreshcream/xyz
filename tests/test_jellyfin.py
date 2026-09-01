import pytest

from jellyfin_bot.jellyfin import (
    JellyfinClient,
    format_duration,
    seconds_to_ticks,
    ticks_to_seconds,
)


@pytest.fixture()
def client() -> JellyfinClient:
    return JellyfinClient(
        "https://media.example.com/",
        api_key="secret",
        user_id="u1",
        device_id="dev",
    )


def test_ticks_round_trip():
    assert ticks_to_seconds(1_800_000_000) == 180.0
    assert ticks_to_seconds(None) == 0.0
    assert ticks_to_seconds("nope") == 0.0
    assert seconds_to_ticks(180) == 1_800_000_000


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0:00"), (9, "0:09"), (61, "1:01"), (3600, "1:00:00"), (3725, "1:02:05")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_base_url_is_normalised(client):
    assert client.base_url == "https://media.example.com"


def test_direct_stream_url(client):
    url = client.stream_url("abc")
    assert url.startswith("https://media.example.com/Audio/abc/stream?")
    assert "static=true" in url
    assert "api_key=secret" in url


def test_transcoded_stream_url(client):
    client.direct_stream = False
    url = client.stream_url("abc")
    assert url.startswith("https://media.example.com/Audio/abc/universal?")
    assert "AudioCodec=aac" in url
    assert "DeviceId=dev" in url


def test_auth_header_includes_token(client):
    header = client._auth_header()
    assert header.startswith("MediaBrowser ")
    assert 'Token="secret"' in header
    assert 'DeviceId="dev"' in header


def test_to_track_maps_fields(client):
    track = client.to_track(
        {
            "Id": "1234",
            "Name": "Blue Monday",
            "Artists": ["New Order"],
            "Album": "Power, Corruption & Lies",
            "AlbumId": "album-1",
            "RunTimeTicks": 4_500_000_000,
            "AlbumPrimaryImageTag": "tag123",
        }
    )
    assert track.name == "Blue Monday"
    assert track.artist == "New Order"
    assert track.duration == 450.0
    assert track.label == "New Order — Blue Monday"
    assert "Items/album-1/Images/Primary" in (track.art_url or "")


def test_to_track_without_artist_falls_back(client):
    track = client.to_track({"Id": "1", "Name": "Untitled", "AlbumArtist": "Various"})
    assert track.artist == "Various"
    assert track.art_url is None
