"""Hand-built CaseBundles for the showcase worlds, as the pipeline should produce them after planning.

Built only from `showcase.py` numbers, so UI / draft / verify work can proceed before the graph engine exists.
The graph engine's output for the same ledger must match these (tests/graph compares them).
"""

from __future__ import annotations

from datetime import datetime

from wapsi.citations import registry  # noqa: F401  (keeps citation IDs importable from fixtures)
from wapsi.fixtures import showcase as sc
from wapsi.models import (
    AccountState,
    AccountType,
    ApportionmentRule,
    Case,
    CaseBundle,
    CaseStatus,
    ClassificationResult,
    Disposition,
    HoldDecision,
    HoldPlan,
    Role,
    StrGround,
    TerminalKind,
    TraceResult,
    TraceTotals,
    TrailEdge,
)
from wapsi.timeutil import IST

_CITES = {
    Disposition.HOLD_AMOUNT: ["SOP-9-1", "SOP-ANNEX-I", "SOP-5-IX"],
    Disposition.HOLD_AMOUNT_NO_ESCALATION: ["SOP-5-XII", "SOP-10-1", "SOP-9-1"],
    Disposition.NO_HOLD_UPDATE_ONLY: ["SOP-9-2"],
    Disposition.HOLD_IN_POOL: ["SOP-9-2"],
    Disposition.HOLD_AMOUNT_INTERMEDIARY: ["SOP-9-1-III", "SOP-9-1", "SOP-ANNEX-I"],
    Disposition.OUTBOUND_CFCFRMS_REQUEST: ["SOP-9-1", "SOP-6"],
    Disposition.FORWARD_HOLD: ["SOP-9-1-I", "SOP-DEF-FIRST-LAYER"],
}

_REASONS = {
    sc.A: ["Account opened 11 days before the fraud", "96% of the victim's money was sent on within 30 minutes",
           "Device shared with 2 other accounts that received the same money"],
    sc.B: ["Account opened 21 days before the fraud", "Received ₹2,88,000 of victim money and forwarded most of it "
           "within an hour", "Device shared with 2 other accounts that received the same money"],
    sc.D: ["Account opened 9 days before the fraud", "DoT Financial Fraud Risk Indicator: very high",
           "2 earlier NCRP reports on this account"],
    sc.L: ["Long-standing account (since 2019) with regular monthly rent credits",
           "Received ₹5,000 labelled 'rent Sept', consistent with its history",
           "May have been operated without the holder's knowledge or connivance (SOP Para 5(xii))"],
    sc.K: ["Merchant with an active GSTIN", "Invoice INV-K-5521 for the payment was delivered at 11:55",
           "Goods delivered: update CFCFRMS only, no hold (SOP Para 9.2)"],
    sc.P: ["Payment-aggregator pool account", "Only the disputed amount is held; the pool account is not "
           "suspended or seized (SOP Para 9.1(iii))"],
    sc.C: ["Other-bank account opened 13 days before the fraud", "Received victim money from a first-layer mule"],
    sc.E: ["Other-bank account opened 8 days before the fraud", "Cashed out ₹50,000 at an ATM within 40 minutes"],
    sc.M: ["Account opened 6 days before the fraud", "Received money from 2 unrelated victims within 10 minutes",
           "2 earlier NCRP reports; complaint carries an FIR"],
}

_SCORES = {sc.A: 0.93, sc.B: 0.88, sc.D: 0.95, sc.L: 0.08, sc.K: 0.05, sc.P: 0.0, sc.C: 0.81, sc.E: 0.84, sc.M: 0.94}


