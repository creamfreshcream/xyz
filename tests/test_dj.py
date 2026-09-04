import json
from datetime import datetime

import pytest

from dj.dj import (
    Catalogue,
    Config,
    Dj,
    DjState,
    _escape,
    annotate_uri,
    build_bridge,
    build_dwell,
    current_daypart,
    mood_distance,
    parse_feature_string,
)
from datetime import timedelta, timezone

SCHEDULE = [
    {"name": "morning", "hours": [6, 10], "mood": {"relaxed": 0.7}},
    {"name": "midday", "hours": [10, 14], "mood": {"danceable": 0.5}},
    {"name": "night", "hours": [23, 6], "mood": {"relaxed": 0.7, "sad": 0.4}},
]


def config(**overrides):
    env = {
        "JELLYFIN_URL": "http://jellyfin:8096/",
        "JELLYFIN_API_KEY": "secret",
        "POSTGRES_PASSWORD": "hunter2",
        **overrides,
    }
    import os

    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return Config.from_env()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_missing_credentials_are_reported(monkeypatch):
    monkeypatch.delenv("JELLYFIN_URL", raising=False)
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    from dj.dj import ConfigError

    with pytest.raises(ConfigError):
        Config.from_env()


def test_parse_feature_string_reads_label_value_pairs():
    assert parse_feature_string("happy:0.5,sad:0.1,party:0.9") == {
        "happy": 0.5,
        "sad": 0.1,
        "party": 0.9,
    }


def test_parse_feature_string_ignores_junk():
    assert parse_feature_string("") == {}
    assert parse_feature_string(None) == {}
    assert parse_feature_string("garbage,also:not-a-number") == {}


def test_mood_distance_only_considers_target_dimensions():
    mood = {"happy": 1.0, "sad": 1.0}
    # "sad" isn't in the target, so it must not affect the distance.
    assert mood_distance(mood, {"happy": 1.0}) == 0.0
    assert mood_distance(mood, {"happy": 0.0}) == 1.0


def test_mood_distance_treats_missing_track_dimensions_as_zero():
    assert mood_distance({}, {"party": 1.0}) == 1.0


def test_current_daypart_picks_the_matching_window():
    assert current_daypart(SCHEDULE, datetime(2024, 1, 1, 7))["name"] == "morning"
    assert current_daypart(SCHEDULE, datetime(2024, 1, 1, 11))["name"] == "midday"


def test_current_daypart_handles_windows_wrapping_past_midnight():
    assert current_daypart(SCHEDULE, datetime(2024, 1, 1, 23, 30))["name"] == "night"
    assert current_daypart(SCHEDULE, datetime(2024, 1, 1, 2))["name"] == "night"


def test_current_daypart_falls_back_to_the_first_entry_on_a_gap():
    # 14-23 isn't covered by SCHEDULE above.
    assert current_daypart(SCHEDULE, datetime(2024, 1, 1, 16))["name"] == "morning"


WEEKEND_SCHEDULE = [
    {"name": "techno_weekend", "days": [4, 5], "hours": [20, 2], "mood": {}},
    {"name": "fallback", "hours": [0, 24], "mood": {}},
]


def test_current_daypart_day_restricted_entry_only_matches_listed_days():
    # 2024-01-05 is a Friday (day 4), 2024-01-01 is a Monday (day 0).
    assert current_daypart(WEEKEND_SCHEDULE, datetime(2024, 1, 5, 21))["name"] == "techno_weekend"
    assert current_daypart(WEEKEND_SCHEDULE, datetime(2024, 1, 1, 21))["name"] == "fallback"


