"""Hard role rules applied before any score, and the guard applied after it.

Order (first match wins):
1. VICTIM - complainant accounts of the trace's complaints, and every layer-0 account of the trace.
2. INTERMEDIARY - NODAL / POOL / ESCROW account types (SOP Para 5(x): never held).
3. MERCHANT - MERCHANT account type; or a BUSINESS account with an active GSTIN whose every tainted inflow is
   covered by a DELIVERED invoice of that account (SOP Para 9.2).

``innocent_blockers`` is the post-score safety guard: a score in the INNOCENT_HOLDER band is downgraded to
UNKNOWN (officer review) when the account *did* something a genuine holder would not normally do with the money.
A legitimate-looking profile (tenure, customer base, turnover) must never outweigh those actions on its own:
between MULE and INNOCENT_HOLDER the product prefers a human decision to a confident wrong label.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from wapsi.models import (
    INTERMEDIARY_ACCOUNT_TYPES,
    AccountType,
    Invoice,
    OrderStatus,
    Role,
    TraceResult,
)
from wapsi.money import fmt_inr
from wapsi.ports import Ledger
from wapsi.timeutil import fmt_ist

_INTERMEDIARY_LABEL = {
    AccountType.NODAL: "Nodal account",
    AccountType.POOL: "Payment-aggregator pool account",
    AccountType.ESCROW: "Escrow account",
}
SOP_91III_REASON = (
    "Only the disputed amount is held; the account is not suspended or seized (SOP Para 9.1(iii))"
)
SOP_92_DELIVERED = "Goods delivered: update CFCFRMS only, no hold (SOP Para 9.2)"
SOP_92_UNDELIVERED = (
    "Goods not delivered: cancel the order and keep the amount in the pool / nodal account (SOP Para 9.2)"
)
SOP_92_UNKNOWN = "No invoice found for the payment received; confirm delivery before deciding (SOP Para 9.2)"


# innocent_blockers thresholds
BLOCK_FAST_MINUTES = 120.0  # forwarding within about two hours
BLOCK_UNEXPLAINED_PASS_THROUGH = 0.5  # half the inflow sent on to counterparties it never paid before
BLOCK_EXIT_SHARE = 0.5  # half the inflow taken out as cash / crypto
BLOCK_PRIOR_REPORTS = 2  # repeatedly reported on NCRP (the s.106 escalation threshold)


def _f(row: Mapping[str, float], key: str) -> float:
    try:
        v = float(row.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0
    return v if v == v else 0.0  # NaN -> 0


def innocent_blockers(row: Mapping[str, float]) -> list[str]:
    """Reasons that forbid an automatic INNOCENT_HOLDER label for this feature row (empty list = none)."""
    out: list[str] = []
    unexplained = _f(row, "pass_through_ratio_24h") - _f(row, "known_payee_out_ratio_24h")
    if (
        unexplained >= BLOCK_UNEXPLAINED_PASS_THROUGH
        and _f(row, "median_forward_minutes") <= BLOCK_FAST_MINUTES
    ):
        out.append("Most of the money received was sent on quickly to accounts it had not paid before")
    exit_share = _f(row, "atm_share_24h") + _f(row, "crypto_share_24h")
    inflow = _f(row, "in_amount_24h")
    if inflow > 0 and exit_share * _f(row, "out_amount_24h") >= BLOCK_EXIT_SHARE * inflow:
        out.append("Most of the money received was withdrawn in cash or sent to crypto exchanges")
    if _f(row, "shared_device_with_seed_accounts") >= 1:
        out.append("Device shared with another account that received the same money")
    if _f(row, "dormant_then_burst") >= 1:
        out.append("Dormant account that suddenly became active around the time of the fraud")
    if _f(row, "ncrp_prior_reports") >= BLOCK_PRIOR_REPORTS:
        out.append("Reported on NCRP more than once before")
    return out


@dataclass(frozen=True)
class RuleHit:
    account_id: str
    role: Role
    rule: str  # VICTIM_COMPLAINANT | INTERMEDIARY_TYPE | MERCHANT_TYPE | BUSINESS_DELIVERED
    reasons: list[str] = field(default_factory=list)


def _tainted_inflows(trace: TraceResult) -> dict[str, list]:
    out: dict[str, list] = {}
    for e in trace.edges:
        if e.to_account and e.tainted_total > 0:
            out.setdefault(e.to_account, []).append(e)
    return out


def _victim_accounts(ledger: Ledger, trace: TraceResult) -> dict[str, float]:
    """victim account -> reported amount traced for it."""
    victims: dict[str, float] = {}
    for ncrp_id in trace.ncrp_ids:
        try:
            complaint = ledger.complaint(ncrp_id)
        except (KeyError, LookupError):
            continue
        amount = trace.victim_seed.get(ncrp_id, complaint.amount_claimed)
        victims[complaint.victim_account] = victims.get(complaint.victim_account, 0.0) + amount
    for acct, st in trace.accounts.items():
        if st.layer == 0:
            victims.setdefault(acct, 0.0)
    return victims


def _invoice_reasons(invoices: list[Invoice]) -> list[str]:
    delivered = [
        i for i in invoices if i.order_status == OrderStatus.DELIVERED and i.delivered_at is not None
    ]
    open_ = [i for i in invoices if i not in delivered]
    reasons: list[str] = []
    if delivered and not open_:
        if len(delivered) == 1:
            inv = delivered[0]
            reasons.append(
                f"Invoice {inv.invoice_id} for the payment was delivered at {fmt_ist(inv.delivered_at)}"
            )
        else:
            reasons.append(f"All {len(delivered)} invoices for the payments received were delivered")
        reasons.append(SOP_92_DELIVERED)
    elif open_:
        ids = ", ".join(sorted(i.invoice_id for i in open_))
        reasons.append(f"Invoice(s) {ids} for the payment received are not marked delivered")
        reasons.append(SOP_92_UNDELIVERED)
    else:
        reasons.append(SOP_92_UNKNOWN)
    return reasons


def hard_rules(ledger: Ledger, trace: TraceResult) -> dict[str, RuleHit]:
    """Role decided by a hard rule, per account of the trace. Accounts without a rule are absent."""
    hits: dict[str, RuleHit] = {}
    victims = _victim_accounts(ledger, trace)
    for acct, amount in victims.items():
        if acct not in trace.accounts:
            continue
        reasons = ["Account of the complainant who reported this fraud"]
        if amount > 0:
            reasons.append(f"Reported loss of {fmt_inr(amount)} from this account")
        reasons.append("Money traced from this account is to be restored to it, not held")
        hits[acct] = RuleHit(acct, Role.VICTIM, "VICTIM_COMPLAINANT", reasons)

    inflows = _tainted_inflows(trace)
    commercial: list[str] = []
    for acct, st in trace.accounts.items():
        if acct in hits:
            continue
        if st.account_type in INTERMEDIARY_ACCOUNT_TYPES:
            label = _INTERMEDIARY_LABEL.get(st.account_type, "Nodal / pool / escrow account")
            hits[acct] = RuleHit(acct, Role.INTERMEDIARY, "INTERMEDIARY_TYPE", [label, SOP_91III_REASON])
        elif st.account_type in (AccountType.MERCHANT, AccountType.BUSINESS):
            commercial.append(acct)
    if not commercial:
        return hits

    accts = ledger.accounts(commercial)
    gstins = {a.gstin for a in accts.values() if a.gstin}
    gst = ledger.gst_records(gstins) if gstins else {}
    txn_ids = [e.txn_id for a in commercial for e in inflows.get(a, [])]
    by_txn: dict[str, list[Invoice]] = {}
    for inv in ledger.invoices_for_transactions(txn_ids) if txn_ids else []:
        by_txn.setdefault(inv.txn_id or "", []).append(inv)

    for acct in commercial:
        st = trace.accounts[acct]
        acc = accts.get(acct)
        gstin = acc.gstin if acc else None
        record = gst.get(gstin) if gstin else None
        gst_active = record is not None and record.status.upper() == "ACTIVE"
        edges = inflows.get(acct, [])
        invoices = [i for e in edges for i in by_txn.get(e.txn_id, []) if i.merchant_account == acct]
        if gst_active:
            gst_reason = "Merchant with an active GSTIN"
        elif gstin:
            gst_reason = (
                f"Merchant account; GSTIN status is {record.status.lower() if record else 'not on record'}"
            )
        else:
            gst_reason = "Merchant account without a GSTIN on record"
        if st.account_type == AccountType.MERCHANT:
            hits[acct] = RuleHit(
                acct, Role.MERCHANT, "MERCHANT_TYPE", [gst_reason, *_invoice_reasons(invoices)]
            )
            continue
        delivered_txns = {
            i.txn_id
            for i in invoices
            if i.order_status == OrderStatus.DELIVERED and i.delivered_at is not None
        }
        if gst_active and edges and all(e.txn_id in delivered_txns for e in edges):
            reasons = ["Business account with an active GSTIN", *_invoice_reasons(invoices)]
            hits[acct] = RuleHit(acct, Role.MERCHANT, "BUSINESS_DELIVERED", reasons)
    return hits
