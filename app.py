"""Soil moisture dashboard for an Ecowitt GW1100 + WH51 setup.

Polls http://<gateway>/get_livedata_info every POLL_INTERVAL seconds,
stores every reading in a local SQLite database, and serves an
interactive Plotly time-series at http://127.0.0.1:8050.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
import base64
from urllib.parse import parse_qs

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
from dash import ALL, Dash, Input, Output, State, dcc, html, no_update

# Deployment-specific, so there is no default: a baked-in address would be
# both wrong for everyone else and a needless detail about the author's LAN.
GATEWAY = os.environ.get("ECOWITT_GATEWAY", "").strip()
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
DB_PATH = Path(os.environ.get("SOIL_DB", Path(__file__).parent / "soil.db"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8050"))

CHANNELS_FILE = Path(
    os.environ.get("CHANNELS_FILE", Path(__file__).parent / "channels.json")
)


def load_channel_names() -> dict:
    """Friendly per-channel labels, read from channels.json if present.

    Kept out of the source, and out of git, on purpose: the labels describe
    someone's home - which rooms, which plants, which beds - so the repo
    ships channels.example.json and leaves the real file local. A missing
    file, or a channel with no entry, just falls back to "Channel N".
    """
    try:
        with CHANNELS_FILE.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[config] ignoring {CHANNELS_FILE}: {exc}", flush=True)
        return {}

    if not isinstance(raw, dict):
        print(f"[config] {CHANNELS_FILE} should be an object of channel -> name", flush=True)
        return {}

    names = {}
    for key, value in raw.items():
        try:
            names[int(key)] = str(value)
        except (TypeError, ValueError):
            print(f"[config] skipping non-numeric channel key {key!r}", flush=True)
    return names


# Override whatever the gateway returns (the WH51's on-device name field is
# often blank / hard to set).
CHANNEL_NAMES = load_channel_names()


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
        # load_latest() asks for MAX(ts) per channel; the primary key is
        # (ts, channel), which cannot serve that. Without this index the
        # lookup scans the whole table (~1.6s on the Pi at 1M rows) and it
        # runs on every refresh.
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_ts"
            " ON soil_readings(channel, ts)"
        )


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


# Roughly how many points per channel the history chart should draw. The DB
# holds ~1M rows; "All" at full resolution would ship tens of megabytes to the
# browser to draw sub-pixel detail.
CHART_POINTS = 400

BUCKET_STEPS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400)


def bucket_seconds(window_hours: float) -> int:
    """Smallest standard bucket that keeps a window under CHART_POINTS."""
    ideal = window_hours * 3600 / CHART_POINTS
    for step in BUCKET_STEPS:
        if ideal <= step:
            return step
    return BUCKET_STEPS[-1]


def _localize(df: pd.DataFrame) -> pd.DataFrame:
    df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(
        datetime.now().astimezone().tzinfo
    )
    # Apply friendly name overrides (CHANNEL_NAMES wins over gateway name).
    df["name"] = df["channel"].map(CHANNEL_NAMES).fillna(df.get("name"))
    return df


def load_series(window_hours: float) -> pd.DataFrame:
    """Down-sampled history for the chart: mean humidity per channel per bucket.

    Averaging happens in SQLite rather than pandas so a long window never
    materialises the raw rows.
    """
    cutoff = int(time.time() - window_hours * 3600)
    bucket = bucket_seconds(window_hours)
    with sqlite3.connect(DB_PATH) as con:
        if bucket <= POLL_INTERVAL:
            df = pd.read_sql_query(
                "SELECT ts, channel, humidity FROM soil_readings"
                " WHERE ts >= ? ORDER BY ts ASC",
                con,
                params=(cutoff,),
            )
        else:
            df = pd.read_sql_query(
                "SELECT (ts / ?) * ? AS ts, channel, AVG(humidity) AS humidity"
                " FROM soil_readings WHERE ts >= ?"
                " GROUP BY channel, ts / ? ORDER BY ts ASC",
                con,
                params=(bucket, bucket, cutoff, bucket),
            )
    df["name"] = None
    return _localize(df) if not df.empty else df


# The set of channels changes only when a sensor is added or removed, but
# SELECT DISTINCT over ~1M rows costs ~0.14s on the Pi - too much to pay on
# every refresh, cheap enough to re-check once a minute.
CHANNEL_CACHE_TTL = 60.0
_channels_cache = {"at": 0.0, "value": []}


def known_channels(con) -> list:
    now = time.time()
    if not _channels_cache["value"] or now - _channels_cache["at"] > CHANNEL_CACHE_TTL:
        rows = con.execute("SELECT DISTINCT channel FROM soil_readings").fetchall()
        _channels_cache["value"] = sorted(int(r[0]) for r in rows)
        _channels_cache["at"] = now
    return _channels_cache["value"]


LATEST_COLUMNS = ["ts", "channel", "name", "humidity", "battery", "voltage"]


def load_latest() -> pd.DataFrame:
    """The most recent reading per channel - what the gauges show.

    One indexed seek per channel rather than a MAX(ts) GROUP BY join: the
    join scans the table (0.36s on the Pi even with the index), the seeks
    total ~1ms, and this runs on every refresh in three callbacks.

    Kept separate from the chart query: the gauges want exact current
    values, not a bucket average.
    """
    rows = []
    with sqlite3.connect(DB_PATH) as con:
        for channel in known_channels(con):
            row = con.execute(
                "SELECT ts, channel, name, humidity, battery, voltage"
                " FROM soil_readings WHERE channel = ?"
                " ORDER BY ts DESC LIMIT 1",
                (channel,),
            ).fetchone()
            if row is not None:
                rows.append(row)

    df = pd.DataFrame(rows, columns=LATEST_COLUMNS)
    return _localize(df) if not df.empty else df


def sparkline_svg(xs, ys, low, high, width=340, height=96):
    """One channel's history as a self-contained SVG string.

    The wall display renders this instead of a Plotly figure. The screensaver
    spawns a growing number of web views (13 seen concurrently); at ~3MB of
    plotly.js each they contend and none of them ever finishes drawing, which
    is what "the graphs don't load" actually was. This is a few KB, needs no
    JavaScript at all, and cannot thrash.

    xs are 0..1 across the window, ys are percentages.
    """
    def px(x):
        return round(x * width, 1)

    def py(v):
        return round((1.0 - max(0.0, min(100.0, v)) / 100.0) * height, 1)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="100%" height="{height}" preserveAspectRatio="none">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fff"/>',
        # the plant's acceptable band
        f'<rect x="0" y="{py(high)}" width="{width}" height="{max(0.0, py(low) - py(high)):.1f}" '
        f'fill="{IDEAL_FILL}"/>',
    ]
    for level in (0, 50, 100):
        parts.append(
            f'<line x1="0" y1="{py(level)}" x2="{width}" y2="{py(level)}" '
            f'stroke="{CHART_GRID}" stroke-width="1"/>'
        )
    # "water now" threshold
    parts.append(
        f'<line x1="0" y1="{py(low - BAND_MARGIN)}" x2="{width}" y2="{py(low - BAND_MARGIN)}" '
        f'stroke="{DRY_LINE}" stroke-width="1" stroke-dasharray="3 3" opacity="0.6"/>'
    )
    if len(xs) >= 2:
        pts = " ".join(f"{px(x)},{py(v)}" for x, v in zip(xs, ys))
        parts.append(
            f'<polyline points="{pts}" fill="none" stroke="{CHART_ACCENT}" '
            f'stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def build_sparklines(series: pd.DataFrame, latest: pd.DataFrame, settings: dict):
    """The kiosk history view: one SVG panel per channel, no charting library."""
    if latest is None or latest.empty:
        return html.Div(
            "No data yet - waiting for the first poll.",
            style={"color": CHART_MUTED, "padding": "18px 0"},
        )

    channels = [int(c) for c in latest["channel"].tolist()]
    current = {int(r.channel): float(r.humidity) for r in latest.itertuples()}
    by_channel = (
        {int(c): g for c, g in series.groupby("channel")} if not series.empty else {}
    )

    # One shared time span so every panel lines up on the same x axis.
    if not series.empty:
        t_min = float(series["ts"].min())
        t_max = float(series["ts"].max())
    else:
        t_min = t_max = 0.0
    span = max(1.0, t_max - t_min)

    panels = []
    for ch in channels:
        low, high = band_for(settings, ch)
        g = by_channel.get(ch)
        if g is not None and not g.empty:
            xs = [(float(t) - t_min) / span for t in g["ts"]]
            ys = [float(v) for v in g["humidity"]]
        else:
            xs, ys = [], []

        pct = current.get(ch)
        head = CHANNEL_NAMES.get(ch, f"Channel {ch}")
        if pct is not None:
            head += f" · {pct:.0f}%"
        head += f"  (ok {low:.0f}\u2013{high:.0f}%)"

        panels.append(
            html.Div(
                [
                    html.Div(
                        head,
                        style={
                            "fontSize": "12px",
                            "lineHeight": "15px",
                            # flex items shrink by default; without this the
                            # title gets squeezed to half-height when the row
                            # is tight.
                            "flex": "0 0 auto",
                            "color": CHART_INK,
                            "marginBottom": "3px",
                            "whiteSpace": "nowrap",
                            "overflow": "hidden",
                            "textOverflow": "ellipsis",
                        },
                    ),
                    # The SVG is drawn with preserveAspectRatio="none", so it
                    # stretches to whatever box the grid gives it. Without this
                    # the img keeps the viewBox ratio, grows with column width,
                    # and the grid overflows the screen.
                    html.Img(
                        src=svg_data_uri(sparkline_svg(xs, ys, low, high)),
                        style={
                            "width": "100%",
                            "flex": "1 1 auto",
                            "minHeight": 0,
                            "objectFit": "fill",
                            "display": "block",
                            "border": f"1px solid {CHART_GRID}",
                            "borderRadius": "4px",
                        },
                    ),
                ],
                style={
                    "minWidth": 0,
                    "minHeight": 0,
                    "display": "flex",
                    "flexDirection": "column",
                },
            )
        )

    return html.Div(
        panels,
        style={
            "display": "grid",
            "gridTemplateColumns": f"repeat({CHART_COLS}, minmax(0, 1fr))",
            # Equal-height rows that divide whatever vertical space the card
            # has, so the wall display never scrolls.
            "gridTemplateRows": f"repeat({chart_rows(len(channels))}, minmax(0, 1fr))",
            "gap": "8px 18px",
            "height": "100%",
        },
    )


# --- chart chrome -------------------------------------------------------
# One accent hue, not eleven: each panel holds a single series and the panel
# title carries identity, so colour never has to tell channels apart. Eleven
# categorical hues would be indistinguishable under colour-vision deficiency
# whatever the palette.
CHART_ACCENT = "#2a78d6"
CHART_GRID = "#e1e0d9"
CHART_MUTED = "#898781"
CHART_INK = "#52514e"
IDEAL_FILL = "rgba(12,163,12,0.10)"
DRY_LINE = "#d03b3b"
CHART_COLS = 3
CHART_HSPACE = 0.045


def build_history_figure(series: pd.DataFrame, latest: pd.DataFrame, settings: dict):
    """One small-multiple panel per channel, shared axes.

    Eleven series on shared axes is a hairball no palette can fix; faceting
    turns "which plant needs water" into a glance instead of a legend hunt.
    """
    if latest is not None and not latest.empty:
        channels = [int(c) for c in latest["channel"].tolist()]
        current = {int(r.channel): float(r.humidity) for r in latest.itertuples()}
    elif not series.empty:
        channels = sorted(int(c) for c in series["channel"].unique())
        current = {}
    else:
        channels, current = [], {}

    if not channels:
        fig = go.Figure()
        fig.add_annotation(
            text="No data yet — waiting for the first poll.",
            showarrow=False,
            font={"size": 14, "color": CHART_MUTED},
        )
        fig.update_layout(
            plot_bgcolor="white", paper_bgcolor="white",
            xaxis={"visible": False}, yaxis={"visible": False},
        )
        return fig

    rows = -(-len(channels) // CHART_COLS)  # ceil
    titles = []
    for ch in channels:
        name = CHANNEL_NAMES.get(ch, f"Channel {ch}")
        pct = current.get(ch)
        lo, hi = band_for(settings, ch)
        band = f"{lo:.0f}\u2013{hi:.0f}%"
        titles.append(
            f"{name} · {pct:.0f}%  (ok {band})" if pct is not None
            else f"{name}  (ok {band})"
        )

    fig = make_subplots(
        rows=rows,
        cols=CHART_COLS,
        shared_xaxes=True,
        shared_yaxes=True,
        subplot_titles=titles,
        vertical_spacing=0.10 if rows <= 4 else 0.07,
        horizontal_spacing=CHART_HSPACE,
    )

    by_channel = {int(c): g for c, g in series.groupby("channel")} if not series.empty else {}
    bottom_row_of = {}  # column -> its last populated row

    for i, ch in enumerate(channels):
        row, col = i // CHART_COLS + 1, i % CHART_COLS + 1
        bottom_row_of[col] = row

        # Context first, underneath the data: the ideal band and the
        # "water now" threshold, so a panel is readable without the axis.
        # Each panel shades that plant's own acceptable band, so a reading is
        # judged against the right target rather than one global rule.
        low, high = band_for(settings, ch)
        # exclude_empty_subplots=False is required: these are added before the
        # trace, and by default Plotly silently drops row/col shapes aimed at a
        # subplot that has no traces yet. It also keeps the reference band on a
        # channel that reported nothing in this window.
        fig.add_hrect(
            y0=low, y1=high,
            fillcolor=IDEAL_FILL, line_width=0, layer="below",
            row=row, col=col, exclude_empty_subplots=False,
        )
        fig.add_hline(
            y=low - BAND_MARGIN, line_dash="dot", line_width=1,
            line_color=DRY_LINE, opacity=0.6,
            row=row, col=col, exclude_empty_subplots=False,
        )

        g = by_channel.get(ch)
        if g is not None and not g.empty:
            fig.add_trace(
                go.Scatter(
                    x=g["time"], y=g["humidity"],
                    mode="lines",
                    line={"color": CHART_ACCENT, "width": 1.8},
                    name=CHANNEL_NAMES.get(ch, f"Channel {ch}"),
                    hovertemplate="%{y:.0f}%  ·  %{x|%b %-d %H:%M}<extra></extra>",
                ),
                row=row, col=col,
            )

    fig.update_yaxes(
        range=[0, 100], tickvals=[0, 50, 100], ticksuffix="%",
        gridcolor=CHART_GRID, zeroline=False,
        tickfont={"size": 10, "color": CHART_MUTED},
    )
    fig.update_xaxes(
        gridcolor=CHART_GRID, zeroline=False,
        tickfont={"size": 10, "color": CHART_MUTED},
    )
    # shared_xaxes only labels the very bottom row. When the last row is
    # short (11 panels in a 3-wide grid), the columns that end a row early
    # would otherwise carry no time axis at all - label each column's own
    # last panel instead.
    for col, row in bottom_row_of.items():
        fig.update_xaxes(showticklabels=True, row=row, col=col)
    # Left-align each subplot title to its own panel. Plotly centres them on
    # the panel's x-domain, so shifting by half a panel width lands on the
    # panel's left edge - the width must account for the inter-panel spacing,
    # or the title slides out over the y tick labels.
    panel_w = (1 - CHART_HSPACE * (CHART_COLS - 1)) / CHART_COLS
    for note in fig.layout.annotations:  # the subplot titles
        note.font.size = 11.5
        note.font.color = CHART_INK
        note.xanchor = "left"
        note.x = note.x - panel_w / 2

    fig.update_layout(
        showlegend=False,
        margin=dict(l=36, r=12, t=26, b=28),
        plot_bgcolor="white",
        paper_bgcolor="white",
        hovermode="x",
    )
    return fig


DEFAULT_WINDOW_HOURS = 168  # 7 days

WINDOW_OPTIONS = [
    {"label": "Last 1h", "value": 1},
    {"label": "Last 6h", "value": 6},
    {"label": "Last 24h", "value": 24},
    {"label": "Last 7d", "value": 24 * 7},
    {"label": "Last 30d", "value": 24 * 30},
    {"label": "All", "value": 24 * 365 * 10},
]


# Each plant gets its own acceptable moisture band; everything else is
# derived from it, so "ideal" means ideal *for that plant*.
DEFAULT_BAND = (30.0, 40.0)

# How far outside the band counts as seriously dry / seriously wet.
BAND_MARGIN = 10.0

# Five states, driest first: the band splits the scale, the margin splits
# the outside of it.
STATE_COLORS = (
    "#d9534f",  # red        - well below the band, water now
    "#f0ad4e",  # orange     - just below the band
    "#5cb85c",  # green      - inside the band
    "#5bc0de",  # light blue - just above the band
    "#3a7bd5",  # deep blue  - well above, saturated
)

# A sensor sitting on a boundary (the classic 39 <-> 40 dither) would repaint
# its gauge on every poll. Requiring the reading to clear the boundary by this
# much before the state changes makes the colour stick.
BAND_HYSTERESIS = 2.0

# channel -> state index currently being displayed.
_band_state = {}


def clean_band(low, high):
    """Coerce a user-entered band into something sane: 0-100 and low < high."""
    try:
        low = float(low)
        high = float(high)
    except (TypeError, ValueError):
        return DEFAULT_BAND
    low = min(99.0, max(0.0, low))
    high = min(100.0, max(0.0, high))
    if high <= low:
        high = min(100.0, low + 1.0)
    return (low, high)


def band_thresholds(low, high):
    """The four boundaries between the five states."""
    return (low - BAND_MARGIN, low, high, high + BAND_MARGIN)


def _raw_state(pct: float, thresholds) -> int:
    for i, edge in enumerate(thresholds):
        if pct < edge:
            return i
    return len(thresholds)


def moisture_state(pct: float, low: float, high: float, channel=None) -> int:
    """State index for a reading, with hysteresis when a channel is given.

    Without a channel this is plain thresholding. With one, the previously
    shown state is held until the reading clears the boundary by
    BAND_HYSTERESIS, so a value hovering on an edge stops flip-flopping.
    """
    edges = band_thresholds(low, high)
    raw = _raw_state(pct, edges)
    if channel is None:
        return raw

    prev = _band_state.get(channel)
    if prev is not None and raw != prev:
        if raw > prev:
            # Moving up: must clear the top of the state we are leaving.
            if pct < edges[prev] + BAND_HYSTERESIS:
                return prev
        else:
            # Moving down: must drop clear of the state we are entering.
            if pct > edges[raw] - BAND_HYSTERESIS:
                return prev
    _band_state[channel] = raw
    return raw


def moisture_color(pct: float, low: float, high: float, channel=None) -> str:
    """Colour for a reading against that plant's band. See moisture_state."""
    return STATE_COLORS[moisture_state(pct, low, high, channel)]


