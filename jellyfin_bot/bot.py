"""Discord bot wiring: client, slash commands and error handling."""

from __future__ import annotations

import logging
import re
import urllib.parse

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .embeds import ACCENT, added_embed, now_playing_embed, queue_embed, track_line
from .jellyfin import JellyfinClient, JellyfinError, Track, format_duration
from .player import GuildPlayer, LoopMode, PlayerError, PlayerManager
from .views import SearchView

log = logging.getLogger(__name__)

GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)
TIMESTAMP_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$|^(\d+)$")


class MusicBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()  # voice_states + guilds is all we need
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            activity=discord.Activity(
                type=discord.ActivityType.listening, name="/play from Jellyfin"
            ),
        )
        self.config = config
        self.jellyfin = JellyfinClient(
            config.jellyfin_url,
            api_key=config.jellyfin_api_key,
            username=config.jellyfin_username,
            password=config.jellyfin_password,
            user_id=config.jellyfin_user_id,
            device_id=config.device_id,
            direct_stream=config.direct_stream,
            max_bitrate=config.max_bitrate,
        )
        self.players = PlayerManager(self, self.jellyfin, config)

    async def setup_hook(self) -> None:
        await self.jellyfin.connect()
        await self.add_cog(MusicCog(self))

        if self.config.guild_ids:
            for guild_id in self.config.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            log.info("Synced commands to guilds: %s", ", ".join(map(str, self.config.guild_ids)))
        else:
            await self.tree.sync()
            log.info("Synced global commands (may take up to an hour to appear)")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s guilds)", self.user, len(self.guilds))

    async def close(self) -> None:
        await self.players.close_all()
        await self.jellyfin.close()
        await super().close()