def test_current_daypart_wrap_tail_is_attributed_to_the_day_that_started_it():
    # Saturday/Sunday 01:00 are the tail of Friday's/Saturday's window.
    assert current_daypart(WEEKEND_SCHEDULE, datetime(2024, 1, 6, 1))["name"] == "techno_weekend"
    assert current_daypart(WEEKEND_SCHEDULE, datetime(2024, 1, 7, 1))["name"] == "techno_weekend"
    # Friday 01:00's previous day is Thursday, which isn't in [4, 5] -- must
    # not inherit Saturday's window a full day early.
    assert current_daypart(WEEKEND_SCHEDULE, datetime(2024, 1, 5, 1))["name"] == "fallback"


def test_escape_collapses_whitespace_and_escapes_quotes():
    assert _escape('He said "hi"\nloudly') == 'He said \\"hi\\" loudly'


class FakeJellyfin:
    def __init__(self, base_url="http://jellyfin:8096", api_key="secret", stream_mode="direct"):
        self.cfg = type("C", (), {"stream_mode": stream_mode, "jellyfin_url": base_url, "api_key": api_key, "max_bitrate": 320000, "transcode_codec": "mp3"})()

    def stream_url(self, item_id):
        return f"http://jellyfin:8096/Audio/{item_id}/stream?static=true&api_key=secret"


def test_annotate_uri_carries_metadata_from_a_jellyfin_item():
    uri = annotate_uri({"Id": "1", "Name": "Blue Monday", "Artists": ["New Order"], "Album": "Power, Corruption & Lies"}, FakeJellyfin())
    assert uri.startswith('annotate:title="Blue Monday",artist="New Order",album="Power, Corruption & Lies":http://jellyfin:8096/Audio/1/stream')


def test_annotate_uri_carries_metadata_from_an_audiomuse_track():
    uri = annotate_uri({"item_id": "1", "title": "X", "author": "Y", "album": "Z"}, FakeJellyfin())
    assert uri.startswith('annotate:title="X",artist="Y",album="Z":')


def test_annotate_uri_skips_items_without_an_id():
    assert annotate_uri({"Name": "orphan"}, FakeJellyfin()) is None


class FakePathJellyfin:
    def __init__(self, path):
        self._path = path

    def find_path(self, start_id, end_id, max_steps=300):
        return self._path


def test_build_bridge_drops_the_starting_track():
    path = [{"item_id": "from"}, {"item_id": "a"}, {"item_id": "to"}]
    jf = FakePathJellyfin(path)
    bridge = build_bridge(jf, "from", "to", max_tracks=10)
    assert [t["item_id"] for t in bridge] == ["a", "to"]


def test_build_bridge_returns_nothing_without_a_known_starting_point():
    jf = FakePathJellyfin([])
    assert build_bridge(jf, None, "to", max_tracks=10) == []


def test_build_bridge_subsamples_long_paths_but_keeps_the_target():
    path = [{"item_id": f"t{i}"} for i in range(50)]
    path[0] = {"item_id": "from"}
    path[-1] = {"item_id": "to"}
    jf = FakePathJellyfin(path)
    bridge = build_bridge(jf, "from", "to", max_tracks=10)
    assert len(bridge) <= 11
    assert bridge[-1]["item_id"] == "to"


class FakeSimilarJellyfin:
    def __init__(self, similar):
        self._similar = similar

    def similar_tracks(self, item_id, n=10):
        return self._similar


def test_build_dwell_excludes_already_played_tracks():
    similar = [{"item_id": "a"}, {"item_id": "b"}, {"item_id": "c"}]
    jf = FakeSimilarJellyfin(similar)
    dwell = build_dwell(jf, "target", n=2, exclude_ids={"a"})
    assert [t["item_id"] for t in dwell] == ["b", "c"]


def test_dj_state_round_trips_through_a_file(tmp_path):
    path = tmp_path / "state.json"
    state = DjState()
    state.record_played({"item_id": "1", "title": "A", "author": "Artist"})
    state.save(str(path))

    reloaded = DjState.load(str(path))
    assert reloaded.last_item_id == "1"
    assert reloaded.history[0]["title"] == "A"


