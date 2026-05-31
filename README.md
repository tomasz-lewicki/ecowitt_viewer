# Soil Moisture Monitor

Local dashboard for an Ecowitt GW1100 Wi-Fi gateway + WH51 soil moisture sensors.

Polls `http://<gateway>/get_livedata_info` every minute, stores readings in
SQLite, and serves an interactive Plotly chart per channel at
<http://127.0.0.1:8050>.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```sh
.venv/bin/python app.py
```

Open <http://127.0.0.1:8050>. The dashboard auto-refreshes every 15s.

## Configuration (env vars)

| Variable           | Default           | Notes                              |
| ------------------ | ----------------- | ---------------------------------- |
| `ECOWITT_GATEWAY`  | `192.168.2.201`   | Gateway IP / hostname              |
| `POLL_INTERVAL`    | `60`              | Seconds between polls              |
| `SOIL_DB`          | `./soil.db`       | SQLite file path                   |

Example:

```sh
ECOWITT_GATEWAY=192.168.2.201 POLL_INTERVAL=30 .venv/bin/python app.py
```

## Data

All readings live in `soil.db` (SQLite). Schema:

```
soil_readings(ts INTEGER, channel INTEGER, name TEXT,
              humidity REAL, battery INTEGER, voltage REAL,
              PRIMARY KEY (ts, channel))
```

Inspect with `sqlite3 soil.db 'SELECT * FROM soil_readings ORDER BY ts DESC LIMIT 20;'`.
