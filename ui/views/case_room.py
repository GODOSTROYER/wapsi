"""Case Room: trail graph on the left; totals, waterfall and the account inspector on the right."""

from __future__ import annotations

import streamlit as st

from ui.components import html as h
from ui.components.compat import stretch
from ui.components.formatting import (
    ACCOUNT_TYPE_LABELS,
    DISPOSITION_LABELS,
    RISK_LABELS,
    ROLE_LABELS,
    RULE_LABELS,
    TERMINAL_LABELS,
    channel_label,
    inr,
    inr_short,
    inr_table,
    ist,
    pct,
    reason_label,
)
from ui.components.theme import DISPOSITION_TONE, ROLE_TONE
from ui.components.totals_waterfall import build_totals_waterfall, located_share
from ui.components.trail_dot import build_trail_dot, legend_html, legend_items
from ui.data_source import screen_text
from ui.views.common import (
    K_ACCOUNT,
    Ctx,
    bank_names,
    case_bar,
    citation_title,
    citations,
    customers_for,
    display_names,
    head,
    md,
    need_case,
)
from wapsi.models import AccountState, ApportionmentRule, CaseBundle, Disposition, Role


def _account_options(bundle: CaseBundle) -> list[str]:
    trace = bundle.trace
    if trace is None:
        return []
    return [a for a, _ in sorted(trace.accounts.items(), key=lambda kv: (kv[1].layer == 0, kv[1].layer, kv[0]))]


def _option_label(bundle: CaseBundle, acct: str) -> str:
    st_ = bundle.trace.accounts[acct]
    if st_.layer == 0:
        return f"Victim · {acct}"
    dec = next((d for d in bundle.plan.decisions if d.account_id == acct), None) if bundle.plan else None
    role = dec.role if dec else (bundle.classifications[acct].role if acct in bundle.classifications else Role.UNKNOWN)
    return f"L{st_.layer} · {acct} · {ROLE_LABELS.get(role, role.value)}"


def render(ctx: Ctx) -> None:
    head("Case room", "Follow the victim's money, see what can still be held, and why.", eyebrow="Case")
    if not need_case(ctx):
        return
    bundle = ctx.bundle
    case_bar(ctx, bundle)
    trace = bundle.trace
    if trace is None:
        md(h.empty_state("Trail not traced yet",
                         "Once the trail is traced, every account the victim's money reached appears here, "
                         "layer by layer."))
        return

    options = _account_options(bundle)
    if st.session_state.get(K_ACCOUNT) not in options:
        default = next((a for a in options if trace.accounts[a].layer == 1), options[0] if options else None)
        st.session_state[K_ACCOUNT] = default
    selected = st.session_state.get(K_ACCOUNT)

    _badges(bundle)
    md('<div class="wp-section">Money trail · layers run left to right</div>')
    placeholder = st.empty()
    placeholder.markdown(h.skeleton(24), unsafe_allow_html=True)
    dot = build_trail_dot(trace, bundle.plan, classifications=bundle.classifications, selected=selected,
                          dark=ctx.is_dark, names=display_names(ctx, bundle), bank_names=bank_names(ctx))
    placeholder.graphviz_chart(dot, **stretch(st.graphviz_chart))
    md(legend_html(legend_items(trace, bundle.plan), ctx.is_dark))
    if trace.warnings:
        with st.expander(f"Trace notes ({len(trace.warnings)})"):
            md(h.bullet_list(trace.warnings))

    left, right = st.columns([1.15, 1], gap="large")
    with left:
        md('<div class="wp-section">Account inspector</div>')
        st.selectbox("Inspect account", options, key=K_ACCOUNT, format_func=lambda a: _option_label(bundle, a),
                     label_visibility="collapsed",
                     help="Pick an account to see its numbers, the reasons behind the recommendation and "
                          "the SOP basis. It is highlighted on the trail.")
        with st.container(border=True):
            if selected:
                _inspector(ctx, bundle, selected)
    with right:
        _totals(ctx, bundle)
        _complaints(ctx, bundle)


