"""Turn a planned ``CaseBundle`` into officer-ready drafts.

Numbers come from data, words come from the LLM: templates are filled from slots (``wapsi.draft.factsheet``), and
the only LLM text (STR grounds, EDD rationale, case summary) is written with ``⟦slot⟧`` placeholders, validated,
retried once with feedback and otherwise replaced by a deterministic paragraph (``structured["llm_fallback"]``).

Every draft carries ``structured["skeleton"]``: its title and body with ``⟦slot⟧`` wherever a slot value was
inserted (templates, prose and cleaned free text alike). The body is exactly the skeleton filled with the slot
values, which lets the verifier bind each value to its position.

Innocent holders (SOP 5(xii)) are never identified in the STR, whichever bank holds the account.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from wapsi.draft import prose as prose_mod
from wapsi.draft.factsheet import (
    CASH_OUT_CHANNELS,
    build_factsheet,
    build_slots,
    case_complaints,
    case_slots,
    decisions_by_account,
    edge_slots,
    inflow_edges,
    qualified_slots,
    qualify,
    require_planned,
    role_of,
    victim_account_slots,
    victims_of,
)
from wapsi.draft.render import (
    Cleaned,
    TextCleaner,
    TrackingSlots,
    checked_citations,
    fill,
    neutralize,
    ordinal,
    plural,
    render_skeleton,
    unmark,
    words,
)
from wapsi.models import (
    HOLDING_DISPOSITIONS,
    INTERMEDIARY_ACCOUNT_TYPES,
    Account,
    AccountState,
    ApportionmentRule,
    Bank,
    CaseBundle,
    Complaint,
    Customer,
    Disposition,
    Draft,
    DraftKind,
    ExternalSignal,
    HoldDecision,
    HoldPlan,
    RestorationProcess,
    Role,
    StrGround,
    TraceResult,
    Transaction,
)
from wapsi.money import r2
from wapsi.ports import LLM, DocSearch, Ledger
from wapsi.timeutil import ensure_utc, fmt_ist, utcnow

log = logging.getLogger(__name__)

HOLD_DRAFT_DISPOSITIONS = frozenset(
    {
        Disposition.HOLD_AMOUNT,
        Disposition.HOLD_AMOUNT_NO_ESCALATION,
        Disposition.HOLD_IN_POOL,
        Disposition.HOLD_AMOUNT_INTERMEDIARY,
        Disposition.FORWARD_HOLD,
    }
)
ESCALATION_CODE = "ESCALATE_S106_CANDIDATE"
STR_SUBJECT_ROLES = frozenset({Role.MULE, Role.UNKNOWN})
NON_STR_DISPOSITIONS = frozenset(
    {
        Disposition.HOLD_AMOUNT_NO_ESCALATION,
        Disposition.NO_HOLD_UPDATE_ONLY,
        Disposition.HOLD_AMOUNT_INTERMEDIARY,
        Disposition.HOLD_IN_POOL,
    }
)

ACTION_TEXT = {
    Disposition.HOLD_AMOUNT: "amount-specific hold [SOP-9-1]",
    Disposition.HOLD_AMOUNT_NO_ESCALATION: "hold only; innocent holder, EDD memo, no escalation [SOP-5-XII]",
    Disposition.HOLD_IN_POOL: "order cancelled; amount held in the pool / nodal account [SOP-9-2]",
    Disposition.FORWARD_HOLD: "forward hold on future credits, first-layer account only [SOP-9-1-I]",
    Disposition.NO_HOLD_UPDATE_ONLY: "no hold: merchant, goods delivered; update only [SOP-9-2]",
    Disposition.HOLD_AMOUNT_INTERMEDIARY: "disputed amount held in the nodal / pool / escrow account; no "
    "suspension or seizure of the account [SOP-9-1-III]",
    Disposition.OUTBOUND_CFCFRMS_REQUEST: "hold requested from the other bank through CFCFRMS [SOP-6]",
    Disposition.CASHED_OUT: "cashed out; nothing left to hold",
    Disposition.ESCALATE_S106_CANDIDATE: "escalation candidate; human decision required [BNSS-106]",
    Disposition.NO_ACTION: "no action",
}
INNOCENT_LABEL = "account operated without the holder's knowledge"
MASK_LABEL = {
    Role.INNOCENT_HOLDER: INNOCENT_LABEL,
    Role.MERCHANT: "merchant account",
    Role.INTERMEDIARY: "nodal / pool / escrow account",
    Role.VICTIM: "victim account",
    Role.LEGIT: "non-subject account",
}
GROUND_TEXT = {
    StrGround.PROCEEDS_OF_CRIME: "give rise to a reasonable ground of suspicion that they may involve proceeds of "
    "an offence",
    StrGround.UNUSUAL_COMPLEXITY: "appear to be made in circumstances of unusual or unjustified complexity",
    StrGround.NO_ECONOMIC_RATIONALE: "appear to have no economic rationale or bona fide purpose",
    StrGround.TERROR_FINANCING: "give rise to a reasonable ground of suspicion that they may involve financing of "
    "activities relating to terrorism",
}
PROCESS_TEXT = {
    RestorationProcess.P1: "Process 1: a single victim has recoverable money",
    RestorationProcess.P2: "Process 2: more than one victim has recoverable money (competing claims), shared "
    "pro-rata",
    RestorationProcess.P3: "Process 3: restoration through the competent court",
    RestorationProcess.P4: "Process 4: attachment / forfeiture under Section 107 BNSS",
    RestorationProcess.P5: "Process 5: as directed by the jurisdictional court",
}


def _rule_text(rule: ApportionmentRule) -> str:
    if rule == ApportionmentRule.PRO_RATA:
        return "pro-rata (PRO_RATA), the SOP default for equitable distribution"
    return f"{rule.value} (differs from the SOP default of pro-rata)"


def draft_id(case_id: str, kind: DraftKind, account_id: str | None) -> str:
    return f"DRF-{case_id}-{kind.value}-{account_id or 'CASE'}"


# --------------------------------------------------------------------------- case context


@dataclass
class _Ctx:
    bundle: CaseBundle
    ledger: Ledger
    llm: LLM
    block_untrusted_copy: bool = True
    trace: TraceResult = field(init=False)
    plan: HoldPlan = field(init=False)

    def __post_init__(self) -> None:
        self.trace, self.plan = require_planned(self.bundle)
        b, lg, tr = self.bundle, self.ledger, self.trace
        self.case_id = b.case.case_id
        self.decisions: dict[str, HoldDecision] = decisions_by_account(b)
        # victims (layer 0) never get account drafts
        self.ordered = sorted(
            (d for d in self.plan.decisions if d.layer != 0), key=lambda d: (d.layer, d.account_id)
        )
        self.banks: dict[str, Bank] = lg.banks()
        self.home_bank_id = lg.home_bank_id()
        home = self.banks.get(self.home_bank_id)
        self.home_bank_name = home.name if home else self.home_bank_id
        ids = sorted(set(tr.accounts) | set(self.decisions))
        self.accounts: dict[str, Account] = lg.accounts(ids)
        cust_ids = {a.customer_id for a in self.accounts.values() if a.customer_id}
        customers: dict[str, Customer] = lg.customers(cust_ids)
        self.customer_of: dict[str, Customer] = {
            a: customers[m.customer_id] for a, m in self.accounts.items() if m.customer_id in customers
        }
        self.signals: dict[str, ExternalSignal] = lg.external_signals(ids)
        self.complaints: dict[str, Complaint] = case_complaints(b, lg)
        self.victim_ncrp: dict[str, str] = {c.victim_account: n for n, c in self.complaints.items()}
        self.warnings: list[str] = []
        self.txns: dict[str, Transaction] = self._load_txns()

        # slot pools
        self.contract_case = case_slots(b, lg)
        layered = {a for a, st in tr.accounts.items() if st.layer >= 1} | set(self.decisions)
        self.case_pool = {
            **self.contract_case,
            **qualified_slots(b, lg, layered),
            **edge_slots(b),
            **victim_account_slots(b, lg),
        }
        # identifiers the verifier accepts in a body
        self.known_accounts = set(tr.accounts) | set(self.victim_ncrp)
        self.known_utrs = {e.utr.upper() for e in tr.edges}
        stamps = [b.case.opened_at, tr.computed_at, tr.window_start, tr.window_end]
        stamps += [e.ts for e in tr.edges] + [c.reported_at for c in self.complaints.values()]
        stamps += [st.first_tainted_at for st in tr.accounts.values() if st.first_tainted_at]
        self.known_dates = {fmt_ist(s, with_time=False) for s in stamps if s is not None}
        self.narratives = [c.narrative_text for c in self.complaints.values()]
        self._facts: dict[str | None, dict[str, Any]] = {}

    def _load_txns(self) -> dict[str, Transaction]:
        edges = self.trace.edges
        if not edges:
            return {}
        start = min(ensure_utc(e.ts) for e in edges)
        end = max(ensure_utc(e.ts) for e in edges)
        ids = {a for e in edges for a in (e.from_account, e.to_account) if a}
        out: dict[str, Transaction] = {}
        for name in ("transactions_touching", "consortium_transactions"):
            try:
                for t in getattr(self.ledger, name)(ids, start, end):
                    out.setdefault(t.txn_id, t)
            except (LookupError, NotImplementedError, OSError, RuntimeError, ValueError) as exc:
                # payer remarks and ATM labels are optional decoration: draft without them, but say so
                note = f"transaction details unavailable ({name}: {type(exc).__name__}); remarks and labels omitted"
                log.warning("case %s: %s: %s", self.case_id, note, exc)
                self.warnings.append(note)
        return out

    # -- helpers
    def facts(self, account_id: str | None = None) -> dict[str, Any]:
        if account_id not in self._facts:
            self._facts[account_id] = build_factsheet(self.bundle, self.ledger, account_id)
        return self._facts[account_id]

    def role(self, acct: str) -> Role:
        return role_of(self.bundle, acct)

    def bank_name(self, bank_id: str | None) -> str:
        if not bank_id:
            return "unknown bank"
        bank = self.banks.get(bank_id)
        return bank.name if bank else f"bank {bank_id}"

    def is_innocent(self, acct: str) -> bool:
        """Innocent holder by classification or by plan decision (same test as the verifier)."""
        if self.role(acct) == Role.INNOCENT_HOLDER:
            return True
        d = self.decisions.get(acct)
        return d is not None and (
            d.role == Role.INNOCENT_HOLDER or d.disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION
        )

    def mask_label(self, acct: str) -> str:
        if self.is_innocent(acct):
            return INNOCENT_LABEL
        return MASK_LABEL.get(self.role(acct), "non-subject account")

    def reasons(self, acct: str) -> list[str]:
        cls = self.bundle.classifications.get(acct)
        return list(cls.reasons) if cls else []

    def cleaner(self, slots: TrackingSlots) -> TextCleaner:
        """Cleans free text for a template: amounts that match a slot become slot markers (see ``unmark``)."""
        return TextCleaner(slots, self.known_accounts, self.known_utrs, self.known_dates, markers=True)

    def account_pool(self, acct: str) -> dict[str, str]:
        return build_slots(self.bundle, self.ledger, acct)

    def is_escalation(self, dec: HoldDecision) -> bool:
        return ESCALATION_CODE in dec.reason_codes

    def escalation_basis(self, acct: str) -> str:
        parts = []
        linked = [self.complaints[n] for n in victims_of(self.trace, acct) if n in self.complaints]
        if any(c.fir_number for c in linked):
            parts.append("an FIR is registered for a linked complaint")
        sig = self.signals.get(acct)
        if sig and sig.ncrp_prior_reports:
            n = sig.ncrp_prior_reports
            parts.append(f"the account has {words(n)} earlier NCRP {plural(n, 'report')}")
        return " and ".join(parts) or "the hold plan raised the flag"

    def cash_outs_from(self, acct: str) -> list:
        return [
            e
            for e in self.trace.edges
            if e.from_account == acct and e.to_account is None and e.channel in CASH_OUT_CHANNELS
        ]

    def str_subjects(self) -> list[str]:
        out = []
        for d in self.ordered:
            st = self.trace.accounts.get(d.account_id)
            if st is None or not st.is_home_bank or st.layer < 1 or st.tainted_received_total <= 0:
                continue
            if d.disposition in NON_STR_DISPOSITIONS or self.role(d.account_id) not in STR_SUBJECT_ROLES:
                continue
            if self.is_innocent(d.account_id):
                continue
            out.append(d.account_id)
        return out

    def needs_str(self) -> bool:
        for acct in self.str_subjects():
            st = self.trace.accounts[acct]
            if self.role(acct) == Role.MULE and (st.tainted_now_total > 0 or self.cash_outs_from(acct)):
                return True
        return False

    # -- assembly
    def make(
        self,
        kind: DraftKind,
        acct: str | None,
        title_skeleton: str,
        template: str,
        slots: TrackingSlots,
        c: dict[str, Any],
        structured: dict[str, Any],
        outcome: prose_mod.ProseOutcome | None = None,
    ) -> Draft:
        """Render the skeleton, fill it with the slot values and record both (positional binding)."""
        body_skeleton = render_skeleton(template, slots, c, outcome.skeleton if outcome else "")
        title, body = fill(title_skeleton, slots), fill(body_skeleton, slots)
        citation_ids = checked_citations(f"{title}\n{body}")
        if outcome is not None:
            structured.update(outcome.structured())
        if self.warnings:
            structured["drafter_warnings"] = list(self.warnings)
        structured["skeleton"] = {"title": title_skeleton, "body": body_skeleton}
        return Draft(
            draft_id=draft_id(self.case_id, kind, acct),
            case_id=self.case_id,
            kind=kind,
            account_id=acct,
            title=title,
            body=body,
            slots=slots.used_slots(),
            structured=structured,
            citation_ids=citation_ids,
            llm_model=self.llm.name if outcome is not None else None,
            created_at=utcnow(),
        )

    def prose(self, task: prose_mod.ProseTask, account_id: str | None, slots: TrackingSlots):
        return prose_mod.write_prose(
            self.llm,
            task,
            self.facts(account_id),
            slots,
            self.narratives,
            block_untrusted_copy=self.block_untrusted_copy,
        )


# --------------------------------------------------------------------------- account drafts


def _mode(dec: HoldDecision) -> str:
    if dec.disposition == Disposition.HOLD_IN_POOL:
        return "pool"
    if dec.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY:
        return "intermediary"
    if dec.disposition == Disposition.FORWARD_HOLD or (dec.hold_amount <= 0 and dec.forward_hold_amount > 0):
        return "forward_only"
    return "hold"


def _intermediary_label(st: AccountState | None) -> str:
    """``pool`` / ``nodal`` / ``escrow`` from the account type; generic when only the role says intermediary."""
    if st is not None and st.account_type in INTERMEDIARY_ACCOUNT_TYPES:
        return st.account_type.value.lower()
    return "nodal / pool / escrow"


def _hold_instruction(cx: _Ctx, dec: HoldDecision) -> Draft:
    acct = dec.account_id
    slots = TrackingSlots(cx.account_pool(acct))
    clean = cx.cleaner(slots)
    escalate = cx.is_escalation(dec)
    mode = _mode(dec)
    c = {
        "case_id": cx.case_id,
        "home_bank_name": cx.home_bank_name,
        "has_holder_name": "holder_name" in slots,
        "mode": mode,
        "intermediary_label": _intermediary_label(cx.trace.accounts.get(acct)),
        "has_forward_hold": dec.forward_hold_amount > 0,
        "is_innocent": dec.disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION,
        "escalate": escalate,
        "escalation_basis": cx.escalation_basis(acct) if escalate else "",
        "requires_human": dec.requires_human,
        "reasons": [clean(r) for r in cx.reasons(acct)],
        "reason_codes": clean(", ".join(dec.reason_codes) or "none"),
    }
    structured = {
        "account_id": acct,
        "bank_id": dec.bank_id,
        "disposition": dec.disposition.value,
        "hold_amount": r2(dec.hold_amount),
        "forward_hold_amount": r2(dec.forward_hold_amount),
        "reason_codes": list(dec.reason_codes),
        "requires_human": dec.requires_human,
        "escalation_flag": escalate,
        "amount_specific": True,
        "suspend_digital_banking": False,
        "seize_account": False,
    }
    if mode == "intermediary":
        structured["intermediary_rule"] = "SOP-9-1-III"
    return cx.make(
        DraftKind.HOLD_INSTRUCTION,
        acct,
        "Hold instruction: ⟦account_id⟧",
        "hold_instruction.j2",
        slots,
        c,
        structured,
    )


def _customer_notice(cx: _Ctx, dec: HoldDecision) -> Draft:
    acct = dec.account_id
    slots = TrackingSlots(cx.account_pool(acct))
    mode = _mode(dec)
    c = {
        "home_bank_name": cx.home_bank_name,
        "has_holder_name": "holder_name" in slots,
        "mode": mode,
        "intermediary_label": _intermediary_label(cx.trace.accounts.get(acct)),
        "has_forward_hold": dec.forward_hold_amount > 0,
    }
    structured = {
        "account_id": acct,
        # nodal / pool / escrow accounts belong to a payment aggregator or other intermediary, not a customer
        "addressee": "INTERMEDIARY" if mode == "intermediary" else "ACCOUNT_HOLDER",
        "channels": ["e-mail", "letter"] if mode == "intermediary" else ["SMS", "e-mail", "letter"],
        "mentions_str": False,
        "tipping_off_basis": ["SOP-9-1-II", "FIU-CIRC-2025-06-02"],
    }
    return cx.make(
        DraftKind.CUSTOMER_NOTICE,
        acct,
        "Customer notice: ⟦account_id⟧",
        "customer_notice.j2",
        slots,
        c,
        structured,
    )


def _edd_memo(cx: _Ctx, dec: HoldDecision) -> Draft:
    acct = dec.account_id
    slots = TrackingSlots(cx.account_pool(acct))
    clean = cx.cleaner(slots)
    st = cx.trace.accounts.get(acct)
    cust = cx.customer_of.get(acct)
    sig = cx.signals.get(acct)
    remarks = []
    for e in cx.trace.edges:
        t = cx.txns.get(e.txn_id)
        if e.to_account == acct and e.tainted_total > 0 and t is not None and t.remark.strip():
            remarks.append(clean(t.remark[:80]))
    devices = {link.device_id for link in cx.ledger.device_links([acct])}
    shared = (
        any(link.account_id != acct for link in cx.ledger.device_links_for_devices(devices))
        if devices
        else False
    )
    outcome = cx.prose(prose_mod.EDD_TASK, acct, slots)
    c = {
        "case_id": cx.case_id,
        "has_holder_name": "holder_name" in slots,
        "kyc_tier": cust.kyc_tier if cust else "not on record",
        "city": clean(cust.city) if cust and cust.city else "not on record",
        "account_type": st.account_type.value if st else "not traced",
        "layer_word": ordinal(st.layer) if st else "unknown",
        "remarks": remarks,
        "reasons": [clean(r) for r in cx.reasons(acct)],
        "fri": sig.fri_risk.value if sig else "not available",
        "prior_reports": words(sig.ncrp_prior_reports, zero="none") if sig else "not available",
        "shared_device": shared,
    }
    structured = {
        "account_id": acct,
        "assessment": Role.INNOCENT_HOLDER.value,
        "recommendation": "HOLD_NO_ESCALATION_NO_STR",
        "grievance_advised": True,
        "timelines_days": {
            "grievance_filing": 7,
            "io_decision": 15,
            "dgo_escalation": 15,
            "hold_lapse": 90,
            "hold_lapse_warning": 75,
        },
        "mulehunter_score": sig.mulehunter_score if sig else None,
        "shared_device": shared,
    }
    return cx.make(
        DraftKind.EDD_MEMO, acct, "EDD memo: ⟦account_id⟧", "edd_memo.j2", slots, c, structured, outcome
    )


def _outbound_request(cx: _Ctx, dec: HoldDecision) -> Draft:
    acct = dec.account_id
    slots = TrackingSlots(cx.account_pool(acct))
    st = cx.trace.accounts.get(acct)
    bank_id = st.bank_id if st else dec.bank_id
    c = {
        "case_id": cx.case_id,
        "home_bank_name": cx.home_bank_name,
        "bank_name": cx.bank_name(bank_id),
    }
    structured = {
        "to_bank_id": bank_id,
        "account_id": acct,
        "request_amount": r2(dec.tainted_amount),
        "utrs": sorted(e.utr for e in cx.trace.edges if e.to_account == acct and e.tainted_total > 0),
        "per_victim": {k: r2(v) for k, v in sorted(dec.per_victim.items())},
        "channel": "CFCFRMS",
    }
    bank_part = "⟦bank_id⟧" if "bank_id" in slots else neutralize(str(bank_id))
    return cx.make(
        DraftKind.OUTBOUND_REQUEST,
        acct,
        f"CFCFRMS request to {bank_part}: ⟦account_id⟧",
        "outbound_request.j2",
        slots,
        c,
        structured,
    )


# --------------------------------------------------------------------------- case drafts


def _victim_rows(cx: _Ctx) -> list[dict[str, Any]]:
    return [
        {"ncrp": n, "name": c.victim_name, "has_recoverable": n in cx.plan.per_victim_recoverable}
        for n, c in cx.complaints.items()
    ]


def _cfcfrms_update(cx: _Ctx) -> Draft:
    slots = TrackingSlots(cx.case_pool)
    rows = []
    for d in cx.ordered:
        st = cx.trace.accounts.get(d.account_id)
        if d.disposition in HOLDING_DISPOSITIONS:
            amount_kind = "hold"
        elif d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST:
            amount_kind = "request"
        else:
            amount_kind = "none"
        flag = ""
        if cx.is_escalation(d):
            flag = "s.106 escalation flag: human decision required"
        elif d.requires_human:
            flag = "human review required"
        rows.append(
            {
                "acct": d.account_id,
                "has_state": st is not None,
                "bank_text": d.bank_id,
                "layer_word": ordinal(d.layer),
                "role": cx.role(d.account_id).value,
                "amount_kind": amount_kind,
                "has_fwd": d.forward_hold_amount > 0,
                "action": ACTION_TEXT.get(d.disposition, d.disposition.value),
                "flag": flag,
            }
        )
    outcome = cx.prose(prose_mod.CASE_SUMMARY_TASK, None, slots)
    c = {
        "case_id": cx.case_id,
        "home_bank_name": cx.home_bank_name,
        "home_bank_id": cx.home_bank_id,
        "rule_text": _rule_text(cx.plan.rule),
        "rows": rows,
        "victims": _victim_rows(cx),
        "has_outbound": any(d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST for d in cx.ordered),
        "escalations": [d.account_id for d in cx.ordered if cx.is_escalation(d)],
    }
    structured = {
        "case_id": cx.case_id,
        "rows": [
            {
                "account_id": d.account_id,
                "bank_id": d.bank_id,
                "layer": d.layer,
                "role": cx.role(d.account_id).value,
                "disposition": d.disposition.value,
                "hold_amount": r2(d.hold_amount),
                "forward_hold_amount": r2(d.forward_hold_amount),
                "tainted_amount": r2(d.tainted_amount),
                "requires_human": d.requires_human,
                "reason_codes": list(d.reason_codes),
            }
            for d in cx.ordered
        ],
        "totals": {
            "stolen": r2(cx.trace.totals.stolen),
            "hold": r2(cx.plan.total_hold),
            "forward_hold": r2(cx.plan.total_forward_hold),
            "outbound": r2(cx.trace.totals.outbound),
            "cashed_out": r2(cx.trace.totals.cashed_out),
            "recoverable": r2(cx.plan.total_recoverable),
            "baseline_freeze_amount": r2(cx.plan.baseline_freeze_amount),
        },
        "per_victim_recoverable": {k: r2(v) for k, v in sorted(cx.plan.per_victim_recoverable.items())},
    }
    return cx.make(
        DraftKind.CFCFRMS_UPDATE,
        None,
        f"CFCFRMS case update: {neutralize(cx.case_id)}",
        "cfcfrms_update.j2",
        slots,
        c,
        structured,
        outcome,
    )


def _party(cx: _Ctx, acct: str | None, subjects: set[str], fallback: str) -> tuple[str | None, str]:
    """(slot key, text) for a counterparty in the STR; non-subject home accounts and innocent holders at any bank
    are not identified."""
    if acct is None:
        return None, fallback
    if acct in subjects:
        return qualify(acct, "account_id"), ""
    if acct in cx.victim_ncrp:
        return f"victim_account:{cx.victim_ncrp[acct]}", ""
    st = cx.trace.accounts.get(acct)
    if st is not None and not st.is_home_bank and not cx.is_innocent(acct):
        return qualify(acct, "account_id"), ""
    return None, f"{cx.mask_label(acct)}, not identified"


def _identified_elsewhere(cx: _Ctx, acct: str) -> bool:
    """An other-bank account the STR may name (never an innocent holder)."""
    st = cx.trace.accounts.get(acct)
    return st is not None and not st.is_home_bank and not cx.is_innocent(acct)


def _str(cx: _Ctx) -> Draft:
    slots = TrackingSlots(cx.case_pool)
    clean = cx.cleaner(slots)
    tr, plan = cx.trace, cx.plan
    subjects = cx.str_subjects()
    subject_set = set(subjects)
    subject_keys = [qualify(a, k) for a in subjects for k in ("account_id", "disputed_amount", "hold_amount")]
    outcome = cx.prose(prose_mod.str_grounds_task(subject_keys), None, slots)

    people, sheet_people = [], []
    for acct in subjects:
        d = cx.decisions[acct]
        st = tr.accounts[acct]
        cust = cx.customer_of.get(acct)
        sig = cx.signals.get(acct)
        model = cx.accounts.get(acct)
        cls = cx.bundle.classifications.get(acct)
        reasons = [clean(r) for r in cx.reasons(acct)]
        sheet_reasons = [unmark(r, slots) for r in reasons]
        devices = sorted({lk.device_id for lk in cx.ledger.device_links([acct])})
        people.append(
            {
                "acct": acct,
                "has_name": qualify(acct, "holder_name") in slots,
                "role": cx.role(acct).value,
                "kyc_tier": cust.kyc_tier if cust else "not on record",
                "city": clean(cust.city) if cust and cust.city else "not on record",
                "fri": sig.fri_risk.value if sig else "not available",
                "prior_reports": words(sig.ncrp_prior_reports, zero="none") if sig else "not available",
                "reasons": reasons,
                "account_type": st.account_type.value,
                "layer_word": ordinal(st.layer),
                "has_fwd": d.forward_hold_amount > 0,
            }
        )
        sheet_people.append(
            {
                "account_id": acct,
                "customer_id": model.customer_id if model else None,
                "name": cust.name if cust else None,
                "mobile_masked": cust.mobile if cust else None,
                "pan_masked": cust.pan_masked if cust else None,
                "kyc_tier": cust.kyc_tier if cust else None,
                "dob": cust.dob.isoformat() if cust and cust.dob else None,
                "city": cust.city if cust else None,
                "role": cx.role(acct).value,
                "mule_score": cls.score if cls else None,
                "reasons": sheet_reasons,
                "fri_risk": sig.fri_risk.value if sig else None,
                "mulehunter_score": sig.mulehunter_score if sig else None,
                "ncrp_prior_reports": sig.ncrp_prior_reports if sig else None,
                "device_ids": devices,
            }
        )

    # transaction detail sheet: edges touching a subject
    txn_rows, sheet_txns = [], []
    for e in tr.edges:
        if e.from_account not in subject_set and e.to_account not in subject_set:
            continue
        t = cx.txns.get(e.txn_id)
        label = clean(t.counterparty_label) if t is not None and t.counterparty_label else ""
        exit_text = Cleaned(f"{e.channel.value} exit{f' ({label})' if label else ''}")  # label is cleaned
        from_key, from_text = _party(cx, e.from_account, subject_set, "cash deposit")
        to_key, to_text = _party(cx, e.to_account, subject_set, exit_text)
        txn_rows.append(
            {
                "txn": e.txn_id,
                "channel": e.channel.value,
                "from_key": from_key,
                "from_text": from_text,
                "to_key": to_key,
                "to_text": to_text,
            }
        )
        sheet_txns.append(
            {
                "txn_id": e.txn_id,
                "utr": e.utr,
                "ts": ensure_utc(e.ts).isoformat(),
                "channel": e.channel.value,
                "amount": r2(e.amount),
                "tainted_amount": r2(e.tainted_total),
                "tainted_by_complaint": {k: r2(v) for k, v in sorted(e.tainted.items())},
                "from_account": slots.peek(from_key) if from_key else None,
                "to_account": slots.peek(to_key) if to_key else None,
                "counterparty_note": unmark(from_text or to_text, slots) or None,
                "counterparty_label": unmark(label, slots) or None,
                "depth": e.depth,
            }
        )

    # fund flow by layer
    by_layer: dict[int, list[str]] = defaultdict(list)
    for acct, st in tr.accounts.items():
        if st.layer >= 1:
            by_layer[st.layer].append(acct)
    layers = []
    for layer in sorted(by_layer):
        items = []
        for acct in sorted(by_layer[layer]):
            st = tr.accounts[acct]
            d = cx.decisions.get(acct)
            if acct in subject_set:
                items.append({"kind": "subject", "acct": acct})
            elif _identified_elsewhere(cx, acct):
                items.append(
                    {
                        "kind": "other_bank",
                        "acct": acct,
                        "bank_name": cx.bank_name(st.bank_id),
                        "is_outbound": d is not None
                        and d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST,
                    }
                )
            else:
                note = ACTION_TEXT.get(d.disposition, "not reported") if d else "not reported"
                # amounts by trail edge, so no account-scoped slot (and no account number) enters the STR
                items.append(
                    {
                        "kind": "masked",
                        "label": cx.mask_label(acct),
                        "note": f"{note}; not a subject of this report",
                        "edges": [e.txn_id for e in inflow_edges(tr, acct)],
                    }
                )
        layers.append({"word": ordinal(layer), "items": items})

    # cash-out points (whole trail)
    cash_outs, sheet_cash = [], []
    for e in tr.edges:
        if e.to_account is not None or e.channel not in CASH_OUT_CHANNELS:
            continue
        t = cx.txns.get(e.txn_id)
        label = clean(t.counterparty_label) if t is not None and t.counterparty_label else ""
        from_key, from_text = _party(cx, e.from_account, subject_set, "an untraced account")
        kind = "cash withdrawal" if e.channel.value == "ATM" else "crypto transfer"
        cash_outs.append(
            {
                "txn": e.txn_id,
                "kind": kind,
                "where": label or e.channel.value,
                "from_key": from_key,
                "from_text": from_text,
            }
        )
        sheet_cash.append(
            {
                "txn_id": e.txn_id,
                "utr": e.utr,
                "channel": e.channel.value,
                "label": unmark(label, slots) or None,
                "from_account": slots.peek(from_key) if from_key else None,
                "amount": r2(e.amount),
                "tainted_amount": r2(e.tainted_total),
                "ts": ensure_utc(e.ts).isoformat(),
            }
        )

    beneficiaries = []
    for d in cx.ordered:
        if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST and _identified_elsewhere(cx, d.account_id):
            beneficiaries.append(
                {"acct": d.account_id, "bank_name": cx.bank_name(tr.accounts[d.account_id].bank_id)}
            )
    holding = [a for a in subjects if cx.decisions[a].hold_amount > 0]

    # network linkage
    subject_links = cx.ledger.device_links(subjects)
    device_ids = sorted({lk.device_id for lk in subject_links})
    members: dict[str, set[str]] = defaultdict(set)
    for lk in cx.ledger.device_links_for_devices(device_ids):
        members[lk.device_id].add(lk.account_id)
    devices, sheet_devices = [], []
    for dev in device_ids:
        accts = members.get(dev, set())
        if len(accts) < 2:
            continue
        shown = sorted(
            a
            for a in accts
            if a in subject_set or (_identified_elsewhere(cx, a) and qualify(a, "account_id") in slots)
        )
        masked = sorted(a for a in accts if a in tr.accounts and a not in shown)
        outside = sorted(a for a in accts if a not in tr.accounts)
        devices.append(
            {
                "members": [qualify(a, "account_id") for a in shown],
                "masked": words(len(masked), zero="") if masked else "",
                "outside": words(len(outside), zero="") if outside else "",
            }
        )
        sheet_devices.append(
            {
                "device_id": dev,
                "accounts_in_case": shown,
                "non_subject_accounts_in_case": len(masked),
                "accounts_outside_case": len(outside),
            }
        )
    multi = []
    for acct in sorted(a for a, st in tr.accounts.items() if st.layer >= 1):
        vs = victims_of(tr, acct)
        if len(vs) >= 2 and (acct in subject_set or _identified_elsewhere(cx, acct)):
            multi.append({"acct": acct, "count": words(len(vs)), "ids": ", ".join(vs)})

    ground = plan.str_ground
    c = {
        "case_id": cx.case_id,
        "home_bank_name": cx.home_bank_name,
        "home_bank_id": cx.home_bank_id,
        "subject_count": words(len(subjects), zero="none"),
        "has_ground": ground is not None,
        "ground_text": GROUND_TEXT.get(ground, "") if ground else "",
        "subjects": people,
        "txns": txn_rows,
        "victims": _victim_rows(cx),
        "layers": layers,
        "cash_outs": cash_outs,
        "holding_subjects": holding,
        "beneficiaries": beneficiaries,
        "devices": devices,
        "multi_victim": multi,
        "single_complaint": len(cx.complaints) == 1 and bool(subjects),
    }
    structured = {
        "report_type": "STR",
        "format": "FINnet 2.0 (modelled)",
        "case_id": cx.case_id,
        "ncrp_ids": sorted(cx.bundle.case.ncrp_ids),
        "reporting_entity": {
            "name": cx.home_bank_name,
            "bank_id": cx.home_bank_id,
            "category": "banking company (synthetic)",
        },
        "grounds": {
            "rule": "PML (Maintenance of Records) Rules, Rule 2(1)(g)",
            "ground": ground.value if ground else None,
            "ground_text": GROUND_TEXT.get(ground) if ground else None,
            "citation_ids": ["PMLR-2-1-G", "PMLR-8"],
            "narrative": outcome.text,
            "due": "within 7 working days of arriving at the suspicion (PML Rule 8)",
        },
        "individuals": sheet_people,
        "accounts": [
            {
                "account_id": a,
                "bank_id": tr.accounts[a].bank_id,
                "account_type": tr.accounts[a].account_type.value,
                "opened_at": ensure_utc(cx.accounts[a].opened_at).isoformat() if a in cx.accounts else None,
                "status": cx.accounts[a].status.value if a in cx.accounts else None,
                "layer": tr.accounts[a].layer,
                "balance_now": r2(cx.decisions[a].balance_now),
                "tainted_received": r2(tr.accounts[a].tainted_received_total),
                "tainted_now": r2(tr.accounts[a].tainted_now_total),
                "hold_amount": r2(cx.decisions[a].hold_amount),
                "forward_hold_amount": r2(cx.decisions[a].forward_hold_amount),
                "disposition": cx.decisions[a].disposition.value,
            }
            for a in subjects
        ],
        "transactions": sheet_txns,
        "cash_out_points": sheet_cash,
        "beneficiaries": [
            {
                "account_id": b["acct"],
                "bank_id": tr.accounts[b["acct"]].bank_id,
                "request_amount": r2(cx.decisions[b["acct"]].tainted_amount),
            }
            for b in beneficiaries
        ],
        "network": {
            "shared_devices": sheet_devices,
            "common_victims": {m["acct"]: victims_of(tr, m["acct"]) for m in multi},
        },
    }
    return cx.make(
        DraftKind.STR,
        None,
        f"Suspicious Transaction Report: {neutralize(cx.case_id)}",
        "str.j2",
        slots,
        c,
        structured,
        outcome,
    )


def _restoration_pack(cx: _Ctx) -> Draft:
    slots = TrackingSlots(cx.case_pool)
    tr, plan = cx.trace, cx.plan
    attribution = []
    for d in cx.ordered:
        if (
            d.disposition not in HOLDING_DISPOSITIONS
            and d.disposition != Disposition.OUTBOUND_CFCFRMS_REQUEST
        ):
            continue
        if d.account_id not in tr.accounts:
            continue
        outbound = d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST
        if outbound:
            status = f"hold requested from {cx.bank_name(tr.accounts[d.account_id].bank_id)}"
        elif d.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY:
            label = _intermediary_label(tr.accounts[d.account_id])
            status = f"held at this bank in the {label} account (disputed amount only)"
        else:
            status = "held at this bank"
        for n in sorted(d.per_victim):
            if d.per_victim[n] <= 0:
                continue
            comp = cx.complaints.get(n)
            attribution.append(
                {
                    "ncrp": n,
                    "acct": d.account_id,
                    "status": status,
                    "home": not outbound,
                    "victim": comp.victim_name if comp else "not on record",
                    "amount": r2(d.per_victim[n]),
                }
            )
    process = plan.restoration_process
    escalations = [
        {"acct": d.account_id, "basis": cx.escalation_basis(d.account_id)}
        for d in cx.ordered
        if cx.is_escalation(d)
    ]
    forward = [d.account_id for d in cx.ordered if d.forward_hold_amount > 0 and d.account_id in tr.accounts]
    c = {
        "case_id": cx.case_id,
        "home_bank_name": cx.home_bank_name,
        "home_bank_id": cx.home_bank_id,
        "rule_text": _rule_text(plan.rule),
        "process_text": PROCESS_TEXT.get(process, process.value),
        "process_cite": f"SOP-11-{process.value}",
        "attribution": attribution,
        "home_attribution": [r for r in attribution if r["home"] and r["ncrp"] in cx.complaints],
        "victims": _victim_rows(cx),
        "forward_holds": forward,
        "escalations": escalations,
        "has_intermediary_hold": any(
            d.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY and d.hold_amount > 0 for d in cx.ordered
        ),
    }
    structured = {
        "process": process.value,
        "apportionment_rule": plan.rule.value,
        "attribution": [
            {
                "ncrp_id": r["ncrp"],
                "account_id": r["acct"],
                "bank_id": tr.accounts[r["acct"]].bank_id,
                "amount": r["amount"],
                "status": "HELD" if r["home"] else "REQUESTED",
                "intermediary": cx.decisions[r["acct"]].disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY,
            }
            for r in attribution
        ],
        "per_victim_recoverable": {k: r2(v) for k, v in sorted(plan.per_victim_recoverable.items())},
        "total_recoverable": r2(plan.total_recoverable),
        "forward_holds": {a: r2(cx.decisions[a].forward_hold_amount) for a in forward},
        "timeline": [
            {"step": "notice", "days": 7, "calendar_days": True, "clock": "P1_NOTICE"},
            {"step": "reply", "days": 15, "clock": "P1_REPLY"},
            {"step": "remittance", "days": 15, "clock": "P1_REMIT"},
        ],
        "escalation_flags": [e["acct"] for e in escalations],
        "documents": [
            "attribution_sheet",
            "bank_noc",
            "remittance_letter",
            "indemnity_bond",
            "supurdignama",
            "timeline",
        ],
    }
    return cx.make(
        DraftKind.RESTORATION_PACK,
        None,
        f"Restoration pack (⟦restoration_process⟧): {neutralize(cx.case_id)}",
        "restoration_pack.j2",
        slots,
        c,
        structured,
    )


# --------------------------------------------------------------------------- public API


def _attach_quotes(drafts: list[Draft], search: DocSearch) -> None:
    cache: dict[str, dict[str, Any] | None] = {}
    for d in drafts:
        quotes: dict[str, Any] = {}
        for cid in d.citation_ids:
            if cid not in cache:
                try:
                    cit = search.citation(cid)
                except Exception:  # noqa: BLE001 - a broken index must not block drafting
                    cit = None
                cache[cid] = (
                    None
                    if cit is None
                    else {
                        "source_title": cit.source_title,
                        "section": cit.section,
                        "quote": cit.quote,
                        "url": cit.url,
                        "page": cit.page,
                    }
                )
            if cache[cid] is not None:
                quotes[cid] = cache[cid]
        d.structured["citations"] = quotes


def draft_case(
    bundle: CaseBundle,
    ledger: Ledger,
    llm: LLM,
    search: DocSearch | None = None,
    *,
    block_untrusted_copy: bool = True,
) -> list[Draft]:
    """Draft every document the plan calls for (docs/draft-contract.md, "Draft kinds per decision").

    ``block_untrusted_copy=False`` lets LLM prose that repeats complaint text through (for evaluating the
    verifier's injection check); by default such prose is rejected and replaced by the fallback paragraph.
    """
    cx = _Ctx(bundle, ledger, llm, block_untrusted_copy=block_untrusted_copy)
    drafts: list[Draft] = []
    for dec in cx.ordered:
        if dec.layer == 0:
            continue
        if dec.disposition in HOLD_DRAFT_DISPOSITIONS:
            drafts.append(_hold_instruction(cx, dec))
            drafts.append(_customer_notice(cx, dec))
            if dec.disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION:
                drafts.append(_edd_memo(cx, dec))
        elif dec.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST:
            drafts.append(_outbound_request(cx, dec))
    if cx.needs_str():
        drafts.append(_str(cx))
    drafts.append(_cfcfrms_update(cx))
    drafts.append(_restoration_pack(cx))
    if search is not None:
        _attach_quotes(drafts, search)
    return drafts
