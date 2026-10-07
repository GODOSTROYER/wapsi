"""Where the stolen money is now: a waterfall from 'Stolen' down to 'Unlocated' (plotly).

Built with floating bars (``go.Bar`` + ``base``) rather than ``go.Waterfall`` so each bucket keeps its own
meaning-colour, matching the trail graph's disposition colours.
"""

from __future__ import annotations

from dataclasses import dataclass

import plotly.graph_objects as go

from ui.components.formatting import inr, inr_short, pct
from ui.components.theme import series_hex
from wapsi.models import TraceTotals

STOLEN_HEX = {False: "#4a4f5a", True: "#c3c7d1"}


@dataclass(frozen=True)
class Bucket:
    key: str
    label: str
    tone: str
    note: str


BUCKETS: list[Bucket] = [
    Bucket("holdable", "Holdable now", "blue", "Victim money still in accounts we can hold"),
    Bucket("cashed_out", "Cashed out", "red", "Withdrawn at ATMs or sent to crypto exits"),
    Bucket("outbound", "Other banks", "orange", "Moved to other banks; request a hold via CFCFRMS"),
    Bucket("intermediary", "Pool / nodal", "violet",
           "In pool, nodal or escrow accounts: amount held there, account not suspended (SOP Para 9.1)"),
    Bucket("merchant_settled", "Merchant settled", "yellow", "Paid to merchants for delivered goods"),
]


@dataclass(frozen=True)
class Step:
    label: str
    value: float
    base: float
    tone: str
    note: str
    is_total: bool = False


def waterfall_steps(totals: TraceTotals) -> list[Step]:
    """Stolen (full bar) -> each bucket as a drop -> Unlocated (remaining bar)."""
    steps = [Step("Stolen", totals.stolen, 0.0, "ink", "Total reported by victims", is_total=True)]
    remaining = totals.stolen
    for b in BUCKETS:
        v = float(getattr(totals, b.key))
        remaining -= v
        steps.append(Step(b.label, v, max(remaining, 0.0), b.tone, b.note))
    steps.append(Step("Unlocated", max(totals.unlocated, 0.0), 0.0, "gray",
                      "Not yet traced to any account or exit", is_total=True))
    return steps


def located_share(totals: TraceTotals) -> float | None:
    if totals.stolen <= 0:
        return None
    located = totals.holdable + totals.cashed_out + totals.outbound + totals.intermediary + totals.merchant_settled
    return located / totals.stolen


def build_totals_waterfall(totals: TraceTotals, *, dark: bool = False, height: int | None = None) -> go.Figure:
    """Horizontal waterfall: categories read top to bottom (Stolen first), bars run along the rupee axis."""
    steps = waterfall_steps(totals)
    stolen = totals.stolen or 1.0
    colours = [STOLEN_HEX[dark] if s.label == "Stolen" else series_hex(s.tone, dark) for s in steps]
    fig = go.Figure(
        go.Bar(
            orientation="h",
            y=[s.label for s in steps],
            x=[s.value for s in steps],
            base=[s.base for s in steps],
            marker={"color": colours, "line": {"width": 0}},
            text=[inr_short(s.value) for s in steps],
            textposition="outside",
            cliponaxis=False,
            customdata=[[inr(s.value), pct(s.value / stolen), s.note] for s in steps],
            hovertemplate="<b>%{y}</b><br>%{customdata[0]} (%{customdata[1]} of stolen)"
                          "<br>%{customdata[2]}<extra></extra>",
            width=0.64,
            name="",
        )
    )
    try:  # rounded bar ends need plotly >= 5.19; older builds just keep square ends
        fig.update_traces(marker_cornerradius=3)
    except (ValueError, AttributeError):
        pass
    # dotted connectors from the end of each step to the next bar
    shapes = []
    level = totals.stolen
    for i, s in enumerate(steps[:-1]):
        nxt = level if s.is_total else s.base
        shapes.append({
            "type": "line", "xref": "x", "yref": "y", "x0": nxt, "x1": nxt, "y0": i + 0.32, "y1": i + 0.68,
            "line": {"width": 1, "dash": "dot", "color": series_hex("gray", dark)},
        })
        level = nxt
    tick_vals = [0, totals.stolen / 2, totals.stolen] if totals.stolen > 0 else [0]
    fig.update_layout(
        height=height or (38 * len(steps) + 50),
        margin={"l": 8, "r": 56, "t": 8, "b": 8},
        showlegend=False,
        bargap=0.3,
        shapes=shapes,
        xaxis={"tickvals": tick_vals, "ticktext": [inr_short(v) for v in tick_vals], "showgrid": True,
               "zeroline": False, "range": [0, max(totals.stolen, 1.0) * 1.02], "fixedrange": True},
        yaxis={"autorange": "reversed", "fixedrange": True, "automargin": True, "tickfont": {"size": 12}},
        hoverlabel={"align": "left"},
        font={"size": 12},
    )
    return fig
