"""Audit: every view, decision, pipeline run and role change, with actor and role, plus the status of each case's
hash-chained audit log (``wapsi.audit``) and a button that re-verifies it."""

from __future__ import annotations

import csv
import io
from typing import Any

import streamlit as st

from ui.components import html as h
from ui.components.formatting import ROLE_TITLES, audit_action_label, humanize, ist, reason_label
from ui.views.common import Ctx, download, flash, head, md
from wapsi.audit.chain import ChainReport, verify_chain
from wapsi.models import AuditEvent, CaseBundle
from wapsi.timeutil import utcnow

MAX_ROWS = 200
DECISION_ACTIONS = ("draft_", "plan_")
DECISION_OUTCOMES = ("_approved", "_rejected", "_changes_requested")
FINGERPRINT_LABELS = {"draft_sha256": "Fingerprint", "plan_sha256": "Plan fingerprint",
                      "trace_sha256": "Trace fingerprint"}
SESSION_CHAIN = "Session"
CHAIN_TONES = {"intact": "good", "broken": "bad", "unchained": "gray"}


def summarize_details(details: dict[str, Any], limit: int = 140) -> str:
    parts = []
    for k, v in details.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, dict):
            v = ", ".join(f"{a}={b}" for a, b in v.items())
        elif isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        if k in FINGERPRINT_LABELS and isinstance(v, str):
            parts.append(f"{FINGERPRINT_LABELS[k]}: {v[:10]}…")
            continue
        if k in ("reason_code", "kind") and isinstance(v, str):
            v = reason_label(v)
        elif k == "page" or k.endswith("_id") or k in ("from", "to"):
            v = str(v)
        elif isinstance(v, str) and v.isupper() and "_" in v:
            v = humanize(v)
        parts.append(f"{humanize(k)}: {v}")
    text = " · ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(ctx: Ctx) -> None:
    head("Audit trail", "Every view, decision and role change, with who did it and in which role.",
         eyebrow="Governance")
    events = ctx.ds.audit_events()
    bundles: dict[str, CaseBundle] = {}
    for cid, _ in events:
        if cid and cid not in bundles:
            b = ctx.ds.get_bundle(cid)
            if b is not None:
                bundles[cid] = b

    c1, c2, c3 = st.columns([1.2, 1.6, 1.6])
    with c1:
        scopes = ["All cases"] + (["This case"] if ctx.case_id else [])
        scope = st.radio("Scope", scopes, key="audit_scope", horizontal=True)
    if scope == "This case":
        events = [(cid, e) for cid, e in events
                  if cid == ctx.case_id or (cid is None and e.details.get("case_id") == ctx.case_id)]
    actions = sorted({e.action for _, e in events})
    roles = sorted({e.role for _, e in events})
    with c2:
        pick_actions = st.multiselect("Activity", actions, key="audit_actions", format_func=audit_action_label,
                                      placeholder="All activity")
    with c3:
        pick_roles = st.multiselect("Role", roles, key="audit_roles",
                                    format_func=lambda r: ROLE_TITLES.get(r, humanize(r)), placeholder="All roles")
    if pick_actions:
        events = [(c, e) for c, e in events if e.action in pick_actions]
    if pick_roles:
        events = [(c, e) for c, e in events if e.role in pick_roles]

    md(h.tiles([
        h.Tile("Events", str(len(events))),
        h.Tile("Decisions", str(sum(1 for _, e in events
                                    if e.action.startswith(DECISION_ACTIONS) and e.action.endswith(DECISION_OUTCOMES)))),
        h.Tile("Role switches", str(sum(1 for _, e in events if e.action == "role_switched"))),
        h.Tile("Page views", str(sum(1 for _, e in events if e.action == "viewed"))),
    ], min_width_rem=8))
    this_case = scope == "This case" and bool(ctx.case_id)
    _chain_panel(ctx, [ctx.case_id] if this_case else sorted(bundles), bundles, this_case=this_case)
    if ctx.viewer.masked:
        md(h.callout("Names and mobile numbers are masked for the auditor role.", strong="Masked view."))

    if not events:
        md(h.empty_state("Nothing recorded yet", "Views, approvals, draft runs and role switches appear here as "
                         "they happen.", glyph="≡"))
        return

    rows = []
    for cid, e in events[:MAX_ROWS]:
        b = bundles.get(cid) if cid else None
        details = ctx.mask(summarize_details(e.details), b) if b else ctx.viewer.text(summarize_details(e.details))
        rows.append(h.Row([
            h.Cell(ist(e.at), nowrap=True),
            h.Cell(e.actor),
            h.Cell(ROLE_TITLES.get(e.role, humanize(e.role))),
            h.Cell(audit_action_label(e.action)),
            h.Cell(html=_object(e, cid)),
            h.Cell(details or "—"),
        ]))
    shown = min(len(events), MAX_ROWS)
    md(h.table([("When (IST)", False), ("Actor", False), ("Role", False), ("Activity", False), ("Object", False),
                ("Details", False)], rows, caption=f"Showing {shown} of {len(events)} events, newest first"))
    download("Download audit CSV", _csv(ctx, events, bundles), "wapsi-audit.csv", key="audit_csv", mime="text/csv")


