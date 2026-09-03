# 📻 Jellyfin Radio

Ein selbst gehosteter Internetradiosender in Docker. Die Musik kommt aus
**Jellyfin**, das DJ-Wissen (Tempo, Tonart, Energie, Stimmung, Ähnlichkeit) aus
**AudioMuse-AI**. Daraus baut die App durchgehende Streams mit **smartem
Crossfade**, zeigt alle Sender in einem **Hub** und gibt die Stream-Adressen nur
**nach Anmeldung** heraus.

Neue Sender werden über typisierte **Structs** angelegt – aus Genres, Moods,
Artists oder Seed-Tracks, in einem Aufruf.

> Der Code, die Kommentare und die Oberfläche sind auf Englisch, diese Anleitung
> auf Deutsch.

---

## Inhalt

- [Schnellstart](#schnellstart)
- [Konfiguration](#konfiguration)
- [Authentifizierung und Tuner-URLs](#authentifizierung-und-tuner-urls)
- [Sender-Structs](#sender-structs)
- [Sender schnell anlegen](#sender-schnell-anlegen)
- [Smartes Crossfade](#smartes-crossfade)
- [Rotation und Flow](#rotation-und-flow)
- [API](#api)
- [Entwicklung](#entwicklung)
- [Fehlersuche](#fehlersuche)

---

## Schnellstart

```bash
git clone <dieses-repo> jellyfin-radio && cd jellyfin-radio
cp .env.example .env

# Secret erzeugen und eintragen
echo "RADIO_SECRET_KEY=$(openssl rand -hex 32)" >> .env
# In .env noch RADIO_JELLYFIN_URL, RADIO_JELLYFIN_API_KEY und
# RADIO_ADMIN_PASSWORD setzen.

docker compose up -d --build
```

Danach `http://localhost:8000` öffnen und mit `RADIO_ADMIN_USERNAME` /
`RADIO_ADMIN_PASSWORD` anmelden. Beim ersten Start legt die App vier
Beispielsender an (`/config/stations.yaml`) und den Admin-Account.

**Jellyfin-API-Key:** Jellyfin → Dashboard → API-Schlüssel → neuer Schlüssel.
Läuft Jellyfin in einem anderen Compose-Projekt, das externe Netzwerk in
`docker-compose.yml` einkommentieren.

---

## Konfiguration

Alles über Umgebungsvariablen mit dem Präfix `RADIO_` (siehe `.env.example`).
Die wichtigsten:

| Variable | Bedeutung |
| --- | --- |
| `RADIO_SECRET_KEY` | Signiert Sessions und Stream-Tokens. **Muss** gesetzt sein. |
| `RADIO_BASE_URL` | Öffentliche URL – daraus werden die Tuner-URLs gebaut. |
| `RADIO_JELLYFIN_URL` / `RADIO_JELLYFIN_API_KEY` | Musikquelle. |
| `RADIO_JELLYFIN_LIBRARY_ID` | Optional: nur eine Musikbibliothek verwenden. |
| `RADIO_AUDIOMUSE_URL` | AudioMuse-AI. Ohne läuft alles weiter, nur ärmer (siehe unten). |
| `RADIO_AUDIOMUSE_*_PATH` | Endpunktpfade – anpassbar, weil AudioMuse-Deployments sich unterscheiden. |
| `RADIO_ALLOW_BASIC_AUTH` | HTTP Basic auf `/stream/*` für klassische Radioplayer. |
| `RADIO_COOKIE_SECURE` | Auf `true` setzen, sobald HTTPS davor liegt. |

Zwei Volumes: `./config` (Senderdefinitionen als YAML) und `radio-data`
(Benutzer, gesperrte Tokens, Analyse-Cache).

> Der Container läuft als **uid 10001**, nicht als root. Damit das Hub Sender
> speichern kann, muss das gemountete Verzeichnis ihm gehören:
> `chown -R 10001:10001 ./config`. Fehlt das Recht, meldet die API einen
> klaren Fehler (`507`) statt Änderungen still zu verwerfen – gelesen wird
> weiterhin.

### Ohne AudioMuse

AudioMuse ist optional. Fällt es aus oder ist es abgeschaltet, misst die App die
Audiodaten selbst: Stille an den Rändern, Eigen-Fades, Lautheit und eine
BPM-Schätzung aus dem Ausklang. Es fehlen dann Tonart, Mood-Vektoren und
Ähnlichkeit – **Mood-Sender** brauchen AudioMuse, alle anderen Sendertypen
laufen weiter, nur mit gröberen Übergängen.

---

## Authentifizierung und Tuner-URLs

Ohne Anmeldung kommt niemand an Audio. Es gibt vier Wege hinein:

| Weg | Wofür |
| --- | --- |
| Session-Cookie | Das Hub im Browser (`POST /api/auth/login`) |
| Bearer-Token | API-Clients |
| Stream-Token in der URL | Radioplayer, Sonos, Autoradio – `…mp3?token=…` |
| HTTP Basic | Player, die keine Header setzen können (VLC, viele Hardware-Radios) |

**Tuner-URL** heißt die fertige Adresse mit eingebautem Token. Im Hub liefert
der Knopf „Tuner URL“ sie in die Zwischenablage:

```
http://radio.example/stream/deep-house.mp3?token=eyJleHAiOjE3…
```

Ein Token ist an **einen** Sender gebunden (oder an alle, wenn beim Anlegen
keine `station_id` angegeben wird), läuft nach `RADIO_STREAM_TOKEN_TTL_HOURS` ab
und lässt sich einzeln sperren:

```bash
curl -X DELETE http://localhost:8000/api/tokens \
     -H 'Content-Type: application/json' -b cookies.txt \
     -d '{"token":"eyJ…"}'
```

**Rollen:** `admin` darf alles (Sender anlegen, ändern, skippen, Benutzer
verwalten), `listener` darf hören. Pro Sender lässt sich zusätzlich einschränken:

```yaml
access:
  visibility: listed      # listed (sichtbar) | private (nur Admins) | public
  allowed_roles: [admin, listener]
  allowed_users: [lisa]   # leer = alle mit passender Rolle
```

> `visibility: public` ist der **einzige** Weg, einen Stream ohne Zugangsdaten
> auszuliefern. Bewusst gesetzt, sonst nie.

---

## Sender-Structs

Ein Sender ist ein typisiertes Objekt (`app/models.py`) – validiert, egal ob er
aus YAML, aus der API oder aus dem Hub kommt. Tippfehler in Feldnamen werden
abgelehnt statt still ignoriert.

```
StationSpec
├── sources[]    WOHER die Musik kommt   (genre | mood | artist | playlist | similar | library)
├── filters      WAS davon on air darf   (Jahr, BPM, Energie, Länge, Ausschlüsse)
├── rotation     WIE sortiert wird       (Flow, Wiederholsperren, Artist-Abstand)
├── crossfade    WIE übergeblendet wird  (Modus, Länge, Kurve, Beat-Align, Bass-Swap)
├── stream       Codec, Bitrate, Listener-Limit, always_on
├── access       Wer hören darf
├── dayparts[]   Tageszeit-Overrides
└── sweepers     Jingles alle N Titel
```

### Quellen

| `kind` | Felder | Beschreibung |
| --- | --- | --- |
| `genre` | `genres[]`, `match: any\|all` | Ein oder mehrere Genres |
| `mood` | `moods[]`, `min_score` | AudioMuse-Stimmungen |
| `artist` | `artists[]`, `include_similar` | Artist-Radio, optional mit ähnlichen Künstlern |
| `playlist` | `playlists[]` | Jellyfin-Playlists (Name oder Id) |
| `similar` | `seeds[]`, `radius` | „Klingt wie diese Titel“ |
| `library` | `search` | Die ganze Bibliothek |

Mehrere Quellen lassen sich mischen und über `weight` gewichten:

```yaml
sources:
  - kind: artist
    artists: [Miles Davis]
    include_similar: true
    weight: 2.0          # wird doppelt so oft gezogen
  - kind: genre
    genres: [Jazz, Bebop]
    weight: 1.0
```

### Mixing-Templates

Statt jedes Crossfade-Feld einzeln zu setzen, gibt es getunte Bündel:

| Template | Charakter |
| --- | --- |
| `club` | Lange, beat-synchrone Blends (16/32 Takte), Bass-Swap. House, Techno, Disco. |
| `radio` | Ausgewogen, 5–8 s. Der Allrounder. |
| `vocal` | Kurze 2–4 s Fades, die keine Gesangsintros zudecken. |
| `chill` | Sehr lange, weiche Überblendungen, Energiedeckel. |
| `workout` | Nur hohe Energie, harte Slams, enges Tempofenster. |
| `sleep` | Minimale Energie, 20 s Dissolves, leiser Zielpegel. |
| `talk` | Harte Schnitte mit Pause. Hörbücher, Podcasts. |

---

## Sender schnell anlegen

**Im Hub:** „Stations“ → Quelle wählen, Werte eintippen, Template wählen,
fertig. Im Feld „Advanced“ lässt sich jedes Struct-Feld als JSON überschreiben.

**Per API:**

```bash
curl -X POST http://localhost:8000/api/stations/quick -b cookies.txt \
  -H 'Content-Type: application/json' -d '{
    "kind": "genre",
    "name": "Deep House Nights",
    "values": ["Deep House", "House", "Nu Disco"],
    "template": "club"
  }'
```

Artist-Radio inklusive ähnlicher Künstler, mit abweichendem Crossfade:

```bash
curl -X POST http://localhost:8000/api/stations/quick -b cookies.txt \
  -H 'Content-Type: application/json' -d '{
    "kind": "artist",
    "name": "Bowie Radio",
    "values": ["David Bowie"],
    "include_similar": true,
    "overrides": {
      "crossfade": {"default_seconds": 4, "min_seconds": 2, "max_seconds": 8},
      "access": {"visibility": "private"}
    }
  }'
```

**Per CLI:**

```bash
docker compose exec radio python -m app.cli add-genre  "Techno Bunker" "Techno,Minimal" --template club
docker compose exec radio python -m app.cli add-mood   "Sunday Morning" "calm,warm"
docker compose exec radio python -m app.cli add-artist "Bowie Radio" "David Bowie" --similar
docker compose exec radio python -m app.cli stations
docker compose exec radio python -m app.cli user add lisa --role listener
```

**Per YAML:** `config/stations.example.yaml` enthält sechs kommentierte Sender
zum Abkupfern. Nach dem Bearbeiten `docker compose restart radio`.

**Aus Python:**

```python
from app.presets import quick_genre, quick_mood, quick_artist

spec = quick_genre("Deep House Nights", ["Deep House"], template="club")
spec = quick_mood("Sunday Morning", ["calm", "warm"])
spec = quick_artist("Bowie Radio", ["David Bowie"], include_similar=True)
```

---

## Smartes Crossfade

Vor jedem Übergang liegen der **Ausklang** des laufenden und der **Anfang** des
nächsten Titels im Speicher. Beide werden vermessen, dann wird entschieden –
nicht nach Metadaten allein, sondern am echten Audio:

| Einfluss | Wirkung auf den Übergang |
| --- | --- |
| **Tempo** | Gleiches Tempo (±6 %, inkl. 2:1-Verhältnis) → länger und auf ganze Takte gerastert (8/16/32). Tempo-Clash → deutlich kürzer, damit die Kollision schnell vorbei ist. |
| **Energie** | Großer Sprung nach oben → kurzer, harter Slam (`exponential`). Abfall → langer Ausklang (`s_curve`). |
| **Tonart** | Camelot-Wheel: gleiche oder benachbarte Tonart → länger. Clash → gekürzt plus Bass-Swap. |
| **Audio** | Stille an den Rändern wird abgeschnitten, ein bereits vorhandener Eigen-Fade begrenzt die Überblendung. Nie mehr als ein Drittel des kürzeren Titels. |
| **Lautheit** | Pro Titel Gain Richtung `target_lufs` (Standard −14 LUFS), gedeckelt, mit weichem Limiter am Ende. |

**Bass-Swap:** Über den auslaufenden Titel läuft ein Hochpass, dessen Grenzfrequenz
während der Blende von 20 Hz auf `bass_swap_hz` (Standard 180 Hz) steigt – so
prallen nie zwei Basslines aufeinander. Umgesetzt als Overlap-Add-STFT in numpy,
also schnell genug für den laufenden Betrieb.

Jeder Übergang protokolliert seine Begründung; das Hub zeigt sie an:

```
station deep-house: 'Alpha – Two Twenty' -> 'Beta – Three Thirty' [7.7s linear, beat-matched]
  tempo matched (124->124); harmonic (8A->9A); 16 beats; bass swap @180 Hz
```

Modi: `smart` (oben beschrieben), `fixed` (feste Länge) und `cut` (harter
Schnitt mit optionaler Pause, für Sprache).

---

## Rotation und Flow

Harte Regeln – keine Wiederholung innerhalb von N Titeln bzw. M Minuten,
Mindestabstand pro Artist, Stundenlimits pro Artist und Album. Sie gelten auch
für die **Vorausplanung**, damit die Warteschlange nicht zweimal denselben
Künstler hintereinander einplant.

Ist der Pool zu klein für die Regeln, werden sie **einzeln nacheinander**
aufgegeben – erst Album-Limit, dann Artist-Limit, dann Artist-Abstand – statt
alle auf einmal. Die Wiederholsperre fällt zuletzt.

Innerhalb des Erlaubten bestimmt `flow` die Reihenfolge:

- `smart` – Ähnlichkeitslauf über Tempo, Tonart, Energie, Mood und Genre, mit
  `temperature` als Zufallsanteil (0 = immer der beste Anschluss, 1 = Shuffle)
- `energy_curve` – folgt dem Energieziel des aktuellen Dayparts
- `random` – Shuffle
- `sorted` – Bibliotheksreihenfolge (Album-/Chronologie-Sender)

---

## API

Alles unter `/api` verlangt eine Anmeldung; Schreibendes verlangt `admin`.
Interaktive Doku: `http://localhost:8000/docs`.

| Methode | Pfad | Zweck |
| --- | --- | --- |
| `POST` | `/api/auth/login` · `/logout` · `/password` | Anmeldung |
| `GET/POST/PATCH/DELETE` | `/api/auth/users…` | Benutzerverwaltung (admin) |
| `GET` | `/api/stations` · `/api/stations/{id}` | Sender lesen |
| `POST` | `/api/stations` · `/api/stations/quick` | Sender anlegen |
| `PATCH/DELETE` | `/api/stations/{id}` | Ändern / löschen |
| `POST` | `/api/stations/{id}/start·stop·skip·refresh` | Steuerung |
| `GET` | `/api/stations/{id}/nowplaying · queue · listeners · events` | Live-Status (SSE) |
| `POST/DELETE` | `/api/tokens` | Tuner-Token erzeugen / sperren |
| `GET` | `/api/templates` | Templates **und JSON-Schema aller Structs** |
| `GET` | `/api/library/genres · artists · playlists · moods` | Autovervollständigung |
| `GET` | `/api/health` · `/healthz` | Readiness / Liveness |
| `GET` | `/stream/{id}.{mp3\|aac\|opus}` | Der Stream |

Der Stream sendet ICY-Header (`icy-name`, `icy-genre`, `icy-br`) und – wenn der
Player `Icy-MetaData: 1` schickt – laufende Titelmetadaten.

---

## Entwicklung

```bash
pip install -r requirements-dev.txt
pytest                                  # 63 Tests
uvicorn app.main:app --reload
```

Die Tests decken das Crossfade-Planning, die Rotationsregeln, Tokens und
Passwörter, die Struct-Validierung samt YAML-Persistenz und die HTTP-Zugriffskontrolle ab. `tests/test_engine.py` fährt die echte Engine mit echtem ffmpeg
gegen erzeugte Testtitel und prüft am dekodierten Ergebnis, dass wirklich
übergeblendet wurde und der Stream nie abreißt (wird ohne ffmpeg übersprungen).

```
app/
├── models.py          die Structs
├── presets.py         Templates + quick_genre/mood/artist/similar/library
├── store.py           stations.yaml lesen/schreiben
├── jellyfin.py        Bibliothek und Audioquelle
├── audiomuse.py       Analyse, Ähnlichkeit, Camelot-Umrechnung, Cache
├── library.py         Quellen -> Trackpool (+ Filter)
├── scheduler.py       Rotationsregeln und Flow
├── audio/
│   ├── analysis.py    Stille, Eigen-Fade, Lautheit, BPM-Schätzung
│   ├── crossfade.py   Übergangsplanung und -rendering
│   ├── ffmpeg.py      Decoder-/Encoder-Prozesse
│   ├── engine.py      die Playout-Schleife
│   └── broadcast.py   Verteilung an die Hörer, ICY-Metadaten
├── manager.py         startet/stoppt Sender nach Bedarf
├── auth.py            Benutzer, Sessions, Stream-Autorisierung
└── api/               HTTP-Routen und Hub-Seiten
```

Sender starten automatisch, sobald der erste Hörer verbindet, und stoppen nach
`idle_timeout_seconds` ohne Hörer wieder – außer bei `always_on: true`.

---

## Fehlersuche

**„Jellyfin unreachable“ im Hub** – `RADIO_JELLYFIN_URL` muss aus dem Container
erreichbar sein (nicht `localhost`, sondern Servicename oder IP). Prüfen:
`docker compose exec radio python -c "import httpx,os;print(httpx.get(os.environ['RADIO_JELLYFIN_URL']+'/System/Info/Public').json())"`

**Sender bleibt „idle“, keine Titel** – Quellen liefern nichts. `POST
/api/stations/{id}/refresh` zeigt, wie viele Titel im Pool landen. Häufig zu
enge Filter (`require_analysis: true` ohne AudioMuse, zu enges BPM-Fenster).

**Übergänge sind immer gleich lang** – ohne AudioMuse fehlen Tonart und Energie;
dann bleibt es beim `default_seconds`. `analysis.source` in
`/api/stations/{id}/nowplaying` zeigt, woher die Daten kommen.

**Player bricht ab** – langsame Clients verlieren Chunks statt den Sender
aufzuhalten. Bei knapper Bandbreite Bitrate senken (`stream.bitrate`).

**Tuner-URL funktioniert nicht mehr** – Token abgelaufen oder gesperrt. Im Hub
neu erzeugen; `RADIO_STREAM_TOKEN_TTL_HOURS` steuert die Laufzeit.
