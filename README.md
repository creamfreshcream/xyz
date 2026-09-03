# Jellyfin Internet Radio

A self-hosted internet radio station: it pulls a playlist out of your own
[Jellyfin](https://jellyfin.org) library, streams it around the clock through Icecast, and
serves a small web player over HTTPS. A live DJ can take over the stream at any time.

Built to run on a single Hetzner box with `docker compose up -d`.

## What's in the box

- **24/7 rotation** from your Jellyfin library — the whole thing, or filtered down to a
  genre, a year range, your favourites, or one named Jellyfin playlist.
- **Correct metadata** — title/artist/album travel from Jellyfin into the Icecast stream,
  so players and the web page show the right track.
- **Live DJ input** — connect butt, Mixxx, or anything else that speaks the Icecast source
  protocol; the playlist fades out, and fades back in when you disconnect.
- **Web player** — a single static page with now-playing, listener count, and the last few
  tracks. No build step, no framework.
- **Automatic HTTPS** via Caddy, and the stream proxied through it so listeners only ever
  see port 443.
- Crossfades, optional loudness levelling, optional station IDs, and an optional second
  high-bitrate mount.

## How it works

```
Jellyfin ──HTTP──►  playlist-sync  ──►  /playlists/radio.m3u
                                              │  (annotate: title/artist/album)
butt / Mixxx ──►  harbor :8005  ──┐           ▼
                                  └──►  Liquidsoap  ──MP3──►  Icecast  ──►  Caddy ──► 🎧
                                       (live wins)                          (TLS + player)
```

- `playlist_sync/jellyfin_playlist.py` asks Jellyfin for tracks and writes them as
  `annotate:` URIs into a playlist file, every `RADIO_REFRESH_INTERVAL` seconds. Standard
  library only — no dependencies to keep up to date.
- `liquidsoap/radio.liq` plays that playlist in random order, crossfades between tracks,
  gives a connected live DJ priority, and pushes MP3 to Icecast. `mksafe` keeps the mount
  alive with silence rather than dropping every listener if a source ever fails.
- `icecast/` is Debian's Icecast with the config rendered from environment variables.
- `Caddyfile` terminates TLS, serves `web/index.html`, and proxies the stream plus
  `/status-json.xsl` (the player reads now-playing from it).

Liquidsoap reads the original file straight from Jellyfin (`static=true`), so Jellyfin does
no transcoding work — one decode happens in Liquidsoap, then a single MP3 encode serves
every listener.

## Setup on the Hetzner box

### 1. DNS and firewall

Point an `A` (and `AAAA`) record at the server, then open the ports you need:

```bash
ufw allow 80,443/tcp
ufw allow 443/udp        # HTTP/3
ufw allow 8005/tcp       # only if you broadcast live
```

Leave 8005 closed unless you actually DJ — the Icecast source protocol sends its password
in the clear, so restrict it to your own IP if you can:

```bash
ufw allow from <your-ip> to any port 8005 proto tcp
```

### 2. Jellyfin credentials

Create an API key under **Dashboard → API Keys**. For favourites or a named playlist you
also need `JELLYFIN_USER_ID` — it's in the URL of **Dashboard → Users → \<user\>**.

`JELLYFIN_URL` has to be reachable from the radio host. If Jellyfin runs on the same
machine under compose, attach the stack to its network:

```yaml
# docker-compose.override.yml
services:
  playlist-sync:
    networks: [jellyfin]
  liquidsoap:
    networks: [jellyfin]
networks:
  jellyfin:
    external: true
```

### 3. Configure and start

```bash
cp .env.example .env
$EDITOR .env          # domain, Jellyfin key, and three passwords
docker compose up -d --build
```

Then open `https://<your-domain>/`. The stream itself lives at
`https://<your-domain>/stream.mp3` — that's the URL to paste into VLC, a car radio, or
a directory listing.

## Configuration reference

Everything lives in `.env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `JELLYFIN_URL` | — | **Required.** e.g. `http://jellyfin:8096` |
| `JELLYFIN_API_KEY` | — | **Required.** Dashboard → API Keys |
| `JELLYFIN_USER_ID` | — | Needed for favourites and named playlists |
| `RADIO_LIMIT` | `2000` | Tracks in the rotation |
| `RADIO_REFRESH_INTERVAL` | `3600` | Seconds between playlist rebuilds |
| `RADIO_GENRES` | — | Comma-separated, e.g. `Jazz, Soul` |
| `RADIO_YEARS` | — | Comma-separated years, e.g. `1978,1979` |
| `RADIO_FAVORITES_ONLY` | `false` | Only your favourites |
| `RADIO_JELLYFIN_PLAYLIST` | — | Name of a Jellyfin playlist; overrides the filters |
| `RADIO_STREAM_MODE` | `direct` | `transcode` makes Jellyfin transcode first |
| `RADIO_MAX_BITRATE` | `320000` | Only used when transcoding |
| `RADIO_NAME` / `RADIO_DESCRIPTION` / `RADIO_GENRE` / `RADIO_URL` | — | Shown in players and directories |
| `RADIO_MOUNT` | `/stream.mp3` | Change it in the `Caddyfile` too |
| `RADIO_BITRATE` | `128` | Rounded down to 64/96/128/192/256/320 |
| `RADIO_HQ_ENABLED` | `false` | Second mount at `RADIO_HQ_BITRATE` |
| `RADIO_CROSSFADE` | `3` | Seconds; `0` turns crossfading off |
| `RADIO_NORMALIZE` | `false` | Levels out a library with mixed loudness |
| `RADIO_JINGLES_DIR` | `/jingles` | Drop station IDs into `./jingles` |
| `RADIO_JINGLE_EVERY` | `0` | One jingle every N tracks; `0` disables |
| `RADIO_HARBOR_PORT` / `RADIO_HARBOR_MOUNT` / `RADIO_HARBOR_PASSWORD` | `8005` / `live` | Live DJ input |
| `RADIO_HARBOR_FADE` | `2` | Seconds of fade when a DJ joins or leaves |
| `ICECAST_SOURCE_PASSWORD` | — | **Change it.** Liquidsoap uses it to publish |
| `ICECAST_ADMIN_USER` / `ICECAST_ADMIN_PASSWORD` | `admin` / — | Icecast admin UI |
| `ICECAST_MAX_LISTENERS` | `200` | Hard cap on concurrent listeners |
| `ICECAST_HOSTNAME` / `ICECAST_LOCATION` / `ICECAST_ADMIN_EMAIL` | — | Cosmetic, shown in status pages |
| `RADIO_DOMAIN` | `localhost` | Your domain, or `:80` to test without TLS |
| `LOG_LEVEL` | `INFO` | `DEBUG` for the playlist sync |

## Going live

Point your broadcasting software at the harbor input:

| Setting | Value |
| --- | --- |
| Server type | Icecast 2 |
| Address / port | `<your-domain>` / `8005` |
| Mount | `live` (no leading slash in most clients) |
| User | `source` |
| Password | `RADIO_HARBOR_PASSWORD` |
| Format | MP3 or Ogg, 128 kbps or better |

Connect and the playlist fades out within `RADIO_HARBOR_FADE` seconds; disconnect and it
fades back in. Listeners never lose the connection because the mount itself never drops.

## Operating it

```bash
docker compose logs -f liquidsoap     # what is playing, and reconnects
docker compose logs -f playlist-sync  # "Wrote 2000 tracks to /playlists/radio.m3u"
docker compose restart liquidsoap     # picks up .liq or .env changes
docker compose exec playlist-sync python jellyfin_playlist.py --once   # rebuild now
```

The Icecast admin UI is deliberately not exposed. Reach it through an SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 you@your-server
# then http://127.0.0.1:8000/admin/ — user "admin", ICECAST_ADMIN_PASSWORD
```

**Bandwidth.** A 128 kbps stream costs about 56 MB per listener-hour, so 50 concurrent
listeners run at roughly 2.8 GB/h. Hetzner's included traffic is generous, but
`ICECAST_MAX_LISTENERS` is what keeps a surprise from becoming an overage.

**Licensing.** Streaming music you don't own the rights to is a licensing question
(GEMA/GVL in Germany) that this repo cannot solve for you.

## Troubleshooting

- **Player says "Off air"** — `docker compose logs liquidsoap`. If it never connected,
  `ICECAST_SOURCE_PASSWORD` differs between the two containers (they share `.env`, so
  check you edited only one place).
- **`playlist-sync` unhealthy, Liquidsoap never starts** — the playlist file is empty:
  wrong `JELLYFIN_URL`, a key without access, or filters that match nothing. The log line
  is `Jellyfin returned no tracks`.
- **Silence on the mount** — Liquidsoap is playing `mksafe`'s blank source because every
  request failed. Usually Jellyfin is unreachable *from the container*; test with
  `docker compose exec liquidsoap curl -sI "<a URL from radio.m3u>"`.
- **No certificate** — Caddy needs port 80 reachable from the internet for the ACME
  challenge, and `RADIO_DOMAIN` must resolve to this box.
- **Live input refused** — most clients want the mount without a leading slash (`live`)
  and the user `source`.
- **Stuttering** — check CPU on the box (`docker stats`); a 320 kbps mount plus
  normalisation on a shared vCPU is the usual cause. Turn off `RADIO_HQ_ENABLED` first.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install pytest
pytest
```

The tests cover the playlist builder — Jellyfin query construction, the `annotate:`
escaping, and the atomic playlist write — against a fake Jellyfin, so no server is needed.

Check the Liquidsoap script without starting the stack:

```bash
docker run --rm -v "$PWD/liquidsoap/radio.liq:/tmp/radio.liq:ro" \
  savonet/liquidsoap:v2.2.5 liquidsoap --check /tmp/radio.liq
```
