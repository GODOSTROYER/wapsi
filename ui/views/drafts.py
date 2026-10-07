"""Drafts & Verification: each draft with its check report above it; failing spans highlighted."""

from __future__ import annotations

import json
import re
from collections import Counter

import streamlit as st

from ui.components import html as h
from ui.components.formatting import (
    APPROVAL_LABELS,
    CHECK_LABELS,
    CHECK_ORDER,
    DRAFT_KIND_LABELS,
    DRAFT_KIND_ORDER,
    PROCESS_LABELS,
    SIMULATED_CFCFRMS_NOTE,
    humanize,
    ist,
)
from ui.components.theme import APPROVAL_TONE
from ui.data_source import latest_approval
from ui.views.common import (
    Ctx,
    case_bar,
    citation_title,
    download,
    generate_button,
    head,
    md,
    nav_button,
    need_case,
)
from wapsi.models import (
    CaseBundle,
    CheckResult,
    Disposition,
    Draft,
    DraftKind,
    HoldPlan,
    Role,
    VerificationReport,
)

MSG_CODE = re.compile(r"^[a-z][a-z0-9_]*:\s*")
CFCFRMS_KINDS = {DraftKind.CFCFRMS_UPDATE, DraftKind.OUTBOUND_REQUEST}

CHECK_HINTS = {
    "numeric": "Every amount, account and UTR matches the case tables",
    "citations": "Every citation resolves to a known SOP / statute section",
    "policy": "Hold rules: victim money only, no pool holds, forward holds on layer 1",
    "injection": "No instructions from complaint text made it into the draft",
}


def expected_drafts(plan: HoldPlan | None) -> list[tuple[DraftKind, int]]:
    """What the drafter will produce for this plan (docs/draft-contract.md, 'Draft kinds per decision')."""
    if plan is None:
        return []
    c: Counter[DraftKind] = Counter()
    holding = {Disposition.HOLD_AMOUNT, Disposition.HOLD_AMOUNT_NO_ESCALATION, Disposition.HOLD_IN_POOL,
               Disposition.FORWARD_HOLD}
    for d in plan.decisions:
        if d.disposition in holding:
            c[DraftKind.HOLD_INSTRUCTION] += 1
            c[DraftKind.CUSTOMER_NOTICE] += 1
        elif d.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY:
            c[DraftKind.HOLD_INSTRUCTION] += 1
        if d.disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION:
            c[DraftKind.EDD_MEMO] += 1
        if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST:
            c[DraftKind.OUTBOUND_REQUEST] += 1
    if any(d.role == Role.MULE and (d.hold_amount > 0 or d.forward_hold_amount > 0) for d in plan.decisions):
        c[DraftKind.STR] += 1
    c[DraftKind.CFCFRMS_UPDATE] += 1
    c[DraftKind.RESTORATION_PACK] += 1
    return [(k, c[k]) for k in DRAFT_KIND_ORDER if c[k]]


def failing_spans(report: VerificationReport | None) -> list[str]:
    """Spans named by failed error-level checks (highlighted red)."""
    if report is None:
        return []
    return [s for c in report.checks if not c.passed and c.severity != "warning" for s in c.spans]


def warning_spans(report: VerificationReport | None) -> list[str]:
    """Spans named by warning-level checks (highlighted amber)."""
    if report is None:
        return []
    return [s for c in report.checks if not c.passed and c.severity == "warning" for s in c.spans]


HEADING = re.compile(r"^(#{1,6})[ \t]", re.MULTILINE)


def demote_headings(markdown: str, by: int = 3) -> str:
    """'# Title' -> '#### Title' so a draft's own headings sit below the page's headings."""
    return HEADING.sub(lambda m: "#" * min(6, len(m.group(1)) + by) + " ", markdown)


def clean_message(message: str) -> str:
    """'ok: 12 slot(s) recomputed' -> '12 slot(s) recomputed'; drops machine codes before the first colon."""
    text = MSG_CODE.sub("", message or "").strip()
    return text[:1].upper() + text[1:] if text else ""


def check_message(check: CheckResult) -> str:
    """A check's message for display: the plain message when the check carries its ``code``; a report stored
    before the field existed keeps the code as a message prefix, which is dropped."""
    if not check.code:
        return clean_message(check.message)
    text = (check.message or "").strip()
    return text[:1].upper() + text[1:] if text else ""


