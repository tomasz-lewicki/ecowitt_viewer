# garden_sensors

Local dashboard for an Ecowitt GW1100 Wi-Fi gateway and WH51 soil moisture
sensors. Polls the gateway, stores every reading in SQLite, and serves a
per-plant dashboard — gauges for right now, small-multiple history charts for
the last week.

Everything runs on your own network. No cloud account, no outbound calls
except to the gateway on your LAN.

## Features

- **A panel per plant.** One small-multiple chart per channel rather than a
  dozen overlapping lines, so "which one needs water" is a glance.
- **Per-plant moisture bands.** Each channel gets its own acceptable range,
  edited in the UI and stored in the database. Gauge colour is judged against
  that plant's band, not one global rule — 35% can be ideal for one pot and
  far too dry for another.
- **Hysteresis.** A reading sitting on a threshold doesn't repaint the gauge
  on every poll.
- **Kiosk mode** (`?kiosk=1`) for a wall display or screensaver: full-bleed,
  no controls, and the history drawn as server-rendered SVG so the page stays
  a few KB and needs no charting library.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

`ECOWITT_GATEWAY` is required — it is specific to your network, so there is
no default:

```sh
ECOWITT_GATEWAY=192.168.1.50 .venv/bin/python app.py
```

Then open <http://127.0.0.1:8050>. The dashboard refreshes every 15s.

## Naming your channels

Channels show as `Channel 1`, `Channel 2`, … until you name them. Copy the
example and edit it:

```sh
cp channels.example.json channels.json
```

```json
{
  "1": "Tomatoes 🍅",
  "2": "Herb box 🌿"
}
```

`channels.json` is gitignored — the labels describe your home, and they are
yours, not the repo's. Restart the app to pick up changes.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `ECOWITT_GATEWAY` | *(required)* | Gateway IP / hostname |
| `POLL_INTERVAL` | `60` | Seconds between polls |
| `SOIL_DB` | `./soil.db` | SQLite file path |
| `CHANNELS_FILE` | `./channels.json` | Channel label overrides |
| `HOST` | `127.0.0.1` | Bind address — see Security |
| `PORT` | `8050` | |

## Security

**There is no authentication.** Anyone who can reach the port sees the
dashboard and can edit the moisture bands.

It binds `127.0.0.1` by default. Setting `HOST=0.0.0.0` to reach it from
other devices is reasonable on a trusted home LAN — but do not port-forward
it, expose it through a tunnel, or put it on a network you share with people
you don't trust. Put it behind a reverse proxy with authentication first.

## Data

Readings live in `soil.db` (gitignored):

```
soil_readings(ts INTEGER, channel INTEGER, name TEXT,
              humidity REAL, battery INTEGER, voltage REAL,
              PRIMARY KEY (ts, channel))

channel_settings(channel INTEGER PRIMARY KEY,
                 band_low REAL, band_high REAL)
```

Chart queries down-sample in SQL (~400 points per channel per window), so a
long window stays fast as the table grows into the millions of rows.

```sh
sqlite3 soil.db 'SELECT * FROM soil_readings ORDER BY ts DESC LIMIT 20;'
```

## Running it as a service

```ini
[Unit]
Description=Soil Moisture Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/garden_sensors
Environment=ECOWITT_GATEWAY=192.168.1.50
Environment=HOST=0.0.0.0
ExecStart=/home/pi/garden_sensors/.venv/bin/python /home/pi/garden_sensors/app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Licence

MIT — see [LICENSE](LICENSE).
