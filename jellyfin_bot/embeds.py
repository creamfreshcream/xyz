"""Embed builders shared by the commands and the player announcements."""

from __future__ import annotations

import discord

from .jellyfin import Track, format_duration

ACCENT = discord.Colour(0x00A4DC)  # Jellyfin blue
QUEUE_PAGE_SIZE = 10


def progress_bar(position: float, duration: float, width: int = 18) -> str:
    if duration <= 0:
        return f"`{format_duration(position)}`"
    filled = int(width * min(1.0, position / duration))
    bar = "─" * filled + "🔘" + "─" * max(0, width - filled - 1)
    return f"`{format_duration(position)}` {bar} `{format_duration(duration)}`"


def track_line(track: Track, *, index: int | None = None) -> str:
    prefix = f"`{index}.` " if index is not None else ""
    duration = f" `[{format_duration(track.duration)}]`" if track.duration else ""
    requester = f" • <@{track.requester_id}>" if track.requester_id else ""
    return f"{prefix}**{discord.utils.escape_markdown(track.name)}** — {discord.utils.escape_markdown(track.artist or 'Unknown artist')}{duration}{requester}"


def now_playing_embed(player, track: Track, *, compact: bool = False) -> discord.Embed:
    embed = discord.Embed(
        title=track.name,
        description=track.artist or "Unknown artist",
        colour=ACCENT,
    )
    embed.set_author(name="Now playing" if not player.is_paused else "Paused")
    if track.album:
        embed.add_field(name="Album", value=track.album, inline=True)
    if not compact:
        embed.add_field(
            name="Position",
            value=progress_bar(player.position, track.duration),
            inline=False,
        )
        embed.add_field(name="Volume", value=f"{int(player.volume * 100)}%", inline=True)
        embed.add_field(name="Loop", value=player.loop_mode.value, inline=True)
        embed.add_field(name="In queue", value=str(len(player.queue)), inline=True)
    elif track.duration:
        embed.add_field(name="Length", value=format_duration(track.duration), inline=True)
    if track.requester_id:
        embed.set_footer(text=f"Requested by {track.requester_name or 'someone'}")
    if track.art_url:
        embed.set_thumbnail(url=track.art_url)
    return embed


def queue_embed(player, page: int = 1) -> discord.Embed:
    total = len(player.queue)
    pages = max(1, (total + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)
    page = max(1, min(page, pages))
    start = (page - 1) * QUEUE_PAGE_SIZE
    items = list(player.queue)[start : start + QUEUE_PAGE_SIZE]

    embed = discord.Embed(title="Queue", colour=ACCENT)
    if player.current:
        embed.add_field(
            name="Now playing",
            value=track_line(player.current)
            + f"\n{progress_bar(player.position, player.current.duration)}",
            inline=False,
        )
    if items:
        embed.add_field(
            name=f"Up next ({total} track{'s' if total != 1 else ''})",
            value="\n".join(
                track_line(track, index=start + offset + 1)
                for offset, track in enumerate(items)
            ),
            inline=False,
        )
    elif not player.current:
        embed.description = "The queue is empty. Add something with `/play`."

    upcoming = sum(t.duration for t in player.queue)
    footer = f"Page {page}/{pages}"
    if upcoming:
        footer += f" • {format_duration(upcoming)} remaining"
    if player.loop_mode.value != "off":
        footer += f" • loop: {player.loop_mode.value}"
    embed.set_footer(text=footer)
    return embed


def added_embed(tracks: list[Track], *, source: str | None = None, position: int | None = None) -> discord.Embed:
    if len(tracks) == 1:
        track = tracks[0]
        embed = discord.Embed(
            title="Added to queue",
            description=track_line(track),
            colour=ACCENT,
        )
        if track.art_url:
            embed.set_thumbnail(url=track.art_url)
    else:
        total = sum(t.duration for t in tracks)
        embed = discord.Embed(
            title=f"Added {len(tracks)} tracks",
            description=(source or "")
            + (f"\nTotal length: {format_duration(total)}" if total else ""),
            colour=ACCENT,
        )
        preview = "\n".join(track_line(t) for t in tracks[:5])
        if preview:
            embed.add_field(name="Includes", value=preview, inline=False)
    if position:
        embed.set_footer(text=f"Position in queue: {position}")
    return embed
