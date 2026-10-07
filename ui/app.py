"""Wapsi · Money-trail copilot — the nodal officer's case room.

Run locally:   uv run streamlit run ui/app.py
The officer lands on the Inbox, never on a chatbot.

Data source: the showcase world by default. ``WAPSI_DATA_SOURCE=demo`` (a world under ``data/generated/``) or
``WAPSI_DATA_SOURCE=<path to a parquet world>`` shows a synthetic world instead (its first ``WAPSI_UI_MAX_CASES``
cases, default 12, run through the live pipeline); the sidebar's Data switch changes it for the session. Editions
that seed ``st.session_state["_wapsi_ds"]`` with their own data source (GCP, AMD) keep it and get no switch.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:  # installed (uv) or on the path already
    import wapsi  # noqa: F401
except ImportError:  # plain checkout without install
    sys.path.insert(0, str(_ROOT / "src"))

import streamlit as st  # noqa: E402

st.set_page_config(
    page_title="Wapsi · Money-trail copilot",
    page_icon="↩️",
    layout="wide",
    initial_sidebar_state="auto",
)

from ui.components import html as h  # noqa: E402
from ui.components.formatting import (  # noqa: E402
    ROLE_ACTORS,
    ROLE_BLURBS,
    ROLE_TITLES,
    ROLES,
    Viewer,
    inr_short,
)
from ui.components.theme import detect_dark, inject_css  # noqa: E402
from ui.data_source import (  # noqa: E402
    SHOWCASE_SOURCE,
    SOURCE_MIXED,
    SOURCE_PIPELINE,
    SOURCE_WORLD,
    ShowcaseDataSource,
    SourceSpec,
    available_sources,
    engine_status,
    latest_approval,
    load_source,
    now_for_ui,
    resolve_source,
)
from ui.views import approvals, audit, case_room, clocks, drafts, hold_plan, inbox, kpis, radar  # noqa: E402
from ui.views.common import (  # noqa: E402
    CASE_PAGES,
    K_CASE,
    K_PAGE,
    K_ROLE,
    PAGES,
    Ctx,
    flash,
    md,
    show_flash,
)
from wapsi.models import ApprovalDecision, AuditEvent  # noqa: E402
from wapsi.timeutil import utcnow  # noqa: E402

log = logging.getLogger("wapsi.ui")

RENDERERS = {
    "Inbox": inbox.render,
    "Case Room": case_room.render,
    "Hold Plan": hold_plan.render,
    "Drafts & Verification": drafts.render,
    "Approvals": approvals.render,
    "Clocks": clocks.render,
    "Regulatory Radar": radar.render,
    "KPIs": kpis.render,
    "Audit": audit.render,
}
DS_KEY = "_wapsi_ds"
K_SOURCE = "data_source"  # the sidebar's Data switch (a SourceSpec key)
_OWN_SOURCE = "_wapsi_ds_spec"  # the SourceSpec this app built the data source from (unset: an edition seeded it)


@st.cache_resource(show_spinner="Tracing the cases…")
def _seed(source_key: str, live: bool):
    """Bundles + ledger of a data source, built once per server process (read-only; sessions deep-copy the
    bundles)."""
    return load_source(resolve_source(source_key), live=live)


def _selected_source() -> SourceSpec:
    """The sidebar's Data switch, else ``WAPSI_DATA_SOURCE``, else the showcase world."""
    try:
        return resolve_source(st.session_state.get(K_SOURCE))
    except ValueError as exc:
        log.warning("%s; showing the showcase world", exc)
        flash(f"{exc}. Showing the showcase world instead.", "warning")
        return SHOWCASE_SOURCE


def data_source() -> ShowcaseDataSource:
    ds = st.session_state.get(DS_KEY)
    if ds is None:
        live = os.environ.get("WAPSI_UI_SOURCE", "pipeline").strip().lower() != "fixtures"
        spec = _selected_source()
        bundles, ledger, source = _seed(spec.key, live)
        ds = ShowcaseDataSource(bundles=[b.model_copy(deep=True) for b in bundles], ledger=ledger, source=source,
                                label=spec.label)
        st.session_state[DS_KEY] = ds
        st.session_state[_OWN_SOURCE] = spec
        st.session_state[K_SOURCE] = spec.key
    return ds


