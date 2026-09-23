"""Audit: every view, decision, pipeline run and role change, with actor and role."""

from __future__ import annotations

import csv
import io
from typing import Any

import streamlit as st

from ui.components import html as h
from ui.components.formatting import ROLE_TITLES, audit_action_label, humanize, ist, reason_label
from ui.views.common import Ctx, download, head, md
from wapsi.models import AuditEvent, CaseBundle

MAX_ROWS = 200
DECISION_ACTIONS = ("draft_", "plan_")


def summarize_details(details: dict[str, Any], limit: int = 140) -> str:
    parts = []
    for k, v in details.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, dict):
            v = ", ".join(f"{a}={b}" for a, b in v.items())
        elif isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
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
        h.Tile("Decisions", str(sum(1 for _, e in events if e.action.startswith(DECISION_ACTIONS)))),
        h.Tile("Role switches", str(sum(1 for _, e in events if e.action == "role_switched"))),
        h.Tile("Page views", str(sum(1 for _, e in events if e.action == "viewed"))),
    ], min_width_rem=8))
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
