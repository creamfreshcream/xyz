from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _environment(tmp_path_factory: pytest.TempPathFactory) -> None:
    root = tmp_path_factory.mktemp("radio")
    os.environ.update(
        {
            "RADIO_DATA_DIR": str(root / "data"),
            "RADIO_CONFIG_DIR": str(root / "config"),
            "RADIO_SECRET_KEY": "test-secret-key",
            "RADIO_ADMIN_USERNAME": "admin",
            "RADIO_ADMIN_PASSWORD": "supersecret1",
            "RADIO_JELLYFIN_API_KEY": "test",
            "RADIO_AUDIOMUSE_ENABLED": "false",
            "RADIO_LOG_LEVEL": "WARNING",
        }
    )
    Path(os.environ["RADIO_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["RADIO_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def client(_environment):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        test_client.post(
            "/api/auth/login", json={"username": "admin", "password": "supersecret1"}
        )
        yield test_client