def _on_source_change() -> None:
    """Another data source: this session's cases (and the decisions recorded on them) are dropped."""
    for key in (DS_KEY, K_CASE, "_last_view"):
        st.session_state.pop(key, None)


def _source_switch() -> None:
    current: SourceSpec | None = st.session_state.get(_OWN_SOURCE)
    if current is None:
        return  # an edition seeded its own data source
    options = available_sources(current)
    if len(options) < 2:
        return
    labels = {s.key: s.label for s in options}
    st.selectbox("Data", list(labels), key=K_SOURCE, format_func=lambda k: labels.get(k, k),
                 on_change=_on_source_change,
                 help="Which synthetic cases to show. Switching starts this session afresh on that data.")


def _pending(ds: ShowcaseDataSource, case_id: str | None) -> int:
    b = ds.get_bundle(case_id) if case_id else None
    if b is None:
        return 0
    ids = [d.draft_id for d in b.drafts] + ([None] if b.plan is not None else [])
    return sum(1 for i in ids if (a := latest_approval(b, i)) is None or a.decision != ApprovalDecision.APPROVED)


def _on_role_change() -> None:
    ds: ShowcaseDataSource | None = st.session_state.get(DS_KEY)
    new = st.session_state.get(K_ROLE, "analyst")
    prev = st.session_state.get("_prev_role")
    st.session_state["_prev_role"] = new
    if ds is not None and new != prev:
        ds.append_audit(None, AuditEvent(at=utcnow(), actor=ROLE_ACTORS.get(new, new), role=new,
                                         action="role_switched", object_type="session", object_id="role",
                                         details={"from": ROLE_TITLES.get(prev or "", prev or ""),
                                                  "to": ROLE_TITLES.get(new, new)}))


def _init_state(ds: ShowcaseDataSource) -> list[str]:
    ss = st.session_state
    if "_deep_link_done" not in ss:  # ?page=case-room&case=CASE-...&role=supervisor (first run only)
        ss["_deep_link_done"] = True
        try:
            qp = st.query_params
            slug = {p.lower().replace(" & ", "-").replace(" ", "-"): p for p in PAGES}
            if qp.get("page") in slug:
                ss[K_PAGE] = slug[qp.get("page")]
            if qp.get("case"):
                ss[K_CASE] = qp.get("case")
            if qp.get("role") in ROLES:
                ss[K_ROLE] = qp.get("role")
        except Exception:  # noqa: BLE001 - deep links are a convenience only
            pass
    ss.setdefault(K_PAGE, "Inbox")
    if ss[K_PAGE] not in PAGES:
        ss[K_PAGE] = "Inbox"
    ss.setdefault(K_ROLE, "analyst")
    if ss[K_ROLE] not in ROLES:
        ss[K_ROLE] = "analyst"
    ss.setdefault("_prev_role", ss[K_ROLE])

    def recoverable(cid: str) -> float:
        b = ds.get_bundle(cid)
        return b.plan.total_recoverable if b is not None and b.plan is not None else 0.0

    case_ids = sorted((c.case_id for c in ds.list_cases()), key=lambda c: -recoverable(c))
    if ss.get(K_CASE) not in case_ids:
        ss[K_CASE] = case_ids[0] if case_ids else None
    return case_ids


def _case_label(ds: ShowcaseDataSource, viewer: Viewer, case_id: str) -> str:
    b = ds.get_bundle(case_id)
    if b is None:
        return case_id
    names = [viewer.name(c.victim_name) for c in b.complaints]
    who = names[0] if len(names) == 1 else f"{names[0]} +{len(names) - 1}" if names else case_id
    amount = inr_short(b.plan.total_recoverable) if b.plan else "not planned"
    return f"{who} · {amount}"