def test_dj_state_recent_track_ids_respects_the_window(monkeypatch):
    state = DjState()
    state.history = [
        {"item_id": "old", "artist": "X", "at": "2000-01-01T00:00:00+00:00"},
    ]
    assert state.recent_track_ids(hours=3) == set()


def test_catalogue_pick_target_prefers_closest_mood_matches():
    # "low" exploration narrows the random pick to the 5 closest candidates
    # (see Catalogue.pick_target) -- with 10 tracks spread evenly by distance,
    # the far half must never be picked.
    cat = Catalogue(config())
    cat._tracks = [{"item_id": f"t{i}", "mood": {"happy": i / 10}} for i in range(10)]
    close_half = {f"t{i}" for i in range(5, 10)}
    for _ in range(20):
        picked = cat.pick_target({"happy": 1.0}, exclude_ids=set(), exploration="low")
        assert picked["item_id"] in close_half


def test_catalogue_pick_target_excludes_recent_tracks():
    cat = Catalogue(config())
    cat._tracks = [{"item_id": "only", "mood": {"happy": 1.0}}]
    picked = cat.pick_target({"happy": 1.0}, exclude_ids={"only"}, exploration="low")
    # Nothing left to exclude to, so it falls back to the full pool rather than None.
    assert picked["item_id"] == "only"


class FakePreferenceJellyfin:
    """Fake covering the artist/genre/tag lookups _pick_preferred uses."""

    def __init__(self, by_artist=None, by_genre=None, by_ids=None):
        self.by_artist = by_artist or {}
        self.by_genre = by_genre or {}
        self.by_ids = by_ids or {}

    def tracks_by_artist(self, name):
        return self.by_artist.get(name, [])

    def tracks_by_genre(self, genre):
        return self.by_genre.get(genre, [])

    def items_by_ids(self, ids):
        return [self.by_ids[i] for i in ids if i in self.by_ids]


class FakePgCursor:
    def __init__(self, server_id, rows):
        self.server_id = server_id
        self.rows = rows
        self._mode = None

    def execute(self, query, params=None):
        self._mode = "server_id" if "music_servers" in query else "rows"

    def fetchone(self):
        return (self.server_id,)

    def fetchall(self):
        return self.rows


class FakePgConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def test_catalogue_tag_pool_filters_by_label_and_threshold(monkeypatch):
    rows = [
        ("a", "female vocalists:0.55,pop:0.5"),
        ("b", "hip-hop:0.6,electronic:0.4"),
        ("c", "female vocalists:0.1"),
    ]
    monkeypatch.setattr("dj.dj.pg_connect", lambda cfg: FakePgConn(FakePgCursor("srv1", rows)))
    cat = Catalogue(config())
    assert cat.tag_pool(["female vocalists"], exclude_ids=set(), min_score=0.3) == ["a"]


def test_catalogue_tag_pool_excludes_given_ids(monkeypatch):
    rows = [("a", "female vocalists:0.55")]
    monkeypatch.setattr("dj.dj.pg_connect", lambda cfg: FakePgConn(FakePgCursor("srv1", rows)))
    cat = Catalogue(config())
    assert cat.tag_pool(["female vocalists"], exclude_ids={"a"}) == []


def make_dj(tmp_path, schedule):
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(json.dumps({"dayparts": schedule}))
    dj = Dj(config(DJ_SCHEDULE_FILE=str(schedule_path), DJ_STATE_FILE=str(tmp_path / "state.json")))
    return dj


