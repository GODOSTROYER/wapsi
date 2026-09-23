"""Hold Plan: every recommendation with its amount, basis and (once drafted) the exact Annexure-I wording."""

from __future__ import annotations

import csv
import io
import re

import streamlit as st

from ui.components import html as h
from ui.components.baseline_strip import baseline_strip_html, summarize
from ui.components.formatting import (
    APPROVAL_LABELS,
    CHECK_LABELS,
    DISPOSITION_LABELS,
    PROCESS_LABELS,
    ROLE_LABELS,
    STR_GROUND_LABELS,
    inr,
    inr_short,
    inr_table,
    reason_label,
)
from ui.components.theme import APPROVAL_TONE, DISPOSITION_TONE
from ui.data_source import latest_approval, plan_reports
from ui.views.common import (
    K_ACCOUNT,
    Ctx,
    case_bar,
    citation_title,
    download,
    generate_button,
    go,
    head,
    md,
    need_case,
)
from wapsi.models import (
    HOLDING_DISPOSITIONS,
    CaseBundle,
    Disposition,
    Draft,
    DraftKind,
    HoldDecision,
)
from wapsi.money import fmt_inr

_HOLDS = set(HOLDING_DISPOSITIONS) | {Disposition.FORWARD_HOLD}
_REQUESTS = {Disposition.OUTBOUND_CFCFRMS_REQUEST}
GROUPS: dict[str, set[Disposition]] = {
    "Holds": _HOLDS,
    "Other-bank requests": _REQUESTS,
    "No hold": set(Disposition) - _HOLDS - _REQUESTS,
}


_SENT_SPLIT = re.compile(r"(?<=[.;])(?<!No\.)\s+(?=[A-Z\[])")
_MD_NOISE = re.compile(r"\*\*|__|`")


def sentences(body: str) -> list[str]:
    """Prose sentences of a Markdown draft (skips headings, tables and list markers)."""
    out: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "|", ">")):
            continue
        line = _MD_NOISE.sub("", line.lstrip("-* ").strip())
        out.extend(p.strip() for p in _SENT_SPLIT.split(line) if p.strip())
    return out


def hold_sentence(draft: Draft, dec: HoldDecision) -> str | None:
    """The Annexure-I-style sentence of a hold instruction ("... put on hold the amount ..."), else the sentence
    stating the hold amount."""
    sents = sentences(draft.body)
    annex = next((x for x in sents if re.search(r"\bput\b.{0,20}\bon hold\b", x, re.IGNORECASE)), None)
    if annex:
        return annex
    amount = draft.slots.get("hold_amount") or fmt_inr(dec.hold_amount)
    return next((x for x in sents if amount in x), None)


def _drafts_by_account(bundle: CaseBundle) -> dict[str, Draft]:
    return {d.account_id: d for d in bundle.drafts if d.kind == DraftKind.HOLD_INSTRUCTION and d.account_id}


