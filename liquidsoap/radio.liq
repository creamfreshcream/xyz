# Internet radio: Jellyfin library on rotation, live DJ takes priority, out to Icecast.
#
#   playlist-sync ──► /playlists/radio.m3u ──┐
#                                            ├─► fallback ─► crossfade ─► Icecast
#   butt / Mixxx ──► input.harbor (:8005) ───┘   (live wins)

settings.log.stdout.set(true)
settings.log.file.set(false)
settings.log.level.set(3)

def env(name, default) =
  value = environment.get(name)
  if value == "" then default else value end
end

def env_int(name, default) =
  int_of_string(default=default, env(name, "#{default}"))
end

def env_float(name, default) =
  float_of_string(default=default, env(name, "#{default}"))
end

def env_bool(name, default) =
  value = string.case(lower=true, env(name, if default then "true" else "false" end))
  list.mem(value, ["1", "true", "yes", "on"])
end

station_name = env("RADIO_NAME", "Jellyfin Radio")
station_description = env("RADIO_DESCRIPTION", "Streaming straight from my Jellyfin library")
station_genre = env("RADIO_GENRE", "Various")
station_url = env("RADIO_URL", "")

icecast_host = env("ICECAST_HOST", "icecast")
icecast_port = env_int("ICECAST_PORT", 8000)
icecast_password = env("ICECAST_SOURCE_PASSWORD", "hackme")

playlist_file = env("RADIO_PLAYLIST_FILE", "/playlists/radio.m3u")
jingles_dir = env("RADIO_JINGLES_DIR", "")
jingle_every = env_int("RADIO_JINGLE_EVERY", 0)
crossfade_duration = env_float("RADIO_CROSSFADE", 3.0)

# ---------------------------------------------------------------- music source

music =
  playlist(
    id="library",
    mode="randomize",
    reload_mode="watch",
    playlist_file
  )

# Optional station IDs / jingles: one jingle every RADIO_JINGLE_EVERY tracks.
music =
  if jingles_dir != "" and jingle_every > 0 and file.exists(jingles_dir) then
    jingles = playlist(id="jingles", mode="randomize", reload_mode="watch", jingles_dir)
    rotate(weights=[1, jingle_every], [jingles, music])
  else
    music
  end

music = if crossfade_duration > 0. then crossfade(duration=crossfade_duration, music) else music end

music = if env_bool("RADIO_NORMALIZE", false) then normalize(music) else music end

# ------------------------------------------------------------------ live input

harbor_mount = env("RADIO_HARBOR_MOUNT", "live")
harbor_port = env_int("RADIO_HARBOR_PORT", 8005)
harbor_password = env("RADIO_HARBOR_PASSWORD", "hackme")
harbor_fade = env_float("RADIO_HARBOR_FADE", 2.0)

live =
  input.harbor(
    id="live",
    port=harbor_port,
    password=harbor_password,
    icy=true,
    buffer=5.0,
    max=20.0,
    on_connect=fun (_) -> log(level=2, "Live DJ connected"),
    on_disconnect=fun () -> log(level=2, "Live DJ disconnected"),
    harbor_mount
  )

# Fade between the two instead of cutting mid-word when a DJ (dis)connects.
def swap(old, new) =
  add(
    normalize=false,
    [fade.out(duration=harbor_fade, old), fade.in(duration=harbor_fade, new)]
  )
end

radio = fallback(id="radio", track_sensitive=false, transitions=[swap, swap], [live, music])

# Silence is still better than the stream dropping and every listener reconnecting.
radio = mksafe(radio)

# --------------------------------------------------------------------- outputs

def mp3_encoder(bitrate) =
  if bitrate >= 320 then %mp3(bitrate = 320, samplerate = 44100, stereo = true)
  elsif bitrate >= 256 then %mp3(bitrate = 256, samplerate = 44100, stereo = true)
  elsif bitrate >= 192 then %mp3(bitrate = 192, samplerate = 44100, stereo = true)
  elsif bitrate >= 128 then %mp3(bitrate = 128, samplerate = 44100, stereo = true)
  elsif bitrate >= 96 then %mp3(bitrate = 96, samplerate = 44100, stereo = true)
  else %mp3(bitrate = 64, samplerate = 44100, stereo = true)
  end
end

output.icecast(
  mp3_encoder(env_int("RADIO_BITRATE", 128)),
  id="out-main",
  host=icecast_host,
  port=icecast_port,
  password=icecast_password,
  mount=env("RADIO_MOUNT", "/stream.mp3"),
  name=station_name,
  description=station_description,
  genre=station_genre,
  url=station_url,
  encoding="UTF-8",
  radio
)

if env_bool("RADIO_HQ_ENABLED", false) then
  ignore(
    output.icecast(
      mp3_encoder(env_int("RADIO_HQ_BITRATE", 320)),
      id="out-hq",
      host=icecast_host,
      port=icecast_port,
      password=icecast_password,
      mount=env("RADIO_HQ_MOUNT", "/stream-hq.mp3"),
      name="#{station_name} (HQ)",
      description=station_description,
      genre=station_genre,
      url=station_url,
      encoding="UTF-8",
      radio
    )
  )
end
