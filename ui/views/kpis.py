"""KPIs: golden-set evaluation from ``evals/reports/latest.json`` (schema: docs/eval-report.md)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import plotly.graph_objects as go
import streamlit as st

from ui.components import html as h
from ui.components.baseline_strip import BaselineSummary, baseline_strip_html
from ui.components.compat import stretch
from ui.components.formatting import humanize, inr_short, ist, pct
from ui.components.theme import series_hex
from ui.data_source import load_eval_report
from ui.views.common import Ctx, head, md

HARNESS_CMD = "uv run python -m evals.harness --world demo"


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    kind: str  # pct | inr | int | sec
    target: float | None = None
    better: str = "high"  # high | low
    headline: bool = False


METRICS: list[Metric] = [
    Metric("traced_amount_recall", "Traced-amount recall", "pct", 0.95, "high", True),
    Metric("over_hold_inr", "Over-hold", "inr", 0.0, "low", True),
    Metric("false_freeze_count", "False freezes", "int", 0, "low", True),
    Metric("draft_numeric_accuracy", "Draft numeric accuracy", "pct", 1.0, "high", True),
    Metric("citation_validity", "Citation validity", "pct", 1.0, "high", True),
    Metric("injection_leaks", "Injection leaks", "int", 0, "low", True),
    Metric("p95_time_to_hold_plan_s", "p95 time to hold plan", "sec", 90.0, "low", True),
    Metric("totals_abs_error_inr", "Totals error vs golden", "inr", 0.0, "low"),
    Metric("hold_exact_match_rate", "Hold amounts matching golden", "pct"),
    Metric("disposition_accuracy", "Disposition accuracy", "pct"),
    Metric("process_accuracy", "Restoration process accuracy", "pct"),
    Metric("str_ground_accuracy", "STR ground accuracy", "pct"),
    Metric("escalation_precision", "s.106 flag precision", "pct"),
    Metric("escalation_recall", "s.106 flag recall", "pct"),
    Metric("draft_verification_pass_rate", "Drafts passing every check", "pct"),
    Metric("llm_only_hallucination_rate", "LLM-only drafts with a wrong amount (baseline)", "pct"),
    Metric("injection_flag_recall", "Adversarial complaints flagged", "pct"),
    Metric("p50_time_to_hold_plan_s", "p50 time to hold plan", "sec"),
    Metric("p95_total_s", "p95 end-to-end time", "sec"),
]
ROLE_TARGETS = {("precision", "INNOCENT_HOLDER"): 0.9, ("recall", "INNOCENT_HOLDER"): 0.8,
                ("precision", "MERCHANT"): 0.95}
ROLE_NAMES = {"MULE": "Mule", "INNOCENT_HOLDER": "Innocent holder", "MERCHANT": "Merchant",
              "INTERMEDIARY": "Pool / nodal", "LEGIT": "Legitimate", "UNKNOWN": "Unclassified"}


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    return None


def fmt_metric(kind: str, v: Any) -> str:
    x = _num(v)
    if x is None:
        return "—"
    if kind == "pct":
        whole = abs(x * 100 - round(x * 100)) < 1e-6
        return pct(x, 0 if whole else 1)
    if kind == "inr":
        return inr_short(x)
    if kind == "int":
        return f"{int(round(x)):,}"
    if kind == "sec":
        return f"{x:.2f} s" if x < 10 else f"{x:.0f} s"
    return str(v)


def meets(m: Metric, v: Any) -> bool | None:
    x = _num(v)
    if x is None or m.target is None:
        return None
    return x >= m.target - 1e-9 if m.better == "high" else x <= m.target + 1e-9


def target_text(m: Metric) -> str:
    if m.target is None:
        return "—"
    sign = "≥" if m.better == "high" else "≤"
    if m.kind in ("inr", "int") and m.target == 0:
        return "0" if m.kind == "int" else "₹0"
    return f"{sign} {fmt_metric(m.kind, m.target)}"


def render(ctx: Ctx) -> None:
    head("KPIs", "How Wapsi scores on the golden set: money located, holds, drafts and safety.",
         eyebrow="Evaluation")
    placeholder = st.empty()
    placeholder.markdown(h.skeleton(6), unsafe_allow_html=True)
    report, error = load_eval_report()
    placeholder.empty()
    if error:
        md(h.empty_state("The evaluation report couldn't be read", error, glyph="!", code=HARNESS_CMD))
        st.button("Try again", key="kpi_retry", type="primary")
        return
    if report is None:
        md(h.empty_state("No evaluation report yet",
                         "Run the golden-set harness to score tracing, holds, drafts and injection resistance. "
                         "Results appear here automatically.", glyph="◎", code=HARNESS_CMD))
        return

    metrics: dict[str, Any] = report.get("metrics") or {}
    cfg = report.get("config") or {}
    meta = [f"World: {report.get('world', '—')}", f"{report.get('cases', '—')} cases",
            f"{report.get('adversarial_cases', 0)} adversarial"]
    if cfg:
        meta.append(" · ".join(f"{humanize(k)}: {v}" for k, v in cfg.items()))
    gen = report.get("generated_at")
    if gen:
        try:
            meta.append(f"Generated {ist(datetime.fromisoformat(str(gen)))}")
        except ValueError:
            meta.append(f"Generated {gen}")
    md(h.chips(h.chip(m, "gray") for m in meta))

    tiles = []
    for m in METRICS:
        if not m.headline:
            continue
        ok = meets(m, metrics.get(m.key))
        note = f"target {target_text(m)}"
        if ok is not None:
            note += " · met" if ok else " · below target"
        tiles.append(h.Tile(m.label, fmt_metric(m.kind, metrics.get(m.key)), note, accent=bool(ok)))
    md(h.tiles(tiles, min_width_rem=9.5))

    baseline = report.get("baseline") or {}
    if baseline and _num(baseline.get("whole_account_freeze_inr")):
        md(h.section("Whole-account freeze baseline vs Wapsi, across the golden set"))
        md(baseline_strip_html(BaselineSummary(
            freeze_amount=_num(baseline.get("whole_account_freeze_inr")) or 0.0,
            freeze_accounts=int(_num(baseline.get("accounts_frozen_baseline")) or 0),
            hold_amount=_num(baseline.get("wapsi_hold_inr")) or 0.0,
            hold_accounts=int(_num(baseline.get("accounts_held")) or 0),
        )))

    left, right = st.columns(2, gap="large")
    with left:
        md(h.section("Role precision and recall"))
        fig = role_chart(metrics, dark=ctx.is_dark)
        if fig is None:
            md('<div class="wp-note">No role metrics in this report.</div>')
        else:
            st.plotly_chart(fig, key="kpi_roles", config={"displayModeBar": False}, **stretch(st.plotly_chart))
    with right:
        md(h.section("Numbers in drafts"))
        llm = _num(metrics.get("llm_only_hallucination_rate"))
        acc = _num(metrics.get("draft_numeric_accuracy"))
        md(h.tiles([
            h.Tile("LLM-only drafts with a wrong amount", fmt_metric("pct", llm), "baseline: no slot discipline"),
            h.Tile("Wapsi drafts with a wrong amount", fmt_metric("pct", None if acc is None else 1 - acc),
                   "numbers come from case tables", accent=True),
        ], min_width_rem=10))
        per = report.get("per_typology") or {}
        if per:
            md(h.section("Hold match by typology"))
            fig = typology_chart(per, dark=ctx.is_dark)
            if fig is not None:
                st.plotly_chart(fig, key="kpi_typology", config={"displayModeBar": False},
                                **stretch(st.plotly_chart))

    md(h.section("All metrics"))
    rows = []
    for m in METRICS:
        if m.key not in metrics:
            continue
        ok = meets(m, metrics.get(m.key))
        status = "—" if ok is None else h.chip("Met" if ok else "Below target", "good" if ok else "bad")
        rows.append(h.Row([h.Cell(m.label), h.Cell(fmt_metric(m.kind, metrics.get(m.key)), right=True),
                           h.Cell(target_text(m), right=True), h.Cell(html=status)]))
    known = {m.key for m in METRICS} | {"role_precision", "role_recall"}
    for k, v in metrics.items():
        if k not in known and _num(v) is not None:
            rows.append(h.Row([h.Cell(humanize(k)), h.Cell(f"{_num(v):,.3g}", right=True), h.Cell("—", right=True),
                               h.Cell("—")]))
    if rows:
        md(h.table([("Metric", False), ("Value", True), ("Target", True), ("Status", False)], rows))
    else:
        md('<div class="wp-note">The report has no metrics.</div>')

    if per := report.get("per_typology") or {}:
        with st.expander(f"Per typology ({len(per)})"):
            cols = sorted({k for v in per.values() if isinstance(v, dict) for k in v})
            trows = [h.Row([h.Cell(humanize(t))] + [
                h.Cell(fmt_metric("int" if c == "cases" else "pct", (v or {}).get(c)), right=True) for c in cols])
                for t, v in sorted(per.items())]
            md(h.table([("Typology", False)] + [(humanize(c), True) for c in cols], trows))

    failures = report.get("failures") or []
    md(h.section(f"Failures ({len(failures)})"))
    if not failures:
        md('<div class="wp-note">No failing cases in this run.</div>')
    else:
        frows = [h.Row([h.Cell(html=h.mono(str(f.get("case_id", "—")))), h.Cell(humanize(str(f.get("check", "—")))),
                        h.Cell(str(f.get("detail", "")))])
                 for f in failures[:200] if isinstance(f, dict)]
        md(h.table([("Case", False), ("Check", False), ("Detail", False)], frows,
                   caption=f"Showing {min(len(failures), 200)} of {len(failures)}"))


def role_chart(metrics: dict[str, Any], *, dark: bool = False) -> go.Figure | None:
    prec = metrics.get("role_precision") or {}
    rec = metrics.get("role_recall") or {}
    if not isinstance(prec, dict) or not isinstance(rec, dict):
        return None
    roles = [r for r in ROLE_NAMES if r in prec or r in rec] + sorted((set(prec) | set(rec)) - set(ROLE_NAMES))
    if not roles:
        return None
    names = [ROLE_NAMES.get(r, humanize(r)) for r in roles]
    fig = go.Figure()
    for i, (label, data) in enumerate((("Precision", prec), ("Recall", rec))):
        vals = [_num(data.get(r)) for r in roles]
        fig.add_trace(go.Bar(
            name=label, x=names, y=vals, marker={"color": series_hex(("blue", "orange")[i], dark)},
            text=[pct(v) if v is not None else "" for v in vals], textposition="outside", cliponaxis=False,
            hovertemplate="%{x} · " + label + ": %{y:.0%}<extra></extra>",
        ))
    fig.update_layout(
        barmode="group", bargap=0.3, bargroupgap=0.08, height=300, margin={"l": 8, "r": 8, "t": 36, "b": 8},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
        yaxis={"range": [0, 1.15], "tickformat": ".0%", "fixedrange": True, "tickvals": [0, 0.5, 1]},
        xaxis={"fixedrange": True},
    )
    return fig


def typology_chart(per: dict[str, Any], *, dark: bool = False) -> go.Figure | None:
    items = [(t, _num(v.get("hold_exact_match_rate"))) for t, v in per.items() if isinstance(v, dict)]
    items = [(t, v) for t, v in items if v is not None]
    if not items:
        return None
    items.sort(key=lambda x: x[1])
    fig = go.Figure(go.Bar(
        orientation="h", y=[humanize(t) for t, _ in items], x=[v for _, v in items],
        marker={"color": series_hex("blue", dark)}, text=[pct(v) for _, v in items], textposition="outside",
        cliponaxis=False, hovertemplate="%{y}: %{x:.0%}<extra></extra>",
    ))
    fig.update_layout(height=max(180, 30 * len(items) + 60), margin={"l": 8, "r": 40, "t": 8, "b": 8},
                      xaxis={"range": [0, 1.12], "tickformat": ".0%", "fixedrange": True, "tickvals": [0, 0.5, 1]},
                      yaxis={"fixedrange": True, "automargin": True}, showlegend=False)
    return fig