def _badges(bundle: CaseBundle) -> None:
    t = bundle.trace.totals
    rule = bundle.trace.rule
    share = located_share(t)
    md(
        '<div class="wp-chips" style="margin:-.35rem 0 .6rem">'
        + h.badge(f"Apportionment: {RULE_LABELS.get(rule, rule.value)}",
                  title="How victim money is attributed when an account mixes it with its own funds. "
                        + ("Pro-rata is the SOP default (Annexure-V illustrations)."
                           if rule == ApportionmentRule.PRO_RATA else "Selected in configuration."))
        + (h.chip(f"{pct(share)} of stolen money located", "good" if share and share >= 0.95 else "warn")
           if share is not None else "")
        + h.chip(f"{sum(1 for s in bundle.trace.accounts.values() if s.layer > 0)} accounts · "
                 f"{max((s.layer for s in bundle.trace.accounts.values()), default=0)} layers", "gray")
        + "</div>"
    )


def _totals(ctx: Ctx, bundle: CaseBundle) -> None:
    t = bundle.trace.totals
    plan = bundle.plan
    md('<div class="wp-section">Where the money is now</div>')
    md(h.tiles([
        h.Tile("Stolen", inr_short(t.stolen), "reported by victims", title=inr(t.stolen)),
        h.Tile("Recoverable", inr_short(plan.total_recoverable) if plan else "—",
               "holds + other-bank requests", accent=True, title=inr(plan.total_recoverable) if plan else ""),
        h.Tile("Holdable now", inr_short(t.holdable), "in our accounts", swatch="blue", title=inr(t.holdable)),
        h.Tile("Other banks", inr_short(t.outbound), "request via CFCFRMS (simulated)", swatch="orange",
               title=inr(t.outbound)),
        h.Tile("Cashed out", inr_short(t.cashed_out), "ATM / crypto exits", swatch="red", title=inr(t.cashed_out)),
        h.Tile("Pool / nodal", inr_short(t.intermediary), "held there, no suspension", swatch="violet",
               title=inr(t.intermediary)),
        h.Tile("Merchant settled", inr_short(t.merchant_settled), "goods delivered, no hold", swatch="yellow",
               title=inr(t.merchant_settled)),
    ], min_width_rem=8.5))
    st.plotly_chart(build_totals_waterfall(t, dark=ctx.is_dark), key="case_waterfall",
                    config={"displayModeBar": False}, **stretch(st.plotly_chart))


