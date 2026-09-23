"""Clock timeline (plotly): one bar per clock from start to due, warning markers and a 'now' line, in IST."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import plotly.graph_objects as go

from ui.components.formatting import CLOCK_STATUS_LABELS, ist
from ui.components.theme import CLOCK_STATUS_HEX, series_hex
from wapsi.models import ClockStatus
from wapsi.timeutil import to_ist

STATUS_ORDER = [ClockStatus.OVERDUE, ClockStatus.WARNING, ClockStatus.OPEN, ClockStatus.DONE]


@dataclass(frozen=True)
class ClockRow:
    label: str
    start: datetime
    due: datetime
    status: ClockStatus
    warn_at: datetime | None = None
    detail: str = ""


def _naive_ist(ts: datetime) -> datetime:
    return to_ist(ts).replace(tzinfo=None)


def build_clock_timeline(rows: list[ClockRow], now: datetime, *, dark: bool = False) -> go.Figure:
    fig = go.Figure()
    order = sorted(rows, key=lambda r: (r.due, r.label))
    labels = [r.label for r in order]
    for status in STATUS_ORDER:
        mine = [r for r in order if r.status == status]
        if not mine:
            continue
        fig.add_trace(go.Bar(
            name=CLOCK_STATUS_LABELS[status],
            orientation="h",
            y=[r.label for r in mine],
            base=[_naive_ist(r.start) for r in mine],
            x=[(r.due - r.start).total_seconds() * 1000 for r in mine],
            marker={"color": CLOCK_STATUS_HEX[status], "line": {"width": 0}},
            customdata=[[ist(r.start), ist(r.due), r.detail] for r in mine],
            hovertemplate="<b>%{y}</b><br>Starts %{customdata[0]}<br>Due %{customdata[1]}"
                          "<br>%{customdata[2]}<extra>" + CLOCK_STATUS_LABELS[status] + "</extra>",
            width=0.5,
        ))
    warn = [r for r in order if r.warn_at is not None and r.status != ClockStatus.DONE]
    if warn:
        fig.add_trace(go.Scatter(
            name="Warning point", mode="markers", y=[r.label for r in warn],
            x=[_naive_ist(r.warn_at) for r in warn],
            marker={"symbol": "diamond", "size": 10, "color": series_hex("ink", dark),
                    "line": {"width": 2, "color": "#ffffff" if not dark else "#0e1117"}},
            hovertemplate="Warn at %{x|%d-%m-%Y}<extra></extra>",
        ))
    now_x = _naive_ist(now)
    fig.add_shape(type="line", x0=now_x, x1=now_x, y0=0, y1=1, yref="paper",
                  line={"width": 1.5, "dash": "dot", "color": series_hex("ink", dark)})
    fig.add_annotation(x=now_x, y=1, yref="paper", text="Now", showarrow=False, yanchor="bottom",
                       font={"size": 11})
    fig.update_layout(
        barmode="overlay",
        height=max(220, 44 * len(labels) + 90),
        margin={"l": 8, "r": 16, "t": 30, "b": 8},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.04, "x": 0, "title": None},
        xaxis={"type": "date", "tickformat": "%d %b", "showgrid": True, "fixedrange": False},
        yaxis={"categoryorder": "array", "categoryarray": list(reversed(labels)), "fixedrange": True,
               "automargin": True},
        hoverlabel={"align": "left"},
    )
    return fig
