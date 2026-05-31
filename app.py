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

# Friendly per-channel labels. Override whatever the gateway returns
# (the WH51's on-device name field is often blank / hard to set).
CHANNEL_NAMES = {
    1: "Tomatoes under Redwood Tree 🍅",
    2: "Sunflowers in a Barrel 🌻",
    3: "Tomatoes 2 🍅",
    4: "Sunflowers and Bougainvillea 🌺",
    5: "Herb box 🌿",
}


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
        # Apply friendly name overrides (CHANNEL_NAMES wins over gateway name).
        df["name"] = df["channel"].map(CHANNEL_NAMES).fillna(df["name"])
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


def battery_indicator(level: int) -> html.Div:
    """WH51 reports battery 0–5 (5 = full). Render as a small battery icon."""
    level = max(0, min(5, int(level)))
    pct = level / 5 * 100
    color = "#5cb85c" if level >= 3 else ("#f0ad4e" if level >= 2 else "#d9534f")
    return html.Div(
        style={
            "width": "26px",
            "height": "11px",
            "border": f"1.5px solid {color}",
            "borderRadius": "2px",
            "position": "relative",
            "display": "inline-block",
            "verticalAlign": "middle",
        },
        children=[
            # Battery "nub" on the right end.
            html.Div(
                style={
                    "position": "absolute",
                    "right": "-3px",
                    "top": "3px",
                    "width": "2px",
                    "height": "5px",
                    "background": color,
                    "borderRadius": "0 1px 1px 0",
                }
            ),
            # Fill.
            html.Div(
                style={
                    "position": "absolute",
                    "left": "1px",
                    "top": "1px",
                    "bottom": "1px",
                    "width": f"calc({pct}% - 2px)",
                    "background": color,
                    "borderRadius": "1px",
                    "transition": "width 0.6s ease, background 0.6s ease",
                }
            ),
        ],
    )


def build_gauges(latest: pd.DataFrame) -> html.Div:
    """Responsive grid of channel cards with a vertical 'fill' gauge."""
    if latest.empty:
        return html.Div(
            "No channels yet.",
            style={"color": "#888", "padding": "12px 0"},
        )

    cards = []
    for _, r in latest.iterrows():
        pct = max(0.0, min(100.0, float(r["humidity"])))
        color = moisture_color(pct)
        name = (r["name"] or f"Channel {int(r['channel'])}").strip()
        battery = int(r["battery"])
        voltage = float(r["voltage"])

        gauge = html.Div(
            style={
                "position": "relative",
                "width": "72px",
                "height": "160px",
                "border": "1px solid #d4d4d4",
                "borderRadius": "10px",
                "background": (
                    "repeating-linear-gradient("
                    "to bottom, #fafafa 0 31px, #f0f0f0 31px 32px)"
                ),
                "overflow": "hidden",
                "margin": "4px auto 12px",
            },
            children=[
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
                html.Div(
                    f"{pct:.0f}%",
                    style={
                        "position": "absolute",
                        "left": 0,
                        "right": 0,
                        "top": "50%",
                        "transform": "translateY(-50%)",
                        "textAlign": "center",
                        "fontWeight": 700,
                        "fontSize": "18px",
                        "color": "#222",
                        "textShadow": "0 0 6px rgba(255,255,255,0.9)",
                    },
                ),
            ],
        )

        cards.append(
            html.Div(
                style={
                    "background": "#fff",
                    "border": "1px solid #e5e5e5",
                    "borderRadius": "12px",
                    "padding": "14px 12px 12px",
                    "display": "flex",
                    "flexDirection": "column",
                    "alignItems": "center",
                    "boxShadow": "0 1px 3px rgba(0,0,0,0.04)",
                },
                children=[
                    html.Div(
                        name,
                        title=name,
                        style={
                            "fontSize": "13px",
                            "fontWeight": 600,
                            "color": "#333",
                            "textAlign": "center",
                            "lineHeight": "1.25",
                            "minHeight": "34px",
                            "display": "flex",
                            "alignItems": "center",
                            "justifyContent": "center",
                        },
                    ),
                    gauge,
                    html.Div(
                        [
                            battery_indicator(battery),
                            html.Span(
                                f"{voltage:.2f} V",
                                style={
                                    "fontSize": "11px",
                                    "color": "#888",
                                    "marginLeft": "8px",
                                },
                            ),
                        ],
                        style={
                            "display": "flex",
                            "alignItems": "center",
                            "justifyContent": "center",
                        },
                    ),
                ],
            )
        )

    return html.Div(
        cards,
        style={
            "display": "grid",
            "gridTemplateColumns": "repeat(auto-fit, minmax(160px, 1fr))",
            "gap": "14px",
            "padding": "4px 0",
        },
    )