SETTINGS_DDL = """
    CREATE TABLE IF NOT EXISTS channel_settings (
        channel   INTEGER PRIMARY KEY,
        band_low  REAL NOT NULL,
        band_high REAL NOT NULL
    )
"""


def load_settings() -> dict:
    """channel -> (low, high). Channels with nothing saved are absent."""
    with sqlite3.connect(DB_PATH) as con:
        con.execute(SETTINGS_DDL)
        rows = con.execute(
            "SELECT channel, band_low, band_high FROM channel_settings"
        ).fetchall()
    return {int(c): (float(lo), float(hi)) for c, lo, hi in rows}


def band_for(settings: dict, channel) -> tuple:
    return settings.get(int(channel), DEFAULT_BAND)


def save_band(channel: int, low, high) -> tuple:
    low, high = clean_band(low, high)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(SETTINGS_DDL)
        con.execute(
            "INSERT INTO channel_settings (channel, band_low, band_high)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(channel) DO UPDATE SET"
            "   band_low = excluded.band_low, band_high = excluded.band_high",
            (int(channel), low, high),
        )
    # The held state was decided against the old band; drop it so the next
    # reading re-evaluates instead of sticking to a now-wrong colour.
    _band_state.pop(int(channel), None)
    return (low, high)


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


def gauge_body(row, settings: dict):
    """The live part of a channel card: fill gauge, reading, battery.

    Kept separate from the card shell so the 15s refresh only replaces this -
    the band inputs beside it keep their DOM node, and typing in them is not
    interrupted by a poll landing mid-edit.
    """
    if row is None:
        return html.Div(
            "no reading",
            style={"color": "#aaa", "fontSize": "12px", "padding": "60px 0"},
        )

    channel = int(row.channel)
    pct = max(0.0, min(100.0, float(row.humidity)))
    low, high = band_for(settings, channel)
    color = moisture_color(pct, low, high, channel)

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
            # The acceptable band, drawn on the gauge itself: the fill either
            # reaches into this stripe or it does not.
            html.Div(
                style={
                    "position": "absolute",
                    "left": 0,
                    "right": 0,
                    "bottom": f"{low}%",
                    "height": f"{max(0.0, high - low)}%",
                    "background": "rgba(12,163,12,0.13)",
                    "borderTop": "1px dashed rgba(12,163,12,0.55)",
                    "borderBottom": "1px dashed rgba(12,163,12,0.55)",
                },
            ),
            html.Div(
                style={
                    "position": "absolute",
                    "left": 0,
                    "right": 0,
                    "bottom": 0,
                    "height": f"{pct}%",
                    "background": color,
                    "opacity": 0.85,
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

    return html.Div(
        [
            gauge,
            html.Div(
                [
                    battery_indicator(int(row.battery)),
                    html.Span(
                        f"{float(row.voltage):.2f} V",
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
        ]
    )


def band_input(kind: str, channel: int, value: float):
    return dcc.Input(
        id={"type": kind, "channel": channel},
        type="number",
        min=0,
        max=100,
        step=1,
        value=value,
        # debounce: commit on blur/Enter, not on every keystroke, so a
        # half-typed "4" is never saved as a band edge.
        debounce=True,
        style={
            "width": "44px",
            "fontSize": "11px",
            "padding": "2px 4px",
            "textAlign": "center",
            "border": "1px solid #dcdcdc",
            "borderRadius": "5px",
        },
    )


def build_gauge_shell(latest: pd.DataFrame, settings: dict, kiosk: bool = False) -> html.Div:
    """Grid of channel cards. Structure only - the readings arrive separately."""
    if latest.empty:
        return html.Div(
            "No channels yet.",
            style={"color": "#888", "padding": "12px 0"},
        )

    cards = []
    for r in latest.itertuples():
        channel = int(r.channel)
        raw_name = r.name
        name = (
            str(raw_name).strip()
            if pd.notna(raw_name) and str(raw_name).strip()
            else f"Channel {channel}"
        )
        low, high = band_for(settings, channel)

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
                    html.Div(id={"type": "gauge-body", "channel": channel}),
                    html.Div(
                        [
                            html.Span(
                                "ok",
                                style={
                                    "fontSize": "10px",
                                    "letterSpacing": "0.06em",
                                    "textTransform": "uppercase",
                                    "color": "#999",
                                    "marginRight": "6px",
                                    "whiteSpace": "nowrap",
                                },
                            ),
                            band_input("band-lo", channel, low),
                            html.Span(
                                "\u2013",
                                style={"color": "#999", "margin": "0 3px"},
                            ),
                            band_input("band-hi", channel, high),
                            html.Span(
                                "%",
                                style={
                                    "fontSize": "11px",
                                    "color": "#999",
                                    "marginLeft": "3px",
                                },
                            ),
                        ],
                        title="Acceptable moisture band for this plant",
                        # Kiosk is a passive wall display - no editable
                        # controls, and the vertical space goes to the chart.
                        style={
                            "display": "none" if kiosk else "flex",
                            "alignItems": "center",
                            "justifyContent": "center",
                            "marginTop": "10px",
                            "paddingTop": "9px",
                            "borderTop": "1px solid #f0f0f0",
                            "width": "100%",
                            "whiteSpace": "nowrap",
                        },
                    ),
                ],
            )
        )

    return html.Div(
        cards,
        style={
            "display": "grid",
            "gridTemplateColumns": "repeat(auto-fit, minmax(180px, 1fr))",
            "gap": "14px",
            "padding": "4px 0",
        },
    )


KIOSK_FALSEY = {"", "0", "false", "no", "off"}


def is_kiosk(search: str) -> bool:
    """True when the dashboard URL carries ?kiosk=1.

    Kiosk is the wall-display / screensaver view: full-bleed instead of a
    1240px column, and the chart grows to fill the viewport instead of
    being pinned to 420px.

    The flag has to come from dcc.Location (the browser's URL) rather than
    flask.request: dash_renderer fetches /_dash-layout with no query
    string, so a request-context lookup never sees it.
    """
    values = parse_qs((search or "").lstrip("?")).get("kiosk")
    return bool(values) and values[-1].lower() not in KIOSK_FALSEY


def root_style(kiosk: bool) -> dict:
    style = {
        "fontFamily": "system-ui, -apple-system, sans-serif",
        "background": "#f7f7f8",
        "boxSizing": "border-box",
    }
    if kiosk:
        # Flex column so the chart card can absorb the leftover height.
        style.update(
            maxWidth="none",
            margin=0,
            padding="16px 24px",
            height="100vh",
            display="flex",
            flexDirection="column",
        )
    else:
        style.update(
            maxWidth="1240px",
            margin="20px auto",
            padding="0 20px",
            minHeight="100vh",
        )
    return style


def controls_style(kiosk: bool) -> dict:
    # Hidden rather than removed - "window" is a callback Input, so taking
    # it out of the tree would break the figure callback.
    return {
        "display": "none" if kiosk else "flex",
        "alignItems": "center",
        "margin": "18px 0 20px",
    }


def card_style(kiosk: bool) -> dict:
    style = {
        "background": "#fff",
        "border": "1px solid #e5e5e5",
        "borderRadius": "12px",
        "padding": "12px 16px 4px",
        "marginTop": "20px",
        "boxShadow": "0 1px 3px rgba(0,0,0,0.04)",
    }
    if kiosk:
        style.update(flex="1 1 auto", minHeight=0, display="flex", flexDirection="column")
    return style


PANEL_HEIGHT = 150  # px per small-multiple row, normal (non-kiosk) view


def graph_style(kiosk: bool, rows: int = 3) -> dict:
    """Size of the chart container.

    Plotly runs responsive here, which means it takes BOTH dimensions from
    this element - so the height has to be set here rather than on the
    figure, and it has to grow with the number of panel rows.
    """
    if kiosk:
        return {"flex": "1 1 auto", "minHeight": 0}
    return {"height": f"{rows * PANEL_HEIGHT + 40}px"}


def chart_rows(n_channels: int) -> int:
    return max(1, -(-n_channels // CHART_COLS))


def make_app() -> Dash:
    app = Dash(__name__)
    app.title = "Soil Moisture"

    app.layout = html.Div(
        id="root",
        style=root_style(False),
        children=[
            dcc.Location(id="url", refresh=False),
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
                        # A week reads better than a day here: watering shows
                        # up as distinct events rather than one flat line.
                        value=DEFAULT_WINDOW_HOURS,
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
                id="controls",
                style=controls_style(False),
            ),
            html.Div(id="gauges"),
            html.Div(
                id="chart-card",
                style=card_style(False),
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
                    # Filled by a callback with EITHER a Plotly graph (normal
                    # browsing) or plain SVG panels (kiosk). It starts empty on
                    # purpose: dcc.Graph pulls ~3MB of plotly.js the moment it
                    # mounts, and the wall display must never pay that.
                    html.Div(id="chart-area", style={"flex": "1 1 auto", "minHeight": 0}),
                ],
            ),
            dcc.Interval(id="refresh", interval=15_000, n_intervals=0),
            # Which channels the card grid was built for, so the shell is only
            # rebuilt when a sensor appears or disappears.
            dcc.Store(id="channels-store"),
            # Bumped whenever a band is saved; makes the chart and the gauges
            # re-read the settings.
            dcc.Store(id="settings-store", data=0),
        ],
    )

    @app.callback(
        Output("root", "style"),
        Output("controls", "style"),
        Output("chart-card", "style"),
        Input("url", "search"),
    )
    def apply_kiosk(search):
        kiosk = is_kiosk(search)
        return root_style(kiosk), controls_style(kiosk), card_style(kiosk)

    @app.callback(
        Output("chart-area", "children"),
        Input("url", "search"),
    )
    def choose_renderer(search):
        if is_kiosk(search):
            # Must carry a definite height: the SVG grid inside sizes with
            # height:100%, and a percentage against an auto-height parent
            # falls back to content size and overflows the screen.
            return html.Div(
                id={"type": "spark", "i": 0},
                style={"height": "100%", "minHeight": 0},
            )
        return dcc.Graph(
            id={"type": "plot", "i": 0},
            config={"displaylogo": False},
            # Without responsive, Plotly falls back to its default 700px
            # width instead of filling the card.
            responsive=True,
            style=graph_style(False),
        )

    @app.callback(
        Output("gauges", "children"),
        Output("channels-store", "data"),
        Input("refresh", "n_intervals"),
        State("channels-store", "data"),
        State("url", "search"),
    )
    def rebuild_gauge_shell(_n, known, search):
        """Rebuild the card grid only when the set of channels changes.

        Rebuilding it on every poll would replace the band inputs' DOM nodes
        underneath the user mid-edit.
        """
        latest = load_latest()
        channels = [int(c) for c in latest["channel"]] if not latest.empty else []
        if known == channels:
            return no_update, no_update
        return build_gauge_shell(latest, load_settings(), is_kiosk(search)), channels

    @app.callback(
        Output({"type": "gauge-body", "channel": ALL}, "children"),
        Input("refresh", "n_intervals"),
        Input("channels-store", "data"),
        Input("settings-store", "data"),
        State({"type": "gauge-body", "channel": ALL}, "id"),
    )
    def fill_gauges(_n, _channels, _settings_version, ids):
        latest = load_latest()
        settings = load_settings()
        by_channel = {int(r.channel): r for r in latest.itertuples()}
        return [gauge_body(by_channel.get(i["channel"]), settings) for i in ids]

    @app.callback(
        Output("settings-store", "data"),
        Input({"type": "band-lo", "channel": ALL}, "value"),
        Input({"type": "band-hi", "channel": ALL}, "value"),
        State({"type": "band-lo", "channel": ALL}, "id"),
        State("settings-store", "data"),
        prevent_initial_call=True,
    )
    def save_bands(lows, highs, ids, version):
        saved = load_settings()
        changed = False
        for low, high, ident in zip(lows, highs, ids):
            if low is None or high is None:
                continue
            channel = ident["channel"]
            if band_for(saved, channel) != clean_band(low, high):
                save_band(channel, low, high)
                changed = True
        if not changed:
            return no_update
        return (version or 0) + 1

    @app.callback(
        Output({"type": "spark", "i": ALL}, "children"),
        Output({"type": "plot", "i": ALL}, "figure"),
        Output({"type": "plot", "i": ALL}, "style"),
        Output("status", "children"),
        Input("refresh", "n_intervals"),
        Input("window", "value"),
        Input("url", "search"),
        Input("settings-store", "data"),
        State({"type": "spark", "i": ALL}, "id"),
        State({"type": "plot", "i": ALL}, "id"),
    )
    def update(_n, window_hours, search, _settings_version, spark_ids, plot_ids):
        kiosk = is_kiosk(search)
        latest = load_latest()
        series = load_series(window_hours)
        settings = load_settings()

        # Only build what this page actually renders - the wall display must
        # not pay to construct a Plotly figure it will never show.
        sparks = (
            [build_sparklines(series, latest, settings)] * len(spark_ids)
            if spark_ids else []
        )
        if plot_ids:
            figs = [build_history_figure(series, latest, settings)] * len(plot_ids)
            styles = [graph_style(kiosk, chart_rows(len(latest)))] * len(plot_ids)
        else:
            figs, styles = [], []

        if latest.empty:
            return sparks, figs, styles, "Waiting for first reading\u2026"

        latest_ts = int(latest["ts"].max())
        latest_local = datetime.fromtimestamp(latest_ts).strftime("%Y-%m-%d %H:%M:%S")
        return sparks, figs, styles, f"Last reading: {latest_local}"

    return app


if __name__ == "__main__":
    if not GATEWAY:
        raise SystemExit(
            "Set ECOWITT_GATEWAY to your gateway's IP or hostname, e.g.\n"
            "    ECOWITT_GATEWAY=192.168.1.50 python3 app.py"
        )
    init_db()
    # Prime the DB with one reading so the dashboard isn't empty on first load.
    try:
        fetch_once()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] initial fetch failed: {e}", flush=True)
    threading.Thread(target=poller, daemon=True).start()
    make_app().run(host=HOST, port=PORT, debug=False)