def render(ctx: Ctx) -> None:
    head("Hold plan", "Hold only the victim's money, account by account, with the SOP basis for each step.",
         eyebrow="Recommended — pending approval")
    if not need_case(ctx):
        return
    bundle = ctx.bundle
    case_bar(ctx, bundle)
    plan = bundle.plan
    if plan is None:
        md(h.empty_state("No hold plan yet",
                         "The plan appears once the trail is traced and every account is classified."))
        return

    md(baseline_strip_html(summarize(plan)))

    outbound = sum(d.tainted_amount for d in plan.decisions if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST)
    md(h.tiles([
        h.Tile("Hold now", inr_short(plan.total_hold), "amount-specific holds", accent=True,
               title=inr(plan.total_hold)),
        h.Tile("Request other banks", inr_short(outbound), "via CFCFRMS", swatch="orange", title=inr(outbound)),
        h.Tile("Recoverable", inr_short(plan.total_recoverable), "holds + requests",
               title=inr(plan.total_recoverable)),
        h.Tile("Forward holds", inr_short(plan.total_forward_hold), "on future credits, first layer only",
               swatch="violet", title=inr(plan.total_forward_hold)),
        h.Tile("Over-hold", inr_short(plan.over_hold_amount),
               "must stay ₹0" if plan.over_hold_amount == 0 else "held beyond victim money"),
    ]))

    facts = [h.chip(PROCESS_LABELS.get(plan.restoration_process, plan.restoration_process.value), "accent",
                    title="Restoration route under SOP Para 11")]
    if plan.str_ground is not None:
        facts.append(h.chip(f"STR ground: {STR_GROUND_LABELS.get(plan.str_ground, plan.str_ground.value)}",
                            "gray", title="PML (Maintenance of Records) Rules, Rule 2(1)(g)"))
    appr = latest_approval(bundle, None)
    if appr is not None:
        facts.append(h.chip(f"Hold plan {APPROVAL_LABELS[appr.decision].lower()} by {appr.approver}",
                            APPROVAL_TONE.get(appr.decision, "gray")))
    for rep in plan_reports(bundle):
        failed = [c for c in rep.checks if not c.passed and c.severity == "error"]
        facts.append(h.chip("Plan checks passed" if rep.passed else f"Plan checks: {len(failed)} failed",
                            "good" if rep.passed else "bad",
                            title="; ".join(f"{CHECK_LABELS.get(c.name, c.name)}: {c.message}" for c in rep.checks)))
    md(h.chips(facts))

    if len(plan.per_victim_recoverable) > 1:
        names = {c.ncrp_id: ctx.viewer.name(c.victim_name) for c in bundle.complaints}
        claimed = {c.ncrp_id: c.amount_claimed for c in bundle.complaints}
        rows = [h.Row([h.Cell(html=h.mono(n)), h.Cell(names.get(n, "—")), h.Cell(inr_table(claimed.get(n)), right=True),
                       h.Cell(inr_table(v), right=True)])
                for n, v in sorted(plan.per_victim_recoverable.items())]
        md(h.section("Pro-rata share per victim (SOP Para 5(xiii))"))
        md(h.table([("NCRP ID", False), ("Victim", False), ("Claimed", True), ("Recoverable", True)], rows))

    md(h.section("Recommendations"))
    c1, c2 = st.columns([3, 1.2], vertical_alignment="bottom")
    with c1:
        groups = st.multiselect("Show", list(GROUPS), default=list(GROUPS), key="plan_groups",
                                help="Filter the table by what the plan recommends.")
    drafts = _drafts_by_account(bundle)
    wanted = set().union(*(GROUPS[g] for g in groups)) if groups else set()
    decisions = sorted(plan.decisions, key=lambda d: (d.layer, -d.hold_amount, d.account_id))
    shown = [d for d in decisions if d.disposition in wanted]
    with c2:
        download("Download CSV", _csv(decisions), f"{bundle.case.case_id}-hold-plan.csv", key="plan_csv",
                 mime="text/csv")

    if not drafts:
        md(h.callout("Drafts not generated yet — the Annexure-I wording for each hold appears in this table once "
                     "drafts exist.", strong="Hold wording."))
        generate_button(ctx, key="plan_generate")

    if not shown:
        md(h.empty_state("Nothing matches this filter", "Choose at least one group above.", glyph="?"))
        return

    rows = []
    for d in shown:
        cites = _cites(d.citation_ids)
        chips_html = h.chip(DISPOSITION_LABELS.get(d.disposition, d.disposition.value),
                            DISPOSITION_TONE.get(d.disposition, "gray"), dot=True)
        if d.requires_human:
            chips_html += " " + h.chip("Needs human decision", "warn",
                                       title="; ".join(reason_label(c) for c in d.reason_codes))
        hold_cell = inr_table(d.hold_amount) if d.hold_amount else "—"
        if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST:
            hold_cell = f"Request {inr_table(d.tainted_amount)}"
        sub = None
        draft = drafts.get(d.account_id)
        if draft is not None and d.hold_amount:
            sentence = hold_sentence(draft, d)
            if sentence:
                sub = (f'<span class="wp-note">Annexure-I wording:</span> “{h.esc(ctx.mask(sentence, bundle))}” '
                       f'<span class="wp-note">Hold {h.esc(inr_table(d.hold_amount))} only.</span>')
        elif d.notes:
            sub = f'<span class="wp-note">{h.esc(ctx.mask(d.notes, bundle))}</span>'
        rows.append(h.Row(
            cells=[
                h.Cell(html=h.mono(d.account_id) + '<div class="wp-note" style="white-space:nowrap">'
                       f"L{d.layer} · {h.esc(ROLE_LABELS.get(d.role, d.role.value))}</div>", title=d.bank_id),
                h.Cell(html=f'<div class="wp-cell-rec">{chips_html}</div>'),
                h.Cell(hold_cell, right=True),
                h.Cell(inr_table(d.forward_hold_amount) if d.forward_hold_amount else "—", right=True),
                h.Cell(inr_table(d.tainted_amount), right=True),
                h.Cell(inr_table(d.balance_now), right=True),
                h.Cell(html=cites or "—"),
            ],
            sub=sub,
            selected=d.account_id == st.session_state.get(K_ACCOUNT),
        ))
    md(h.table(
        [("Account · layer · role", False), ("Recommendation", False), ("Hold", True), ("Forward hold", True),
         ("Victim money", True), ("Balance", True), ("Basis", False)],
        rows,
        caption=f"{len(shown)} of {len(decisions)} accounts · amounts in rupees · hover a citation for its section",
    ))

    options = [d.account_id for d in shown]
    c1, c2, _ = st.columns([2, 1, 2], vertical_alignment="bottom")
    with c1:
        pick = st.selectbox("Open an account in the case room", options, key="plan_pick")
    with c2:
        st.button("Inspect", key="plan_inspect", on_click=_inspect, args=(pick,))


def _cites(ids: list[str], limit: int = 2) -> str:
    shown = "".join(h.cite(c, citation_title(c)) for c in ids[:limit])
    rest = ids[limit:]
    if rest:
        shown += h.cite(f"+{len(rest)}", " · ".join(f"{c}: {citation_title(c)}" for c in rest))
    return shown


def _inspect(account_id: str | None) -> None:
    if account_id:
        st.session_state[K_ACCOUNT] = account_id
    go("Case Room")


def _csv(decisions: list[HoldDecision]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["account_id", "bank_id", "layer", "role", "recommendation", "hold_amount", "forward_hold_amount",
                "victim_money", "balance_now", "citations", "requires_human"])
    for d in decisions:
        w.writerow([d.account_id, d.bank_id, d.layer, ROLE_LABELS.get(d.role, d.role.value),
                    DISPOSITION_LABELS.get(d.disposition, d.disposition.value), f"{d.hold_amount:.2f}",
                    f"{d.forward_hold_amount:.2f}", f"{d.tainted_amount:.2f}", f"{d.balance_now:.2f}",
                    " ".join(d.citation_ids), "yes" if d.requires_human else "no"])
    return buf.getvalue()
