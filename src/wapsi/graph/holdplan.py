"""Hold plan: one amount-specific decision per account that received tainted money (AGENTS.md, convention 6).

- hold only the tainted amount, ``min(tainted_now, balance)``; never the whole account (SOP 5(ix), 9.1)
- home-bank nodal / pool / escrow accounts -> ``HOLD_AMOUNT_INTERMEDIARY`` (SOP 9.1(iii)): hold the disputed
  amount only, never suspend digital banking or seize the account, never a forward hold, never an s.106 flag
- merchants (SOP 9.2): every tainted payment paid for delivered goods -> ``NO_HOLD_UPDATE_ONLY``; otherwise
  ``HOLD_IN_POOL`` for the tainted money still in the account that paid for undelivered / cancelled /
  un-invoiced goods, ``min(undelivered, balance)``
- innocent holders / legitimate accounts -> ``HOLD_AMOUNT_NO_ESCALATION`` (SOP 5(xii))
- other-bank accounts (including other-bank intermediaries) holding tainted money -> ``OUTBOUND_CFCFRMS_REQUEST``
  (hold 0, request in tainted_amount)
- forward hold only on first-layer, non-intermediary accounts whose balance is below the disputed amount
  (SOP 9.1(i)): the tainted money they sent to ATM / crypto exits or to unlocated destinations
- s.106 BNSS escalation is only a flag (reason code + ``requires_human``); FIR + prior NCRP reports needed
- a holding disposition always holds money: nothing left to hold -> ``CASHED_OUT`` / ``NO_ACTION``
- a victim account of the case that holds another complaint's money gets its amount-specific decision, is
  never an escalation candidate and always needs a human (``VICTIM_ACCOUNT_ON_TRAIL``)
- whether other-bank accounts were followed (consortium) is read from the trace, not from ``cfg``
- ``total_hold`` covers every holding disposition (intermediary and in-pool holds included);
  ``total_recoverable`` = holds + outbound request amounts. Money at any layer can be released to the victim
  (SOP 5(x)). Per-victim shares of a partial hold use largest-remainder apportionment in paise.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping

from wapsi.config import WapsiConfig
from wapsi.graph.common import (
    PASS_THROUGH_TERMINALS,
    delivery_fractions,
    fully_delivered,
    merchant_inflows,
    merchant_undelivered,
    round_vec,
    vec_total,
)
from wapsi.models import (
    HOLDING_DISPOSITIONS,
    INTERMEDIARY_ACCOUNT_TYPES,
    AccountState,
    AccountType,
    ClassificationResult,
    Disposition,
    HoldDecision,
    HoldPlan,
    RestorationProcess,
    Role,
    StrGround,
    TerminalKind,
    TraceResult,
    TrailEdge,
)
from wapsi.money import EPSILON, fmt_inr, r2
from wapsi.ports import Ledger

DISPOSITION_CITATIONS: dict[Disposition, list[str]] = {
    Disposition.HOLD_AMOUNT: ["SOP-9-1", "SOP-ANNEX-I", "SOP-5-IX"],
    Disposition.HOLD_AMOUNT_NO_ESCALATION: ["SOP-5-XII", "SOP-10-1", "SOP-9-1"],
    Disposition.NO_HOLD_UPDATE_ONLY: ["SOP-9-2"],
    Disposition.HOLD_IN_POOL: ["SOP-9-2"],
    Disposition.HOLD_AMOUNT_INTERMEDIARY: ["SOP-9-1-III", "SOP-9-1", "SOP-ANNEX-I"],
    Disposition.OUTBOUND_CFCFRMS_REQUEST: ["SOP-9-1", "SOP-6"],
    Disposition.FORWARD_HOLD: ["SOP-9-1-I", "SOP-DEF-FIRST-LAYER"],
    Disposition.CASHED_OUT: ["SOP-9-1"],
    Disposition.NO_ACTION: [],
}
FORWARD_HOLD_CITATIONS = ["SOP-9-1-I", "SOP-DEF-FIRST-LAYER"]
ESCALATION_CITATIONS = ["BNSS-106", "CASE-KARTIK-CHATUR"]
P2_CITATIONS = ["SOP-5-XIII", "SOP-11-P2"]

REASON_FORWARD_HOLD = "FORWARD_HOLD_FIRST_LAYER"
REASON_ESCALATE = "ESCALATE_S106_CANDIDATE"
REASON_ROLE_UNKNOWN = "ROLE_UNKNOWN"
REASON_HOLD_CAPPED = "HOLD_CAPPED_AT_BALANCE"
REASON_NO_SUSPENSION = "NO_SUSPENSION_OR_SEIZURE"  # SOP 9.1(iii): hold the amount, keep the account running
REASON_VICTIM_ON_TRAIL = (
    "VICTIM_ACCOUNT_ON_TRAIL"  # a victim account of the case holds another victim's money
)

_NO_ESCALATION_ROLES = frozenset({Role.LEGIT, Role.INNOCENT_HOLDER})
_REVIEW_ROLES = frozenset({Role.UNKNOWN, Role.VICTIM})
_FORWARD_ROLES = frozenset({Role.MULE, Role.UNKNOWN, Role.VICTIM})


def account_role(st: AccountState, classifications: Mapping[str, ClassificationResult]) -> Role:
    if st.account_type in INTERMEDIARY_ACCOUNT_TYPES:
        return Role.INTERMEDIARY
    if st.account_type == AccountType.MERCHANT:
        return Role.MERCHANT
    c = classifications.get(st.account_id)
    return c.role if c is not None else Role.UNKNOWN


def _add(acc: dict[str, float], vec: Mapping[str, float]) -> None:
    for k, v in vec.items():
        acc[k] += v


def _is_outbound_dest(e: TrailEdge, home: str, follow_consortium: bool) -> bool:
    return e.to_bank_id is not None and e.to_bank_id != home and not follow_consortium


def exit_vectors(trace: TraceResult) -> dict[str, dict[str, float]]:
    """account -> tainted money (per ncrp_id) it sent to ATM / crypto exits."""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in trace.edges:
        if e.to_account is None and e.from_account is not None:
            _add(out[e.from_account], e.tainted)
    return {a: round_vec(v, trace.ncrp_ids) for a, v in out.items()}


def consortium_followed(
    trace: TraceResult,
    home_bank_id: str,
    victims: Iterable[str] = (),
    held_outbound: float | None = None,
) -> bool:
    """Whether the trace followed other-bank accounts (``TraceConfig.follow_consortium``), read from the trace.

    Evidence, in order: a traced other-bank account (not a victim of the case) that is an ``OTHER_BANK`` terminal
    (not followed) or no terminal at all (followed). Otherwise: tainted money sent to untraced other-bank accounts
    is part of ``totals.outbound`` only when they were not followed. ``held_outbound`` is the part of
    ``totals.outbound`` still sitting in traced other-bank accounts (default: their tainted money).
    """
    skip = set(victims)
    for st in trace.accounts.values():
        if st.layer == 0 or st.is_home_bank or st.account_id in skip:
            continue
        if st.terminal == TerminalKind.OTHER_BANK:
            return False
        if st.terminal is None:
            return True
    untraced = sum(
        e.tainted_total
        for e in trace.edges
        if e.to_account is not None
        and e.to_account not in trace.accounts
        and e.to_bank_id is not None
        and e.to_bank_id != home_bank_id
    )
    if untraced <= 0:
        return True  # nothing depends on it
    if held_outbound is None:
        held_outbound = sum(st.tainted_now_total for st in trace.accounts.values() if not st.is_home_bank)
    return trace.totals.outbound - held_outbound < untraced / 2


def forward_hold_vectors(
    trace: TraceResult, home_bank_id: str, follow_consortium: bool | None = None
) -> dict[str, dict[str, float]]:
    """account -> tainted money (per ncrp_id) it sent to exits or to unlocated destinations.

    Unlocated destinations are accounts outside ``trace.accounts`` that are not un-followed other-bank accounts
    (those are outbound). Money sent back to a victim account is not counted. ``follow_consortium=None`` reads
    it from the trace (``consortium_followed``).
    """
    if follow_consortium is None:
        follow_consortium = consortium_followed(trace, home_bank_id)
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in trace.edges:
        if e.from_account is None:
            continue
        if e.to_account is None:
            _add(out[e.from_account], e.tainted)
        elif e.to_account not in trace.accounts and not _is_outbound_dest(e, home_bank_id, follow_consortium):
            _add(out[e.from_account], e.tainted)
    return {a: round_vec(v, trace.ncrp_ids) for a, v in out.items()}


def _scale(vec: Mapping[str, float], target: float, order: list[str]) -> dict[str, float]:
    """Scale a per-victim vector down to ``target`` rupees.

    Largest-remainder apportionment in paise: every part is >= 0 and <= the victim's own amount, and the parts
    sum exactly to ``target``; ties go to victims earlier in ``order``.
    """
    total = sum(vec.values())
    target = r2(target)
    if total <= 0 or target <= 0:
        return {}
    if target >= r2(total):
        return dict(vec)
    rank = {k: i for i, k in enumerate(order)}
    keys = sorted((k for k, v in vec.items() if v > 0), key=lambda k: (rank.get(k, len(rank)), k))
    goal = round(target * 100)
    exact = {k: vec[k] * goal / total for k in keys}
    paise = {k: math.floor(exact[k]) for k in keys}
    left = goal - sum(paise.values())
    for k in sorted(keys, key=lambda k: -(exact[k] - paise[k])):  # stable: ties keep ``order``
        if left <= 0:
            break
        paise[k] += 1
        left -= 1
    return round_vec({k: p / 100 for k, p in paise.items()}, order)


def _case_victims(ledger: Ledger, ncrp_ids: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for n in ncrp_ids:
        try:
            out.add(ledger.complaint(n).victim_account)
            out.update(t.from_account for t in ledger.complaint_transactions(n) if t.from_account)
        except (LookupError, ValueError):  # complaint unknown to this ledger
            pass
    return out


def build_hold_plan(
    trace: TraceResult,
    classifications: Mapping[str, ClassificationResult] | None,
    ledger: Ledger,
    cfg: WapsiConfig | None = None,
) -> HoldPlan:
    cfg = cfg or WapsiConfig()
    classifications = classifications or {}
    home = ledger.home_bank_id()
    order = list(trace.ncrp_ids)

    fir_ids: set[str] = set()
    for n in order:
        try:
            if ledger.complaint(n).fir_number:
                fir_ids.add(n)
        except (LookupError, ValueError):  # complaint unknown to this ledger
            pass

    states = [st for st in trace.accounts.values() if st.layer > 0 and st.tainted_received_total > 0]
    signals = ledger.external_signals([st.account_id for st in states])
    roles = {st.account_id: account_role(st, classifications) for st in states}
    victims = _case_victims(ledger, order)
    terminal_sources = {a for a, st in trace.accounts.items() if st.terminal in PASS_THROUGH_TERMINALS}
    merchants = [a for a, r in roles.items() if r == Role.MERCHANT]
    fractions: dict[str, dict[str, float]] = {}
    undelivered: dict[str, dict[str, float]] = {}
    if merchants:
        fractions = delivery_fractions(ledger, merchant_inflows(trace.edges, merchants, terminal_sources))
        undelivered = merchant_undelivered(
            trace.edges,
            fractions,
            {m: trace.accounts[m].tainted_now for m in merchants},
            terminal_sources,
            order,
        )
    delivered = fully_delivered(fractions)
    held_outbound = sum(
        vec_total(undelivered[st.account_id]) if st.account_id in undelivered else st.tainted_now_total
        for st in states
        if not st.is_home_bank
    )
    follow = consortium_followed(trace, home, victims, held_outbound)
    exits = exit_vectors(trace)
    forwards = forward_hold_vectors(trace, home, follow)
    reached: dict[str, set[str]] = defaultdict(set)  # account -> ncrp_ids whose money touched it
    for e in trace.edges:
        if e.from_account is not None:
            reached[e.from_account].update(e.tainted)
        # payouts of merchant / intermediary terminals taint nobody
        if e.to_account is not None and e.from_account not in terminal_sources:
            reached[e.to_account].update(e.tainted)

    decisions: list[HoldDecision] = []
    for st in sorted(states, key=lambda s: (s.layer, s.account_id)):
        acct = st.account_id
        role = roles[acct]
        tn = st.tainted_now_total
        bal = st.balance_now
        exited = vec_total(exits.get(acct, {})) > 0
        hold = 0.0
        forward = 0.0
        per_victim: dict[str, float] = {}
        extra: list[str] = []
        notes: list[str] = []

        if role == Role.INTERMEDIARY and st.is_home_bank:
            # SOP 9.1(iii): hold the disputed amount only; SOP 9.1(i): never a forward hold here
            hold = r2(min(tn, max(bal, 0.0)))
            if hold > 0:
                disp = Disposition.HOLD_AMOUNT_INTERMEDIARY
                per_victim = _scale(st.tainted_now, hold, order)
                extra.append(REASON_NO_SUSPENSION)
                notes.append(
                    "Nodal / pool / escrow account: only the disputed amount is held; digital banking is not "
                    "suspended and the account is not seized."
                )
                if hold < tn - EPSILON:
                    extra.append(REASON_HOLD_CAPPED)
                    notes.append(f"Balance covers only {fmt_inr(hold)} of the {fmt_inr(tn)} tainted money.")
            else:
                disp = Disposition.NO_ACTION
        elif role == Role.MERCHANT:
            # SOP 9.2: goods delivered -> update only; otherwise hold the undelivered money in the pool
            und = undelivered.get(acct, {})
            und_total = vec_total(und)
            if acct in delivered or (und_total <= 0 and tn > 0):
                disp = Disposition.NO_HOLD_UPDATE_ONLY
            elif not st.is_home_bank:
                if und_total > 0:
                    disp = Disposition.OUTBOUND_CFCFRMS_REQUEST
                    tn = und_total  # the request covers the undelivered money only
                    per_victim = dict(und)
                else:
                    disp = Disposition.CASHED_OUT if exited else Disposition.NO_ACTION
            else:
                hold = r2(min(und_total, max(bal, 0.0)))
                if hold > 0:
                    disp = Disposition.HOLD_IN_POOL
                    per_victim = _scale(und, hold, order)
                    if hold < und_total - EPSILON:
                        extra.append(REASON_HOLD_CAPPED)
                        notes.append(
                            f"Balance covers only {fmt_inr(hold)} of the {fmt_inr(und_total)} tainted money paid "
                            "for undelivered goods."
                        )
                else:
                    disp = Disposition.CASHED_OUT if exited else Disposition.NO_ACTION
            if und_total > 0 and und_total < st.tainted_now_total - EPSILON:
                notes.append(
                    f"{fmt_inr(r2(st.tainted_now_total - und_total))} of the tainted money paid for delivered goods "
                    "(update only)."
                )
        elif not st.is_home_bank:
            if tn > 0:
                disp = Disposition.OUTBOUND_CFCFRMS_REQUEST
                per_victim = dict(st.tainted_now)
            else:
                disp = Disposition.CASHED_OUT if exited else Disposition.NO_ACTION
        else:
            hold = r2(min(tn, max(bal, 0.0)))
            if role in _NO_ESCALATION_ROLES:
                primary = Disposition.HOLD_AMOUNT_NO_ESCALATION
            else:
                primary = Disposition.HOLD_AMOUNT
            if (
                st.layer == 1
                and role in _FORWARD_ROLES
                and st.account_type not in INTERMEDIARY_ACCOUNT_TYPES
                and bal < st.tainted_received_total - EPSILON
            ):
                forward = vec_total(forwards.get(acct, {}))
            if hold > 0:
                disp = primary
            elif forward > 0:
                disp = Disposition.FORWARD_HOLD
            else:
                disp = Disposition.CASHED_OUT if exited else Disposition.NO_ACTION
            per_victim = _scale(st.tainted_now, hold, order) if hold > 0 else {}
            if hold > 0 and hold < tn - EPSILON:
                extra.append(REASON_HOLD_CAPPED)
                notes.append(f"Balance covers only {fmt_inr(hold)} of the {fmt_inr(tn)} tainted money.")
            if forward > 0:
                how = (
                    "cashed out"
                    if forward <= vec_total(exits.get(acct, {})) + EPSILON
                    else ("cashed out or moved to untraced accounts")
                )
                notes.append(
                    f"Forward hold on future credits for {fmt_inr(forward)} {how} by this first-layer account"
                )

        on_trail_victim = acct in victims
        escalate = False
        if (
            st.is_home_bank
            and not on_trail_victim
            and role in (Role.MULE, Role.UNKNOWN)
            and disp
            in (
                Disposition.HOLD_AMOUNT,
                Disposition.FORWARD_HOLD,
            )
        ):
            touched = (reached.get(acct, set()) | set(st.tainted_now)) & fir_ids
            sig = signals.get(acct)
            prior = sig.ncrp_prior_reports if sig is not None else 0
            if touched and prior >= cfg.escalation_min_prior_reports:
                escalate = True
                notes.append(
                    f"s.106 BNSS escalation candidate (FIR on complaint {', '.join(sorted(touched))}; "
                    f"{prior} prior NCRP reports) - for the investigating officer to decide."
                )
        if role in _REVIEW_ROLES:
            extra.append(REASON_ROLE_UNKNOWN)
            notes.append("No reliable role classification; review before acting.")
        if on_trail_victim:
            extra.append(REASON_VICTIM_ON_TRAIL)
            notes.append(
                "This is a victim account of the case that also received money traced to another complaint; "
                "never an escalation candidate - review before acting."
            )

        reason_codes = [disp.value]
        citation_ids = list(DISPOSITION_CITATIONS[disp])
        if escalate:
            reason_codes.append(REASON_ESCALATE)
        if forward > 0:
            reason_codes.append(REASON_FORWARD_HOLD)
            citation_ids += FORWARD_HOLD_CITATIONS
        if escalate:
            citation_ids += ESCALATION_CITATIONS
        reason_codes += extra

        decisions.append(
            HoldDecision(
                account_id=acct,
                bank_id=st.bank_id,
                disposition=disp,
                hold_amount=hold,
                forward_hold_amount=forward,
                tainted_amount=tn,
                balance_now=bal,
                layer=st.layer,
                role=role,
                per_victim=per_victim,
                citation_ids=list(dict.fromkeys(citation_ids)),
                reason_codes=list(dict.fromkeys(reason_codes)),
                requires_human=escalate or role in _REVIEW_ROLES or on_trail_victim,
                notes=" ".join(notes),
            )
        )

    # ---- plan totals
    recoverable_by_victim: dict[str, float] = {n: 0.0 for n in order}
    total_hold = total_forward = outbound = over_hold = 0.0
    for d in decisions:
        total_hold += d.hold_amount
        total_forward += d.forward_hold_amount
        if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST:
            outbound += d.tainted_amount
        if d.disposition in HOLDING_DISPOSITIONS or d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST:
            for n, x in d.per_victim.items():
                recoverable_by_victim[n] = recoverable_by_victim.get(n, 0.0) + x
        over_hold += max(0.0, d.hold_amount - d.tainted_amount)
    per_victim_recoverable = {n: r2(x) for n, x in recoverable_by_victim.items()}
    with_money = sum(1 for x in per_victim_recoverable.values() if x > 0)
    process = RestorationProcess.P2 if with_money > 1 else RestorationProcess.P1
    if process == RestorationProcess.P2:
        decisions = [
            d.model_copy(update={"citation_ids": list(dict.fromkeys([*d.citation_ids, *P2_CITATIONS]))})
            for d in decisions
        ]

    baseline = [st for st in states if st.is_home_bank]
    str_ground = None
    for d in decisions:
        if d.role == Role.MULE and (d.tainted_amount > 0 or vec_total(exits.get(d.account_id, {})) > 0):
            str_ground = StrGround.PROCEEDS_OF_CRIME
            break

    return HoldPlan(
        case_id=trace.case_id,
        rule=trace.rule,
        decisions=decisions,
        total_hold=r2(total_hold),
        total_forward_hold=r2(total_forward),
        total_recoverable=r2(total_hold + outbound),
        per_victim_recoverable=per_victim_recoverable,
        over_hold_amount=r2(over_hold),
        baseline_freeze_amount=r2(sum(max(st.balance_now, 0.0) for st in baseline)),
        baseline_freeze_accounts=len(baseline),
        restoration_process=process,
        str_ground=str_ground,
    )
