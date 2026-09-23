"""Approvals: maker-checker. Supervisors approve, return or reject each item with a reason code."""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from ui.components import html as h
from ui.components.formatting import (
    ALL_APPROVAL_REASONS,
    APPROVAL_LABELS,
    APPROVAL_REASON_LABELS,
    DRAFT_KIND_LABELS,
    humanize,
    inr_short,
    ist,
)
from ui.components.theme import APPROVAL_TONE
from ui.data_source import latest_approval, plan_reports
from ui.views.common import Ctx, case_bar, flash, head, md, nav_button, need_case
from wapsi.models import HOLDING_DISPOSITIONS, Approval, ApprovalDecision, CaseBundle, VerificationReport

REASONS = APPROVAL_REASON_LABELS
ALL_REASONS = ALL_APPROVAL_REASONS
DECISION_VERBS = {
    ApprovalDecision.APPROVED: "Approve",
    ApprovalDecision.CHANGES_REQUESTED: "Request changes",
    ApprovalDecision.REJECTED: "Reject",
}


@dataclass
class Item:
    key: str
    draft_id: str | None
    title: str
    kind: str
    detail: str
    report_ok: bool | None  # None = no checks recorded
    latest: Approval | None


def items_for(bundle: CaseBundle) -> list[Item]:
    out: list[Item] = []
    plan = bundle.plan
    if plan is not None:
        held = [d for d in plan.decisions if d.disposition in HOLDING_DISPOSITIONS and d.hold_amount > 0]
        reports = plan_reports(bundle)
        out.append(Item(
            key="plan", draft_id=None, title="Hold plan",
            kind="Hold plan",
            detail=f"{len(held)} amount-specific hold{'s' if len(held) != 1 else ''} totalling "
                   f"{inr_short(plan.total_hold)} · recoverable {inr_short(plan.total_recoverable)}",
            report_ok=all(r.passed for r in reports) if reports else None,
            latest=latest_approval(bundle, None),
        ))
    for d in bundle.drafts:
        rep: VerificationReport | None = bundle.verifications.get(d.draft_id)
        out.append(Item(
            key=d.draft_id, draft_id=d.draft_id, title=d.title,
            kind=DRAFT_KIND_LABELS.get(d.kind, humanize(d.kind.value)),
            detail=d.account_id or "Case-level document",
            report_ok=None if rep is None else rep.passed,
            latest=latest_approval(bundle, d.draft_id),
        ))
    return out


def _is_open(item: Item) -> bool:
    return item.latest is None or item.latest.decision != ApprovalDecision.APPROVED


def render(ctx: Ctx) -> None:
    head("Approvals", "Approve, return or reject each item. Every decision is recorded with your name and role.",
         eyebrow="Maker-checker")
    if not need_case(ctx):
        return
    bundle = ctx.bundle
    case_bar(ctx, bundle)
    items = items_for(bundle)
    if not items:
        md(h.empty_state("Nothing to approve yet", "The hold plan and drafts appear here once they're ready."))
        return

    if ctx.viewer.role == "analyst":
        md(h.callout("You're signed in as an analyst (maker). A supervisor records the decision — switch role in "
                     "the sidebar to try the checker's view.", strong="View only."))
    elif ctx.viewer.role == "auditor":
        md(h.callout("Auditors see decisions and history but can't change them. Personal details are masked.",
                     strong="Read-only."))

    counts = {"Pending": 0, **{APPROVAL_LABELS[d]: 0 for d in ApprovalDecision}}
    for it in items:
        counts["Pending" if it.latest is None else APPROVAL_LABELS[it.latest.decision]] += 1
    md(h.tiles([h.Tile(k, str(v), accent=(k == "Pending" and v > 0)) for k, v in counts.items()],
               min_width_rem=8))

    if not bundle.drafts:
        c1, c2 = st.columns([3, 1.3], vertical_alignment="center")
        with c1:
            md(h.callout("Drafts not generated yet. You can decide on the hold plan now; hold instructions, the STR "
                         "and the restoration pack join this queue once drafted.", strong="Drafts."))
        with c2:
            nav_button("Open drafts", "Drafts & Verification", key="appr_to_drafts")

    ready = [it for it in items if _is_open(it) and it.report_ok is True]
    if ctx.viewer.can_approve and len(ready) > 1:
        with st.container(border=True):
            c1, c2 = st.columns([3, 1.3], vertical_alignment="center")
            with c1:
                md(f'<div class="wp-row-title">{len(ready)} items passed every check</div>'
                   '<div class="wp-note">Approve them together once reviewed. Each item still gets its own '
                   "approval and audit entry.</div>")
                confirm = st.checkbox(f"I have reviewed all {len(ready)} items", key="bulk_confirm")
            with c2:
                st.button(f"Approve {len(ready)} items", key="bulk_approve", type="primary", disabled=not confirm,
                          on_click=_bulk_approve,
                          args=(ctx.ds, bundle.case.case_id, [it.draft_id for it in ready], ctx.viewer.actor,
                                ctx.viewer.role))

    show = st.radio("Show", ["Needs a decision", "All items"], key="appr_filter", horizontal=True,
                    label_visibility="collapsed")
    visible = [it for it in items if show == "All items" or _is_open(it)]
    if not visible:
        md(h.empty_state("All caught up", "Every item in this case has been approved.", glyph="✓"))
    for it in visible:
        _item(ctx, bundle, it)

    _actions(bundle)
    _history(ctx, bundle)


