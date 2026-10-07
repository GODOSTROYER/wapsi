"""Shared view plumbing: per-run context, navigation callbacks, headers, chips and flash messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

import streamlit as st

from ui.components import html as h
from ui.components.compat import optional, stretch
from ui.components.formatting import (
    CASE_STATUS_LABELS,
    CLOCK_KIND_LABELS,
    Viewer,
    inr_short,
    relative_due,
)
from ui.components.theme import CASE_STATUS_TONE, CLOCK_STATUS_TONE
from ui.data_source import EngineStatus, ShowcaseDataSource, clock_status, doc_search
from wapsi.citations import registry
from wapsi.models import CaseBundle, Citation, ClockKind, ClockStatus

PAGES: list[str] = ["Inbox", "Voice Intake", "Case Room", "Hold Plan", "Drafts & Verification", "Approvals",
                    "Clocks", "Regulatory Radar", "KPIs", "Audit"]
CASE_PAGES = {"Case Room", "Hold Plan", "Drafts & Verification", "Approvals", "Clocks", "Regulatory Radar"}

K_PAGE, K_CASE, K_ROLE, K_ACCOUNT, K_FLASH = "page", "case_id", "role", "inspect_account", "_flash"


@dataclass
class Ctx:
    ds: ShowcaseDataSource
    viewer: Viewer
    case_id: str | None
    bundle: CaseBundle | None
    now: datetime
    dark: bool | None
    engine: EngineStatus

    @property
    def is_dark(self) -> bool:
        return bool(self.dark)

    def pii(self, bundle: CaseBundle | None = None) -> tuple[list[str], list[str]]:
        """Known names and mobiles for this case (used to mask free text for auditors)."""
        b = bundle or self.bundle
        names, mobiles = [], []
        if b is None:
            return names, mobiles
        for c in b.complaints:
            names.append(c.victim_name)
            mobiles.append(c.victim_mobile)
        for cust in customers_for(self, b).values():
            names.append(cust.name)
            mobiles.append(cust.mobile)
        return names, mobiles

    def mask(self, text: str, bundle: CaseBundle | None = None) -> str:
        if not self.viewer.masked:
            return text
        names, mobiles = self.pii(bundle)
        return self.viewer.text(text, names, mobiles)


# --------------------------------------------------------------------------- ledger lookups


def customers_for(ctx: Ctx, bundle: CaseBundle) -> dict[str, object]:
    """account_id -> Customer (holder) for trail accounts, when the ledger knows them."""
    ledger = ctx.ds.ledger()
    if ledger is None or bundle.trace is None:
        return {}
    try:
        accts = ledger.accounts(bundle.trace.accounts.keys())
        cust = ledger.customers({a.customer_id for a in accts.values() if a.customer_id})
    except Exception:  # noqa: BLE001 - enrichment is optional
        return {}
    return {aid: cust[a.customer_id] for aid, a in accts.items() if a.customer_id in cust}


def display_names(ctx: Ctx, bundle: CaseBundle) -> dict[str, str]:
    """account_id -> holder / victim name as this viewer may see it."""
    out = {aid: ctx.viewer.name(c.name) for aid, c in customers_for(ctx, bundle).items()}
    for c in bundle.complaints:
        out[c.victim_account] = ctx.viewer.name(c.victim_name)
    return out


def bank_names(ctx: Ctx) -> dict[str, str]:
    ledger = ctx.ds.ledger()
    try:
        return {k: b.name for k, b in ledger.banks().items()} if ledger is not None else {}
    except Exception:  # noqa: BLE001
        return {}


@lru_cache(maxsize=1)
def citations() -> dict[str, Citation]:
    """Canonical registry; quotes filled from the local docs index where the registry has none."""
    reg = registry()
    search = doc_search()
    if search is None:
        return reg
    for cid, c in reg.items():
        if c.quote:
            continue
        try:
            found = search.citation(cid)
        except Exception:  # noqa: BLE001
            found = None
        if found is not None and found.quote:
            reg[cid] = c.model_copy(update={"quote": found.quote, "page": found.page or c.page})
    return reg


def citation_title(cid: str) -> str:
    c = citations().get(cid)
    return c.section if c else "Unknown citation"


# --------------------------------------------------------------------------- output helpers


def md(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def head(title: str, subtitle: str = "", eyebrow: str = "") -> None:
    md(h.page_head(title, subtitle, eyebrow))


def flash(message: str, kind: str = "success") -> None:
    st.session_state[K_FLASH] = (kind, message)


def show_flash() -> None:
    item = st.session_state.pop(K_FLASH, None)
    if not item:
        return
    kind, message = item
    {"success": st.success, "info": st.info, "warning": st.warning, "error": st.error}.get(kind, st.info)(message)


def go(page: str, case_id: str | None = None) -> None:
    """Navigation callback (runs before widgets are rebuilt, so widget keys may be set)."""
    if case_id is not None:
        st.session_state[K_CASE] = case_id
    st.session_state[K_PAGE] = page


def nav_button(label: str, page: str, *, case_id: str | None = None, key: str, primary: bool = False,
               help: str | None = None) -> None:
    st.button(label, key=key, on_click=go, args=(page, case_id), type="primary" if primary else "secondary",
              help=help, **stretch(st.button))


def victims_text(ctx: Ctx, bundle: CaseBundle) -> str:
    names = [ctx.viewer.name(c.victim_name) for c in bundle.complaints]
    if not names:
        return "No complaint attached"
    return names[0] if len(names) == 1 else f"{names[0]} + {len(names) - 1} more"


def first_layer(bundle: CaseBundle) -> list[str]:
    if bundle.trace is None:
        return []
    return sorted(a for a, s in bundle.trace.accounts.items() if s.layer == 1)


def status_chip(bundle: CaseBundle) -> str:
    st_ = bundle.case.status
    return h.chip(CASE_STATUS_LABELS.get(st_, st_.value), CASE_STATUS_TONE.get(st_, "gray"))


def sla_chips(ctx: Ctx, bundle: CaseBundle) -> list[str]:
    """STR-due and hold-lapse chips; a neutral chip when clocks haven't started."""
    clocks = ctx.ds.clocks(bundle, ctx.now)
    if not clocks:
        return [h.chip("Clocks start after drafting", "gray",
                       title="STR (7 working days) and hold-lapse (90 days) clocks start once drafts exist")]
    out = []
    live = [c for c in clocks if clock_status(c, ctx.now) != ClockStatus.DONE]
    for kind, word in ((ClockKind.STR_DUE, "STR"), (ClockKind.HOLD_LAPSE, "Holds lapse")):
        mine = sorted((c for c in live if c.kind == kind), key=lambda c: c.due)
        if not mine:
            continue
        c = mine[0]
        status = clock_status(c, ctx.now)
        text = f"{word} {relative_due(c.due, ctx.now).replace('due ', '')}"
        tone = CLOCK_STATUS_TONE[status] if status != ClockStatus.OPEN else "accent"
        out.append(h.chip(text, tone, title=CLOCK_KIND_LABELS.get(kind, kind.value)))
    warn = [c for c in live if clock_status(c, ctx.now) in (ClockStatus.WARNING, ClockStatus.OVERDUE)
            and c.kind not in (ClockKind.STR_DUE, ClockKind.HOLD_LAPSE)]
    if warn:
        n = len(warn)
        out.append(h.chip(f"{n} other clock{'s need' if n != 1 else ' needs'} action", "warn",
                          title=", ".join(sorted({CLOCK_KIND_LABELS.get(c.kind, c.kind.value) for c in warn}))))
    return out or [h.chip("All clocks done", "good")]


