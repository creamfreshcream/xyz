"""Password hashing, tokens and stream authorisation."""

from __future__ import annotations

import time

import pytest

from app.auth import UserStore
from app.security import TokenError, hash_password, sign_token, verify_password, verify_token


def test_password_hashing_round_trip():
    encoded = hash_password("correct horse battery")
    assert encoded.startswith("scrypt$")
    assert verify_password("correct horse battery", encoded)
    assert not verify_password("wrong", encoded)
    # Same password, different salt -> different hash.
    assert encoded != hash_password("correct horse battery")


def test_malformed_hash_is_rejected_not_crashed():
    assert not verify_password("x", "garbage")
    assert not verify_password("x", "md5$1$2$3$4$5")


def test_token_signing_and_verification():
    token = sign_token({"sub": "lisa", "typ": "session"}, "secret", ttl_seconds=60)
    payload = verify_token(token, "secret")
    assert payload["sub"] == "lisa"
    assert payload["exp"] > time.time()


def test_token_with_a_different_secret_is_rejected():
    token = sign_token({"sub": "lisa"}, "secret", 60)
    with pytest.raises(TokenError, match="bad signature"):
        verify_token(token, "other-secret")


def test_tampered_token_is_rejected():
    token = sign_token({"sub": "lisa", "role": "listener"}, "secret", 60)
    body, signature = token.split(".")
    with pytest.raises(TokenError):
        verify_token(f"{body}x.{signature}", "secret")
    with pytest.raises(TokenError):
        verify_token("not-a-token", "secret")


def test_expired_token_is_rejected():
    with pytest.raises(TokenError, match="expired"):
        verify_token(sign_token({"sub": "x"}, "secret", -1), "secret")


def test_user_store_round_trip(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create("lisa", "listenup123", "listener")
    assert store.authenticate("lisa", "listenup123") is not None
    assert store.authenticate("lisa", "nope") is None
    assert store.authenticate("nobody", "listenup123") is None

    with pytest.raises(ValueError, match="already exists"):
        store.create("lisa", "another12345")
    with pytest.raises(ValueError, match="at least 8"):
        store.create("bob", "short")

    store.update("lisa", disabled=True)
    assert store.authenticate("lisa", "listenup123") is None

    # Reloading from disk keeps everything.
    assert UserStore(tmp_path / "users.json").get("lisa").role == "listener"


def test_bootstrap_admin_runs_only_once(tmp_path):
    path = tmp_path / "users.json"
    store = UserStore(path)
    store.bootstrap_admin("admin", "supersecret1")
    assert store.get("admin").role == "admin"
    UserStore(path).bootstrap_admin("other", "supersecret1")
    assert UserStore(path).get("other") is None