def _item(ctx: Ctx, bundle: CaseBundle, it: Item) -> None:
    with st.container(border=True):
        left, right = st.columns([1.35, 1], gap="medium")
        with left:
            if it.report_ok is None:
                check = h.chip("No checks recorded" if it.draft_id else "Plan checks run with drafting", "gray")
            elif it.report_ok:
                check = h.chip("Checks passed", "good")
            else:
                check = h.chip("Failed checks", "bad")
            status = h.chip(APPROVAL_LABELS[it.latest.decision] if it.latest else "Pending approval",
                            APPROVAL_TONE.get(it.latest.decision if it.latest else None, "gray"))
            md(f'<div class="wp-eyebrow">{h.esc(it.kind)}</div>'
               f'<div class="wp-row-title">{h.esc(ctx.mask(it.title, bundle))}</div>'
               f'<div class="wp-note" style="margin:.1rem 0 .4rem">{h.esc(it.detail)}</div>'
               + h.chips([status, check]))
            if it.latest is not None:
                reason = ALL_REASONS.get(it.latest.reason_code, humanize(it.latest.reason_code))
                comment = f" — “{ctx.mask(it.latest.comment, bundle)}”" if it.latest.comment else ""
                md(f'<div class="wp-note" style="margin-top:.35rem">{h.esc(APPROVAL_LABELS[it.latest.decision])} by '
                   f"{h.esc(it.latest.approver)} ({h.esc(it.latest.role)}) · {h.esc(ist(it.latest.at))} · "
                   f"{h.esc(reason)}{h.esc(comment)}</div>")
        with right:
            if not _is_open(it):
                md('<div class="wp-note">Approved. Reopen by regenerating drafts if the plan changes.</div>')
            elif not ctx.viewer.can_approve:
                md('<div class="wp-note">Waiting for a supervisor.</div>')
            else:
                _controls(ctx, bundle, it)


def _controls(ctx: Ctx, bundle: CaseBundle, it: Item) -> None:
    decision = st.radio("Decision", list(REASONS), key=f"dec_{it.key}", horizontal=True,
                        format_func=lambda d: DECISION_VERBS[d])
    reasons = REASONS[decision]
    reason = st.selectbox("Reason", list(reasons), key=f"reason_{it.key}_{decision.value}",
                          format_func=lambda r: reasons[r])
    comment = st.text_input("Note (optional)", key=f"note_{it.key}", max_chars=300,
                            placeholder="Visible in the audit trail")
    blocked = decision == ApprovalDecision.APPROVED and it.report_ok is False
    if blocked:
        md('<div class="wp-note">This item failed a check, so it can only be returned or rejected.</div>')
    if st.button(DECISION_VERBS[decision], key=f"record_{it.key}", disabled=blocked,
                 type="primary" if decision == ApprovalDecision.APPROVED else "secondary"):
        out = ctx.ds.save_approval(bundle.case.case_id, draft_id=it.draft_id, decision=decision,
                                   approver=ctx.viewer.actor, role=ctx.viewer.role, reason_code=reason,
                                   comment=comment.strip())
        flash(out.message, "success" if out.ok else "error")
        st.rerun()


def _bulk_approve(ds, case_id: str, draft_ids: list[str | None], actor: str, role: str) -> None:
    """Button callback: one approval per item; the confirmation box resets afterwards."""
    done, failed = 0, []
    for did in draft_ids:
        out = ds.save_approval(case_id, draft_id=did, decision=ApprovalDecision.APPROVED, approver=actor, role=role,
                               reason_code="CHECKS_PASSED", comment="Approved together after review")
        if out.ok:
            done += 1
        else:
            failed.append(out.message)
    st.session_state["bulk_confirm"] = False
    if failed:
        flash(f"Approved {done} of {len(draft_ids)} items. Not approved: {failed[0]}", "warning")
    else:
        flash(f"Approved {done} items. Each decision is recorded in the audit trail.", "success")


def _actions(bundle: CaseBundle) -> None:
    if not bundle.actions:
        return
    md(h.section("Actions queued by approvals"))
    rows = [h.Row([h.Cell(ist(a.created_at), nowrap=True), h.Cell(humanize(a.kind.value)),
                   h.Cell(html=h.chip(humanize(a.status.value), {"DONE": "good", "FAILED": "bad"}.get(
                       a.status.value, "warn"))),
                   h.Cell(str(a.payload.get("title") or a.payload.get("summary") or "—")),
                   h.Cell(a.result or "—")])
            for a in sorted(bundle.actions, key=lambda a: a.created_at, reverse=True)]
    md(h.table([("Queued", False), ("Action", False), ("Status", False), ("Subject", False), ("Result", False)],
               rows))


def _history(ctx: Ctx, bundle: CaseBundle) -> None:
    md(h.section("Decision history"))
    if not bundle.approvals:
        md('<div class="wp-note">No decisions recorded for this case yet.</div>')
        return
    titles = {d.draft_id: d.title for d in bundle.drafts}
    rows = []
    for a in sorted(bundle.approvals, key=lambda a: a.at, reverse=True):
        rows.append(h.Row([
            h.Cell(ist(a.at), nowrap=True),
            h.Cell(a.approver),
            h.Cell(a.role.title()),
            h.Cell(ctx.mask(titles.get(a.draft_id, "Hold plan" if a.draft_id is None else a.draft_id), bundle)),
            h.Cell(html=h.chip(APPROVAL_LABELS[a.decision], APPROVAL_TONE.get(a.decision, "gray"))),
            h.Cell(ALL_REASONS.get(a.reason_code, humanize(a.reason_code)) if a.reason_code else "—"),
            h.Cell(ctx.mask(a.comment, bundle) or "—"),
        ]))
    md(h.table([("When", False), ("Who", False), ("Role", False), ("Item", False), ("Decision", False),
                ("Reason", False), ("Note", False)], rows))
