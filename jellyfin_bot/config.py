"""Runtime configuration, loaded from environment variables / .env."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

try:  # optional convenience dependency
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False


class ConfigError(RuntimeError):
    """Raised when the environment is missing something the bot needs."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Config:
    discord_token: str
    jellyfin_url: str
    jellyfin_api_key: str | None = None
    jellyfin_username: str | None = None
    jellyfin_password: str | None = None
    jellyfin_user_id: str | None = None
    device_id: str = field(default_factory=lambda: f"discord-jellyfin-{uuid.uuid4().hex[:12]}")

    guild_ids: tuple[int, ...] = ()
    default_volume: float = 0.5
    max_queue_length: int = 500
    idle_timeout: int = 300
    search_limit: int = 25
    direct_stream: bool = True
    max_bitrate: int = 320_000
    report_playback: bool = True

    @classmethod
    def from_env(cls, dotenv_path: str | None = None) -> "Config":
        load_dotenv(dotenv_path) if dotenv_path else load_dotenv()

        token = _env("DISCORD_TOKEN")
        if not token:
            raise ConfigError("DISCORD_TOKEN is required")

        url = _env("JELLYFIN_URL")
        if not url:
            raise ConfigError("JELLYFIN_URL is required (e.g. https://jellyfin.example.com)")

        api_key = _env("JELLYFIN_API_KEY")
        username = _env("JELLYFIN_USERNAME")
        password = os.environ.get("JELLYFIN_PASSWORD") or None
        if not api_key and not (username and password is not None):
            raise ConfigError(
                "Provide either JELLYFIN_API_KEY, or JELLYFIN_USERNAME + JELLYFIN_PASSWORD"
            )

        raw_guilds = _env("DISCORD_GUILD_IDS", "")
        guild_ids: tuple[int, ...] = ()
        if raw_guilds:
            try:
                guild_ids = tuple(int(part) for part in raw_guilds.replace(";", ",").split(",") if part.strip())
            except ValueError as exc:
                raise ConfigError("DISCORD_GUILD_IDS must be a comma-separated list of IDs") from exc

        volume = _env_int("DEFAULT_VOLUME", 50)
        if not 0 <= volume <= 200:
            raise ConfigError("DEFAULT_VOLUME must be between 0 and 200")

        device_id = _env("JELLYFIN_DEVICE_ID") or f"discord-jellyfin-{uuid.uuid4().hex[:12]}"

        return cls(
            discord_token=token,
            jellyfin_url=url.rstrip("/"),
            jellyfin_api_key=api_key,
            jellyfin_username=username,
            jellyfin_password=password,
            jellyfin_user_id=_env("JELLYFIN_USER_ID"),
            device_id=device_id,
            guild_ids=guild_ids,
            default_volume=volume / 100,
            max_queue_length=_env_int("MAX_QUEUE_LENGTH", 500),
            idle_timeout=_env_int("IDLE_TIMEOUT", 300),
            search_limit=max(1, min(25, _env_int("SEARCH_LIMIT", 25))),
            direct_stream=_env_bool("JELLYFIN_DIRECT_STREAM", True),
            max_bitrate=_env_int("JELLYFIN_MAX_BITRATE", 320_000),
            report_playback=_env_bool("JELLYFIN_REPORT_PLAYBACK", True),
        )
