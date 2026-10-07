"""Clocks: statutory and SOP deadlines per case, with what lapses in the next N days."""

from __future__ import annotations

import streamlit as st

from ui.components import html as h
from ui.components.clock_timeline import ClockRow, build_clock_timeline
from ui.components.compat import stretch
from ui.components.formatting import (
    CLOCK_KIND_LABELS,
    CLOCK_STATUS_LABELS,
    DRAFT_KIND_LABELS,
    days_between,
    humanize,
    ist,
    relative_due,
)
from ui.components.theme import CLOCK_STATUS_TONE
from ui.data_source import clock_status
from ui.views.common import Ctx, case_bar, citation_title, generate_button, head, md
from wapsi.models import CaseBundle, Clock, ClockStatus


def subject_parts(c: Clock, bundle: CaseBundle) -> tuple[str, str]:
    """(what the clock applies to, note). The note is ``Clock.notes``; clocks stored before that field existed
    carry it in the subject: 'HB01-… (provisional: hold not yet executed)' -> ('HB01-…', 'Provisional…')."""
    raw = (c.subject or "").strip()
    if c.notes:
        main, note = raw, c.notes.strip()
    else:
        main, _, note = raw.partition(" (")
        note = note.rstrip(")").strip()
    note = note[:1].upper() + note[1:] if note else ""
    drafts = {d.draft_id: d for d in bundle.drafts}
    if main in drafts:
        d = drafts[main]
        main = DRAFT_KIND_LABELS.get(d.kind, humanize(d.kind.value)) + (f" · {d.account_id}" if d.account_id else "")
    elif main == bundle.case.case_id:
        main = "Whole case"
    elif main in bundle.case.ncrp_ids:
        main = f"NCRP {main}"
    return main or "Whole case", note


def _label(c: Clock, bundle: CaseBundle, multi_case: bool) -> str:
    base = CLOCK_KIND_LABELS.get(c.kind, humanize(c.kind.value))
    subject, _ = subject_parts(c, bundle)
    if subject and subject != "Whole case":
        base += f" · {subject}"
    if multi_case:
        base = f"{bundle.case.case_id[-6:]} · {base}"
    return base


def render(ctx: Ctx) -> None:
    head("Clocks", "Deadlines that start once holds are placed, so nothing lapses unnoticed.", eyebrow="Deadlines")
    scopes = ["This case", "All cases"] if ctx.bundle is not None else ["All cases"]
    scope = st.radio("Scope", scopes, key="clock_scope", horizontal=True, label_visibility="collapsed")
    if scope == "This case" and ctx.bundle is not None:
        case_bar(ctx, ctx.bundle)
        bundles = [ctx.bundle]
    else:
        bundles = [b for b in (ctx.ds.get_bundle(c.case_id) for c in ctx.ds.list_cases()) if b is not None]

    items: list[tuple[CaseBundle, Clock, ClockStatus]] = []
    for b in bundles:
        for c in ctx.ds.clocks(b, ctx.now):
            items.append((b, c, clock_status(c, ctx.now)))

    if not items:
        md(h.empty_state(
            "No clocks running yet",
            "Clocks start when drafts are generated and approved: the STR is due within 7 working days "
            "(PML Rule 8) and each hold lapses after 90 days, with a reminder to the LEA on day 75 "
            "(SOP Para 10.1).",
            glyph="⏱",
        ))
        if ctx.bundle is not None and scope == "This case":
            _, mid, _ = st.columns([1, 1.2, 1])
            with mid:
                generate_button(ctx, key="clocks_generate")
        return

    multi = len(bundles) > 1
    slider_col, _ = st.columns([1.2, 2])
    with slider_col:
        window = st.slider("Show what falls due within (days)", min_value=1, max_value=90, value=15,
                           key="clock_window")
    soon = [(b, c, s) for b, c, s in items
            if s != ClockStatus.DONE and days_between(c.due, ctx.now) <= window]
    counts = {s: sum(1 for _, _, x in items if x == s) for s in ClockStatus}
    md(h.tiles([
        h.Tile("Overdue", str(counts[ClockStatus.OVERDUE]), "act now" if counts[ClockStatus.OVERDUE] else "none"),
        h.Tile(f"Due within {window} days", str(len(soon)), "including overdue", accent=bool(soon)),
        h.Tile("On track", str(counts[ClockStatus.OPEN])),
        h.Tile("Done", str(counts[ClockStatus.DONE])),
    ], min_width_rem=8))

    rows = [ClockRow(label=_label(c, b, multi), start=c.start, due=c.due, status=s, warn_at=c.warn_at,
                     detail=("Working days" if c.business_days else "Calendar days")
                     + (f" · {citation_title(c.citation_id)}" if c.citation_id else "")
                     + (f" · {subject_parts(c, b)[1]}" if subject_parts(c, b)[1] else ""))
            for b, c, s in items]
    st.plotly_chart(build_clock_timeline(rows, ctx.now, dark=ctx.is_dark), key="clock_timeline",
                    config={"displayModeBar": False}, **stretch(st.plotly_chart))

    md(h.section(f"Due or lapsing in the next {window} days"))
    if not soon:
        md(f'<div class="wp-note">Nothing falls due in the next {window} days.</div>')
    else:
        md(_table(soon, ctx, multi))
    with st.expander(f"All clocks ({len(items)})"):
        md(_table(items, ctx, multi))


def _subject_html(c: Clock, bundle: CaseBundle) -> str:
    subject, note = subject_parts(c, bundle)
    out = h.esc(subject)
    if note:
        out += " " + h.chip("Provisional" if note.lower().startswith("provisional") else "Note", "gray", title=note)
    return out


def _table(items: list[tuple[CaseBundle, Clock, ClockStatus]], ctx: Ctx, multi: bool) -> str:
    rows = []
    for b, c, s in sorted(items, key=lambda x: x[1].due):
        cells = []
        if multi:
            cells.append(h.Cell(html=h.mono(b.case.case_id)))
        cells += [
            h.Cell(CLOCK_KIND_LABELS.get(c.kind, humanize(c.kind.value))),
            h.Cell(html=_subject_html(c, b)),
            h.Cell(ist(c.due)),
            h.Cell("—" if s == ClockStatus.DONE else relative_due(c.due, ctx.now)),
            h.Cell(html=h.chip(CLOCK_STATUS_LABELS[s], CLOCK_STATUS_TONE[s])),
            h.Cell(html=h.cite(c.citation_id, citation_title(c.citation_id)) if c.citation_id else "—"),
        ]
        rows.append(h.Row(cells))
    headers = ([("Case", False)] if multi else []) + [
        ("Clock", False), ("Applies to", False), ("Due (IST)", False), ("When", False), ("Status", False),
        ("Basis", False)]
    return h.table(headers, rows)