def test_pick_preferred_returns_none_without_artists_or_genres(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    assert dj._pick_preferred({"mood": {}}, exclude=set()) is None


def test_pick_preferred_pulls_from_named_artists(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakePreferenceJellyfin(by_artist={"Some Artist": [{"Id": "1", "Name": "A", "Artists": ["Some Artist"]}]})
    picked = dj._pick_preferred({"artists": ["Some Artist"]}, exclude=set())
    assert picked["item_id"] == "1"


def test_pick_preferred_excludes_recent_tracks(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakePreferenceJellyfin(by_artist={"A": [{"Id": "1", "Name": "Only"}]})
    assert dj._pick_preferred({"artists": ["A"]}, exclude={"1"}) is None


def test_pick_preferred_falls_back_to_none_when_pool_empty(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakePreferenceJellyfin()
    assert dj._pick_preferred({"artists": ["Nobody Here"], "genres": ["Nonexistent"]}, exclude=set()) is None


def test_pick_preferred_pulls_from_tags_via_the_audiomuse_classifier(tmp_path, monkeypatch):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakePreferenceJellyfin(by_ids={"5": {"Id": "5", "Name": "Tagged Track"}})
    monkeypatch.setattr(dj.catalogue, "tag_pool", lambda tags, exclude, min_score=0.3: ["5"])
    picked = dj._pick_preferred({"tags": ["female vocalists"]}, exclude=set())
    assert picked["item_id"] == "5"


def test_pick_preferred_combines_artists_and_tags(tmp_path, monkeypatch):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakePreferenceJellyfin(
        by_artist={"A": [{"Id": "1", "Name": "By artist"}]},
        by_ids={"2": {"Id": "2", "Name": "By tag"}},
    )
    monkeypatch.setattr(dj.catalogue, "tag_pool", lambda tags, exclude, min_score=0.3: ["2"])
    ids = {dj._pick_preferred({"artists": ["A"], "tags": ["female vocalists"]}, exclude=set())["item_id"] for _ in range(20)}
    assert ids == {"1", "2"}


class FakePlaylistJellyfin:
    def __init__(self):
        self.created = []
        self.deleted = []
        self._next_id = 1

    def create_playlist(self, name, item_ids):
        if not item_ids:
            return None
        new_id = f"pl{self._next_id}"
        self._next_id += 1
        self.created.append((name, list(item_ids), new_id))
        return new_id

    def delete_item(self, item_id):
        self.deleted.append(item_id)


def history_entry(item_id):
    return {"item_id": item_id, "artist": "A", "title": "T", "at": datetime.now(timezone.utc).isoformat()}


def test_sync_playlist_does_nothing_without_history(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakePlaylistJellyfin()
    dj._sync_playlist()
    assert dj.jf.created == []
    assert dj.state.synced_playlist_id is None


def test_sync_playlist_creates_from_recent_history(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakePlaylistJellyfin()
    dj.state.history = [history_entry("1"), history_entry("2")]
    dj._sync_playlist()
    assert dj.jf.created == [(dj.cfg.playlist_name, ["1", "2"], "pl1")]
    assert dj.state.synced_playlist_id == "pl1"
    assert dj.state.last_playlist_sync is not None


def test_sync_playlist_replaces_the_previous_one(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakePlaylistJellyfin()
    dj.state.history = [history_entry("1")]
    dj._sync_playlist()
    dj.state.last_playlist_sync = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    dj.state.history.append(history_entry("2"))
    dj._sync_playlist()
    assert dj.jf.created[-1] == (dj.cfg.playlist_name, ["1", "2"], "pl2")
    assert dj.jf.deleted == ["pl1"]
    assert dj.state.synced_playlist_id == "pl2"


def test_sync_playlist_respects_the_sync_interval(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakePlaylistJellyfin()
    dj.state.history = [history_entry("1")]
    dj._sync_playlist()
    dj.state.history.append(history_entry("2"))
    dj._sync_playlist()  # too soon -- default interval hasn't elapsed
    assert len(dj.jf.created) == 1


def test_sync_playlist_caps_track_count(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakePlaylistJellyfin()
    dj.cfg = Config(**{**dj.cfg.__dict__, "playlist_max_tracks": 2})
    dj.state.history = [history_entry(str(i)) for i in range(5)]
    dj._sync_playlist()
    assert dj.jf.created[0][1] == ["3", "4"]