def _edges(ledger, spec: dict, taint_by_victim: dict, default_victim: str) -> list[TrailEdge]:
    txns = {t.txn_id: t for t in ledger.all_transactions()}
    out = []
    for txn_id, (depth, tainted) in spec.items():
        t = txns[txn_id]
        by_victim = taint_by_victim.get(txn_id)
        if by_victim is None:
            if depth == 1:
                by_victim = {next(n for n, tid in _SEED_TXNS.items() if tid == txn_id): tainted}
            else:
                by_victim = {default_victim: tainted}
        out.append(TrailEdge(
            txn_id=t.txn_id, ts=t.ts, from_account=t.from_account, to_account=t.to_account,
            from_bank_id=t.from_bank_id, to_bank_id=t.to_bank_id, channel=t.channel, utr=t.utr,
            amount=t.amount, depth=depth, tainted=by_victim, tainted_total=tainted,
            via_consortium=(t.from_bank_id != sc.HOME and t.to_bank_id != sc.HOME),
        ))
    return sorted(out, key=lambda e: (e.ts, e.txn_id))


_SEED_TXNS = {sc.N1: "TX-001", sc.N2: "TX-101", sc.N3: "TX-102"}


def _states(ledger, exp: dict, edges: list[TrailEdge], tainted_now_by_victim: dict, victim_of: str | None):
    accts = ledger.all_accounts()
    states: dict[str, AccountState] = {}
    for acct_id, layer in exp["layers"].items():
        a = accts[acct_id]
        inflow = [e for e in edges if e.to_account == acct_id]
        received = exp.get("tainted_received", {}).get(acct_id, 0.0)
        now_total = exp.get("tainted_now", {}).get(acct_id)
        by_victim = tainted_now_by_victim.get(acct_id)
        if by_victim is None and now_total is not None:
            by_victim = {victim_of: now_total}
        by_victim = by_victim or {}
        now_total = sum(by_victim.values())
        terminal = None
        if a.account_type in (AccountType.NODAL, AccountType.POOL, AccountType.ESCROW):
            terminal = TerminalKind.NODAL_POOL_ESCROW
        elif a.account_type == AccountType.MERCHANT:
            terminal = TerminalKind.MERCHANT
        states[acct_id] = AccountState(
            account_id=acct_id, bank_id=a.bank_id, account_type=a.account_type,
            is_home_bank=a.bank_id == sc.HOME, layer=layer,
            first_tainted_at=min((e.ts for e in inflow), default=None) if layer else None,
            balance_now=exp["balance_now"].get(acct_id, ledger.balances_at([acct_id], _END)[acct_id]),
            tainted_now=by_victim, tainted_now_total=now_total,
            tainted_received_total=received, tainted_sent_total=round(received - now_total, 2) if layer else 0.0,
            terminal=terminal,
            parents=sorted({e.from_account for e in inflow if e.from_account}),
        )
    return states


_END = datetime(2026, 9, 30, tzinfo=IST)


def _classifications(roles: dict[str, Role]) -> dict[str, ClassificationResult]:
    return {
        acct: ClassificationResult(account_id=acct, role=role, score=_SCORES.get(acct, 0.5),
                                   reasons=_REASONS.get(acct, []), model="fixture")
        for acct, role in roles.items()
    }


def _decisions(states, exp, roles, per_victim_hold, per_victim_forward) -> list[HoldDecision]:
    out = []
    for acct, disp in exp["dispositions"].items():
        st = states[acct]
        hold = exp["hold"].get(acct, 0.0)
        fwd = exp.get("forward_hold", {}).get(acct, 0.0)
        outbound = exp.get("outbound_request", {}).get(acct, 0.0)
        cites = list(_CITES[disp])
        reasons = [disp.value]
        requires_human = exp.get("requires_human", {}).get(acct, False)
        reasons += exp.get("reason_codes_include", {}).get(acct, [])
        if disp == Disposition.HOLD_AMOUNT_INTERMEDIARY:
            reasons.append("NO_SUSPENSION_OR_SEIZURE")
        if fwd:
            reasons.append("FORWARD_HOLD_FIRST_LAYER")
            cites += ["SOP-9-1-I", "SOP-DEF-FIRST-LAYER"]
        if "ESCALATE_S106_CANDIDATE" in reasons:
            cites += ["BNSS-106", "CASE-KARTIK-CHATUR"]
        if exp["process"].value == "P2":
            cites += ["SOP-5-XIII", "SOP-11-P2"]
        out.append(HoldDecision(
            account_id=acct, bank_id=st.bank_id, disposition=disp, hold_amount=hold, forward_hold_amount=fwd,
            tainted_amount=outbound if disp == Disposition.OUTBOUND_CFCFRMS_REQUEST else st.tainted_now_total,
            balance_now=st.balance_now, layer=st.layer, role=roles[acct],
            per_victim=per_victim_hold.get(acct, dict(st.tainted_now) if hold or outbound else {}),
            citation_ids=list(dict.fromkeys(cites)), reason_codes=reasons, requires_human=requires_human,
            notes=("Forward hold on future credits for "
                   f"{'₹96,000.00' if acct == sc.A else '₹40,000.00'} cashed out by this first-layer account")
            if fwd else "",
        ))
        _ = per_victim_forward
    return out


