# Jellyfin Internet Radio

A self-hosted internet radio station: it pulls a playlist out of your own
[Jellyfin](https://jellyfin.org) library, streams it around the clock through Icecast, and
serves a small web player over HTTPS. A live DJ can take over the stream at any time.

Built to run on a single Hetzner box with `docker compose up -d`.

## What's in the box

- **An autonomous DJ**, not a shuffled playlist — `dj/` picks a mood target for the
  time of day, sonically bridges to it from whatever's already queued (via AudioMuse-AI's
  path-finding over your library's audio embeddings), then dwells nearby for a while before
  picking the next target. Never a hard playlist swap; new arrivals in Jellyfin become
  eligible candidates automatically, no re-sync step.
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
AudioMuse-AI Postgres ──mood data──┐
Jellyfin ──HTTP──────────┐         ▼
                          └──►  dj  ──telnet push──►  request.queue ──┐
butt / Mixxx ──►  harbor :8005  ────────────────────────┐             ├─►  Liquidsoap  ──MP3──►  Icecast  ──►  Caddy ──► 🎧
                                                         └─────────────┘   (live wins)              (TLS + player)
```

- `dj/dj.py` decides what plays, one track at a time: it picks a mood target for the
  current daypart (`dj/schedule.json`) by comparing candidate tracks' mood vectors —
  read straight out of the `audiomuse-postgres` database AudioMuse-AI's own Jellyfin
  plugin maintains — against that daypart's targets; bridges to it from the last queued
  track using the plugin's `find_path` sonic-pathfinding endpoint; lingers near the target
  for a while via `similar_tracks`; and pushes the resulting `annotate:` URIs onto
  Liquidsoap's `request.queue` over its telnet control socket, topping the queue up before
  it runs dry. It also serves a small status/override HTTP API (see below).
- `liquidsoap/radio.liq` reads from that queue (nothing to reload, no file on disk),
  smart-crossfades between tracks — Liquidsoap measures the loudness at each track
  boundary and picks the transition accordingly, rather than a fixed blind fade — gives a
  connected live DJ priority, and pushes MP3 to Icecast. `mksafe` keeps the mount alive
  with silence rather than dropping every listener if a source ever fails.
- `icecast/` is Debian's Icecast with the config rendered from environment variables.
- `Caddyfile` terminates TLS, serves `web/index.html`, and proxies the stream plus
  `/status-json.xsl` (the player reads now-playing from it) and `/dj/status`.

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
  dj:
    networks: [jellyfin]
networks:
  jellyfin:
    external: true
```

The `dj` service also needs to reach AudioMuse-AI's Postgres database for mood data —
`docker-compose.yml` already attaches it to `audiomuse_default` (AudioMuse-AI's own compose
network) as an external network; adjust that name if your AudioMuse-AI stack's project name
differs (`docker network ls` shows the real one).

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
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_USER` / `POSTGRES_DB` | `postgres` / `5432` / `audiomuse` / `audiomusedb` | AudioMuse-AI's own database, read-only |
| `POSTGRES_PASSWORD` | — | **Required.** Same one AudioMuse-AI's containers use |
| `DJ_LOOKAHEAD_TRACKS` | `6` | Tracks kept queued ahead in Liquidsoap |
| `DJ_LOOP_INTERVAL` | `60` | Seconds between queue top-up checks |
| `DJ_BRIDGE_MAX_TRACKS` | `14` | Cap on tracks spent bridging to a new target |
| `DJ_DWELL_TRACKS` | `6` | Tracks spent lingering near a target |
| `DJ_RECENT_TRACK_HOURS` / `DJ_RECENT_ARTIST_MINUTES` | `3` / `45` | Repeat-avoidance windows |
| `DJ_STATUS_PORT` | `9090` | The `dj` service's own status/override API |
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
| `LOG_LEVEL` | `INFO` | `DEBUG` for the `dj` service's logs |

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

## The DJ

`dj/schedule.json` holds the daypart guardrails — edit it and restart the `dj` service to
pick up changes, no rebuild needed:

```bash
$EDITOR dj/schedule.json
docker compose restart dj
```

Check what it's doing, or push a target immediately (bridges to it from whatever's
currently queued, same as an automatic transition):

```bash
curl -u creamfresh:<password> https://<your-domain>/dj/status
curl -u creamfresh:<password> -X POST https://<your-domain>/dj/target \
  -d '{"query": "ultradespair"}'
# or by Jellyfin item ID: -d '{"item_id": "8e0567b799acbea96fb855c90a81cea8"}'
```

## Operating it

```bash
docker compose logs -f liquidsoap  # what is playing, and reconnects
docker compose logs -f dj          # target picks, bridges, and queue top-ups
docker compose restart liquidsoap  # picks up .liq or .env changes
docker compose restart dj          # picks up schedule.json or .env changes
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
- **`dj` never pushes anything** — check `docker compose logs dj` first; it retries
  forever, but a wrong `LIQUIDSOAP_HOST`/telnet port, an unreachable Postgres
  (`POSTGRES_PASSWORD` mismatch, or `dj` not on AudioMuse-AI's network), or zero candidate
  tracks (AudioMuse-AI hasn't analysed anything from this Jellyfin server yet) will all
  show up there as repeated warnings.
- **Silence on the mount** — Liquidsoap is playing `mksafe`'s blank source because the
  queue ran dry. Check `docker compose logs dj` for push failures, and confirm Jellyfin is
  reachable *from the `liquidsoap` container*, not just from `dj`.
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

The tests cover `dj`'s pure logic — mood-vector distance, daypart resolution, path
subsampling, the `annotate:` escaping, and state persistence — against fakes, so no
Jellyfin, Postgres, or Liquidsoap instance is needed.

Check the Liquidsoap script without starting the stack:

```bash
docker run --rm -v "$PWD/liquidsoap/radio.liq:/tmp/radio.liq:ro" \
  savonet/liquidsoap:v2.2.5 liquidsoap --check /tmp/radio.liq
```
