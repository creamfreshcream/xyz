# Internet radio: a `dj` service decides tracks one at a time and pushes them
# onto this queue over the telnet control socket — there's no playlist file
# to reload, so there's nothing to jump or restart when the target changes.
#
#   dj (telnet push) ──► request.queue ──┐
#                                        ├─► fallback ─► crossfade ─► Icecast
#   butt / Mixxx ──► input.harbor (:8005) ┘   (live wins)

settings.log.stdout.set(true)
settings.log.file.set(false)
settings.log.level.set(3)

# Telnet control socket for the `dj` service to push tracks and check queue
# depth (`dj_queue.push <uri>`, `dj_queue.queue`). Bound to all interfaces
# *inside this container* so the `dj` container can reach it by service name
# over the compose network — but docker-compose.yml never publishes this
# port to the host, so it's unreachable from outside the stack.
settings.server.telnet.set(true)
settings.server.telnet.bind_addr.set("0.0.0.0")
settings.server.telnet.port.set(1234)

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

jingles_dir = env("RADIO_JINGLES_DIR", "")
jingle_every = env_int("RADIO_JINGLE_EVERY", 0)
crossfade_duration = env_float("RADIO_CROSSFADE", 3.0)

# Every mount that carries this station's audio, so a metadata push (see
# smart_transition below) can reach all of them, not just the main one.
radio_mounts =
  list.append(
    [env("RADIO_MOUNT", "/stream.mp3")],
    if env_bool("RADIO_HQ_ENABLED", false) then [env("RADIO_HQ_MOUNT", "/stream-hq.mp3")] else [] end
  )

# ------------------------------------------------------------- metadata cleanup

# Some library tags carry literal HTML entities (e.g. "&#269;" instead of "č")
# rather than the real character -- almost certainly baked in by whatever tool
# tagged the file, not something introduced here. Icecast's own status-json.xsl
# does its own separate entity-escaping on top of that for the web player,
# handled client-side; this instead cleans the metadata itself before it goes
# anywhere, so every consumer of the stream (ICY metadata included) sees the
# real character.
def utf8_char(code) =
  if code < 0x80 then
    string.char(code)
  elsif code < 0x800 then
    string.char(0xC0 + code / 64) ^ string.char(0x80 + code mod 64)
  elsif code < 0x10000 then
    string.char(0xE0 + code / 4096) ^
    string.char(0x80 + (code / 64) mod 64) ^
    string.char(0x80 + code mod 64)
  else
    string.char(0xF0 + code / 262144) ^
    string.char(0x80 + (code / 4096) mod 64) ^
    string.char(0x80 + (code / 64) mod 64) ^
    string.char(0x80 + code mod 64)
  end
end

def decode_entities(s) =
  s = regexp(flags=["g"], "&#[0-9]+;").replace(fun (m) -> begin
    let code = int_of_string(string.sub(m, start=2, length=string.length(m)-3), default=0)
    if code > 0 then utf8_char(code) else m end
  end, s)
  s = regexp(flags=["g"], "&#x[0-9a-fA-F]+;").replace(fun (m) -> begin
    let hex = string.sub(m, start=3, length=string.length(m)-4)
    let code = int_of_string("0x" ^ hex, default=0)
    if code > 0 then utf8_char(code) else m end
  end, s)
  s = string.replace(pattern="&amp;", fun (_) -> "&", s)
  s = string.replace(pattern="&lt;", fun (_) -> "<", s)
  s = string.replace(pattern="&gt;", fun (_) -> ">", s)
  s = string.replace(pattern="&quot;", fun (_) -> "\"", s)
  s = string.replace(pattern="&#39;|&apos;", fun (_) -> "'", s)
  s
end

def decode_metadata(m) =
  list.map(fun (kv) -> begin
    let (k, v) = kv
    (k, decode_entities(v))
  end, m)
end

# ---------------------------------------------------------------- music source

# The `dj` service is the only writer: it pushes one track at a time via
# telnet (`dj_queue.push <annotate-uri>`) and tops the queue up before it
# runs dry, checking depth with `dj_queue.queue`. `interactive` (telnet
# control) defaults to true, which is exactly what's needed here.
music = request.queue(id="dj_queue")
music = metadata.map(decode_metadata, music)

# Optional station IDs / jingles: one jingle every RADIO_JINGLE_EVERY tracks.
music =
  if jingles_dir != "" and jingle_every > 0 and file.exists(jingles_dir) then
    jingles = playlist(id="jingles", mode="randomize", reload_mode="watch", jingles_dir)
    rotate(weights=[1, jingle_every], [jingles, music])
  else
    music
  end