def _inspector(ctx: Ctx, bundle: CaseBundle, acct: str) -> None:
    trace, plan = bundle.trace, bundle.plan
    st_: AccountState = trace.accounts[acct]
    dec = next((d for d in plan.decisions if d.account_id == acct), None) if plan else None
    cls = bundle.classifications.get(acct)
    banks = bank_names(ctx)
    cust = customers_for(ctx, bundle).get(acct)

    if st_.layer == 0:
        _victim_panel(ctx, bundle, acct, st_, banks)
        return

    role = dec.role if dec and dec.role != Role.UNKNOWN else (cls.role if cls else Role.UNKNOWN)
    holder = ctx.viewer.name(cust.name) if cust else "Holder not on file"
    md(f'<div class="wp-eyebrow">Layer {st_.layer}{" · first-layer account" if st_.layer == 1 else ""}</div>'
       f'<div class="wp-row"><span class="wp-row-title wp-mono">{h.esc(acct)}</span></div>'
       f'<div class="wp-note">{h.esc(holder)} · {h.esc(ACCOUNT_TYPE_LABELS.get(st_.account_type, st_.account_type.value))}'
       f" · {h.esc(banks.get(st_.bank_id, st_.bank_id))}</div>")
    chip_list = [h.chip(ROLE_LABELS.get(role, role.value), ROLE_TONE.get(role, "gray"))]
    if dec is not None:
        chip_list.append(h.chip(f"Recommended: {DISPOSITION_LABELS.get(dec.disposition, dec.disposition.value)}",
                                DISPOSITION_TONE.get(dec.disposition, "gray"), dot=True,
                                title="Recommended — pending approval"))
        if dec.requires_human:
            chip_list.append(h.chip("Needs human decision", "warn"))
    if not st_.is_home_bank:
        chip_list.append(h.chip("Other bank", "gray"))
    if st_.terminal is not None:
        chip_list.append(h.chip(TERMINAL_LABELS.get(st_.terminal, st_.terminal.value), "gray"))
    md('<div style="margin:.45rem 0 .2rem">' + h.chips(chip_list) + "</div>")

    pairs = [("Balance now", inr_table(st_.balance_now)), ("Victim money now", inr_table(st_.tainted_now_total))]
    if dec is not None:
        if dec.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST:
            pairs.append(("Request amount", inr_table(dec.tainted_amount)))
        else:
            pairs.append(("Hold", inr_table(dec.hold_amount)))
        if dec.forward_hold_amount:
            pairs.append(("Forward hold", inr_table(dec.forward_hold_amount)))
    pairs += [("Victim money received", inr_table(st_.tainted_received_total)),
              ("Sent on", inr_table(st_.tainted_sent_total))]
    if st_.first_tainted_at:
        pairs.append(("First victim credit", ist(st_.first_tainted_at)))
    md(h.kv(pairs))

    if st_.tainted_now:
        victims = {c.ncrp_id: c for c in bundle.complaints}
        rows = []
        for ncrp, amt in sorted(st_.tainted_now.items()):
            c = victims.get(ncrp)
            held = dec.per_victim.get(ncrp) if dec else None
            rows.append(h.Row([
                h.Cell(html=h.mono(ncrp)),
                h.Cell(ctx.viewer.name(c.victim_name) if c else "—"),
                h.Cell(inr_table(amt), right=True),
                h.Cell(inr_table(held) if held is not None else "—", right=True),
            ]))
        md(h.section("Victim money now, per complaint"))
        md(h.table([("NCRP ID", False), ("Victim", False), ("In account", True), ("Held for victim", True)], rows))

    md(h.section("Why this recommendation"))
    reasons = (cls.reasons[:3] if cls else []) or ["No explanation recorded for this account."]
    md(h.bullet_list(ctx.mask(r, bundle) for r in reasons))
    if cls is not None and cls.model:
        md(f'<div class="wp-note">Mule score {cls.score:.2f} · source: {h.esc(_model_label(cls.model))}</div>')

    signals = _signals(ctx, acct)
    if signals:
        md(h.section("External signals"))
        md(h.kv(signals))

    if dec is not None:
        md(h.section("Basis"))
        extra = [c for c in dec.reason_codes if c != dec.disposition.value]
        if extra:
            md(h.bullet_list(reason_label(c) for c in extra))
        if dec.notes:
            md(f'<div class="wp-note" style="margin-bottom:.4rem">{h.esc(ctx.mask(dec.notes, bundle))}</div>')
        reg = citations()
        rows = []
        for cid in dec.citation_ids:
            c = reg.get(cid)
            rows.append(h.Row([h.Cell(html=h.cite(cid)),
                               h.Cell(c.section if c else "Unknown citation"),
                               h.Cell(_short_source(c.source_title) if c else "—")]))
        if rows:
            md(h.table([("Citation", False), ("Section", False), ("Source", False)], rows))
        quoted = [(cid, reg[cid]) for cid in dec.citation_ids if cid in reg and reg[cid].quote]
        if quoted:
            with st.expander("Quoted text"):
                for cid, c in quoted:
                    md(h.quote(c.quote, f"{cid} · {citation_title(cid)}"))
        md('<div class="wp-note">Recommended — pending approval.</div>')

    edges = [e for e in trace.edges if acct in (e.from_account, e.to_account)]
    if edges:
        with st.expander(f"Transactions on the trail ({len(edges)})"):
            rows = []
            for e in edges:
                incoming = e.to_account == acct
                other = e.from_account if incoming else (e.to_account or channel_label(e.channel) + " exit")
                rows.append(h.Row([
                    h.Cell(ist(e.ts)),
                    h.Cell("In" if incoming else "Out"),
                    h.Cell(html=h.mono(other or "—")),
                    h.Cell(channel_label(e.channel)),
                    h.Cell(html=h.mono(e.utr)),
                    h.Cell(inr_table(e.amount), right=True),
                    h.Cell(inr_table(e.tainted_total), right=True),
                ]))
            md(h.table([("Time", False), ("", False), ("Counterparty", False), ("Channel", False), ("UTR", False),
                        ("Amount", True), ("Victim money", True)], rows))


