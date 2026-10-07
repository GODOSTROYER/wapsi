"""Independent re-check of the hard rules in AGENTS.md item 6, against the trace (not the plan's own fields).

Plan level (``check_plan``) and draft level (``check_draft_policy``):

- hold <= min(tainted money still in the account, balance) (SOP 5(ix), 9.1); over-hold is an error
- nodal / pool / escrow accounts (SOP 9.1(iii)): home-bank ones only ever carry ``HOLD_AMOUNT_INTERMEDIARY``
  (or ``NO_ACTION`` when nothing is holdable), other-bank ones an outbound CFCFRMS request; never a forward hold
  (SOP 9.1(i)), never an s.106 flag; ``HOLD_AMOUNT_INTERMEDIARY`` is reserved for such accounts
- forward hold only on first-layer accounts (SOP 9.1(i))
- merchant with delivered goods is never held (SOP 9.2); a merchant is update-only (``NO_HOLD_UPDATE_ONLY``) only
  when every tainted payment into it has an invoice and all of them are DELIVERED
- innocent holders (SOP 5(xii)), bona fide recipients and unwitting conduits alike, are never flagged for s.106
  escalation and never named in an (automatic) STR; a conduit's decision recommends an STR for the officer's
  decision (``STR_RECOMMENDED_OFFICER_DECISION``, ``requires_human``), a bona fide recipient's never does
- ``EXCEPTIONAL_ACCOUNT_LEVEL_HOLD`` (amount-specific holds are the default; account-level action only as a
  recorded, supervisor-approved exception): >= 1 ``AccountLevelReason`` in the reason codes, an officer request
  (``ACCOUNT_LEVEL_HOLD_REQUEST`` draft) with the same reasons, a supervisor approval bound to that request's exact
  content (SHA-256, maker != checker), a human, a home-bank non-intermediary non-merchant account
- the s.106 escalation flag needs an FIR, >= ``cfg.escalation_min_prior_reports`` prior reports and a human
- a STR names a ``StrGround`` in both its structured payload and its body (PML Rules 2(1)(g))
- HOLD_INSTRUCTION / CUSTOMER_NOTICE only for holding dispositions
- no draft instructs a whole-account or debit freeze, or blocking / holding the account itself, whatever amounts
  the sentence also mentions (negated, descriptive and counterfactual mentions are allowed, and so is a freeze
  whose object is an amount: "a freeze of ₹43,200.00", "freeze the disputed amount")
- no draft instructs suspension of digital banking or seizure of an account (SOP 9.1(iii)); a hold instruction
  for a nodal / pool / escrow account states that rule
- Process 2 whenever more than one victim has recoverable money
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable

from wapsi import money
from wapsi.config import WapsiConfig
from wapsi.models import (
    HOLDING_DISPOSITIONS,
    INNOCENT_ROLES,
    INTERMEDIARY_ACCOUNT_TYPES,
    AccountLevelReason,
    AccountState,
    AccountType,
    Approval,
    ApprovalDecision,
    CaseBundle,
    CheckResult,
    Complaint,
    Disposition,
    Draft,
    DraftKind,
    HoldDecision,
    OrderStatus,
    RestorationProcess,
    Role,
    StrGround,
)
from wapsi.ports import Ledger
from wapsi.verify.report import fail, ok, warn

NAME = "policy"
EPS = money.EPSILON
ESCALATE = "ESCALATE_S106_CANDIDATE"
INTERMEDIARY_CITE = "SOP-9-1-III"
INTERMEDIARY_IDLE = frozenset({Disposition.NO_ACTION, Disposition.CASHED_OUT})
# innocent-holder split (the verifier keeps its own copies of the plan's reason codes)
STR_RECOMMENDED = "STR_RECOMMENDED_OFFICER_DECISION"
# exceptional account-level hold
ACCOUNT_LEVEL = Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD
ACCOUNT_LEVEL_CODES = frozenset(r.value for r in AccountLevelReason)
ACCOUNT_LEVEL_BASE = frozenset({Disposition.HOLD_AMOUNT, Disposition.HOLD_AMOUNT_NO_ESCALATION})

HOLD_DRAFT_DISPOSITIONS = HOLDING_DISPOSITIONS | {Disposition.FORWARD_HOLD}
CASE_LEVEL_KINDS = {DraftKind.STR, DraftKind.CFCFRMS_UPDATE, DraftKind.RESTORATION_PACK}
ACCOUNT_KINDS = {
    DraftKind.HOLD_INSTRUCTION,
    DraftKind.CUSTOMER_NOTICE,
    DraftKind.EDD_MEMO,
    DraftKind.OUTBOUND_REQUEST,
    DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST,
}

fmt = money.fmt_inr


# --------------------------------------------------------------------------- helpers


def _gt(a: float, b: float) -> bool:
    return money.r2(a) - money.r2(b) >= EPS


def tainted_now(st: AccountState) -> float:
    return money.r2(sum(st.tainted_now.values())) if st.tainted_now else money.r2(st.tainted_now_total)


def hold_cap(st: AccountState) -> float:
    return money.r2(max(0.0, min(tainted_now(st), st.balance_now)))


def role_of(bundle: CaseBundle, acct: str, dec: HoldDecision | None = None) -> Role:
    cls = bundle.classifications.get(acct)
    if cls is not None:
        return cls.role
    return dec.role if dec is not None else Role.UNKNOWN


def is_innocent(bundle: CaseBundle, acct: str, dec: HoldDecision | None = None) -> bool:
    cls = bundle.classifications.get(acct)
    if cls is not None and cls.role in INNOCENT_ROLES:
        return True
    return dec is not None and (
        dec.role in INNOCENT_ROLES or dec.disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION
    )


def is_intermediary(bundle: CaseBundle, st: AccountState, dec: HoldDecision | None = None) -> bool:
    """Nodal / pool / escrow by account type (from the trace) or by assessed role."""
    return (
        st.account_type in INTERMEDIARY_ACCOUNT_TYPES
        or role_of(bundle, st.account_id, dec) == Role.INTERMEDIARY
    )


def innocent_accounts(bundle: CaseBundle) -> set[str]:
    out = {a for a, c in bundle.classifications.items() if c.role in INNOCENT_ROLES}
    if bundle.plan is not None:
        out |= {d.account_id for d in bundle.plan.decisions if is_innocent(bundle, d.account_id, d)}
    return out


def _decision(bundle: CaseBundle, acct: str | None) -> HoldDecision | None:
    if bundle.plan is None or acct is None:
        return None
    return next((d for d in bundle.plan.decisions if d.account_id == acct), None)


# --------------------------------------------------------------------------- exceptional account-level hold


def recorded_reasons(values: Iterable[object]) -> list[str]:
    """The values that are on the closed ``AccountLevelReason`` list (order kept)."""
    return list(dict.fromkeys(str(v) for v in values if str(v) in ACCOUNT_LEVEL_CODES))


def account_level_requests(bundle: CaseBundle, acct: str) -> list[Draft]:
    """The officer requests (``ACCOUNT_LEVEL_HOLD_REQUEST`` drafts) for ``acct``, newest first."""
    found = [d for d in bundle.drafts if d.kind == DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST and d.account_id == acct]
    return sorted(found, key=lambda d: (d.version, d.created_at), reverse=True)


def account_level_request(bundle: CaseBundle, acct: str) -> Draft | None:
    """The latest officer request (``ACCOUNT_LEVEL_HOLD_REQUEST`` draft) for ``acct``."""
    found = account_level_requests(bundle, acct)
    return found[0] if found else None


def _latest_approval(bundle: CaseBundle, draft_id: str) -> Approval | None:
    return next((a for a in reversed(bundle.approvals) if a.draft_id == draft_id), None)


def _request_approval_problem(bundle: CaseBundle, req: Draft) -> tuple[Approval | None, str]:
    from wapsi.pipeline.run import draft_fingerprint  # same fingerprint the approval step records

    appr = _latest_approval(bundle, req.draft_id)
    if appr is None or appr.decision != ApprovalDecision.APPROVED:
        return None, f"the officer's request {req.draft_id} has not been approved by a supervisor"
    if appr.role.strip().lower() != "supervisor":
        return None, f"{req.draft_id} was approved in the role '{appr.role}', not by a supervisor"
    if appr.draft_sha256 != draft_fingerprint(req):
        return None, f"{req.draft_id} changed after it was approved; the approval does not cover it"
    requester = str(req.structured.get("requested_by") or "").strip()
    if not requester or requester.casefold() == appr.approver.strip().casefold():
        return None, "maker-checker: the officer who requested the exception cannot approve it"
    return appr, ""


def account_level_approval(bundle: CaseBundle, acct: str) -> tuple[Approval | None, str]:
    """(supervisor approval of an officer's request for ``acct``, problem). The approval must be the latest
    decision on its request, APPROVED by a supervisor other than the requesting officer (maker-checker), and bound
    to the request's exact current content (the SHA-256 fingerprint ``wapsi.pipeline.run.approve`` records).
    Requests are tried newest first, so a later request that is still pending or was rejected (e.g. a duplicate
    raised while the first was waiting) does not void an exception applied on an earlier approved request; the
    problem reported is the newest request's."""
    requests = account_level_requests(bundle, acct)
    if not requests:
        return None, "no officer request (ACCOUNT_LEVEL_HOLD_REQUEST) is on record for the account"
    problems = []
    for req in requests:
        appr, problem = _request_approval_problem(bundle, req)
        if appr is not None:
            return appr, ""
        problems.append(problem)
    return None, problems[0]


def approved_account_level(bundle: CaseBundle, dec: HoldDecision | None, draft: Draft) -> bool:
    """The draft is the hold instruction of an account whose decision is an approved exceptional account-level hold
    (recorded reasons, supervisor approval bound to the officer's request)."""
    return (
        draft.kind == DraftKind.HOLD_INSTRUCTION
        and dec is not None
        and dec.disposition == ACCOUNT_LEVEL
        and bool(recorded_reasons(dec.reason_codes))
        and account_level_approval(bundle, dec.account_id)[0] is not None
    )


def _check_account_level(dec: HoldDecision, st: AccountState, bundle: CaseBundle) -> list[CheckResult]:
    """Amount-specific holds by default; account-level action only as a recorded, supervisor-approved exception
    (SOP 5(ix): unwarranted freezing of accounts is discouraged; SOP 9.1)."""
    acct = dec.account_id
    out: list[CheckResult] = []
    reasons = recorded_reasons(dec.reason_codes)
    if not reasons:
        out.append(
            fail(
                NAME,
                "account_level_without_reasons",
                f"{acct} carries an account-level hold with no recorded reason from the closed list; hold the "
                "disputed amount only [SOP-5-IX] [SOP-9-1]",
                [acct],
            )
        )
    appr, problem = account_level_approval(bundle, acct)
    if appr is None:
        out.append(
            fail(
                NAME,
                "account_level_unapproved",
                f"{acct} carries an account-level hold without supervisor approval ({problem}); amount-specific "
                "holds are the default [SOP-5-IX] [SOP-9-1]",
                [acct],
            )
        )
    else:  # the reasons of the request that approval covers
        req = next((d for d in bundle.drafts if d.draft_id == appr.draft_id), None)
        requested = set(recorded_reasons((req.structured.get("reasons") or []) if req else []))
        if reasons and requested != set(reasons):
            out.append(
                fail(
                    NAME,
                    "account_level_reason_mismatch",
                    f"{acct}: the plan's reasons ({', '.join(reasons)}) differ from the approved request's "
                    f"({', '.join(sorted(requested)) or 'none'})",
                    [acct],
                )
            )
    if not dec.requires_human:
        out.append(fail(NAME, "account_level_without_human", f"{acct}: an account-level hold needs a human", [acct]))
    if not st.is_home_bank:
        out.append(
            fail(
                NAME,
                "account_level_other_bank",
                f"{acct} is at another bank; only a hold request through CFCFRMS is possible [SOP-6]",
                [acct],
            )
        )
    if st.account_type == AccountType.MERCHANT or dec.role == Role.MERCHANT:
        out.append(
            fail(
                NAME,
                "account_level_merchant",
                f"{acct} is a merchant account; merchants are updated or held in the pool [SOP-9-2]",
                [acct],
            )
        )
    return out


def _complaints(bundle: CaseBundle, ledger: Ledger | None) -> dict[str, Complaint]:
    out = {c.ncrp_id: c for c in bundle.complaints}
    if ledger is not None:
        for n in bundle.case.ncrp_ids:
            if n not in out:
                try:
                    out[n] = ledger.complaint(n)
                except LookupError:
                    pass
    return out


def _inflow_edges(bundle: CaseBundle, acct: str):
    return (
        [e for e in bundle.trace.edges if e.to_account == acct and e.tainted_total > 0]
        if bundle.trace
        else []
    )


def merchant_delivery(bundle: CaseBundle, ledger: Ledger | None, acct: str) -> str | None:
    """Delivery status of the tainted payments into merchant ``acct`` (SOP 9.2).

    ``delivered``: every tainted payment has at least one invoice of this merchant and all of them are DELIVERED;
    ``undelivered``: some invoice is UNDELIVERED or CANCELLED; ``unproven``: some payment has no invoice;
    None: cannot be checked (no ledger, or no tainted payment).
    """
    if ledger is None:
        return None
    txns = sorted({e.txn_id for e in _inflow_edges(bundle, acct)})
    if not txns:
        return None
    by_txn: dict[str, list] = defaultdict(list)
    for inv in ledger.invoices_for_transactions(txns):
        if inv.merchant_account == acct and inv.txn_id:
            by_txn[inv.txn_id].append(inv)
    if any(inv.order_status != OrderStatus.DELIVERED for invs in by_txn.values() for inv in invs):
        return "undelivered"
    if any(not by_txn.get(t) for t in txns):
        return "unproven"
    return "delivered"


# --------------------------------------------------------------------------- decision checks


def check_decision(
    dec: HoldDecision, bundle: CaseBundle, ledger: Ledger | None, cfg: WapsiConfig
) -> list[CheckResult]:
    out: list[CheckResult] = []
    acct = dec.account_id
    st = bundle.trace.accounts.get(acct) if bundle.trace else None
    if st is None:
        return [
            fail(
                NAME,
                "decision_unknown_account",
                f"{acct} has a plan decision but is not in the trace",
                [acct],
            )
        ]

    disp = dec.disposition
    holding = disp in HOLDING_DISPOSITIONS
    hold, fwd = money.r2(dec.hold_amount), money.r2(dec.forward_hold_amount)
    innocent = is_innocent(bundle, acct, dec)
    role = role_of(bundle, acct, dec)

    if st.layer == 0:
        out.append(
            fail(
                NAME,
                "victim_in_plan",
                f"{acct} is a victim account (layer 0); hold plans skip layer 0",
                [acct],
            )
        )
    if hold < 0 or fwd < 0 or dec.tainted_amount < 0:
        out.append(fail(NAME, "negative_amount", f"{acct} has a negative amount", [acct]))

    cap = hold_cap(st)
    if _gt(hold, cap):
        out.append(
            fail(
                NAME,
                "over_hold",
                f"{acct} hold {fmt(hold)} exceeds min(tainted {fmt(tainted_now(st))}, "
                f"balance {fmt(st.balance_now)}) = {fmt(cap)}; hold the tainted amount only "
                "[SOP-5-IX] [SOP-9-1]",
                [fmt(hold), fmt(cap)],
            )
        )
    if hold > 0 and not holding:
        out.append(
            fail(
                NAME,
                "hold_without_holding_disposition",
                f"{acct} holds {fmt(hold)} under non-holding disposition {disp}",
                [fmt(hold)],
            )
        )
    if not money.eq(dec.balance_now, st.balance_now):
        out.append(
            warn(
                NAME,
                "balance_differs",
                f"{acct} decision balance {fmt(dec.balance_now)} differs from the "
                f"trace balance {fmt(st.balance_now)}",
                [fmt(dec.balance_now)],
            )
        )

    escalated = ESCALATE in dec.reason_codes or disp == Disposition.ESCALATE_S106_CANDIDATE
    if is_intermediary(bundle, st, dec):
        out += _check_intermediary(dec, st, escalated)
    elif disp == Disposition.HOLD_AMOUNT_INTERMEDIARY:
        out.append(
            fail(
                NAME,
                "intermediary_disposition_misused",
                f"{acct} is a {st.account_type} account; HOLD_AMOUNT_INTERMEDIARY is only for nodal, pool "
                f"and escrow accounts [{INTERMEDIARY_CITE}]",
                [acct],
            )
        )

    if (fwd > 0 or disp == Disposition.FORWARD_HOLD) and st.layer != 1:
        out.append(
            fail(
                NAME,
                "forward_hold_not_first_layer",
                f"{acct} is at layer {st.layer}; forward holds are only for first-layer accounts [SOP-9-1-I]",
                [acct, fmt(fwd)],
            )
        )
    if fwd > 0 and st.layer == 1 and not _gt(st.tainted_received_total, st.balance_now):
        out.append(
            warn(
                NAME,
                "forward_hold_unneeded",
                f"{acct} balance {fmt(st.balance_now)} covers the disputed "
                f"amount {fmt(st.tainted_received_total)}; forward hold not needed",
                [fmt(fwd)],
            )
        )

    if disp == Disposition.NO_HOLD_UPDATE_ONLY and (hold > 0 or fwd > 0):
        out.append(
            fail(NAME, "update_only_with_hold", f"{acct} is update-only but carries a hold", [fmt(hold)])
        )

    if st.account_type == AccountType.MERCHANT or role == Role.MERCHANT:
        delivery = merchant_delivery(bundle, ledger, acct)
        if delivery == "delivered" and (holding or hold > 0 or fwd > 0):
            out.append(
                fail(
                    NAME,
                    "delivered_merchant_held",
                    f"{acct} is a merchant whose goods were delivered; "
                    "update CFCFRMS only, no hold [SOP-9-2]",
                    [acct],
                )
            )
        if disp == Disposition.NO_HOLD_UPDATE_ONLY and delivery in ("undelivered", "unproven"):
            why = (
                "has an undelivered or cancelled order"
                if delivery == "undelivered"
                else "received a tainted payment with no invoice"
            )
            out.append(
                fail(
                    NAME,
                    "update_only_undelivered_merchant",
                    f"{acct} {why}; update-only needs delivered goods for every tainted payment. Cancel the "
                    "order and hold the amount in the pool / nodal account [SOP-9-2]",
                    [acct],
                )
            )
        elif delivery == "undelivered" and disp != Disposition.HOLD_IN_POOL:
            out.append(
                warn(
                    NAME,
                    "undelivered_merchant",
                    f"{acct} has undelivered orders; expected HOLD_IN_POOL [SOP-9-2], got {disp}",
                    [acct],
                )
            )
        if disp == Disposition.NO_HOLD_UPDATE_ONLY and ledger is None:
            out.append(
                warn(
                    NAME,
                    "merchant_delivery_unverified",
                    f"{acct}: delivery of the goods not checked (no ledger) [SOP-9-2]",
                    [acct],
                )
            )
    elif disp == Disposition.HOLD_IN_POOL:
        out.append(
            warn(NAME, "hold_in_pool_non_merchant", f"{acct} is not a merchant but is HOLD_IN_POOL", [acct])
        )
    elif disp == Disposition.NO_HOLD_UPDATE_ONLY:
        out.append(
            fail(
                NAME,
                "update_only_non_merchant",
                f"{acct} is not a merchant; NO_HOLD_UPDATE_ONLY is only for merchants with delivered goods "
                "[SOP-9-2]",
                [acct],
            )
        )

    if disp == Disposition.ESCALATE_S106_CANDIDATE:
        out.append(
            fail(
                NAME,
                "reserved_disposition",
                f"{acct}: ESCALATE_S106_CANDIDATE is a flag (reason code), not a disposition, in v1",
                [acct],
            )
        )
    # an approved account-level exception on an innocent holder's account is checked by _check_account_level
    if innocent and holding and disp not in (Disposition.HOLD_AMOUNT_NO_ESCALATION, ACCOUNT_LEVEL):
        out.append(
            warn(
                NAME,
                "innocent_disposition",
                f"{acct} looks like an innocent holder [SOP-5-XII]; expected "
                f"HOLD_AMOUNT_NO_ESCALATION, got {disp}",
                [acct],
            )
        )
    # no automatic STR; the officer decides; STR recommended where the account was used as a conduit
    if role == Role.UNWITTING_CONDUIT and st.is_home_bank and holding:
        if STR_RECOMMENDED not in dec.reason_codes:
            out.append(
                fail(
                    NAME,
                    "conduit_str_not_recommended",
                    f"{acct} was used as a conduit; its decision must carry {STR_RECOMMENDED} (STR recommended, "
                    "the officer decides) [SOP-5-XII] [PMLR-2-1-G]",
                    [acct],
                )
            )
        if not dec.requires_human:
            out.append(
                fail(
                    NAME,
                    "conduit_without_human",
                    f"{acct} was used as a conduit; the STR decision is the officer's, so requires_human is needed",
                    [acct],
                )
            )
    if role == Role.BONA_FIDE_RECIPIENT and STR_RECOMMENDED in dec.reason_codes:
        out.append(
            fail(
                NAME,
                "bona_fide_str_recommended",
                f"{acct} is a bona fide recipient: no STR by default, so no STR recommendation [SOP-5-XII]",
                [acct],
            )
        )
    if disp == ACCOUNT_LEVEL or ACCOUNT_LEVEL.value in dec.reason_codes:
        out += _check_account_level(dec, st, bundle)
    if escalated:
        out += _check_escalation(dec, st, bundle, ledger, cfg, innocent)

    if disp == Disposition.OUTBOUND_CFCFRMS_REQUEST:
        if hold > 0:
            out.append(
                fail(
                    NAME,
                    "outbound_with_hold",
                    f"{acct} is an outbound CFCFRMS request; hold_amount must be 0",
                    [fmt(hold)],
                )
            )
        if _gt(dec.tainted_amount, tainted_now(st)):
            out.append(
                fail(
                    NAME,
                    "outbound_over_request",
                    f"{acct} request {fmt(dec.tainted_amount)} exceeds "
                    f"tainted money in the account {fmt(tainted_now(st))}",
                    [fmt(dec.tainted_amount)],
                )
            )
        if st.is_home_bank:
            out.append(
                warn(
                    NAME,
                    "outbound_home_account",
                    f"{acct} is a home-bank account but is routed as an outbound request",
                    [acct],
                )
            )

    for n, v in dec.per_victim.items():
        if v < 0 or _gt(v, st.tainted_now.get(n, 0.0)):
            out.append(
                fail(
                    NAME,
                    "per_victim_exceeds_taint",
                    f"{acct} attributes {fmt(v)} to {n} but only "
                    f"{fmt(st.tainted_now.get(n, 0.0))} of that victim's money is in the account "
                    "[SOP-5-XIII]",
                    [fmt(v)],
                )
            )
    pv_total = money.r2(sum(dec.per_victim.values()))
    limit = (
        hold if holding else (dec.tainted_amount if disp == Disposition.OUTBOUND_CFCFRMS_REQUEST else None)
    )
    if limit is not None and _gt(pv_total, limit):
        out.append(
            fail(
                NAME,
                "per_victim_exceeds_amount",
                f"{acct} per-victim amounts {fmt(pv_total)} exceed {fmt(limit)}",
                [fmt(pv_total)],
            )
        )
    return out


def _check_intermediary(dec: HoldDecision, st: AccountState, escalated: bool) -> list[CheckResult]:
    """SOP 9.1(iii) / 9.1(i): nodal, pool and escrow accounts get an amount-specific hold only."""
    acct, disp = dec.account_id, dec.disposition
    out: list[CheckResult] = []
    kind = f"{acct} is a nodal / pool / escrow account ({st.account_type})"
    if st.is_home_bank:
        allowed = {Disposition.HOLD_AMOUNT_INTERMEDIARY}
        if hold_cap(st) <= 0:
            allowed |= INTERMEDIARY_IDLE
        expected = "HOLD_AMOUNT_INTERMEDIARY (hold the disputed amount only)"
    else:
        allowed = {Disposition.OUTBOUND_CFCFRMS_REQUEST, *INTERMEDIARY_IDLE}
        expected = "OUTBOUND_CFCFRMS_REQUEST (the other bank holds the disputed amount)"
    if disp not in allowed:
        out.append(
            fail(
                NAME,
                "intermediary_disposition",
                f"{kind}; expected {expected}, got {disp}. Hold the disputed amount only, without suspending "
                f"digital banking or seizing the account [{INTERMEDIARY_CITE}]",
                [acct, str(disp)],
            )
        )
    if money.r2(dec.forward_hold_amount) > 0 or disp == Disposition.FORWARD_HOLD:
        out.append(
            fail(
                NAME,
                "forward_hold_on_intermediary",
                f"{kind}; forward holds are only for first-layer accounts and never for nodal, pool or "
                "escrow accounts [SOP-9-1-I]",
                [acct, fmt(dec.forward_hold_amount)],
            )
        )
    if escalated:
        out.append(
            fail(
                NAME,
                "intermediary_escalated",
                f"{kind}; never flag it for s.106 escalation: hold the disputed amount only, no suspension "
                f"or seizure [{INTERMEDIARY_CITE}]",
                [acct],
            )
        )
    return out


def _check_escalation(
    dec: HoldDecision,
    st: AccountState,
    bundle: CaseBundle,
    ledger: Ledger | None,
    cfg: WapsiConfig,
    innocent: bool,
) -> list[CheckResult]:
    acct = dec.account_id
    out: list[CheckResult] = []
    if innocent:
        out.append(
            fail(
                NAME,
                "innocent_escalated",
                f"{acct} is an innocent holder [SOP-5-XII]; never flag it for s.106 escalation",
                [acct],
            )
        )
    if not dec.requires_human:
        out.append(
            fail(NAME, "escalation_without_human", f"{acct} escalation flag must set requires_human", [acct])
        )
    if dec.disposition not in (
        Disposition.HOLD_AMOUNT,
        Disposition.FORWARD_HOLD,
        Disposition.ESCALATE_S106_CANDIDATE,
        ACCOUNT_LEVEL,  # an approved exception keeps the advisory flag (checked by _check_account_level)
    ):
        out.append(
            fail(
                NAME,
                "escalation_disposition",
                f"{acct} escalation candidates keep HOLD_AMOUNT as the "
                f"primary disposition, got {dec.disposition}",
                [acct],
            )
        )
    ncrps = set(st.tainted_now) | set(dec.per_victim)
    for e in _inflow_edges(bundle, acct):
        ncrps |= set(e.tainted)
    complaints = _complaints(bundle, ledger)
    relevant = [complaints[n] for n in sorted(ncrps or bundle.case.ncrp_ids) if n in complaints]
    if not any(c.fir_number for c in relevant):
        out.append(
            fail(
                NAME,
                "escalation_without_fir",
                f"{acct} is flagged for s.106 escalation but no related complaint carries an FIR",
                [acct],
            )
        )
    if ledger is None:
        out.append(
            warn(NAME, "escalation_unverified", f"{acct}: prior NCRP reports not checked (no ledger)", [acct])
        )
    else:
        sig = ledger.external_signals([acct]).get(acct)
        prior = sig.ncrp_prior_reports if sig else 0
        if prior < cfg.escalation_min_prior_reports:
            out.append(
                fail(
                    NAME,
                    "escalation_insufficient_reports",
                    f"{acct} has {prior} prior NCRP report(s); "
                    f"escalation needs >= {cfg.escalation_min_prior_reports}",
                    [acct],
                )
            )
    return out


# --------------------------------------------------------------------------- plan checks


def recomputed_per_victim(decisions: Iterable[HoldDecision]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for d in decisions:
        if d.disposition in HOLDING_DISPOSITIONS or d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST:
            for n, v in d.per_victim.items():
                out[n] += v
    return {n: money.r2(v) for n, v in out.items() if money.r2(v) > 0}


def check_plan(
    bundle: CaseBundle, ledger: Ledger | None = None, cfg: WapsiConfig | None = None
) -> list[CheckResult]:
    cfg = cfg or WapsiConfig()
    plan, trace = bundle.plan, bundle.trace
    if plan is None or trace is None:
        return [fail(NAME, "no_plan", "bundle has no " + ("hold plan" if plan is None else "trace"))]
    out: list[CheckResult] = []
    seen: set[str] = set()
    for dec in plan.decisions:
        if dec.account_id in seen:
            out.append(
                fail(
                    NAME,
                    "duplicate_decision",
                    f"{dec.account_id} has more than one decision",
                    [dec.account_id],
                )
            )
        seen.add(dec.account_id)
        out += check_decision(dec, bundle, ledger, cfg)

    decided = {d.account_id for d in plan.decisions}
    undecided = sorted(
        a for a, st in trace.accounts.items() if st.layer >= 1 and a not in decided and tainted_now(st) > 0
    )
    if undecided:
        out.append(
            warn(
                NAME,
                "undecided_accounts",
                "accounts holding tainted money have no decision: " + ", ".join(undecided),
                undecided,
            )
        )

    total_hold = money.r2(sum(d.hold_amount for d in plan.decisions))
    total_fwd = money.r2(sum(d.forward_hold_amount for d in plan.decisions))
    outbound = money.r2(
        sum(d.tainted_amount for d in plan.decisions if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST)
    )
    for label, stated, expected in (
        ("total_hold", plan.total_hold, total_hold),
        ("total_forward_hold", plan.total_forward_hold, total_fwd),
        ("total_recoverable", plan.total_recoverable, total_hold + outbound),
    ):
        if not money.eq(stated, expected):
            out.append(
                fail(
                    NAME,
                    "plan_total_mismatch",
                    f"{label} is {fmt(stated)}, decisions add up to {fmt(expected)}",
                    [fmt(stated)],
                )
            )

    over = money.r2(
        sum(
            max(0.0, d.hold_amount - hold_cap(trace.accounts[d.account_id]))
            for d in plan.decisions
            if d.account_id in trace.accounts
        )
    )
    if _gt(plan.over_hold_amount, 0.0) or _gt(over, 0.0):
        out.append(
            fail(
                NAME,
                "over_hold",
                f"plan over-holds {fmt(max(over, plan.over_hold_amount))} beyond "
                "tainted money; must be ₹0.00 [SOP-5-IX]",
                [fmt(max(over, plan.over_hold_amount))],
            )
        )

    per_victim = recomputed_per_victim(plan.decisions)
    stated_pv = {n: money.r2(v) for n, v in plan.per_victim_recoverable.items() if money.r2(v) > 0}
    for n in sorted(set(per_victim) | set(stated_pv)):
        if not money.eq(per_victim.get(n, 0.0), stated_pv.get(n, 0.0)):
            out.append(
                fail(
                    NAME,
                    "per_victim_mismatch",
                    f"recoverable for {n} is "
                    f"{fmt(stated_pv.get(n, 0.0))}, decisions add up to {fmt(per_victim.get(n, 0.0))}",
                    [fmt(stated_pv.get(n, 0.0))],
                )
            )

    victims = len(per_victim)
    if victims > 1 and plan.restoration_process != RestorationProcess.P2:
        out.append(
            fail(
                NAME,
                "restoration_process",
                f"{victims} victims have recoverable money; restoration "
                f"must follow Process 2 [SOP-11-P2], plan says {plan.restoration_process}",
                [str(plan.restoration_process)],
            )
        )
    if victims <= 1 and plan.restoration_process == RestorationProcess.P2:
        out.append(
            warn(
                NAME,
                "restoration_process",
                f"Process 2 with {victims} victim(s) holding recoverable money",
                [str(plan.restoration_process)],
            )
        )

    mule_money = any(role_of(bundle, d.account_id, d) == Role.MULE for d in plan.decisions) or any(
        role_of(bundle, a) == Role.MULE for a in trace.accounts
    )
    if mule_money and plan.str_ground is None:
        out.append(warn(NAME, "str_ground_missing", "mule accounts found but the plan names no STR ground"))
    return out


# --------------------------------------------------------------------------- draft checks

_FREEZE_RE = re.compile(
    r"\b(?:debit[\s-]*)?(?:freez(?:e|es|ing)|frozen|froze)\b"
    r"|\bblock(?:s|ed|ing)?\s+(?:all\s+)?(?:debits?|operations|transactions|withdrawals)\b"
    r"|\bblock(?:s|ed|ing)?\s+(?:(?:the|this|that|said|his|her|their|its)\s+)?"
    r"(?:(?:whole|entire|bank|savings|current|mule|subject|beneficiary|holder's|customer's)\s+)*(?:a/c|account)\b"
    r"|\bhold\s+(?:(?:the|this|that|said)\s+)?(?:a/c|account)\b"
    r"|\b(?:put|puts|place|places|keep|keeps|mark|marks)\s+(?:(?:the|this|that|said)\s+)?(?:a/c|account)"
    r"(?:\s+no\.?)?(?:\s+[A-Za-z]{2}\d{2}-\d{10})?\s+(?:on|under)\s+(?:hold|lien|(?:debit[\s-]*)?freeze)\b"
    r"|\bstop\s+all\s+(?:debits?|transactions|operations|withdrawals)\b"
    r"|\bdebit[\s-]*(?:block|stop)\b"
    r"|\b(?:hold|lien)\s+(?:on\s+)?(?:the\s+)?(?:entire|whole|full|total)\s+(?:balance|account)\b",
    re.IGNORECASE,
)
_STRONG_RE = re.compile(
    r"\b(?:entire|whole|full|complete|total)\b"
    r"|\ball\s+(?:debits?|transactions|operations|withdrawals|funds|money|balance)\b"
    r"|\bdebit[\s-]*(?:freez|block|stop)"
    r"|\bblock(?:s|ed|ing)?\s+(?:all\s+)?(?:debits?|operations|transactions|withdrawals)\b"
    r"|\bstop\s+all\b",
    re.IGNORECASE,
)
_PRE_CUE_RE = re.compile(
    r"(?:\bnot|\bno|\bnever|n't|\bwithout|\binstead\s+of|\brather\s+than|\bavoid(?:s|ing|ed)?|\bin\s+place\s+of"
    r"|\bas\s+opposed\s+to|\bcompared\s+(?:with|to)|\bversus|\bvs\.?|\bbaseline|\bunwarranted|\bnor|\bneither"
    r"|\bprevent(?:s|ing)?|\bunlike|\bwhereas)\b(?:\W+[\w'-]+){0,4}?\W+$",
    re.IGNORECASE,
)
_POST_CUE_RE = re.compile(
    r"^\W*(?:of\s+(?:[\w₹,.'-]+\s+){0,4})?(?:would|could|might|is\s+not|was\s+not|must\s+not|should\s+not"
    r"|shall\s+not|is\s+(?:discouraged|prohibited|unwarranted))\b"
    r"|\b(?:is|are)\s+(?:discouraged|prohibited|unwarranted)\b|\bnot\s+(?:permitted|allowed|warranted)\b"
    # descriptive statements of a legal requirement ("a debit freeze needs a Magistrate's order")
    r"|^\W*(?:of\s+(?:[\w₹,.'-]+\s+){0,4})?(?:needs?|requires?|can\s+only|may\s+only|is\s+only)\b"
    # counterfactuals ("freezing the touched accounts outright would have immobilised ...")
    r"|^\W*(?:(?!(?:as|because|since|so|if|it|which|that|and|or|but|until|till|when|while|unless)\b)"
    r"[\w₹,.'/-]+\s+){0,5}(?:would|could|might)\s+(?:have|be)\b"
    # the name of the comparison ("the whole-account freeze baseline of ₹5,03,50,000.00")
    r"|^[ \t]+baseline\b(?![ \t]+(?:a/c|accounts?)\b)",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[A-Za-z0-9)\]])\.(?=\s+[A-Z(\[])|[;!?\n]")


_SUSPEND_RE = re.compile(
    r"\bsuspen(?:d|ds|ded|ding|sion)\b(?:\s+(?:of|all|the|its|his|her|their|this|that|said|any))*\s+"
    r"(?:digital|internet|online|net|mobile|upi|imps|neft|e)[\s-]*(?:banking|services?|channels?|access|transfers?)?\b"
    r"|\b(?:suspen(?:d|ds|ded|ding|sion)|deactivat(?:e|es|ed|ing|ion)|disabl(?:e|es|ed|ing))\b"
    r"(?:\s+of)?\s+(?:the|this|that|said|its)\s+(?:(?:pool|nodal|escrow|merchant|whole|entire)\s+)?account\b"
    r"|\bseiz(?:e|es|ed|ing|ure)\b(?:\s+of)?\s+(?:the|this|that|said|such|its|all)?\s*"
    r"(?:(?:pool|nodal|escrow|whole|entire)\s+)?(?:accounts?|balances?|funds)\b",
    re.IGNORECASE,
)
_SUSPEND_PRE_CUE_RE = re.compile(
    r"(?:\bnot|\bno|\bnever|n't|\bwithout|\binstead\s+of|\brather\s+than|\bavoid(?:s|ing|ed)?|\bin\s+place\s+of"
    r"|\bnor|\bneither|\brefrain(?:s|ed|ing)?(?:\s+from)?|\bprevent(?:s|ing)?|\bunlike|\bbaseline"
    r"|\bas\s+opposed\s+to|\bcompared\s+(?:with|to)|\bversus|\bvs\.?|\bunwarranted|\bwhereas)\b"
    r"(?:[^\w,:]+[\w/'-]+){0,10}?[^\w,:]+$",  # the negation reaches up to 10 words, not past a comma
    re.IGNORECASE,
)


def suspension_instructions(text: str) -> list[str]:
    """Sentences that instruct suspension of digital banking or seizure of an account (SOP 9.1(iii)).

    Negated ("do not suspend digital banking or seize the account") and descriptive mentions ("seizure of the
    account needs an FIR") are allowed.
    """
    found: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        for m in _SUSPEND_RE.finditer(sentence):
            before = sentence[: m.start()]
            after = sentence[m.end() : m.end() + 60]
            if _SUSPEND_PRE_CUE_RE.search(before) or _POST_CUE_RE.search(after):
                continue
            found.append(sentence.strip())
            break
    return found


_AMOUNT_OBJECT_RE = re.compile(
    r"^\s*(?:(?:on|of|for|to|up\s+to|upto|only|a\s+sum\s+of|an\s+amount\s+of|the\s+amount\s+of"
    r"|amounting\s+to)\s+)*"
    r"(?:(?P<money>(?=[₹₨\d]|rs\b|inr\b|rupees?\b))"
    r"|(?:(?:the|an?|this|that|such)\s+)?"
    r"(?:(?:disputed|tainted|victim's|victim|fraud|identified|specific|said|credited|equivalent|traced|same)\s+)*"
    r"(?:amount|sum)\b"
    r"(?!\s+of\s+(?:(?:the|this|that|said|its)\s+)?(?:a/c|account|balance)"
    r"|\s+in\s+(?:the\s+)?(?:whole|entire|full)))",
    re.IGNORECASE,
)


_PLAIN_FREEZE_RE = re.compile(r"freez(?:e|es|ing)|frozen|froze", re.IGNORECASE)


def _amount_object(after: str) -> bool:
    """Is the object of a freeze verb an amount ("freeze ₹43,200.00", "a freeze on the disputed amount")?"""
    m = _AMOUNT_OBJECT_RE.match(after)
    if m is None:
        return False
    if m.group("money") is None:
        return True
    return any(x.start == m.end() for x in money.find_money_mentions(after))


def freeze_instructions(text: str) -> list[str]:
    """Sentences that instruct a whole-account / debit freeze or a block / hold on the account itself.

    Amounts elsewhere in the sentence do not excuse it ("Freeze account X with balance ₹45,000.00"). Allowed:
    negated, descriptive and counterfactual mentions, and a (non-debit, non-whole) freeze whose object is an
    amount ("place a freeze on ₹43,200.00 only").
    """
    found: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        for m in _FREEZE_RE.finditer(sentence):
            before = re.sub(r"\bwithout\s+(?:delay|fail)\b", "", sentence[: m.start()], flags=re.IGNORECASE)
            if _PRE_CUE_RE.search(before) or _POST_CUE_RE.search(sentence[m.end() : m.end() + 80]):
                continue
            if (
                _PLAIN_FREEZE_RE.fullmatch(m.group(0))  # not "block / hold / put the account ..."
                and not _STRONG_RE.search(sentence)
                and _amount_object(sentence[m.end() :])
            ):
                continue
            found.append(sentence.strip())
            break
    return found


def _grounds(obj) -> set[StrGround]:
    out: set[StrGround] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out |= _grounds(k) | _grounds(v)
    elif isinstance(obj, list | tuple | set):
        for v in obj:
            out |= _grounds(v)
    elif isinstance(obj, str):
        norm = obj.strip().upper().replace(" ", "_").replace("-", "_")
        if norm in StrGround.__members__:
            out.add(StrGround(norm))
    return out


def _grounds_in_text(text: str) -> set[StrGround]:
    low = text.lower()
    return {g for g in StrGround if g.value in text or g.value.replace("_", " ").lower() in low}


def _customer_name(ledger: Ledger | None, acct: str) -> str | None:
    if ledger is None:
        return None
    a = ledger.accounts([acct]).get(acct)
    if a is None or not a.customer_id:
        return None
    c = ledger.customers([a.customer_id]).get(a.customer_id)
    return c.name if c else None


def check_str(draft: Draft, bundle: CaseBundle, ledger: Ledger | None) -> list[CheckResult]:
    out: list[CheckResult] = []
    text = f"{draft.title}\n{draft.body}"
    structured = json.dumps(draft.structured, ensure_ascii=False, default=str)
    in_struct, in_body = _grounds(draft.structured), _grounds_in_text(text)
    if not in_struct:
        out.append(
            fail(
                NAME,
                "str_ground_missing",
                "STR structured payload names no PML Rule 2(1)(g) ground [PMLR-2-1-G]",
            )
        )
    if not in_body:
        out.append(fail(NAME, "str_ground_missing", "STR body names no PML Rule 2(1)(g) ground [PMLR-2-1-G]"))
    if in_struct and in_body and not (in_struct & in_body):
        out.append(
            fail(
                NAME,
                "str_ground_mismatch",
                "STR body and structured payload name different grounds",
                sorted(g.value for g in in_struct | in_body),
            )
        )
    plan_ground = bundle.plan.str_ground if bundle.plan else None
    if plan_ground and in_struct and plan_ground not in in_struct:
        out.append(
            warn(
                NAME,
                "str_ground_differs",
                f"plan names {plan_ground}, STR names " + ", ".join(sorted(g.value for g in in_struct)),
            )
        )
    named: list[str] = []
    slots = json.dumps(draft.slots, ensure_ascii=False)
    blob = "\n".join((text, structured, slots))
    mentioned = {canon for _, canon in money.find_account_ids(blob)}
    for acct in sorted(innocent_accounts(bundle)):
        if acct in mentioned or acct in blob:
            named.append(acct)
        name = _customer_name(ledger, acct)
        if name and name.casefold() in blob.casefold():
            named.append(name)
    if named:
        out.append(
            fail(
                NAME,
                "innocent_in_str",
                "innocent holders [SOP-5-XII] (bona fide recipients and unwitting conduits) must not be reported "
                "in an automatic STR; for a conduit the officer decides on a separate STR",
                named,
            )
        )
    return out


def _check_edd_str(draft: Draft, bundle: CaseBundle, acct: str, dec: HoldDecision | None) -> list[CheckResult]:
    """EDD memo: a conduit's memo recommends an STR for the officer's decision; a bona fide recipient's does not."""
    role = role_of(bundle, acct, dec)
    recommended = draft.structured.get("str_recommended") is True
    if role == Role.UNWITTING_CONDUIT and not recommended:
        return [
            fail(
                NAME,
                "edd_conduit_str_missing",
                f"EDD memo for {acct}: the account was used as a conduit, so an STR is recommended for the "
                "officer's decision (never filed automatically)",
                [acct],
            )
        ]
    if role in (Role.BONA_FIDE_RECIPIENT, Role.LEGIT) and recommended:
        return [
            fail(
                NAME,
                "edd_bona_fide_str",
                f"EDD memo for {acct}: a bona fide recipient gets no STR by default [SOP-5-XII]",
                [acct],
            )
        ]
    return []


def _check_account_level_request(
    draft: Draft, bundle: CaseBundle, dec: HoldDecision | None, st: AccountState | None
) -> list[CheckResult]:
    """The officer's request for an exceptional account-level hold: recorded reasons from the closed list, a named
    requesting officer, supervisor approval pending or given, and an eligible account."""
    out: list[CheckResult] = []
    acct = draft.account_id or ""
    raw = draft.structured.get("reasons")
    values = [str(v) for v in raw] if isinstance(raw, list) else []
    unknown = [v for v in values if v not in ACCOUNT_LEVEL_CODES]
    if not recorded_reasons(values):
        out.append(
            fail(
                NAME,
                "account_level_without_reasons",
                f"the request for {acct} records no reason from the closed list "
                f"({', '.join(sorted(ACCOUNT_LEVEL_CODES))}) [SOP-5-IX]",
                [acct],
            )
        )
    if unknown:
        out.append(
            fail(NAME, "account_level_unknown_reason", "reasons not on the closed list: " + ", ".join(unknown), unknown)
        )
    if not str(draft.structured.get("requested_by") or "").strip():
        out.append(fail(NAME, "account_level_no_requester", f"the request for {acct} names no requesting officer"))
    if draft.structured.get("supervisor_approval_required") is not True:
        out.append(
            fail(
                NAME,
                "account_level_no_maker_checker",
                f"the request for {acct} must state that a supervisor approves it (maker-checker)",
                [acct],
            )
        )
    if dec is not None and st is not None:
        why = None
        if st.layer == 0:
            why = "a victim account"
        elif not st.is_home_bank:
            why = "an account at another bank"
        elif is_intermediary(bundle, st, dec):
            why = f"a nodal / pool / escrow account (no suspension or seizure) [{INTERMEDIARY_CITE}]"
        elif st.account_type == AccountType.MERCHANT or role_of(bundle, acct, dec) == Role.MERCHANT:
            why = "a merchant account [SOP-9-2]"
        elif dec.disposition not in ACCOUNT_LEVEL_BASE | {ACCOUNT_LEVEL}:
            why = f"an account without an amount-specific hold ({dec.disposition})"
        if why:
            out.append(
                fail(
                    NAME,
                    "account_level_not_eligible",
                    f"{acct} is {why}; an account-level exception cannot be raised on it",
                    [acct],
                )
            )
    return out


def check_draft_policy(
    draft: Draft, bundle: CaseBundle, ledger: Ledger | None = None, cfg: WapsiConfig | None = None
) -> list[CheckResult]:
    cfg = cfg or WapsiConfig()
    out: list[CheckResult] = []
    acct = draft.account_id
    dec = _decision(bundle, acct)
    st = bundle.trace.accounts.get(acct) if (bundle.trace and acct) else None

    if acct and dec is not None:
        out += check_decision(dec, bundle, ledger, cfg)
    if draft.kind in ACCOUNT_KINDS and (acct is None or dec is None):
        out.append(
            fail(
                NAME,
                "draft_without_decision",
                f"{draft.kind} draft needs an account with a plan decision",
                [acct or ""],
            )
        )

    if draft.kind in (DraftKind.HOLD_INSTRUCTION, DraftKind.CUSTOMER_NOTICE):
        if dec is not None and dec.disposition not in HOLD_DRAFT_DISPOSITIONS:
            out.append(
                fail(
                    NAME,
                    "hold_instruction_not_holding",
                    f"{draft.kind} for {acct} whose disposition is {dec.disposition}",
                    [acct or ""],
                )
            )
        if draft.kind == DraftKind.CUSTOMER_NOTICE and dec is not None and dec.disposition == ACCOUNT_LEVEL:
            out.append(
                fail(
                    NAME,
                    "customer_notice_account_level",
                    f"the notice tells the holder of {acct} that only an amount is on hold, but an exceptional "
                    "account-level hold applies; the officer informs the holder instead",
                    [acct or ""],
                )
            )
        if (
            draft.kind == DraftKind.HOLD_INSTRUCTION
            and st is not None
            and is_intermediary(bundle, st, dec)
            and INTERMEDIARY_CITE not in draft.citation_ids
        ):
            out.append(
                warn(
                    NAME,
                    "intermediary_rule_not_stated",
                    f"hold instruction for nodal / pool / escrow account {acct} does not state that only the "
                    f"disputed amount is held, with no suspension or seizure [{INTERMEDIARY_CITE}]",
                    [acct or ""],
                )
            )
    if (
        draft.kind == DraftKind.EDD_MEMO
        and dec is not None
        and dec.disposition != Disposition.HOLD_AMOUNT_NO_ESCALATION
    ):
        out.append(
            fail(
                NAME,
                "edd_memo_wrong_disposition",
                f"EDD memo for {acct} whose disposition is {dec.disposition}",
                [acct or ""],
            )
        )
    if draft.kind == DraftKind.EDD_MEMO and acct:
        out += _check_edd_str(draft, bundle, acct, dec)
    if draft.kind == DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST:
        out += _check_account_level_request(draft, bundle, dec, st)
    if (
        draft.kind == DraftKind.OUTBOUND_REQUEST
        and dec is not None
        and dec.disposition != Disposition.OUTBOUND_CFCFRMS_REQUEST
    ):
        out.append(
            fail(
                NAME,
                "outbound_wrong_disposition",
                f"outbound request for {acct} whose disposition is {dec.disposition}",
                [acct or ""],
            )
        )
    if draft.kind == DraftKind.STR:
        out += check_str(draft, bundle, ledger)

    if acct and is_innocent(bundle, acct, dec):
        blob = " ".join([draft.body, json.dumps(draft.slots), json.dumps(draft.structured, default=str)])
        if ESCALATE in blob or "ESCALATE_S106" in blob:
            out.append(
                fail(
                    NAME,
                    "innocent_escalated",
                    f"draft for innocent holder {acct} carries an s.106 escalation flag [SOP-5-XII]",
                    [acct],
                )
            )

    freezes = freeze_instructions(f"{draft.title}\n{draft.body}")
    if freezes and approved_account_level(bundle, dec, draft):
        # the hold instruction of a recorded, supervisor-approved exception may state the account-level action
        out.append(
            warn(
                NAME,
                "account_level_instruction",
                f"the draft instructs account-level action on {acct}, covered by the supervisor-approved exception "
                "on record [SOP-5-IX]",
                freezes,
            )
        )
    elif freezes:
        out.append(
            fail(
                NAME,
                "whole_account_freeze",
                "the draft instructs a whole-account or debit freeze; holds are amount-specific by default and "
                "account-level action needs a recorded, supervisor-approved exception [SOP-5-IX] "
                "[CASE-KARTIK-CHATUR]",
                freezes,
            )
        )

    suspensions = suspension_instructions(f"{draft.title}\n{draft.body}")
    if suspensions:
        pool_draft = st is not None and is_intermediary(bundle, st, dec)
        out.append(
            fail(
                NAME,
                "suspension_or_seizure",
                "the draft instructs suspension of digital banking or seizure of "
                + (f"nodal / pool / escrow account {acct}" if pool_draft else "an account")
                + f"; hold the disputed amount only [{INTERMEDIARY_CITE}] [SOP-5-IX]",
                suspensions,
            )
        )

    if draft.kind in CASE_LEVEL_KINDS:
        out += check_plan(bundle, ledger, cfg)

    if not any(not r.passed and r.severity == "error" for r in out):
        out.append(
            ok(NAME, "hold, intermediary, escalation, STR and freeze rules re-checked against the trace")
        )
    return _dedupe(out)


def _dedupe(results: list[CheckResult]) -> list[CheckResult]:
    seen: set[tuple] = set()
    out = []
    for r in results:
        key = (r.name, r.passed, r.severity, r.code, r.message, tuple(r.spans))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out
