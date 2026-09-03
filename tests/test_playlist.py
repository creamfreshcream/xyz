from urllib.parse import parse_qs, urlparse

import pytest

from playlist_sync.jellyfin_playlist import (
    Config,
    ConfigError,
    Jellyfin,
    annotate_uri,
    render_playlist,
    stream_url,
    sync_once,
)

BASE_ENV = {
    "JELLYFIN_URL": "http://jellyfin:8096/",
    "JELLYFIN_API_KEY": "secret",
}


def config(**overrides):
    return Config.from_env({**BASE_ENV, **overrides})


def test_missing_credentials_are_reported():
    with pytest.raises(ConfigError):
        Config.from_env({"JELLYFIN_URL": "http://jellyfin:8096"})
    with pytest.raises(ConfigError):
        Config.from_env({"JELLYFIN_API_KEY": "secret"})


def test_trailing_slash_is_stripped_from_the_url():
    assert config().jellyfin_url == "http://jellyfin:8096"


def test_invalid_int_and_stream_mode_are_rejected():
    with pytest.raises(ConfigError):
        config(RADIO_LIMIT="lots")
    with pytest.raises(ConfigError):
        config(RADIO_STREAM_MODE="stream-it-somehow")


def test_direct_stream_url_asks_for_the_original_file():
    url = stream_url("abc123", config())
    parsed = urlparse(url)
    assert parsed.path == "/Audio/abc123/stream"
    assert parse_qs(parsed.query) == {"static": ["true"], "api_key": ["secret"]}


def test_transcode_stream_url_uses_the_universal_endpoint():
    url = stream_url("abc123", config(RADIO_STREAM_MODE="transcode", RADIO_MAX_BITRATE="192000"))
    parsed = urlparse(url)
    assert parsed.path == "/Audio/abc123/universal"
    query = parse_qs(parsed.query)
    assert query["audioCodec"] == ["mp3"]
    assert query["maxStreamingBitrate"] == ["192000"]


def test_annotate_uri_carries_metadata():
    uri = annotate_uri(
        {
            "Id": "1",
            "Name": "Blue Monday",
            "AlbumArtist": "New Order",
            "Album": "Power, Corruption & Lies",
            "ProductionYear": 1983,
        },
        config(),
    )
    assert uri.startswith(
        'annotate:title="Blue Monday",artist="New Order",'
        'album="Power, Corruption & Lies",year="1983":http://jellyfin:8096/Audio/1/stream'
    )


def test_annotate_uri_escapes_quotes_and_collapses_newlines():
    uri = annotate_uri({"Id": "1", "Name": 'He said "hi"\nloudly'}, config())
    assert uri.startswith('annotate:title="He said \\"hi\\" loudly":')


def test_annotate_uri_falls_back_to_the_first_artist():
    uri = annotate_uri({"Id": "1", "Name": "X", "Artists": ["Aphex Twin"]}, config())
    assert 'artist="Aphex Twin"' in uri


def test_annotate_uri_skips_items_without_an_id():
    assert annotate_uri({"Name": "orphan"}, config()) is None


def test_render_playlist_writes_one_line_per_track():
    playlist = render_playlist(
        [{"Id": "1", "Name": "A"}, {"Name": "no id"}, {"Id": "2", "Name": "B"}],
        config(),
    )
    lines = playlist.splitlines()
    assert lines[0] == "#EXTM3U"
    assert len([line for line in lines if line.startswith("annotate:")]) == 2
    assert playlist.endswith("\n")


class FakeJellyfin(Jellyfin):
    """Records requests and replays canned responses instead of doing HTTP."""

    def __init__(self, responses):
        super().__init__("http://jellyfin:8096", "secret")
        self.responses = responses
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, params))
        return self.responses[path]


def test_favorites_require_a_user_id():
    client = FakeJellyfin({})
    with pytest.raises(ConfigError):
        client.tracks(config(RADIO_FAVORITES_ONLY="true"))


def test_favorites_filter_is_passed_through():
    client = FakeJellyfin({"/Items": {"Items": [{"Id": "1", "Name": "A"}]}})
    client.tracks(config(RADIO_FAVORITES_ONLY="yes", JELLYFIN_USER_ID="u1"))
    _, params = client.calls[0]
    assert params["Filters"] == "IsFavorite"
    assert params["userId"] == "u1"


def test_genres_are_pipe_separated():
    client = FakeJellyfin({"/Items": {"Items": []}})
    client.tracks(config(RADIO_GENRES="Jazz, Soul "))
    _, params = client.calls[0]
    assert params["Genres"] == "Jazz|Soul"


def test_named_playlist_is_resolved_then_expanded():
    client = FakeJellyfin(
        {
            "/Items": {"Items": [{"Id": "pl1", "Name": "Late Night"}]},
            "/Playlists/pl1/Items": {
                "Items": [
                    {"Id": "1", "Name": "A", "Type": "Audio"},
                    {"Id": "2", "Name": "cover.jpg", "Type": "Photo"},
                ]
            },
        }
    )
    tracks = client.tracks(config(RADIO_JELLYFIN_PLAYLIST="late night"))
    assert [t["Id"] for t in tracks] == ["1"]


def test_unknown_playlist_name_raises():
    client = FakeJellyfin({"/Items": {"Items": [{"Id": "pl1", "Name": "Other"}]}})
    with pytest.raises(ConfigError):
        client.tracks(config(RADIO_JELLYFIN_PLAYLIST="Late Night"))


def test_sync_once_keeps_the_old_playlist_when_jellyfin_returns_nothing(tmp_path):
    target = tmp_path / "radio.m3u"
    target.write_text("#EXTM3U\nannotate:title=\"old\":http://x\n")
    client = FakeJellyfin({"/Items": {"Items": []}})

    written = sync_once(client, config(RADIO_PLAYLIST_FILE=str(target)))

    assert written == 0
    assert "old" in target.read_text()


def test_sync_once_replaces_the_playlist_atomically(tmp_path):
    target = tmp_path / "nested" / "radio.m3u"
    client = FakeJellyfin({"/Items": {"Items": [{"Id": "1", "Name": "A"}]}})

    written = sync_once(client, config(RADIO_PLAYLIST_FILE=str(target)))

    assert written == 1
    assert 'title="A"' in target.read_text()
    assert not (target.parent / "radio.m3u.tmp").exists()