def _sidebar(ds: ShowcaseDataSource, viewer: Viewer, case_ids: list[str]) -> None:
    md('<div class="wp-brand"><div class="wp-brand-mark" aria-hidden="true">₹</div><div>'
       '<div class="wp-brand-name">Wapsi</div><div class="wp-brand-sub">Money-trail copilot</div></div></div>')
    if case_ids:
        st.selectbox("Case", case_ids, key=K_CASE, format_func=lambda c: _case_label(ds, viewer, c),
                     help="The case the case-level pages show. Sorted by money still recoverable.")
    else:
        md('<div class="wp-note">No cases yet.</div>')

    pending = _pending(ds, st.session_state.get(K_CASE))
    b = ds.get_bundle(st.session_state.get(K_CASE) or "")
    n_drafts = len(b.drafts) if b else 0

    def nav_label(page: str) -> str:
        if page == "Approvals" and pending:
            return f"{page} · {pending} pending"
        if page == "Drafts & Verification" and n_drafts:
            return f"{page} · {n_drafts}"
        return page

    st.radio("Go to", PAGES, key=K_PAGE, format_func=nav_label)

    st.divider()
    st.selectbox("Signed in as", ROLES, key=K_ROLE, format_func=lambda r: ROLE_TITLES[r],
                 on_change=_on_role_change,
                 help="Demo role switcher. Supervisors approve; auditors see masked personal details.")
    md(f'<div class="wp-foot"><span class="wp-mono">{h.esc(viewer.actor)}</span><br>'
       f"{h.esc(ROLE_BLURBS.get(viewer.role, ''))}</div>")
    _source_switch()
    status = engine_status()
    n = len(case_ids)
    engine = {
        SOURCE_PIPELINE: "Showcase ledger · processed by the live pipeline",
        SOURCE_MIXED: "Showcase ledger · some cases from saved fixtures",
        SOURCE_WORLD: f"{getattr(ds, 'label', None) or 'Synthetic world'} · {n} case{'s' if n != 1 else ''} "
                      "processed by the live pipeline",
    }.get(getattr(ds, "source", ""),
          "Showcase fixtures" if status.pipeline_ready else "Showcase fixtures · drafting engine not installed yet")
    md(f'<div class="wp-foot" style="margin-top:1rem" title="{h.esc(", ".join(status.missing) or "all modules present")}">'
       f"{h.esc(engine)}<br>All data is synthetic. Times in IST.</div>")


def _log_view(ds: ShowcaseDataSource, ctx: Ctx, page: str) -> None:
    case_id = ctx.case_id if page in CASE_PAGES else None
    key = (page, case_id, ctx.viewer.role)
    if st.session_state.get("_last_view") == key:
        return
    st.session_state["_last_view"] = key
    ds.append_audit(case_id, AuditEvent(at=utcnow(), actor=ctx.viewer.actor, role=ctx.viewer.role,
                                        action="viewed", object_type="page", object_id=page,
                                        details={"case_id": case_id} if case_id else {}))


def _error_state(page: str, exc: BaseException) -> None:
    log.exception("page %s failed", page)
    md(h.empty_state("This page couldn't load",
                     "Something went wrong while building it. Your data is unchanged — try again or open another "
                     "page.", glyph="!"))
    _, mid, _ = st.columns([1, 1, 1])
    with mid:
        st.button("Try again", key="page_retry", type="primary")
    with st.expander("Technical details"):
        st.code("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:])


def main() -> None:
    inject_css(detect_dark())
    try:
        ds = data_source()
        case_ids = _init_state(ds)
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("WAPSI_UI_RAISE"):
            raise
        _error_state("startup", exc)
        return
    viewer = Viewer(st.session_state[K_ROLE])
    with st.sidebar:
        _sidebar(ds, viewer, case_ids)
    page = st.session_state[K_PAGE]
    case_id = st.session_state.get(K_CASE)
    ctx = Ctx(ds=ds, viewer=viewer, case_id=case_id, bundle=ds.get_bundle(case_id) if case_id else None,
              now=now_for_ui(), dark=detect_dark(), engine=engine_status())
    _log_view(ds, ctx, page)
    show_flash()
    try:
        RENDERERS[page](ctx)
    except Exception as exc:  # noqa: BLE001 - every page gets an explicit error state
        if os.environ.get("WAPSI_UI_RAISE"):
            raise
        _error_state(page, exc)


main()