def case_bar(ctx: Ctx, bundle: CaseBundle) -> None:
    plan = bundle.plan
    parts = [
        f'<span class="wp-mono"><b>{h.esc(bundle.case.case_id)}</b></span>',
        '<span class="wp-sep"></span>',
        f"<span>{h.esc(victims_text(ctx, bundle))}</span>",
        '<span class="wp-sep"></span>',
        f'<span class="wp-muted">NCRP</span> <span class="wp-mono">{h.esc(", ".join(bundle.case.ncrp_ids))}</span>',
    ]
    if plan is not None:
        parts += ['<span class="wp-sep"></span>',
                  f'<span><span class="wp-muted">Recoverable</span> <b class="wp-num">'
                  f"{h.esc(inr_short(plan.total_recoverable))}</b></span>"]
    parts += ['<span class="wp-sep"></span>', status_chip(bundle)]
    md(f'<div class="wp-casebar">{"".join(parts)}</div>')


def need_case(ctx: Ctx) -> bool:
    if ctx.bundle is not None:
        return True
    md(h.empty_state("Pick a case to continue",
                     "Choose a hold notice from the Inbox. Its trail, hold plan, drafts and clocks open here."))
    nav_button("Go to Inbox", "Inbox", key="empty_go_inbox", primary=True)
    return False


def generate_button(ctx: Ctx, *, key: str, label: str = "Generate drafts") -> None:
    """Primary CTA that runs the pipeline for the current case (disabled for auditors)."""
    if ctx.bundle is None:
        return
    disabled = not ctx.viewer.can_run
    help_text = ("Auditors have read-only access." if disabled else
                 "Fills every amount, account and UTR from the hold plan, then checks each draft.")
    clicked = st.button(label, key=key, type="primary", disabled=disabled, help=help_text,
                        **optional(st.button, icon=":material/edit_document:"))
    if clicked:
        with st.spinner("Drafting and checking…"):
            out = ctx.ds.run_pipeline(ctx.bundle.case.case_id, actor=ctx.viewer.actor, role=ctx.viewer.role)
        if out.ok:
            flash(out.message, "success")
        elif out.unavailable:
            flash(out.message, "info")
        else:
            flash(out.message, "error")
        st.rerun()


def download(label: str, data: str, file_name: str, *, key: str, mime: str = "text/plain") -> None:
    st.download_button(label, data=data.encode("utf-8"), file_name=file_name, mime=mime, key=key,
                       **optional(st.download_button, icon=":material/download:"))
