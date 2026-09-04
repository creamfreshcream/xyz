import json
from datetime import datetime

import pytest

import struct as struct_mod

from dj.dj import (
    AlbumIndex,
    Catalogue,
    Config,
    Dj,
    DjState,
    SongMap,
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


def test_dj_state_load_defaults_a_null_list_field_to_empty(tmp_path):
    # A field added after some state files already existed on disk -- or one
    # a prior buggy load() already round-tripped as a literal `null` -- must
    # come back as [], not None (which crashes anything doing `x in field`).
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"last_item_id": "1", "liked_track_ids": None}))
    state = DjState.load(str(path))
    assert state.liked_track_ids == []
    assert state.last_item_id == "1"


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


def test_djstate_like_marks_liked_and_clears_penalty():
    state = DjState()
    state.penalize("1")
    state.like("1")
    assert state.liked_track_ids == ["1"]
    assert state.active_penalty_ids() == set()


def test_djstate_penalize_bans_and_clears_like():
    state = DjState()
    state.like("1")
    state.penalize("1")
    assert state.active_penalty_ids() == {"1"}
    assert state.liked_track_ids == []


def test_djstate_penalize_escalates_on_repeat_offenses():
    state = DjState()
    state.penalize("1")
    first_until = state.penalties["1"]["until"]
    state.penalize("1")
    assert state.penalties["1"]["strikes"] == 2
    assert state.penalties["1"]["until"] > first_until


def test_djstate_like_and_ban_are_idempotent():
    state = DjState()
    state.like("1")
    state.like("1")
    assert state.liked_track_ids == ["1"]


def test_djstate_penalize_is_scoped_to_the_given_daypart():
    state = DjState()
    state.penalize("1", "night")
    assert state.active_penalty_ids("night") == {"1"}
    assert state.active_penalty_ids("morning") == set()
    assert state.active_penalty_ids() == set()


def test_djstate_penalize_without_a_daypart_is_global():
    state = DjState()
    state.penalize("1")
    assert state.active_penalty_ids("night") == {"1"}
    assert state.active_penalty_ids("morning") == {"1"}
    assert state.active_penalty_ids() == {"1"}


class FakeFeedbackJellyfin:
    def __init__(self):
        self.favorited = []

    def set_favorite(self, item_id, favorite=True):
        self.favorited.append((item_id, favorite))