def render(ctx: Ctx) -> None:
    head("Drafts & verification", "Review each draft after its checks. Nothing is final until a supervisor "
         "approves it.", eyebrow="Recommended — pending approval")
    if not need_case(ctx):
        return
    bundle = ctx.bundle
    case_bar(ctx, bundle)

    if not bundle.drafts:
        _empty(ctx, bundle)
        return

    reports = bundle.verifications
    passed = sum(1 for d in bundle.drafts if reports.get(d.draft_id) and reports[d.draft_id].passed)
    failed = sum(1 for d in bundle.drafts if reports.get(d.draft_id) and not reports[d.draft_id].passed)
    unchecked = len(bundle.drafts) - passed - failed
    summary = [h.chip(f"{len(bundle.drafts)} drafts", "gray"), h.chip(f"{passed} passed every check", "good")]
    if failed:
        summary.append(h.chip(f"{failed} need attention", "bad"))
    if unchecked:
        summary.append(h.chip(f"{unchecked} not checked yet", "warn"))
    c1, c2 = st.columns([4, 1.3], vertical_alignment="center")
    with c1:
        md(h.chips(summary))
    with c2:
        if ctx.viewer.can_approve:
            nav_button("Go to approvals", "Approvals", key="drafts_to_approvals", primary=True)

    kinds = [k for k in DRAFT_KIND_ORDER if any(d.kind == k for d in bundle.drafts)]
    kinds += sorted({d.kind for d in bundle.drafts} - set(kinds))
    labels = []
    for k in kinds:
        ds = [d for d in bundle.drafts if d.kind == k]
        bad = sum(1 for d in ds if reports.get(d.draft_id) is not None and not reports[d.draft_id].passed)
        lab = DRAFT_KIND_LABELS.get(k, humanize(k.value))
        lab += f" ({len(ds)})" if len(ds) > 1 else ""
        lab += " · needs attention" if bad else ""
        labels.append(lab)
    for tab, kind in zip(st.tabs(labels), kinds, strict=True):
        with tab:
            ds = sorted((d for d in bundle.drafts if d.kind == kind), key=lambda d: (d.account_id or "", d.title))
            draft = ds[0]
            if len(ds) > 1:
                ids = [d.draft_id for d in ds]
                by_id = {d.draft_id: d for d in ds}
                pick = st.radio("Draft", ids, key=f"draft_pick_{kind.value}", horizontal=True,
                                format_func=lambda i, by_id=by_id: _draft_option(by_id[i]),
                                label_visibility="collapsed")
                draft = by_id.get(pick, ds[0])
            _panel(ctx, bundle, draft)

    with st.expander("Regenerate drafts"):
        md('<div class="wp-note">Drafts are rebuilt from the current hold plan and checked again. '
           "Earlier approvals stay in the audit trail.</div>")
        generate_button(ctx, key="drafts_regenerate", label="Regenerate drafts")


def _draft_option(d: Draft) -> str:
    return d.account_id or d.title


def _empty(ctx: Ctx, bundle: CaseBundle) -> None:
    exp = expected_drafts(bundle.plan)
    body = ("Wapsi fills every amount, account number, UTR and date from the hold plan, lets the language model "
            "write only the prose, and checks each draft before it reaches a supervisor.")
    md(h.empty_state("Drafts not generated yet", body, glyph="✎"))
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        generate_button(ctx, key="drafts_generate")
    if exp:
        md(h.section("What will be drafted for this plan"))
        items = []
        for kind, n in exp:
            label = DRAFT_KIND_LABELS.get(kind, humanize(kind.value))
            if kind == DraftKind.RESTORATION_PACK and bundle.plan is not None:
                label += f" — {PROCESS_LABELS.get(bundle.plan.restoration_process, '')}"
            items.append(f"{n} × {label}" if n > 1 else label)
        md(h.bullet_list(items))
    md(h.section("Checks each draft must pass"))
    md(_checks_html(None))


def _checks_html(report: VerificationReport | None) -> str:
    grouped: dict[str, list[CheckResult]] = {}
    for c in report.checks if report else []:
        grouped.setdefault(c.name, []).append(c)
    names = CHECK_ORDER + [n for n in grouped if n not in CHECK_ORDER]
    cells = []
    for name in names:
        results = grouped.get(name, [])
        errors = [c for c in results if not c.passed and c.severity != "warning"]
        warnings = [c for c in results if not c.passed and c.severity == "warning"]
        if report is None:
            tone, state, msg, cls = "gray", "Pending", CHECK_HINTS.get(name, ""), "wp-check"
        elif not results:
            tone, state, msg, cls = "gray", "Not run", CHECK_HINTS.get(name, ""), "wp-check"
        elif errors:
            tone, state, cls = "bad", "Failed", "wp-check wp-check-fail"
            msg = " · ".join(check_message(c) for c in errors if c.message) or "See highlighted text"
        elif warnings:
            tone, state, cls = "warn", "Passed with warnings", "wp-check wp-check-warn"
            msg = " · ".join(check_message(c) for c in warnings if c.message)
        else:
            tone, state, cls = "good", "Passed", "wp-check wp-check-pass"
            msg = " · ".join(check_message(c) for c in results if c.message) or CHECK_HINTS.get(name, "")
        cells.append(
            f'<div class="{cls}" role="listitem"><div class="wp-check-h">{h.icon(tone)}'
            f"{h.esc(CHECK_LABELS.get(name, humanize(name)))}"
            f'<span class="wp-note" style="margin-left:auto;font-weight:500">{h.esc(state)}</span></div>'
            f'<div class="wp-check-m">{h.esc(msg)}</div></div>'
        )
    return f'<div class="wp-checks" role="list">{"".join(cells)}</div>'


