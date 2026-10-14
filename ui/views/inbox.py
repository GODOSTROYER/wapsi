"""Inbox: hold notices ordered by money still recoverable, with SLA chips."""

from __future__ import annotations

import streamlit as st

from ui.components import html as h
from ui.components.formatting import inr, inr_short, ist, pct, relative_past
from ui.data_source import clock_status, latest_approval
from ui.views.common import (
    Ctx,
    first_layer,
    head,
    md,
    nav_button,
    sla_chips,
    status_chip,
    victims_text,
)
from wapsi.models import ApprovalDecision, CaseBundle, CaseStatus, ClockStatus

SORTS = {
    "Recoverable at risk (highest first)": lambda b: -(b.plan.total_recoverable if b.plan else 0.0),
    "Newest notice first": lambda b: -b.case.opened_at.timestamp(),
    "Oldest notice first": lambda b: b.case.opened_at.timestamp(),
}


def _matches(ctx: Ctx, b: CaseBundle, query: str) -> bool:
    if not query:
        return True
    q = query.lower().strip()
    hay = [b.case.case_id, *b.case.ncrp_ids, *first_layer(b)]
    hay += [c.victim_account for c in b.complaints]
    if not ctx.viewer.masked:
        hay += [c.victim_name for c in b.complaints]
    return any(q in s.lower() for s in hay)


def _pending_count(b: CaseBundle) -> int:
    items = [d.draft_id for d in b.drafts] or ([None] if b.plan is not None else [])
    count = 0
    for did in items:
        a = latest_approval(b, did)
        if a is None or a.decision != ApprovalDecision.APPROVED:
            count += 1
    return count


def render(ctx: Ctx) -> None:
    head("Inbox", "Hold notices waiting for you, with the money still recoverable in each.",
         eyebrow="Nodal officer desk")
    try:
        bundles = [b for b in (ctx.ds.get_bundle(c.case_id) for c in ctx.ds.list_cases()) if b is not None]
    except Exception as exc:  # noqa: BLE001
        md(h.empty_state("Couldn't load the inbox", f"The case store didn't answer: {exc}", glyph="!"))
        st.button("Try again", key="inbox_retry", type="primary")
        return

    if not bundles:
        md(h.empty_state("No hold notices yet",
                         "New NCRP / 1930 notices pushed through CFCFRMS (a simulated interface in this build) appear "
                         "here as soon as they arrive.",
                         glyph="✓"))
        return

    open_ = [b for b in bundles if b.case.status != CaseStatus.CLOSED]
    at_risk = sum(b.plan.total_recoverable for b in open_ if b.plan)
    stolen = sum(b.trace.totals.stolen for b in open_ if b.trace)
    pending = sum(_pending_count(b) for b in open_)
    due_soon = 0
    for b in open_:
        for c in ctx.ds.clocks(b, ctx.now):
            if clock_status(c, ctx.now) in (ClockStatus.WARNING, ClockStatus.OVERDUE):
                due_soon += 1
    md(h.tiles([
        h.Tile("Recoverable at risk", inr_short(at_risk), f"of {inr_short(stolen)} reported stolen", accent=True,
               title=inr(at_risk)),
        h.Tile("Open notices", str(len(open_)), f"{sum(len(b.case.ncrp_ids) for b in open_)} victim complaints"),
        h.Tile("Awaiting approval", str(pending), "drafts or hold plans"),
        h.Tile("Clocks needing action", str(due_soon), "due soon or overdue" if due_soon else "nothing urgent"),
    ]))

    c1, c2, _ = st.columns([2.2, 1.6, 2])
    with c1:
        query = st.text_input("Search", key="inbox_query", placeholder="NCRP ID, account or victim",
                              label_visibility="collapsed")
    with c2:
        sort_label = st.selectbox("Sort", list(SORTS), key="inbox_sort", label_visibility="collapsed")

    rows = sorted((b for b in bundles if _matches(ctx, b, query)), key=SORTS[sort_label])
    if not rows:
        md(h.empty_state("No notices match your search", "Try an NCRP ID, a case ID or an account number.",
                         glyph="?"))
        return

    for b in rows:
        _row(ctx, b)
    md(f'<div class="wp-note">{len(rows)} notice{"s" if len(rows) != 1 else ""} · synthetic data</div>')


def _row(ctx: Ctx, b: CaseBundle) -> None:
    plan, trace = b.plan, b.trace
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([2.6, 2.0, 1.7, 1.1], vertical_alignment="center")
        with c1:
            sources = sorted({c.source for c in b.complaints}) or ["NCRP"]
            reported = min((c.reported_at for c in b.complaints), default=b.case.opened_at)
            md(f'<div class="wp-row"><span class="wp-row-title">{h.esc(victims_text(ctx, b))}</span>'
               f'<span class="wp-note">{h.esc(" / ".join(sources))} · reported {h.esc(ist(reported))}'
               f" · {h.esc(relative_past(reported, ctx.now))}</span></div>"
               f'<div class="wp-note wp-mono" style="margin:.15rem 0 .35rem">{h.esc(b.case.case_id)}</div>'
               + h.chips([status_chip(b), *sla_chips(ctx, b)]))
        with c2:
            fl = first_layer(b)
            if not fl:
                fl_text = "Not traced yet"
            elif len(fl) == 1:
                fl_text = fl[0]
            else:
                fl_text = f"{fl[0]} +{len(fl) - 1}"
            accounts = len([a for a, s_ in trace.accounts.items() if s_.layer > 0]) if trace else 0
            md('<div class="wp-note">First-layer account</div>'
               f'<div class="wp-mono" title="{h.esc(", ".join(fl))}">{h.esc(fl_text)}</div>'
               f'<div class="wp-note">{accounts} account{"s" if accounts != 1 else ""} on the trail</div>')
        with c3:
            if plan is not None and trace is not None:
                share = plan.total_recoverable / trace.totals.stolen if trace.totals.stolen else None
                md(f'<div class="wp-note">Recoverable at risk</div>'
                   f'<div class="wp-big" title="{h.esc(inr(plan.total_recoverable))}">'
                   f"{h.esc(inr_short(plan.total_recoverable))}</div>"
                   f'<div class="wp-note">{h.esc(pct(share))} of {h.esc(inr_short(trace.totals.stolen))} stolen</div>')
            else:
                md('<div class="wp-note">Recoverable at risk</div><div class="wp-big">—</div>'
                   '<div class="wp-note">Trail not traced yet</div>')
        with c4:
            nav_button("Open case", "Case Room", case_id=b.case.case_id, key=f"open_{b.case.case_id}",
                       primary=True, help="Open the case room: trail, totals and evidence")
            if b.plan is not None:
                nav_button("Hold plan", "Hold Plan", case_id=b.case.case_id, key=f"plan_{b.case.case_id}")