def _victim_panel(ctx: Ctx, bundle: CaseBundle, acct: str, st_: AccountState, banks: dict[str, str]) -> None:
    complaints = [c for c in bundle.complaints if c.victim_account == acct]
    md('<div class="wp-eyebrow">Victim account</div>'
       f'<div class="wp-row"><span class="wp-row-title wp-mono">{h.esc(acct)}</span></div>'
       f'<div class="wp-note">{h.esc(banks.get(st_.bank_id, st_.bank_id))}</div>')
    for c in complaints:
        md(h.kv([
            ("Victim", ctx.viewer.name(c.victim_name)),
            ("Mobile", ctx.viewer.mobile(c.victim_mobile)),
            ("Amount claimed", inr_table(c.amount_claimed)),
            ("Reported", ist(c.reported_at)),
            ("Source", c.source),
            ("FIR", c.fir_number or "Not filed"),
        ]))
        restored = bundle.plan.per_victim_recoverable.get(c.ncrp_id) if bundle.plan else None
        if restored is not None:
            md(h.chips([h.chip(f"Recoverable for this victim: {inr_short(restored)}", "accent",
                               title=inr(restored))]))


def _complaints(ctx: Ctx, bundle: CaseBundle) -> None:
    if not bundle.complaints:
        return
    md(h.section("Complaints"))
    flagged_ids = {e.object_id for e in bundle.audit if e.action == "untrusted_instruction_detected"}
    for c in bundle.complaints:
        title = f"{c.ncrp_id} · {ctx.viewer.name(c.victim_name)} · {inr_short(c.amount_claimed)}"
        with st.expander(title):
            guard = screen_text(c.narrative_text)
            spans = list(guard.quoted_spans) if guard is not None and guard.flagged else []
            if spans or c.ncrp_id in flagged_ids:
                md(h.callout("This complaint contains instruction-like text. It is shown for information only; "
                             "Wapsi never acts on it.", "warn", strong="Untrusted text."))
                if spans:
                    md(h.bullet_list(f"“{ctx.mask(sp, bundle)}”" for sp in spans))
            md(h.quote(ctx.mask(c.narrative_text, bundle), "Complainant's words · untrusted, shown as received"))
            md(h.kv([("Category", c.category.replace("_", " ").title()), ("Language", c.language),
                     ("Source", c.source), ("FIR", c.fir_number or "Not filed"),
                     ("Victim account", c.victim_account)], mono_keys=["Victim account"]))


def _signals(ctx: Ctx, acct: str) -> list[tuple[str, str]]:
    ledger = ctx.ds.ledger()
    if ledger is None:
        return []
    out: list[tuple[str, str]] = []
    try:
        sig = ledger.external_signals([acct]).get(acct)
        links = ledger.device_links([acct])
        shared = {ln.account_id for ln in ledger.device_links_for_devices({ln.device_id for ln in links})} - {acct}
    except Exception:  # noqa: BLE001
        return []
    if sig is not None:
        out += [("DoT fraud risk (FRI)", RISK_LABELS.get(sig.fri_risk, sig.fri_risk.value)),
                ("MuleHunter-style score", f"{sig.mulehunter_score:.2f}"),
                ("Earlier NCRP reports", str(sig.ncrp_prior_reports))]
    if links:
        out.append(("Device shared with", f"{len(shared)} other account{'s' if len(shared) != 1 else ''}"
                    if shared else "No other accounts"))
    return out


def _model_label(model: str) -> str:
    return {"fixture": "showcase fixture", "rules": "rules", "xgb": "XGBoost", "gnn": "graph model"}.get(model, model)


def _short_source(title: str) -> str:
    if title.startswith("Standard Operating Procedure"):
        return "MHA/I4C SOP (02.01.2026)"
    return title
