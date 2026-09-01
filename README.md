# Jellyfin Discord Music Bot

A Discord bot that streams music straight from your own [Jellyfin](https://jellyfin.org)
server into a voice channel. No YouTube scraping, no third-party APIs — it searches your
library, queues tracks/albums/artists/playlists, and reports playback back to Jellyfin so
plays are counted and the session shows up in the dashboard.

## Features

- **Slash commands** with live autocomplete against your Jellyfin library (🎵 tracks,
  💿 albums, 🎤 artists, 📃 playlists).
- **Queue management** — shuffle, loop (track/queue), move, remove, clear, play-next.
- **Playback control** — pause/resume, seek, per-guild volume, skip N tracks.
- **Discovery** — `/mix` (Jellyfin instant mix), `/favorites`, `/random`.
- **Direct streaming by default** — ffmpeg reads the original file, so your server does no
  transcoding work; flip a switch if you'd rather Jellyfin transcode to AAC.
- Auto-leaves when the voice channel empties or the queue has been idle for a while.

## Commands

| Command | What it does |
| --- | --- |
| `/play <query> [next]` | Queue a track, album, artist, playlist, or a Jellyfin URL |
| `/search <query>` | Search and pick several results from a dropdown |
| `/album` `/artist` `/playlist` | Queue a whole album / artist / Jellyfin playlist |
| `/mix <query>` | Instant mix seeded from a track or artist |
| `/favorites [limit]` `/random [limit]` | Queue favourites (shuffled) or random tracks |
| `/queue [page]` `/nowplaying` | Inspect the queue and current track |
| `/skip [count]` `/stop` `/pause` `/resume` | Transport controls |
| `/seek <1:23>` `/volume <0-200>` | Jump within a track, set volume |
| `/shuffle` `/move <pos> <to>` `/remove <pos>` `/clear` | Reorder or trim the queue |
| `/join` `/leave` `/status` | Voice + connection info |

## Setup

### 1. Jellyfin credentials

Either create an API key (Jellyfin **Dashboard → API Keys**), or use a normal username and
password. An API key is simplest; if you use one and your server has several users, set
`JELLYFIN_USER_ID` so favourites and playlists resolve to the right account (find it in the
URL of **Dashboard → Users → \<user\>**).

### 2. Discord application

1. Create an application at <https://discord.com/developers/applications> and add a bot.
2. Copy the **bot token** into `DISCORD_TOKEN`.
3. Invite it with the `bot` and `applications.commands` scopes and the **Connect**,
   **Speak**, **Send Messages**, and **Embed Links** permissions:

   ```
   https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&permissions=3148800&scope=bot%20applications.commands
   ```

No privileged intents are required.

### 3. Configure

```bash
cp .env.example .env
$EDITOR .env
```

Set `DISCORD_GUILD_IDS` to your server's ID while testing — commands appear instantly
instead of taking up to an hour to propagate globally.

### 4. Run

**Docker (recommended):**

```bash
docker compose up -d --build
```

**Locally** (needs `ffmpeg` and `libopus`; Python 3.11 or 3.12):

```bash
sudo apt install ffmpeg libopus0        # or: brew install ffmpeg opus
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m jellyfin_bot
```

## Configuration reference

| Variable | Default | Notes |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | **Required.** Bot token |
| `JELLYFIN_URL` | — | **Required.** e.g. `http://jellyfin.local:8096` |
| `JELLYFIN_API_KEY` | — | Required unless username/password are set |
| `JELLYFIN_USERNAME` / `JELLYFIN_PASSWORD` | — | Alternative to an API key |
| `JELLYFIN_USER_ID` | auto | Which user's library/favourites to use |
| `DISCORD_GUILD_IDS` | — | Comma-separated; instant command sync |
| `JELLYFIN_DIRECT_STREAM` | `true` | `false` makes Jellyfin transcode to AAC |
| `JELLYFIN_MAX_BITRATE` | `320000` | Only used when transcoding |
| `JELLYFIN_REPORT_PLAYBACK` | `true` | Report sessions/progress to Jellyfin |
| `DEFAULT_VOLUME` | `50` | Percent, 0–200 |
| `MAX_QUEUE_LENGTH` | `500` | Per guild |
| `IDLE_TIMEOUT` | `300` | Seconds before leaving an idle channel |
| `SEARCH_LIMIT` | `25` | Results shown by `/search` (max 25) |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose logs |

## How it works

```
Discord voice  ◄── opus ── discord.py ◄── PCM ── ffmpeg ◄── HTTP ── Jellyfin
                              ▲
                       GuildPlayer (queue, loop, seek)
```

`jellyfin_bot/jellyfin.py` wraps the Jellyfin HTTP API (auth, search, album/artist/playlist
lookups, stream URLs, playback reporting). `jellyfin_bot/player.py` runs one asyncio task
per guild that pulls from the queue and feeds ffmpeg output to the voice client.
`jellyfin_bot/bot.py` holds the slash commands.

With `JELLYFIN_DIRECT_STREAM=true` the bot fetches `/Audio/{id}/stream?static=true` — the
original FLAC/MP3/whatever — and ffmpeg decodes it locally. Set it to `false` if you have
codecs ffmpeg can't handle or want to cap bandwidth between the bot and the server.

## Troubleshooting

- **Commands don't show up** — set `DISCORD_GUILD_IDS` and restart; global commands can
  take up to an hour.
- **Bot joins but is silent** — ffmpeg or libopus is missing (`ffmpeg -version`), or the
  bot lacks **Speak**. The log warns at startup when libopus isn't loaded.
- **"Jellyfin ... failed (401)"** — bad API key, or the token belongs to a deleted session.
- **Playback stutters** — try `JELLYFIN_DIRECT_STREAM=false` to have Jellyfin transcode to
  a lighter stream.

## Development

```bash
pip install -r requirements.txt pytest
pytest
```

The tests cover the Jellyfin URL/parsing layer and drive the playback loop through a fake
voice client, so they run without Discord, ffmpeg, or a server.
