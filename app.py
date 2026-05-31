"""Soil moisture dashboard for an Ecowitt GW1100 + WH51 setup.

Polls http://<gateway>/get_livedata_info every POLL_INTERVAL seconds,
stores every reading in a local SQLite database, and serves an
interactive Plotly time-series at http://127.0.0.1:8050.
"""

import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
from dash import Dash, Input, Output, dcc, html

GATEWAY = os.environ.get("ECOWITT_GATEWAY", "192.168.2.201")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
DB_PATH = Path(os.environ.get("SOIL_DB", Path(__file__).parent / "soil.db"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8050"))


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS soil_readings (
                ts        INTEGER NOT NULL,
                channel   INTEGER NOT NULL,
                name      TEXT,
                humidity  REAL,
                battery   INTEGER,
                voltage   REAL,
                PRIMARY KEY (ts, channel)
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON soil_readings(ts)")


def fetch_once() -> int:
    url = f"http://{GATEWAY}/get_livedata_info"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    ts = int(time.time())
    rows = []
    for ch in data.get("ch_soil", []):
        humidity_raw = ch.get("humidity", "")
        try:
            humidity = float(str(humidity_raw).rstrip("%"))
        except ValueError:
            continue
        rows.append(
            (
                ts,
                int(ch.get("channel", 0)),
                (ch.get("name") or None),
                humidity,
                int(ch.get("battery", 0)),
                float(ch.get("voltage", 0) or 0),
            )
        )
    if rows:
        with sqlite3.connect(DB_PATH) as con:
            con.executemany(
                "INSERT OR REPLACE INTO soil_readings"
                "(ts, channel, name, humidity, battery, voltage)"
                " VALUES (?,?,?,?,?,?)",
                rows,
            )
    return len(rows)


def poller() -> None:
    while True:
        try:
            n = fetch_once()
            print(
                f"[{datetime.now().isoformat(timespec='seconds')}] wrote {n} rows",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001 — log and keep polling
            print(f"[poller] error: {e}", flush=True)
        time.sleep(POLL_INTERVAL)


def load_df(window_hours: float) -> pd.DataFrame:
    cutoff = int(time.time() - window_hours * 3600)
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql_query(
            "SELECT ts, channel, name, humidity, battery, voltage"
            " FROM soil_readings WHERE ts >= ? ORDER BY ts ASC",
            con,
            params=(cutoff,),
        )
    if not df.empty:
        df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(
            datetime.now().astimezone().tzinfo
        )
    return df


WINDOW_OPTIONS = [
    {"label": "Last 1h", "value": 1},
    {"label": "Last 6h", "value": 6},
    {"label": "Last 24h", "value": 24},
    {"label": "Last 7d", "value": 24 * 7},
    {"label": "Last 30d", "value": 24 * 30},
    {"label": "All", "value": 24 * 365 * 10},
]


def moisture_color(pct: float) -> str:
    """Color band for a soil moisture reading (Bay Area garden tuned)."""
    if pct < 20:
        return "#d9534f"  # red — dry, water now
    if pct < 40:
        return "#f0ad4e"  # orange — getting low
    if pct < 70:
        return "#5cb85c"  # green — ideal
    if pct < 90:
        return "#5bc0de"  # light blue — well watered
    return "#3a7bd5"      # deep blue — saturated


def build_gauges(latest: pd.DataFrame) -> html.Div:
    """Row of vertical 'glass half full' bars, one per channel."""
    if latest.empty:
        return html.Div(
            "No channels yet.",
            style={"color": "#888", "padding": "12px 0"},
        )

    cards = []
    for _, r in latest.iterrows():
        pct = max(0.0, min(100.0, float(r["humidity"])))
        color = moisture_color(pct)
        label = (r["name"] or "").strip()
        title = f"Ch {int(r['channel'])}" + (f" · {label}" if label else "")

        bar = html.Div(
            style={
                "position": "relative",
                "width": "60px",
                "height": "200px",
                "border": "1px solid #ccc",
                "borderRadius": "8px",
                "background": (
                    "repeating-linear-gradient("
                    "to bottom, #fafafa 0 19px, #f0f0f0 19px 20px)"
                ),
                "overflow": "hidden",
                "margin": "0 auto",
            },
            children=[
                # The fill grows from the bottom up.
                html.Div(
                    style={
                        "position": "absolute",
                        "left": 0,
                        "right": 0,
                        "bottom": 0,
                        "height": f"{pct}%",
                        "background": color,
                        "transition": "height 0.6s ease, background 0.6s ease",
                    }
                ),
                # Centered percentage label on top of the fill.
                html.Div(
                    f"{pct:.0f}%",
                    style={
                        "position": "absolute",
                        "left": 0,
                        "right": 0,
                        "top": "50%",
                        "transform": "translateY(-50%)",
                        "textAlign": "center",
                        "fontWeight": 600,
                        "fontSize": "16px",
                        "color": "#222",
                        "textShadow": "0 0 4px rgba(255,255,255,0.85)",
                    },
                ),
            ],
        )

        cards.append(
            html.Div(
                style={
                    "display": "flex",
                    "flexDirection": "column",
                    "alignItems": "center",
                    "minWidth": "90px",
                    "padding": "8px",
                },
                children=[
                    html.Div(
                        title,
                        style={
                            "fontSize": "13px",
                            "color": "#444",
                            "marginBottom": "8px",
                            "whiteSpace": "nowrap",
                        },
                    ),
                    bar,
                    html.Div(
                        f"{r['voltage']:.2f} V",
                        style={
                            "fontSize": "11px",
                            "color": "#888",
                            "marginTop": "6px",
                        },
                    ),
                ],
            )
        )

    return html.Div(
        cards,
        style={
            "display": "flex",
            "flexWrap": "wrap",
            "justifyContent": "flex-start",
            "gap": "8px",
            "padding": "8px 0",
        },
    )


def make_app() -> Dash:
    app = Dash(__name__)
    app.title = "Soil Moisture"

    app.layout = html.Div(
        style={
            "fontFamily": "system-ui, -apple-system, sans-serif",
            "maxWidth": "1100px",
            "margin": "20px auto",
            "padding": "0 16px",
        },
        children=[
            html.H1("Soil Moisture Monitor"),
            html.Div(
                f"Gateway {GATEWAY} · polling every {POLL_INTERVAL}s",
                style={"color": "#666", "marginBottom": "12px"},
            ),
            html.Div(
                [
                    html.Label("Time window:", style={"marginRight": "8px"}),
                    dcc.Dropdown(
                        id="window",
                        options=WINDOW_OPTIONS,
                        value=24,
                        clearable=False,
                        style={"width": "200px", "display": "inline-block"},
                    ),
                    html.Span(
                        id="status",
                        style={"marginLeft": "16px", "color": "#666"},
                    ),
                ],
                style={
                    "display": "flex",
                    "alignItems": "center",
                    "marginBottom": "16px",
                },
            ),
            html.H2("Current levels", style={"marginTop": "8px"}),
            html.Div(id="gauges"),
            html.H2("History", style={"marginTop": "32px"}),
            dcc.Graph(id="humidity-chart"),
            html.H2("Current readings", style={"marginTop": "32px"}),
            html.Div(id="status-table"),
            dcc.Interval(id="refresh", interval=15_000, n_intervals=0),
        ],
    )

    @app.callback(
        Output("humidity-chart", "figure"),
        Output("status", "children"),
        Output("status-table", "children"),
        Output("gauges", "children"),
        Input("refresh", "n_intervals"),
        Input("window", "value"),
    )
    def update(_n, window_hours):
        df = load_df(window_hours)
        fig = go.Figure()
        if df.empty:
            fig.add_annotation(
                text="No data yet — waiting for the first poll.",
                showarrow=False,
                font={"size": 14, "color": "#888"},
            )
        else:
            for ch, sub in df.groupby("channel"):
                label = sub["name"].dropna().iloc[0] if sub["name"].notna().any() else None
                name = f"Ch {int(ch)}" + (f" — {label}" if label else "")
                fig.add_trace(
                    go.Scatter(
                        x=sub["time"],
                        y=sub["humidity"],
                        mode="lines+markers",
                        name=name,
                    )
                )
        fig.update_layout(
            xaxis_title="Time",
            yaxis_title="Soil moisture (%)",
            yaxis=dict(range=[0, 100]),
            hovermode="x unified",
            margin=dict(l=50, r=20, t=20, b=40),
            height=480,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        )

        if df.empty:
            return fig, "Waiting for first reading…", html.Div(), build_gauges(df)

        latest_ts = int(df["ts"].max())
        latest_local = datetime.fromtimestamp(latest_ts).strftime("%Y-%m-%d %H:%M:%S")
        status_text = f"Last reading: {latest_local}"

        latest = (
            df.sort_values("ts").groupby("channel").tail(1).sort_values("channel")
        )
        header = html.Tr(
            [
                html.Th(c, style={"textAlign": "left", "padding": "6px 12px"})
                for c in ("Channel", "Name", "Humidity", "Battery", "Voltage")
            ]
        )
        body_rows = []
        for _, r in latest.iterrows():
            body_rows.append(
                html.Tr(
                    [
                        html.Td(int(r["channel"]), style={"padding": "6px 12px"}),
                        html.Td(r["name"] or "—", style={"padding": "6px 12px"}),
                        html.Td(
                            f"{r['humidity']:.0f}%", style={"padding": "6px 12px"}
                        ),
                        html.Td(
                            int(r["battery"]), style={"padding": "6px 12px"}
                        ),
                        html.Td(
                            f"{r['voltage']:.2f} V",
                            style={"padding": "6px 12px"},
                        ),
                    ]
                )
            )
        table = html.Table(
            [header, *body_rows],
            style={
                "borderCollapse": "collapse",
                "width": "100%",
                "border": "1px solid #ddd",
            },
        )
        return fig, status_text, table, build_gauges(latest)

    return app


if __name__ == "__main__":
    init_db()
    # Prime the DB with one reading so the dashboard isn't empty on first load.
    try:
        fetch_once()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] initial fetch failed: {e}", flush=True)
    threading.Thread(target=poller, daemon=True).start()
    make_app().run(host=HOST, port=PORT, debug=False)