def showcase_bundle(which: int = 1) -> CaseBundle:
    ledger = sc.showcase_ledger()
    if which == 1:
        exp = sc.SHOWCASE_1
        victim = sc.N1
        edges = _edges(ledger, exp["edges"], {}, victim)
        states = _states(ledger, exp, edges, {}, victim)
        seed = {sc.N1: 480000.0}
        per_victim_hold: dict = {}
        per_victim_recoverable = {sc.N1: exp["total_recoverable"]}
        str_ground = StrGround.PROCEEDS_OF_CRIME
        opened = datetime(2026, 9, 10, 13, 45, tzinfo=IST)
    else:
        exp = sc.SHOWCASE_2
        victim = None
        edges = _edges(ledger, exp["edges"], exp["edge_taint_by_victim"], sc.N2)
        states = _states(ledger, exp, edges, exp["tainted_now_by_victim"], None)
        seed = {sc.N2: 100000.0, sc.N3: 60000.0}
        per_victim_hold = {sc.M: dict(exp["tainted_now_by_victim"][sc.M])}
        per_victim_recoverable = dict(exp["per_victim_recoverable"])
        str_ground = StrGround.PROCEEDS_OF_CRIME
        opened = datetime(2026, 9, 11, 16, 25, tzinfo=IST)

    roles = {**{a: Role.VICTIM for a, lay in exp["layers"].items() if lay == 0}, **exp["roles"]}
    ncrp_ids = list(exp["ncrp_ids"])
    case_id = f"CASE-{ncrp_ids[0]}"
    trace = TraceResult(
        case_id=case_id, ncrp_ids=ncrp_ids, rule=ApportionmentRule.PRO_RATA,
        window_start=min(e.ts for e in edges), window_end=_window_end(edges), victim_seed=seed,
        edges=edges, accounts=states, totals=TraceTotals(**exp["totals"]), computed_at=opened,
    )
    decisions = _decisions(states, exp, roles, per_victim_hold, exp.get("forward_hold_by_victim", {}))
    plan = HoldPlan(
        case_id=case_id, rule=ApportionmentRule.PRO_RATA, decisions=decisions,
        total_hold=sum(d.hold_amount for d in decisions),
        total_forward_hold=sum(d.forward_hold_amount for d in decisions),
        total_recoverable=sum(d.hold_amount for d in decisions)
        + sum(d.tainted_amount for d in decisions if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST),
        per_victim_recoverable=per_victim_recoverable,
        over_hold_amount=0.0,
        baseline_freeze_amount=exp["baseline_freeze_amount"],
        baseline_freeze_accounts=exp["baseline_freeze_accounts"],
        restoration_process=exp["process"], str_ground=str_ground,
    )
    complaints = [ledger.complaint(n) for n in ncrp_ids]
    case = Case(case_id=case_id, ncrp_ids=ncrp_ids, opened_at=opened, status=CaseStatus.PLANNED,
                assigned_to="nodal.officer@hb01", priority_amount=plan.total_recoverable)
    return CaseBundle(case=case, complaints=complaints, trace=trace,
                      classifications=_classifications(roles), plan=plan)


def _window_end(edges: list[TrailEdge]) -> datetime:
    from datetime import timedelta

    return min(e.ts for e in edges) + timedelta(hours=72)


def all_showcase_bundles() -> list[CaseBundle]:
    return [showcase_bundle(1), showcase_bundle(2)]
