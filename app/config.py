"""Application settings, loaded from environment variables (prefix ``RADIO_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RADIO_", env_file=".env", extra="ignore", case_sensitive=False
    )

    # --- core -------------------------------------------------------------
    app_name: str = "Jellyfin Radio"
    base_url: str = "http://localhost:8000"
    secret_key: str = "insecure-dev-key-change-me"
    log_level: str = "INFO"
    data_dir: Path = Path("./data")
    config_dir: Path = Path("./config")

    admin_username: str = "admin"
    admin_password: str = "admin"

    # --- jellyfin ---------------------------------------------------------
    jellyfin_url: str = "http://jellyfin:8096"
    jellyfin_api_key: str = ""
    jellyfin_user_id: str = ""
    jellyfin_library_id: str = ""
    jellyfin_report_playback: bool = True
    jellyfin_timeout_seconds: float = 20.0

    # --- audiomuse --------------------------------------------------------
    audiomuse_enabled: bool = True
    audiomuse_url: str = "http://audiomuse:8000"
    audiomuse_api_key: str = ""
    # Deployments differ, so the paths are configurable. {id} -> Jellyfin item id.
    audiomuse_track_path: str = "/api/track/{id}"
    audiomuse_similar_path: str = "/api/similar_tracks"
    audiomuse_search_path: str = "/api/search_tracks"
    audiomuse_timeout_seconds: float = 15.0

    # --- audio ------------------------------------------------------------
    sample_rate: int = 44100
    channels: int = 2
    default_bitrate: int = 192
    # Frames per processing block (~93 ms at 44.1 kHz).
    block_frames: int = 4096
    prebuffer_seconds: float = 3.0
    max_listeners_per_station: int = 50
    # Fallback BPM/loudness analysis from the decoded audio when AudioMuse
    # has nothing for a track.
    local_analysis_enabled: bool = True
    ffmpeg_binary: str = "ffmpeg"

    # --- auth -------------------------------------------------------------
    session_ttl_hours: int = 720
    stream_token_ttl_hours: int = 8760
    allow_basic_auth: bool = True
    cookie_secure: bool = False
    cookie_name: str = "radio_session"

    @property
    def stations_file(self) -> Path:
        return self.config_dir / "stations.yaml"

    @property
    def users_file(self) -> Path:
        return self.data_dir / "users.json"

    @property
    def revocations_file(self) -> Path:
        return self.data_dir / "revoked_tokens.json"

    @property
    def analysis_cache_file(self) -> Path:
        return self.data_dir / "analysis_cache.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