# Loudness-aware crossfade, hand-rolled on top of `cross()` instead of
# `crossfade(smart=true, ...)` -- the built-in "smart" mode makes the same
# kind of decision but never explains it in the logs, which makes the
# thresholds impossible to tune from the outside. This logs every
# transition's measured dB levels and the decision made from them, and the
# thresholds themselves are env-configurable so they can actually be tuned
# against what shows up in the log rather than guessed at blind.
crossfade_high_db = env_float("RADIO_CROSSFADE_HIGH_DB", -15.)
crossfade_low_db = env_float("RADIO_CROSSFADE_LOW_DB", -32.)
crossfade_margin_db = env_float("RADIO_CROSSFADE_MARGIN_DB", 4.)

def smart_transition(a, b) =
  let {db_level = a_level, metadata = a_meta, source = a_source} = a
  let {db_level = b_level, metadata = b_meta, source = b_source} = b
  let a_title = list.assoc(default="?", "title", a_meta)
  let b_title = list.assoc(default="?", "title", b_meta)
  # Set by the dj service (annotate: ...,segue="true":...) whenever the
  # incoming track is genuinely album-consecutive with the one ending --
  # naturally, via its "double feature" fix, or as a prelude/main-track
  # pair. Overrides the dB-based decision entirely: albums are sequenced by
  # the artist, a blended transition would work against that.
  let segue = list.assoc(default="false", "segue", b_meta) == "true"
  let segue_note = if segue then " [segue: album-consecutive, no crossfade]" else "" end
  log(label="smart_crossfade", level=3,
    "#{a_title} (#{a_level}dB) -> #{b_title} (#{b_level}dB)#{segue_note}")
  # Icecast's title was found live to get stuck on whatever track was
  # playing when the stream started, never updating on later transitions --
  # output.icecast's own automatic ICY-update detection doesn't reliably
  # fire for a track that only ever appears as the "b" side of a
  # dynamically-composed cross() transition source. Push the update
  # explicitly here instead, since this function demonstrably runs once per
  # real transition (the log line above proves it).
  #
  # thread.run(fast=false, ...) is not optional: a first attempt called
  # icy.update_metadata directly here, which blocked the main streaming
  # thread on the HTTP round-trip long enough that Icecast's own read
  # timeout fired and dropped the source connection entirely (confirmed
  # live: "Disconnecting source due to socket timeout" ~17s after a
  # successful update, killing the stream for all listeners).
  list.iter(fun (mount) ->
    thread.run(fast=false, fun () ->
      icy.update_metadata(
        host=icecast_host, port=icecast_port, password=icecast_password,
        mount=mount, b_meta
      )
    )
  , radio_mounts)
  # Both "no crossfade" cases below use a near-zero-duration fade rather than
  # sequence() -- sequence() inside cross() was observed live to leave
  # Icecast's title metadata stuck on the outgoing track for many tracks
  # in a row (found via the segue case, which made this path common enough
  # to notice; presumably always affected the quiet-track case too). A
  # ~0-duration add()/fade pair is audibly a hard cut but stays on the same
  # metadata-safe path the crossfade branches already use correctly.
  if segue then
    log(label="smart_crossfade", level=3, "  decision: hard cut (album-consecutive)")
    add(normalize=false, [fade.out(duration=0.01, a_source), fade.in(duration=0.01, b_source)])
  elsif a_level >= crossfade_high_db and b_level >= crossfade_high_db then
    log(label="smart_crossfade", level=3, "  decision: crossfade (both loud)")
    add(normalize=false, [fade.out(duration=crossfade_duration, a_source), fade.in(duration=crossfade_duration, b_source)])
  elsif a_level < crossfade_low_db or b_level < crossfade_low_db then
    log(label="smart_crossfade", level=3, "  decision: hard cut (one side too quiet to blend)")
    add(normalize=false, [fade.out(duration=0.01, a_source), fade.in(duration=0.01, b_source)])
  elsif abs(a_level - b_level) <= crossfade_margin_db then
    log(label="smart_crossfade", level=3, "  decision: crossfade (similar levels)")
    add(normalize=false, [fade.out(duration=crossfade_duration, a_source), fade.in(duration=crossfade_duration, b_source)])
  else
    log(label="smart_crossfade", level=3, "  decision: fade (level mismatch)")
    add(normalize=false, [fade.out(duration=crossfade_duration, a_source), fade.in(duration=crossfade_duration, b_source)])
  end
end

music =
  if crossfade_duration > 0. then
    cross(id="smart_crossfade", duration=crossfade_duration + 2., smart_transition, music)
  else
    music
  end

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
