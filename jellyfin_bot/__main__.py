"""Entry point: ``python -m jellyfin_bot``."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord

from .bot import MusicBot
from .config import Config, ConfigError


def setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def run() -> int:
    log = logging.getLogger("jellyfin_bot")

    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        log.error("Copy .env.example to .env and fill it in.")
        return 2

    if not discord.opus.is_loaded():
        # discord.py loads libopus automatically on most systems; warn if not.
        log.warning(
            "libopus is not loaded — install it (apt: libopus0) or voice playback will fail."
        )

    bot = MusicBot(config)
    try:
        await bot.start(config.discord_token)
    except discord.LoginFailure:
        log.error("Discord rejected the token — check DISCORD_TOKEN.")
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        if not bot.is_closed():
            await bot.close()
    return 0


def main() -> None:
    setup_logging()
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        sys.exit(0)


if __name__ == "__main__":
    main()