def _object(e: AuditEvent, cid: str | None) -> str:
    """'Page · Hold Plan' or 'Draft · <id>' (ids in monospace), plus the case when it differs."""
    kind = humanize(e.object_type)
    ident = e.object_id or ""
    looks_like_id = any(ch.isdigit() for ch in ident) or ident.isupper()
    out = h.esc(kind) + (" · " + (h.mono(ident) if looks_like_id else h.esc(ident)) if ident else "")
    if cid and ident != cid:
        out += f'<div class="wp-note wp-mono">{h.esc(cid)}</div>'
    return out


def _csv(ctx: Ctx, events: list[tuple[str | None, AuditEvent]], bundles: dict[str, CaseBundle]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["at_utc", "actor", "role", "action", "object_type", "object_id", "case_id", "details"])
    for cid, e in events:
        b = bundles.get(cid) if cid else None
        details = summarize_details(e.details, limit=10_000)
        details = ctx.mask(details, b) if b else ctx.viewer.text(details)
        w.writerow([e.at.isoformat(), e.actor, e.role, e.action, e.object_type, e.object_id, cid or "", details])
    return buf.getvalue()


# --------------------------------------------------------------------------- audit chain


def chain_report(ctx: Ctx, case_id: str | None, bundle: CaseBundle | None = None) -> ChainReport:
    """Verify one case's audit chain (``None``: the session's own events, e.g. role switches)."""
    verify = getattr(ctx.ds, "verify_audit", None)
    if callable(verify):
        return verify(case_id)
    return verify_chain(bundle.audit if bundle is not None else [], case_id=case_id)


def chain_chip(label: str, report: ChainReport) -> str:
    """Small status badge for one audit chain; the tooltip carries the full finding."""
    if report.status == "intact":
        text = f"{label} · chain intact · {report.length} event{'s' if report.length != 1 else ''}"
    elif report.status == "unchained":
        text = f"{label} · not chained"
    else:
        text = f"{label} · chain broken at event #{report.first_bad_seq}"
    return h.chip(text, CHAIN_TONES[report.status], title=report.summary())


def _chain_panel(ctx: Ctx, case_ids: list[str], bundles: dict[str, CaseBundle], *, this_case: bool) -> None:
    targets: list[tuple[str, str | None]] = [(cid, cid) for cid in case_ids if cid in bundles]
    if not this_case and any(c is None for c, _ in ctx.ds.audit_events()):
        targets.append((SESSION_CHAIN, None))
    if not targets:
        return
    reports = [(label, chain_report(ctx, cid, bundles.get(cid) if cid else None)) for label, cid in targets]
    c1, c2 = st.columns([4, 1.2], vertical_alignment="center")
    with c1:
        md('<div class="wp-note">Hash-chained audit log: editing, deleting or reordering a recorded event breaks '
           "the chain.</div>" + h.chips([chain_chip(label, r) for label, r in reports]))
    with c2:
        st.button("Verify chain", key="audit_verify_chain", on_click=_verify_chains, args=(ctx, targets, this_case),
                  help="Recompute every event's hash from the first event and check each link.")


def _verify_chains(ctx: Ctx, targets: list[tuple[str, str | None]], this_case: bool) -> None:
    """Button callback: verify each chain, report the result and record the check itself in the audit trail."""
    reports = [(label, chain_report(ctx, cid, ctx.ds.get_bundle(cid) if cid else None)) for label, cid in targets]
    broken = [(label, r) for label, r in reports if r.status == "broken"]
    intact = [r for _, r in reports if r.ok]
    unchained = [label for label, r in reports if r.status == "unchained"]
    events = sum(r.length for r in intact)
    note = (f" Not chained yet (their store does not seal events): {', '.join(unchained)}." if unchained else "")
    if broken:
        flash(" ".join(f"{label}: {r.summary()}" for label, r in broken) + note, "error")
    elif intact:
        flash(f"Verified {events} events in {len(intact)} chain{'s' if len(intact) != 1 else ''}: every hash "
              "and link checks out." + note, "info" if unchained else "success")
    else:
        flash(note.strip(), "info")
    ctx.ds.append_audit(None, AuditEvent(
        at=utcnow(), actor=ctx.viewer.actor, role=ctx.viewer.role, action="audit_chain_verified",
        object_type="audit_log", object_id=ctx.case_id if this_case and ctx.case_id else "all cases",
        details={"chains": len(reports), "events": events,
                 "broken": [f"{label}@{r.first_bad_seq}" for label, r in broken], "not_chained": unchained},
    ))