def _panel(ctx: Ctx, bundle: CaseBundle, draft: Draft) -> None:
    report = bundle.verifications.get(draft.draft_id)
    appr = latest_approval(bundle, draft.draft_id)
    chips_ = []
    if report is None:
        chips_.append(h.chip("Not checked yet", "warn"))
    elif report.passed:
        chips_.append(h.chip("Ready for approval", "good"))
    else:
        chips_.append(h.chip("Blocked: failed checks", "bad"))
    chips_.append(h.chip(APPROVAL_LABELS[appr.decision] if appr else "Pending approval",
                         APPROVAL_TONE.get(appr.decision if appr else None, "gray")))
    if draft.account_id:
        chips_.append(h.chip(draft.account_id, "gray"))
    md(f'<div class="wp-row" style="margin:.4rem 0 .3rem"><span class="wp-row-title">'
       f"{h.esc(ctx.mask(draft.title, bundle))}</span>"
       f'<span class="wp-note">v{draft.version} · {h.esc(ist(draft.created_at))}</span></div>' + h.chips(chips_))
    if draft.kind in CFCFRMS_KINDS:
        md(h.callout(SIMULATED_CFCFRMS_NOTE, strong="Not sent."))
    elif draft.kind == DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST:
        md(h.callout("Amount-specific holds are the default. This officer's request applies an account-level hold "
                     "only after a supervisor other than the requester approves it; the plan changes then, and the "
                     "account's hold instruction and the case's CFCFRMS update are redrafted for a fresh approval.",
                     strong="Recorded exception."))

    md(h.section("Checks"))
    md(_checks_html(report))
    if report is not None:
        md(f'<div class="wp-note">Checked {h.esc(ist(report.verified_at))}. Hover highlighted text in plain view '
           "to see why it was flagged.</div>")

    errors = [ctx.mask(s, bundle) for s in failing_spans(report)]
    warnings = [ctx.mask(s, bundle) for s in warning_spans(report)]
    body = ctx.mask(draft.body, bundle)
    c1, c2 = st.columns([3, 1.4], vertical_alignment="bottom")
    with c1:
        md(h.section("Draft"))
    with c2:
        views = ["Formatted", "Plain text with highlights"]
        view = st.radio("View", views, index=1 if errors else 0, key=f"view_{draft.draft_id}", horizontal=True,
                        label_visibility="collapsed")
    if view == views[0]:
        with st.container(border=True):
            st.markdown(demote_headings(body))
    else:
        md(h.document(body, errors, warnings))
    prose = (f"Prose by {draft.llm_model}; " if draft.llm_model else "") + "every number is filled from case tables."
    md(f'<div class="wp-note">{h.esc(prose)}</div>')

    if draft.citation_ids:
        md(h.section("Citations"))
        rows = [h.Row([h.Cell(html=h.cite(c)), h.Cell(citation_title(c)),
                       h.Cell(html=h.chip("Cited in text", "gray") if f"[{c}]" in draft.body else "—")])
                for c in draft.citation_ids]
        md(h.table([("Citation", False), ("Section", False), ("", False)], rows))

    c1, c2 = st.columns([1, 1])
    with c1:
        if draft.slots:
            with st.expander(f"Values filled from case tables ({len(draft.slots)})"):
                rows = [h.Row([h.Cell(html=h.mono(k)), h.Cell(ctx.mask(v, bundle))])
                        for k, v in sorted(draft.slots.items())]
                md(h.table([("Slot", False), ("Value", False)], rows))
    with c2:
        if draft.structured:
            label = "FINnet-style sheets" if draft.kind == DraftKind.STR else "Structured fields"
            with st.expander(label):
                if ctx.viewer.masked:
                    st.code(ctx.mask(json.dumps(draft.structured, indent=2, ensure_ascii=False, default=str),
                                     bundle), language="json")
                else:
                    st.json(json.loads(json.dumps(draft.structured, default=str)), expanded=False)
    download("Download draft (.md)", body, f"{draft.draft_id}.md", key=f"dl_{draft.draft_id}",
             mime="text/markdown")
