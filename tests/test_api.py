"""HTTP surface: auth gates, station CRUD, quick setup, tokens."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def anon(_environment):
    with TestClient(app) as client:
        yield client


def test_everything_needs_authentication(anon):
    assert anon.get("/api/stations").status_code == 401
    assert anon.get("/api/stations/shuffle/nowplaying").status_code == 401
    assert anon.get("/", follow_redirects=False).status_code == 303
    assert anon.get("/admin", follow_redirects=False).headers["location"].startswith("/login")


def test_streams_are_protected_and_offer_basic_auth(anon):
    response = anon.get("/stream/shuffle.mp3")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_health_endpoint_needs_no_auth(anon):
    body = anon.get("/healthz").json()
    assert body["status"] == "ok"


def test_login_rejects_bad_credentials(anon):
    assert anon.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    assert anon.post("/api/auth/login", json={"username": "ghost", "password": "nope"}).status_code == 401


def test_login_sets_a_session(client):
    assert client.get("/api/auth/me").json()["role"] == "admin"
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_quick_setup_creates_a_station_from_a_genre(client):
    response = client.post(
        "/api/stations/quick",
        json={"kind": "genre", "name": "Jazz Bar", "values": ["Jazz", "Soul"], "template": "vocal"},
    )
    assert response.status_code == 201
    station = response.json()
    assert station["id"] == "jazz-bar"
    assert station["sources"][0] == {
        "kind": "genre", "genres": ["Jazz", "Soul"], "match": "any", "weight": 1.0, "limit": 500,
    }
    # The 'vocal' template's short fades came along.
    assert station["crossfade"]["default_seconds"] == 3.0
    assert station["mount"] == "/stream/jazz-bar.mp3"


def test_quick_setup_accepts_struct_overrides(client):
    response = client.post(
        "/api/stations/quick",
        json={
            "kind": "artist",
            "name": "Bowie Radio",
            "values": ["David Bowie"],
            "include_similar": True,
            "overrides": {
                "crossfade": {"default_seconds": 9, "min_seconds": 2, "max_seconds": 15},
                "access": {"visibility": "private"},
            },
        },
    )
    assert response.status_code == 201
    station = response.json()
    assert station["crossfade"]["default_seconds"] == 9.0
    assert station["access"]["visibility"] == "private"
    assert station["sources"][0]["include_similar"] is True


def test_quick_setup_validates_input(client):
    assert client.post("/api/stations/quick", json={"kind": "genre", "name": "x", "values": []}).status_code == 422
    assert client.post("/api/stations/quick", json={"kind": "nonsense", "name": "x"}).status_code == 422
    client.post("/api/stations/quick", json={"kind": "genre", "name": "Dupe", "values": ["Pop"]})
    duplicate = client.post("/api/stations/quick", json={"kind": "genre", "name": "Dupe", "values": ["Pop"]})
    assert duplicate.status_code == 409


def test_station_update_and_delete(client):
    client.post("/api/stations/quick", json={"kind": "genre", "name": "Temp", "values": ["Pop"]})
    patched = client.patch("/api/stations/temp", json={"description": "changed", "enabled": False})
    assert patched.status_code == 200
    assert patched.json()["description"] == "changed"
    assert patched.json()["enabled"] is False

    # An invalid crossfade must be refused, not silently stored.
    bad = client.patch("/api/stations/temp", json={"crossfade": {"min_seconds": 20, "max_seconds": 5}})
    assert bad.status_code == 422

    assert client.delete("/api/stations/temp").status_code == 204
    assert client.get("/api/stations/temp").status_code == 404


def test_disabled_station_does_not_serve_audio(client):
    client.post("/api/stations/quick", json={"kind": "genre", "name": "Off Air", "values": ["Pop"]})
    client.patch("/api/stations/off-air", json={"enabled": False})
    token = client.post("/api/tokens", json={"station_id": "off-air"}).json()["token"]
    assert client.get("/stream/off-air.mp3", params={"token": token}).status_code == 409


def test_stream_tokens_are_scoped_and_revocable(client):
    client.post("/api/stations/quick", json={"kind": "genre", "name": "Scoped", "values": ["Pop"]})
    minted = client.post("/api/tokens", json={"station_id": "scoped", "ttl_hours": 5}).json()
    assert minted["stream_url"].endswith(minted["token"])

    # Bound to one station only.
    assert client.get("/stream/shuffle.mp3", params={"token": minted["token"]}).status_code == 403

    assert client.request("DELETE", "/api/tokens", json={"token": minted["token"]}).status_code == 200
    assert client.get("/stream/scoped.mp3", params={"token": minted["token"]}).status_code == 401


def test_wrong_file_extension_is_not_found(client):
    assert client.get("/stream/shuffle.ogg").status_code in (401, 404)


def test_templates_expose_the_station_structs(client):
    body = client.get("/api/templates").json()
    assert {"club", "radio", "vocal", "chill", "workout", "sleep", "talk"} <= {
        t["key"] for t in body["templates"]
    }
    assert "crossfade" in body["schema"]["station"]["properties"]


def test_listeners_cannot_administer_or_see_private_stations(client):
    client.post(
        "/api/stations/quick",
        json={"kind": "genre", "name": "Secret", "values": ["Pop"],
              "overrides": {"access": {"visibility": "private"}}},
    )
    client.post("/api/auth/users", json={"username": "lisa", "password": "listenup123", "role": "listener"})
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "lisa", "password": "listenup123"})

    assert "secret" not in {s["id"] for s in client.get("/api/stations").json()}
    assert client.post("/api/stations/quick", json={"kind": "genre", "name": "n", "values": ["Pop"]}).status_code == 403
    assert client.post("/api/stations/shuffle/skip").status_code == 403
    assert client.get("/api/auth/users").status_code == 403


def test_the_last_admin_cannot_be_deleted(client):
    assert client.delete("/api/auth/users/admin").status_code == 400