def make_app() -> Dash:
    app = Dash(__name__)
    app.title = "Soil Moisture"

    app.layout = html.Div(
        style={
            "fontFamily": "system-ui, -apple-system, sans-serif",
            "maxWidth": "1240px",
            "margin": "20px auto",
            "padding": "0 20px",
            "background": "#f7f7f8",
            "minHeight": "100vh",
        },
        children=[
            html.Div(
                style={
                    "display": "flex",
                    "alignItems": "baseline",
                    "justifyContent": "space-between",
                    "flexWrap": "wrap",
                    "gap": "12px",
                    "marginBottom": "4px",
                },
                children=[
                    html.H1(
                        "Soil Moisture Monitor",
                        style={"margin": 0, "fontSize": "26px"},
                    ),
                    html.Div(
                        f"Gateway {GATEWAY} · polling every {POLL_INTERVAL}s",
                        style={"color": "#777", "fontSize": "13px"},
                    ),
                ],
            ),
            html.Div(
                [
                    html.Label(
                        "Time window:",
                        style={"marginRight": "8px", "fontSize": "13px"},
                    ),
                    dcc.Dropdown(
                        id="window",
                        options=WINDOW_OPTIONS,
                        value=24,
                        clearable=False,
                        style={"width": "180px"},
                    ),
                    html.Span(
                        id="status",
                        style={
                            "marginLeft": "16px",
                            "color": "#666",
                            "fontSize": "13px",
                        },
                    ),
                ],
                style={
                    "display": "flex",
                    "alignItems": "center",
                    "margin": "18px 0 20px",
                },
            ),
            html.Div(id="gauges"),
            html.Div(
                style={
                    "background": "#fff",
                    "border": "1px solid #e5e5e5",
                    "borderRadius": "12px",
                    "padding": "12px 16px 4px",
                    "marginTop": "20px",
                    "boxShadow": "0 1px 3px rgba(0,0,0,0.04)",
                },
                children=[
                    html.H2(
                        "History",
                        style={
                            "fontSize": "16px",
                            "fontWeight": 600,
                            "margin": "4px 0 8px",
                            "color": "#333",
                        },
                    ),
                    dcc.Graph(id="humidity-chart", config={"displaylogo": False}),
                ],
            ),
            dcc.Interval(id="refresh", interval=15_000, n_intervals=0),
        ],
    )

    @app.callback(
        Output("humidity-chart", "figure"),
        Output("status", "children"),
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
                name = label or f"Ch {int(ch)}"
                fig.add_trace(
                    go.Scatter(
                        x=sub["time"],
                        y=sub["humidity"],
                        mode="lines",
                        line=dict(width=2),
                        name=name,
                        hovertemplate="%{y:.0f}%<extra>%{fullData.name}</extra>",
                    )
                )
        fig.update_layout(
            xaxis_title=None,
            yaxis_title="Soil moisture (%)",
            yaxis=dict(range=[0, 100], gridcolor="#eee", zeroline=False),
            xaxis=dict(gridcolor="#eee"),
            hovermode="x unified",
            margin=dict(l=50, r=20, t=10, b=40),
            height=420,
            plot_bgcolor="white",
            paper_bgcolor="white",
            legend=dict(
                orientation="v",
                yanchor="top",
                y=1,
                xanchor="left",
                x=1.02,
                font=dict(size=12),
                bgcolor="rgba(0,0,0,0)",
            ),
        )

        if df.empty:
            return fig, "Waiting for first reading…", build_gauges(df)

        latest_ts = int(df["ts"].max())
        latest_local = datetime.fromtimestamp(latest_ts).strftime("%Y-%m-%d %H:%M:%S")
        status_text = f"Last reading: {latest_local}"

        latest = (
            df.sort_values("ts").groupby("channel").tail(1).sort_values("channel")
        )
        return fig, status_text, build_gauges(latest)

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
