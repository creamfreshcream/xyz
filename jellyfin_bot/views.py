"""Interactive components (currently the /search result picker)."""

from __future__ import annotations

from typing import Awaitable, Callable

import discord

from .jellyfin import Track, format_duration

AddCallback = Callable[[discord.Interaction, list[Track]], Awaitable[None]]


class TrackSelect(discord.ui.Select):
    def __init__(self, tracks: list[Track], on_add: AddCallback) -> None:
        self._tracks = tracks[:25]
        self._on_add = on_add
        options = [
            discord.SelectOption(
                label=track.name[:100] or "Unknown title",
                description=(
                    f"{track.artist or 'Unknown artist'}"
                    + (f" • {format_duration(track.duration)}" if track.duration else "")
                )[:100],
                value=str(index),
            )
            for index, track in enumerate(self._tracks)
        ]
        super().__init__(
            placeholder="Pick one or more tracks to queue…",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        chosen = [self._tracks[int(value)] for value in sorted(self.values, key=int)]
        await self._on_add(interaction, chosen)


class SearchView(discord.ui.View):
    """Ephemeral picker shown by /search."""

    def __init__(self, tracks: list[Track], requester_id: int, on_add: AddCallback) -> None:
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.message: discord.Message | None = None
        self.add_item(TrackSelect(tracks, on_add))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran `/search` can pick from this list.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:  # pragma: no cover - UI timing
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