def test_record_feedback_up_favorites_and_likes(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakeFeedbackJellyfin()
    dj.state.last_item_id = "42"
    result = dj.record_feedback({"vote": "up"})
    assert result == {"item_id": "42", "vote": "up"}
    assert dj.state.liked_track_ids == ["42"]
    assert dj.jf.favorited == [("42", True)]


def test_record_feedback_down_bans_the_current_track(tmp_path, monkeypatch):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakeFeedbackJellyfin()
    dj.state.last_item_id = "42"
    skipped = []
    monkeypatch.setattr("dj.dj.telnet_command", lambda host, port, cmd: skipped.append(cmd) or "Done.")
    result = dj.record_feedback({"vote": "down"})
    assert result == {"item_id": "42", "vote": "down"}
    assert dj.state.active_penalty_ids() == {"42"}
    assert skipped == ["dj_queue.skip"]


def test_record_feedback_down_only_bans_the_current_daypart(tmp_path, monkeypatch):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakeFeedbackJellyfin()
    dj.state.last_item_id = "42"
    dj.state.daypart = "night"
    monkeypatch.setattr("dj.dj.telnet_command", lambda host, port, cmd: "Done.")
    dj.record_feedback({"vote": "down"})
    assert dj.state.active_penalty_ids("night") == {"42"}
    assert dj.state.active_penalty_ids("morning") == set()


def test_record_feedback_down_on_a_non_current_track_does_not_skip(tmp_path, monkeypatch):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakeFeedbackJellyfin()
    dj.state.last_item_id = "42"
    skipped = []
    monkeypatch.setattr("dj.dj.telnet_command", lambda host, port, cmd: skipped.append(cmd) or "Done.")
    dj.record_feedback({"vote": "down", "item_id": "99"})
    assert dj.state.active_penalty_ids() == {"99"}
    assert skipped == []


def test_record_feedback_requires_a_current_track(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakeFeedbackJellyfin()
    with pytest.raises(ValueError):
        dj.record_feedback({"vote": "up"})


def test_record_feedback_rejects_unknown_vote(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakeFeedbackJellyfin()
    dj.state.last_item_id = "1"
    with pytest.raises(ValueError):
        dj.record_feedback({"vote": "sideways"})


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


class FakeMapPgCursor:
    def __init__(self, proj_blob, id_map_json, server_id, fp_rows):
        self.proj_blob = proj_blob
        self.id_map_json = id_map_json
        self.server_id = server_id
        self.fp_rows = fp_rows
        self._mode = None

    def execute(self, query, params=None):
        if "map_projection_data" in query:
            self._mode = "map"
        elif "music_servers" in query:
            self._mode = "server"
        elif "track_server_map" in query:
            self._mode = "fp"

    def fetchone(self):
        if self._mode == "map":
            return (self.proj_blob, self.id_map_json)
        if self._mode == "server":
            return (self.server_id,)
        return None

    def fetchall(self):
        return self.fp_rows


def make_song_map(monkeypatch, points, fp_ids, fp_rows):
    blob = struct_mod.pack(f"<{len(points) * 2}f", *[v for p in points for v in p])
    cursor = FakeMapPgCursor(blob, json.dumps(fp_ids), "srv1", fp_rows)
    monkeypatch.setattr("dj.dj.pg_connect", lambda cfg: FakePgConn(cursor))
    return SongMap(config())


def test_song_map_position_looks_up_by_jellyfin_id(monkeypatch):
    sm = make_song_map(
        monkeypatch,
        points=[(0.0, 0.0), (1.0, 1.0), (0.5, -0.5)],
        fp_ids=["fp1", "fp2", "fp3"],
        fp_rows=[("fp2", "jf-b")],
    )
    pos = sm.position("jf-b")
    assert pos["x"] == 1.0 and pos["y"] == 1.0
    assert pos["bounds"] == {"xmin": 0.0, "xmax": 1.0, "ymin": -0.5, "ymax": 1.0}


def test_song_map_position_returns_none_for_an_unmapped_track(monkeypatch):
    sm = make_song_map(
        monkeypatch,
        points=[(0.0, 0.0)],
        fp_ids=["fp1"],
        fp_rows=[("fp1", "jf-a")],
    )
    assert sm.position("nope") is None


def _history_entry(item_id, minutes_ago):
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {"item_id": item_id, "artist": "A", "title": item_id, "at": at.isoformat()}


def test_map_trail_orders_oldest_to_newest_and_skips_unmapped_tracks(tmp_path, monkeypatch):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.song_map = make_song_map(
        monkeypatch,
        points=[(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)],
        fp_ids=["fp1", "fp2", "fp3"],
        fp_rows=[("fp1", "jf-a"), ("fp2", "jf-b"), ("fp3", "jf-c")],
    )
    dj.state.history = [
        _history_entry("jf-a", minutes_ago=30),
        _history_entry("unmapped", minutes_ago=20),
        _history_entry("jf-b", minutes_ago=10),
        _history_entry("jf-c", minutes_ago=0),
    ]
    trail = dj._map_trail()
    assert trail == [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"x": 2.0, "y": 2.0}]


def test_map_trail_is_capped_to_the_configured_length(tmp_path, monkeypatch):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    n = dj.MAP_TRAIL_LENGTH + 3
    points = [(float(i), float(i)) for i in range(n)]
    fp_ids = [f"fp{i}" for i in range(n)]
    fp_rows = [(f"fp{i}", f"jf-{i}") for i in range(n)]
    dj.song_map = make_song_map(monkeypatch, points=points, fp_ids=fp_ids, fp_rows=fp_rows)
    dj.state.history = [_history_entry(f"jf-{i}", minutes_ago=n - i) for i in range(n)]
    trail = dj._map_trail()
    assert len(trail) == dj.MAP_TRAIL_LENGTH
    assert trail[-1] == {"x": float(n - 1), "y": float(n - 1)}
    assert trail[0] == {"x": float(n - dj.MAP_TRAIL_LENGTH), "y": float(n - dj.MAP_TRAIL_LENGTH)}


# --------------------------------------------------------- album awareness

def make_track(track_id, album_id, index, disc=1, artists=None, runtime_seconds=180.0):
    return {
        "Id": track_id,
        "Name": f"Track {track_id}",
        "AlbumId": album_id,
        "IndexNumber": index,
        "ParentIndexNumber": disc,
        "Artists": artists or ["Artist"],
        "RunTimeTicks": int(runtime_seconds * 10_000_000),
    }


class FakeAlbumJellyfin:
    """Doubles as both the album_tracks() source AlbumIndex needs and the
    item() lookup Dj._full_item() needs, keyed off the same track dicts."""

    def __init__(self, items_by_id):
        self.items_by_id = items_by_id

    def item(self, item_id):
        return self.items_by_id.get(item_id)

    def album_tracks(self, album_id):
        return [t for t in self.items_by_id.values() if t.get("AlbumId") == album_id]


def test_album_index_next_track_returns_the_following_track():
    a1, a2 = make_track("a1", "alb", 1), make_track("a2", "alb", 2)
    idx = AlbumIndex(FakeAlbumJellyfin({"a1": a1, "a2": a2}))
    assert idx.next_track(a1)["Id"] == "a2"


def test_album_index_next_track_returns_none_at_the_end_of_the_album():
    a1 = make_track("a1", "alb", 1)
    idx = AlbumIndex(FakeAlbumJellyfin({"a1": a1}))
    assert idx.next_track(a1) is None


def test_album_index_next_track_respects_disc_boundaries():
    a1 = make_track("a1", "alb", 1, disc=1)
    b1 = make_track("b1", "alb", 1, disc=2)
    idx = AlbumIndex(FakeAlbumJellyfin({"a1": a1, "b1": b1}))
    assert idx.next_track(a1) is None


def test_album_index_is_album_consecutive():
    a1, a2, a3 = make_track("a1", "alb", 1), make_track("a2", "alb", 2), make_track("a3", "alb", 3)
    idx = AlbumIndex(FakeAlbumJellyfin({"a1": a1, "a2": a2, "a3": a3}))
    assert idx.is_album_consecutive(a1, a2) is True
    assert idx.is_album_consecutive(a1, a3) is False
    assert idx.is_album_consecutive(None, a2) is False


def test_album_index_prelude_of_detects_a_short_preceding_track():
    intro = make_track("intro", "alb", 1, runtime_seconds=20)
    main = make_track("main", "alb", 2, runtime_seconds=200)
    idx = AlbumIndex(FakeAlbumJellyfin({"intro": intro, "main": main}))
    assert idx.prelude_of(main)["Id"] == "intro"


def test_album_index_prelude_of_ignores_a_normal_length_preceding_track():
    t1 = make_track("t1", "alb", 1, runtime_seconds=200)
    t2 = make_track("t2", "alb", 2, runtime_seconds=200)
    idx = AlbumIndex(FakeAlbumJellyfin({"t1": t1, "t2": t2}))
    assert idx.prelude_of(t2) is None


def test_album_index_prelude_of_returns_none_for_the_first_track():
    t1 = make_track("t1", "alb", 1)
    idx = AlbumIndex(FakeAlbumJellyfin({"t1": t1}))
    assert idx.prelude_of(t1) is None


def test_album_index_main_of_returns_the_track_a_short_prelude_leads_into():
    intro = make_track("intro", "alb", 1, runtime_seconds=20)
    main = make_track("main", "alb", 2, runtime_seconds=200)
    idx = AlbumIndex(FakeAlbumJellyfin({"intro": intro, "main": main}))
    assert idx.main_of(intro)["Id"] == "main"


def test_album_index_main_of_returns_none_for_a_normal_length_track():
    t1 = make_track("t1", "alb", 1, runtime_seconds=200)
    t2 = make_track("t2", "alb", 2, runtime_seconds=200)
    idx = AlbumIndex(FakeAlbumJellyfin({"t1": t1, "t2": t2}))
    assert idx.main_of(t1) is None


def test_album_index_main_of_returns_none_for_a_short_final_track():
    intro = make_track("intro", "alb", 1, runtime_seconds=20)
    idx = AlbumIndex(FakeAlbumJellyfin({"intro": intro}))
    assert idx.main_of(intro) is None


def test_apply_album_awareness_double_feature_swaps_in_the_real_album_successor(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    a1 = make_track("a1", "alb", 1, artists=["Artist"])
    a2 = make_track("a2", "alb", 2, artists=["Artist"])
    wrong_pick = make_track("wrong", "other-alb", 1, artists=["Artist"])
    dj.jf = FakeAlbumJellyfin({"a1": a1, "a2": a2, "wrong": wrong_pick})
    dj.album_index = AlbumIndex(dj.jf)
    dj.state.last_item_id = "a1"
    result = dj._apply_album_awareness({"item_id": "wrong"})
    assert [t["Id"] for t in result] == ["a2"]
    assert result[0]["segue"] is True


def test_apply_album_awareness_double_feature_never_swaps_out_the_target_track(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    a1 = make_track("a1", "alb", 1, artists=["Artist"])
    a2 = make_track("a2", "alb", 2, artists=["Artist"])
    wrong_pick = make_track("wrong", "other-alb", 1, artists=["Artist"])
    dj.jf = FakeAlbumJellyfin({"a1": a1, "a2": a2, "wrong": wrong_pick})
    dj.album_index = AlbumIndex(dj.jf)
    dj.state.last_item_id = "a1"
    dj.state.target = {"item_id": "wrong"}
    result = dj._apply_album_awareness({"item_id": "wrong"})
    assert [t["Id"] for t in result] == ["wrong"]


def test_apply_album_awareness_leaves_a_genuine_album_sequence_alone(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    a1 = make_track("a1", "alb", 1)
    a2 = make_track("a2", "alb", 2)
    dj.jf = FakeAlbumJellyfin({"a1": a1, "a2": a2})
    dj.album_index = AlbumIndex(dj.jf)
    dj.state.last_item_id = "a1"
    result = dj._apply_album_awareness({"item_id": "a2"})
    assert [t["Id"] for t in result] == ["a2"]
    assert result[0]["segue"] is True


def test_apply_album_awareness_leaves_different_artists_alone(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    a1 = make_track("a1", "alb", 1, artists=["Artist A"])
    b1 = make_track("b1", "alb2", 1, artists=["Artist B"])
    dj.jf = FakeAlbumJellyfin({"a1": a1, "b1": b1})
    dj.album_index = AlbumIndex(dj.jf)
    dj.state.last_item_id = "a1"
    result = dj._apply_album_awareness({"item_id": "b1"})
    assert [t["Id"] for t in result] == ["b1"]
    assert result[0]["segue"] is False


def test_apply_album_awareness_double_feature_skips_an_excluded_successor(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    a1 = make_track("a1", "alb", 1, artists=["Artist"])
    a2 = make_track("a2", "alb", 2, artists=["Artist"])
    wrong_pick = make_track("wrong", "other-alb", 1, artists=["Artist"])
    dj.jf = FakeAlbumJellyfin({"a1": a1, "a2": a2, "wrong": wrong_pick})
    dj.album_index = AlbumIndex(dj.jf)
    dj.state.last_item_id = "a1"
    dj.state.history = [_history_entry("a2", minutes_ago=5)]  # a2 was just played -> excluded
    result = dj._apply_album_awareness({"item_id": "wrong"})
    assert [t["Id"] for t in result] == ["wrong"]


def test_apply_album_awareness_injects_the_prelude_on_a_coin_flip_hit(tmp_path, monkeypatch):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    intro = make_track("intro", "alb", 1, runtime_seconds=20)
    main = make_track("main", "alb", 2, runtime_seconds=200)
    dj.jf = FakeAlbumJellyfin({"intro": intro, "main": main})
    dj.album_index = AlbumIndex(dj.jf)
    monkeypatch.setattr("dj.dj.random.random", lambda: 0.0)
    result = dj._apply_album_awareness({"item_id": "main"})
    assert [t["Id"] for t in result] == ["intro", "main"]
    assert result[0]["segue"] is False
    assert result[1]["segue"] is True


def test_apply_album_awareness_skips_the_prelude_on_a_coin_flip_miss(tmp_path, monkeypatch):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    intro = make_track("intro", "alb", 1, runtime_seconds=20)
    main = make_track("main", "alb", 2, runtime_seconds=200)
    dj.jf = FakeAlbumJellyfin({"intro": intro, "main": main})
    dj.album_index = AlbumIndex(dj.jf)
    monkeypatch.setattr("dj.dj.random.random", lambda: 0.99)
    result = dj._apply_album_awareness({"item_id": "main"})
    assert [t["Id"] for t in result] == ["main"]


def test_apply_album_awareness_appends_the_main_track_on_a_coin_flip_hit(tmp_path, monkeypatch):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    intro = make_track("intro", "alb", 1, runtime_seconds=20)
    main = make_track("main", "alb", 2, runtime_seconds=200)
    dj.jf = FakeAlbumJellyfin({"intro": intro, "main": main})
    dj.album_index = AlbumIndex(dj.jf)
    monkeypatch.setattr("dj.dj.random.random", lambda: 0.0)
    result = dj._apply_album_awareness({"item_id": "intro"})
    assert [t["Id"] for t in result] == ["intro", "main"]
    assert result[0]["segue"] is False
    assert result[1]["segue"] is True


def test_apply_album_awareness_skips_the_main_track_on_a_coin_flip_miss(tmp_path, monkeypatch):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    intro = make_track("intro", "alb", 1, runtime_seconds=20)
    main = make_track("main", "alb", 2, runtime_seconds=200)
    dj.jf = FakeAlbumJellyfin({"intro": intro, "main": main})
    dj.album_index = AlbumIndex(dj.jf)
    monkeypatch.setattr("dj.dj.random.random", lambda: 0.99)
    result = dj._apply_album_awareness({"item_id": "intro"})
    assert [t["Id"] for t in result] == ["intro"]


def test_apply_album_awareness_falls_back_to_the_original_item_on_lookup_failure(tmp_path):
    dj = make_dj(tmp_path, [{"name": "x", "hours": [0, 24], "mood": {}}])
    dj.jf = FakeAlbumJellyfin({})
    item = {"item_id": "missing", "title": "T", "author": "A"}
    assert dj._apply_album_awareness(item) == [item]
