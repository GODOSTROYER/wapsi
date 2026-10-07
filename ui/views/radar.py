"""Regulatory radar: a curated, verified diff between the SOP Wapsi follows and new or pending rules.

Read-only. Rows come from ``data/docs/reg_radar.json`` (written by hand from the primary texts, every quote checked
by ``wapsi.search.check_citations``); "what changes for this case" is computed by ``wapsi.domain.regradar`` from the
case's own hold plan. Nothing here edits a rule, a hold or a draft.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from ui.components import html as h
from ui.components.formatting import CLOCK_KIND_LABELS, DISPOSITION_LABELS, DRAFT_KIND_LABELS, humanize
from ui.views.common import Ctx, case_bar, citation_title, head, md
from wapsi.domain.regradar import (
    Clause,
    ClauseImpact,
    Radar,
    RadarSource,
    Side,
    case_impact,
    load_radar_cached,
)

SOURCE_STATUS = {
    "draft": ("DRAFT · not in force", "warn"),
    "final": ("Final · not yet in force", "accent"),
    "in-force": ("In force", "good"),
    "repealed": ("Repealed", "gray"),
    "reference": ("Reference", "gray"),
}
CHANGE = {
    "scope": ("Changes who it covers", "accent"),
    "stricter": ("Stricter than the SOP", "warn"),
    "new": ("New duty", "accent"),
    "aligned": ("Already aligned", "good"),
    "repealed": ("Repealed text", "gray"),
}
WAPSI_STATUS = {
    "open": ("Decision pending", "warn"),
    "reflected": ("Already reflected in Wapsi", "good"),
    "no_change": ("No change needed", "good"),
}
REVIEW = {"pending": ("Awaiting supervisor review", "gray"), "approved": ("Approved", "good"),
          "rejected": ("Rejected", "bad")}
DRAFT_CHIP = "DRAFT · not in force"
SEVERITY = {"action": ("Needs action", "bad"), "review": ("Needs review", "warn"), "info": ("For information", "gray")}


def _d(value: date | None) -> str:
    return value.strftime("%d-%m-%Y") if value else "—"


def _link(url: str, text: str) -> str:
    if not url.startswith("https://"):
        return h.esc(text)
    return f'<a href="{h.esc(url)}" target="_blank" rel="noopener noreferrer">{h.esc(text)}</a>'


def _source_name(radar: Radar, source_id: str) -> str:
    s = radar.source(source_id)
    return s.short if s else source_id


def _where(radar: Radar, side: Side) -> str:
    parts = [_source_name(radar, side.source_id), f"para {side.para}" if side.para[:1].isdigit() else side.para]
    if side.page is not None:
        parts.append(f"PDF p. {side.page}")
    if side.citation_id:
        parts.append(side.citation_id)
    return " · ".join(parts)


def _dates(s: RadarSource) -> str:
    bits = []
    if s.comments_close:
        bits.append(f"comments close {_d(s.comments_close)}")
    if s.effective:
        bits.append(f"{'proposed effective' if s.status == 'draft' else 'effective'} {_d(s.effective)}")
    return "; ".join(bits) or "—"


def _sources_table(radar: Radar) -> str:
    rows = []
    for s in radar.sources:
        label, tone = SOURCE_STATUS[s.status]
        rows.append(h.Row([
            h.Cell(html=f"<b>{h.esc(s.short)}</b>", title=s.title or s.short),
            h.Cell(html=h.chip(label, tone)),
            h.Cell(_d(s.issued), nowrap=True),
            h.Cell(_dates(s)),
            h.Cell(html=_link(s.landing_url or s.url, "Open") if (s.landing_url or s.url) else "Corpus"),
        ]))
    headers = [("Document", False), ("Status", False), ("Issued", False), ("Key dates", False), ("Source", False)]
    return h.table(headers, rows)


def _draft_chip(radar: Radar, c: Clause | None) -> str:
    return h.chip(DRAFT_CHIP, "warn") if c is not None and radar.rests_on_draft(c) else ""


def _impact_table(ctx: Ctx, radar: Radar, impacts: list[ClauseImpact]) -> str:
    rows = []
    for imp in impacts:
        label, tone = SEVERITY[imp.severity]
        sub = h.bullet_list(ctx.mask(f.text) for f in imp.findings) if imp.findings else None
        draft = _draft_chip(radar, radar.clause(imp.clause_id))
        rows.append(h.Row([
            h.Cell(html=h.mono(imp.clause_id), nowrap=True),
            h.Cell(html=f"<b>{h.esc(imp.title)}</b> {draft}<br>{h.esc(ctx.mask(imp.summary))}"),
            h.Cell(html=h.chip(label, tone)),
        ], sub=sub))
    return h.table([("Row", False), ("What changes", False), ("", False)], rows)


def _touchpoints(c: Clause) -> str:
    a = c.affects
    items = [h.cite(cid, citation_title(cid)) for cid in a.citation_ids]
    items += [h.chip(CLOCK_KIND_LABELS.get(k, humanize(k.value)), "gray") for k in a.clocks]
    items += [h.chip(DISPOSITION_LABELS.get(k, humanize(k.value)), "gray") for k in a.dispositions]
    items += [h.chip(DRAFT_KIND_LABELS.get(k, humanize(k.value)), "gray") for k in a.draft_kinds]
    return h.chips(items) if items else ""


def _clause(radar: Radar, c: Clause) -> None:
    change, change_tone = CHANGE[c.change]
    status, status_tone = WAPSI_STATUS[c.wapsi_status]
    review, review_tone = REVIEW[c.review.status]
    verified = (h.chip(f"Quotes verified {_d(c.verification.checked_on)}", "good") if c.verified
                else h.chip("Unverified · hidden by default", "bad"))
    chips = [h.chip(change, change_tone), h.chip(status, status_tone), h.chip(review, review_tone), verified]
    md(h.chips([x for x in (_draft_chip(radar, c), *chips) if x]))
    left, right = st.columns(2)
    with left:
        md(h.section("New or pending rule"))
        md("".join(h.quote(s.quote, _where(radar, s)) for s in c.new))
    with right:
        md(h.section("Current baseline"))
        if c.baseline:
            md("".join(h.quote(s.quote, _where(radar, s)) for s in c.baseline))
        else:
            md(f'<div class="wp-note">{h.esc(c.baseline_note)}</div>')
    md("".join(f'<p style="margin:.35rem 0"><span class="wp-muted">{h.esc(k)}</span><br>{h.esc(v)}</p>'
               for k, v in (("Applies to", c.applies_to), ("What it means for Wapsi", c.impact))))
    md(h.callout(c.suggested_action, strong="Suggested action:"))
    if (tp := _touchpoints(c)):
        md(f'<div class="wp-note" style="margin-top:.4rem">Wapsi rules this touches</div>{tp}')
    if c.affects.rules:
        md(h.bullet_list(c.affects.rules))


def render(ctx: Ctx) -> None:
    head("Regulatory radar",
         "New and pending rules set against the SOP Wapsi follows, and what they would change. Curated by hand from "
         "the primary texts; it suggests, a supervisor decides.", eyebrow="Rules")
    radar = load_radar_cached()
    if radar is None:
        md(h.empty_state("No regulatory radar yet",
                         "The curated rule-change registry isn't in this deployment's docs folder.",
                         glyph="◎", code="data/docs/reg_radar.json"))
        return

    show_all = st.checkbox("Include unverified rows", key="radar_unverified", value=False,
                           help="Rows whose quotes could not be checked against their source are hidden by default.")
    clauses = radar.visible(include_unverified=show_all)
    hidden = len(radar.clauses) - len(radar.visible())
    draft = next((s for s in radar.sources if s.status == "draft"), None)
    md(h.tiles([
        h.Tile("Rows shown", str(len(clauses)),
               f"{hidden} unverified hidden" if hidden and not show_all else "all quotes verified" if not hidden
               else "including unverified"),
        h.Tile("Decisions pending", str(sum(c.wapsi_status == "open" for c in clauses)), "open for a supervisor",
               accent=any(c.wapsi_status == "open" for c in clauses)),
        h.Tile("Awaiting review", str(sum(c.review.status == "pending" for c in clauses)), "rows not yet approved"),
        h.Tile("DRAFT comments close", _d(draft.comments_close) if draft else "—",
               f"RBI DRAFT; proposed effective {_d(draft.effective)}" if draft else "no open draft"),
    ], min_width_rem=9))

    md(h.section("What would change for this case"))
    if ctx.bundle is None:
        md('<div class="wp-note">Pick a case in the sidebar to see what each rule would mean for its holds.</div>')
    else:
        case_bar(ctx, ctx.bundle)
        impacts = case_impact(radar, ctx.bundle, ctx.now, include_unverified=show_all)
        hits = [i for i in impacts if i.applies]
        if hits:
            md(_impact_table(ctx, radar, hits))
        else:
            md('<div class="wp-note">None of the rows touches this case.</div>')
        rest = [i for i in impacts if not i.applies]
        if rest:
            md('<div class="wp-note" style="margin-top:.4rem">No effect on this case: '
               + h.esc("; ".join(f"{i.clause_id} {i.title}" for i in rest)) + ".</div>")

    md(h.section(f"All rows ({len(clauses)})"))
    if not clauses:
        md('<div class="wp-note">No verified rows yet. Run the quote check to verify them.</div>')
    for c in clauses:
        with st.expander(f"{c.id} · {c.title}" + (" · DRAFT" if radar.rests_on_draft(c) else "")):
            _clause(radar, c)

    md(h.section("Documents tracked"))
    md(_sources_table(radar))
    md('<div class="wp-note" style="margin-top:.5rem">Read-only. Every quote is checked word for word against its '
       f"source document (register as of {h.esc(_d(radar.as_of))}). A row changes Wapsi only after a supervisor "
       "approves it.</div>")