class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        self.jellyfin = bot.jellyfin
        self.config = bot.config

    # ------------------------------------------------------------- utilities

    async def _player_for(
        self, interaction: discord.Interaction, *, connect: bool = True
    ) -> GuildPlayer:
        if interaction.guild is None:
            raise PlayerError("Music commands only work inside a server.")

        player = self.bot.players.get(interaction.guild)
        player.text_channel = interaction.channel

        if not connect:
            return player

        user = interaction.user
        channel = getattr(getattr(user, "voice", None), "channel", None)
        if channel is None:
            if player.connected:
                return player
            raise PlayerError("Join a voice channel first.")

        me = interaction.guild.me
        perms = channel.permissions_for(me)
        if not perms.connect or not perms.speak:
            raise PlayerError(f"I need Connect and Speak permissions in {channel.mention}.")

        if player.connected and player.voice.channel.id != channel.id and player.is_playing:
            listeners = [m for m in player.voice.channel.members if not m.bot]
            if listeners:
                raise PlayerError(
                    f"I'm already playing in {player.voice.channel.mention}."
                )

        await player.connect_to(channel)
        return player

    def _existing_player(self, interaction: discord.Interaction) -> GuildPlayer:
        if interaction.guild is None:
            raise PlayerError("Music commands only work inside a server.")
        player = self.bot.players.existing(interaction.guild)
        if player is None:
            raise PlayerError("I'm not playing anything right now.")
        player.text_channel = interaction.channel
        return player

    def _stamp(self, tracks: list[Track], user: discord.abc.User) -> list[Track]:
        for track in tracks:
            track.requester_id = user.id
            track.requester_name = getattr(user, "display_name", str(user))
        return tracks

    async def _queue(
        self,
        interaction: discord.Interaction,
        player: GuildPlayer,
        tracks: list[Track],
        *,
        source: str | None = None,
        play_next: bool = False,
    ) -> None:
        if not tracks:
            await interaction.followup.send("No matching tracks on your Jellyfin server.")
            return

        self._stamp(tracks, interaction.user)
        was_idle = player.current is None and not player.queue
        position = 1 if play_next else len(player.queue) + 1
        added = player.add_next(tracks) if play_next else player.add(tracks)

        if added < len(tracks):
            source = (source or "") + f"\n⚠️ Queue is full — only added {added} of {len(tracks)}."
        if added == 0:
            await interaction.followup.send("The queue is full.")
            return

        embed = added_embed(
            tracks[:added], source=source, position=None if was_idle else position
        )
        await interaction.followup.send(embed=embed)

    async def _resolve(self, query: str) -> tuple[list[Track], str | None]:
        """Turn a /play argument into tracks: autocomplete value, URL, or free text."""
        query = query.strip()

        kind, _, ident = query.partition(":")
        if kind in {"audio", "album", "artist", "playlist"} and GUID_RE.fullmatch(ident):
            return await self._tracks_for(kind, ident)

        item_id = self._id_from_url(query)
        if item_id:
            item = await self.jellyfin.get_item(item_id)
            if item:
                type_map = {
                    "Audio": "audio",
                    "MusicAlbum": "album",
                    "MusicArtist": "artist",
                    "Playlist": "playlist",
                }
                kind = type_map.get(str(item.get("Type")), "audio")
                return await self._tracks_for(kind, item_id)

        tracks = await self.jellyfin.search_tracks(query, limit=1)
        if tracks:
            return tracks, None

        # Nothing matched a track name — try an album, then an artist.
        for item_type, kind in (("MusicAlbum", "album"), ("MusicArtist", "artist")):
            items = await self.jellyfin.search_items(query, item_type, limit=1)
            if items:
                return await self._tracks_for(kind, items[0].id)
        return [], None

    async def _tracks_for(self, kind: str, item_id: str) -> tuple[list[Track], str | None]:
        if kind == "audio":
            item = await self.jellyfin.get_item(item_id)
            if not item:
                return [], None
            return [self.jellyfin.to_track(item)], None
        if kind == "album":
            tracks = await self.jellyfin.album_tracks(item_id)
            item = await self.jellyfin.get_item(item_id)
            name = (item or {}).get("Name", "album")
            return tracks, f"Album: **{name}**"
        if kind == "artist":
            tracks = await self.jellyfin.artist_tracks(item_id)
            item = await self.jellyfin.get_item(item_id)
            name = (item or {}).get("Name", "artist")
            return tracks, f"Artist: **{name}**"
        if kind == "playlist":
            tracks = await self.jellyfin.playlist_tracks(item_id)
            item = await self.jellyfin.get_item(item_id)
            name = (item or {}).get("Name", "playlist")
            return tracks, f"Playlist: **{name}**"
        return [], None

    @staticmethod
    def _id_from_url(query: str) -> str | None:
        if "://" not in query:
            return None
        parsed = urllib.parse.urlparse(query)
        params = urllib.parse.parse_qs(parsed.fragment or "") | urllib.parse.parse_qs(parsed.query)
        for key in ("id", "Id", "itemId"):
            values = params.get(key)
            if values and GUID_RE.fullmatch(values[0]):
                return values[0]
        match = GUID_RE.search(parsed.fragment or parsed.path)
        return match.group(0) if match else None

    # ---------------------------------------------------------- autocompletes

    async def _autocomplete(
        self, current: str, item_types: tuple[str, ...], prefix: str | None = None
    ) -> list[app_commands.Choice[str]]:
        current = current.strip()
        if len(current) < 2:
            return []
        choices: list[app_commands.Choice[str]] = []
        try:
            for item_type in item_types:
                raw = await self.jellyfin.search(current, item_types=(item_type,), limit=8)
                for item in raw:
                    kind = prefix or {
                        "Audio": "audio",
                        "MusicAlbum": "album",
                        "MusicArtist": "artist",
                        "Playlist": "playlist",
                    }.get(item_type, "audio")
                    artist = item.get("AlbumArtist") or ", ".join(item.get("Artists") or [])
                    label = item.get("Name") or "Unknown"
                    tag = {"audio": "🎵", "album": "💿", "artist": "🎤", "playlist": "📃"}[kind]
                    name = f"{tag} {label}" + (f" — {artist}" if artist else "")
                    choices.append(
                        app_commands.Choice(name=name[:100], value=f"{kind}:{item['Id']}")
                    )
        except JellyfinError:
            log.debug("Autocomplete lookup failed", exc_info=True)
        return choices[:25]

    async def play_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._autocomplete(current, ("Audio", "MusicAlbum", "Playlist", "MusicArtist"))

    async def album_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(current, ("MusicAlbum",))

    async def artist_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(current, ("MusicArtist",))

    async def playlist_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(current, ("Playlist",))

    # -------------------------------------------------------------- commands

    @app_commands.command(description="Play a track, album, artist or playlist from Jellyfin")
    @app_commands.describe(
        query="Song, album, artist, playlist name — or a Jellyfin URL",
        next="Put it at the front of the queue",
    )
    @app_commands.autocomplete(query=play_autocomplete)
    async def play(self, interaction: discord.Interaction, query: str, next: bool = False) -> None:
        await interaction.response.defer()
        player = await self._player_for(interaction)
        tracks, source = await self._resolve(query)
        await self._queue(interaction, player, tracks, source=source, play_next=next)

    @app_commands.command(description="Search Jellyfin and pick tracks from a list")
    @app_commands.describe(query="What to search for")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        player = await self._player_for(interaction)
        tracks = await self.jellyfin.search_tracks(query, limit=self.config.search_limit)
        if not tracks:
            await interaction.followup.send(f"Nothing found for **{query}**.")
            return

        async def on_add(picker: discord.Interaction, chosen: list[Track]) -> None:
            await picker.response.defer()
            self._stamp(chosen, picker.user)
            added = player.add(chosen)
            await picker.followup.send(
                embed=added_embed(chosen[:added], source=f"From search: **{query}**")
            )

        view = SearchView(tracks, interaction.user.id, on_add)
        embed = discord.Embed(
            title=f"Results for “{query}”",
            description="\n".join(
                track_line(t, index=i + 1) for i, t in enumerate(tracks[:10])
            ),
            colour=ACCENT,
        )
        view.message = await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(description="Queue a whole album")
    @app_commands.autocomplete(query=album_autocomplete)
    async def album(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        player = await self._player_for(interaction)
        tracks, source = await self._lookup(query, "album", "MusicAlbum")
        await self._queue(interaction, player, tracks, source=source)

    @app_commands.command(description="Queue everything by an artist")
    @app_commands.autocomplete(query=artist_autocomplete)
    async def artist(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        player = await self._player_for(interaction)
        tracks, source = await self._lookup(query, "artist", "MusicArtist")
        await self._queue(interaction, player, tracks, source=source)

    @app_commands.command(description="Queue a Jellyfin playlist")
    @app_commands.autocomplete(query=playlist_autocomplete)
    async def playlist(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        player = await self._player_for(interaction)
        tracks, source = await self._lookup(query, "playlist", "Playlist")
        await self._queue(interaction, player, tracks, source=source)

    async def _lookup(self, query: str, kind: str, item_type: str):
        prefix, _, ident = query.partition(":")
        if prefix == kind and GUID_RE.fullmatch(ident):
            return await self._tracks_for(kind, ident)
        items = await self.jellyfin.search_items(query, item_type, limit=1)
        if not items:
            return [], None
        return await self._tracks_for(kind, items[0].id)

    @app_commands.command(description="Queue an instant mix based on a track or artist")
    @app_commands.autocomplete(query=play_autocomplete)
    async def mix(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        player = await self._player_for(interaction)

        prefix, _, ident = query.partition(":")
        seed_id = ident if GUID_RE.fullmatch(ident or "") else None
        seed_name = query
        if seed_id is None:
            found = await self.jellyfin.search(query, limit=1)
            if not found:
                await interaction.followup.send(f"Nothing found for **{query}**.")
                return
            seed_id = str(found[0]["Id"])
            seed_name = str(found[0].get("Name") or query)

        tracks = await self.jellyfin.instant_mix(seed_id, limit=50)
        await self._queue(interaction, player, tracks, source=f"Instant mix from **{seed_name}**")

    @app_commands.command(description="Queue your Jellyfin favourites, shuffled")
    @app_commands.describe(limit="How many tracks (default 50)")
    async def favorites(self, interaction: discord.Interaction, limit: int = 50) -> None:
        await interaction.response.defer()
        player = await self._player_for(interaction)
        tracks = await self.jellyfin.favorite_tracks(limit=max(1, min(200, limit)))
        await self._queue(interaction, player, tracks, source="Your Jellyfin favourites")

    @app_commands.command(description="Queue random tracks from the library")
    @app_commands.describe(limit="How many tracks (default 25)")
    async def random(self, interaction: discord.Interaction, limit: int = 25) -> None:
        await interaction.response.defer()
        player = await self._player_for(interaction)
        tracks = await self.jellyfin.random_tracks(limit=max(1, min(200, limit)))
        await self._queue(interaction, player, tracks, source="Random picks from your library")

    @app_commands.command(description="Show the queue")
    @app_commands.describe(page="Page number")
    async def queue(self, interaction: discord.Interaction, page: int = 1) -> None:
        player = self._existing_player(interaction)
        await interaction.response.send_message(embed=queue_embed(player, page))

    @app_commands.command(name="nowplaying", description="Show the current track")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        if player.current is None:
            raise PlayerError("Nothing is playing.")
        await interaction.response.send_message(embed=now_playing_embed(player, player.current))

    @app_commands.command(description="Skip the current track")
    @app_commands.describe(count="How many tracks to skip")
    async def skip(self, interaction: discord.Interaction, count: int = 1) -> None:
        player = self._existing_player(interaction)
        skipped = player.current
        player.skip(max(1, count))
        await interaction.response.send_message(
            f"⏭️ Skipped **{skipped.name}**." if skipped else "⏭️ Skipped."
        )

    @app_commands.command(description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        player.stop()
        await interaction.response.send_message("⏹️ Stopped and cleared the queue.")

    @app_commands.command(description="Pause playback")
    async def pause(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        player.pause()
        await interaction.response.send_message("⏸️ Paused.")

    @app_commands.command(description="Resume playback")
    async def resume(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        player.resume()
        await interaction.response.send_message("▶️ Resumed.")

    @app_commands.command(description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        if not player.queue:
            raise PlayerError("The queue is empty.")
        player.shuffle()
        await interaction.response.send_message(f"🔀 Shuffled {len(player.queue)} tracks.")

    @app_commands.command(description="Set the loop mode")
    @app_commands.describe(mode="What to repeat")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="track", value="track"),
            app_commands.Choice(name="queue", value="queue"),
        ]
    )
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        player = self._existing_player(interaction)
        player.loop_mode = LoopMode(mode.value)
        icons = {"off": "➡️", "track": "🔂", "queue": "🔁"}
        await interaction.response.send_message(f"{icons[mode.value]} Loop: **{mode.value}**.")

    @app_commands.command(description="Remove a track from the queue")
    @app_commands.describe(position="Queue position, as shown by /queue")
    async def remove(self, interaction: discord.Interaction, position: int) -> None:
        player = self._existing_player(interaction)
        track = player.remove(position)
        await interaction.response.send_message(f"🗑️ Removed **{track.label}**.")

    @app_commands.command(description="Move a track to another queue position")
    @app_commands.describe(position="Current position", to="New position")
    async def move(self, interaction: discord.Interaction, position: int, to: int) -> None:
        player = self._existing_player(interaction)
        track = player.move(position, to)
        await interaction.response.send_message(f"↕️ Moved **{track.label}** to position {to}.")

    @app_commands.command(description="Clear the queue but keep playing")
    async def clear(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        count = len(player.queue)
        player.clear()
        await interaction.response.send_message(f"🧹 Cleared {count} queued tracks.")

    @app_commands.command(description="Set playback volume")
    @app_commands.describe(level="0–200 percent")
    async def volume(self, interaction: discord.Interaction, level: int) -> None:
        player = self._existing_player(interaction)
        if not 0 <= level <= 200:
            raise PlayerError("Volume must be between 0 and 200.")
        player.set_volume(level / 100)
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")

    @app_commands.command(description="Jump to a position in the current track")
    @app_commands.describe(position="Timestamp like 1:23, or seconds")
    async def seek(self, interaction: discord.Interaction, position: str) -> None:
        player = self._existing_player(interaction)
        seconds = _parse_timestamp(position)
        if seconds is None:
            raise PlayerError("Use a timestamp like `1:23`, `1:02:03`, or a number of seconds.")
        player.seek(seconds)
        await interaction.response.send_message(f"⏩ Seeking to `{format_duration(seconds)}`.")

    @app_commands.command(description="Join your voice channel")
    async def join(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        player = await self._player_for(interaction)
        channel = player.voice.channel if player.voice else None
        await interaction.followup.send(f"👋 Joined {channel.mention if channel else 'voice'}.")

    @app_commands.command(description="Leave the voice channel")
    async def leave(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            raise PlayerError("Music commands only work inside a server.")
        await self.bot.players.destroy(interaction.guild)
        await interaction.response.send_message("👋 Left the voice channel.")

    @app_commands.command(description="Show Jellyfin connection info")
    async def status(self, interaction: discord.Interaction) -> None:
        player = self.bot.players.existing(interaction.guild) if interaction.guild else None
        embed = discord.Embed(title="Status", colour=ACCENT)
        embed.add_field(
            name="Jellyfin",
            value=f"{self.jellyfin.server_name or self.config.jellyfin_url}\n"
            f"`{self.config.jellyfin_url}`",
            inline=False,
        )
        embed.add_field(
            name="Streaming mode",
            value="direct (no transcode)" if self.config.direct_stream else "transcoded (aac)",
            inline=True,
        )
        if player and player.current:
            embed.add_field(name="Playing", value=track_line(player.current), inline=False)
            embed.add_field(name="Queued", value=str(len(player.queue)), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------------------------------------------------------- events

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild = member.guild
        player = self.bot.players.existing(guild)
        if player is None:
            return

        # The bot itself was disconnected or moved out.
        if member.id == self.bot.user.id and after.channel is None:
            await self.bot.players.destroy(guild)
            return

        channel = player.voice.channel if player.voice else None
        if channel is None:
            return
        if not any(not m.bot for m in channel.members):
            log.info("Voice channel empty in guild %s — disconnecting", guild.id)
            await self.bot.players.destroy(guild)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        await self._report_error(interaction, error)

    async def _report_error(
        self, interaction: discord.Interaction, error: BaseException
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, PlayerError):
            message = str(original)
        elif isinstance(original, JellyfinError):
            message = f"Jellyfin error: {original}"
            log.warning("Jellyfin error: %s", original)
        else:
            message = "Something went wrong running that command."
            log.exception("Command error", exc_info=original)

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:  # pragma: no cover - best effort
            log.debug("Could not deliver error message", exc_info=True)


def _parse_timestamp(value: str) -> float | None:
    value = value.strip()
    match = TIMESTAMP_RE.match(value)
    if not match:
        return None
    if match.group(4) is not None:
        return float(match.group(4))
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    return float(hours * 3600 + minutes * 60 + seconds)
