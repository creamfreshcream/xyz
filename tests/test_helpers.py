import pytest

from jellyfin_bot.bot import MusicCog, _parse_timestamp


@pytest.mark.parametrize(
    ("value", "expected"),
    [("90", 90.0), ("1:30", 90.0), ("0:05", 5.0), ("1:02:03", 3723.0), (" 45 ", 45.0)],
)
def test_parse_timestamp(value, expected):
    assert _parse_timestamp(value) == expected


@pytest.mark.parametrize("value", ["abc", "1:2:3:4", "", "-5", "1:600:00"])
def test_parse_timestamp_rejects_junk(value):
    assert _parse_timestamp(value) is None


def test_id_from_url_web_client():
    url = (
        "https://media.example.com/web/index.html"
        "#!/details?id=0f9a1b2c3d4e5f60718293a4b5c6d7e8&serverId=abc"
    )
    assert MusicCog._id_from_url(url) == "0f9a1b2c3d4e5f60718293a4b5c6d7e8"


def test_id_from_url_ignores_plain_text():
    assert MusicCog._id_from_url("blue monday") is None


def test_id_from_url_accepts_dashed_guid():
    url = "https://media.example.com/web/#/details?id=0f9a1b2c-3d4e-5f60-7182-93a4b5c6d7e8"
    assert MusicCog._id_from_url(url) == "0f9a1b2c-3d4e-5f60-7182-93a4b5c6d7e8"


def test_id_from_url_reads_query_string():
    url = "https://media.example.com/Items?id=0f9a1b2c3d4e5f60718293a4b5c6d7e8"
    assert MusicCog._id_from_url(url) == "0f9a1b2c3d4e5f60718293a4b5c6d7e8"
