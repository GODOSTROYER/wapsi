"""Rules-only completeness check of a police / LEA hold request (no LLM).

A hold request from a Law Enforcement Agency is checked against an eight-field checklist:

1. ``COMPLAINT_REF``      NCRP acknowledgement number or FIR reference (matches a complaint of the case, if known)
2. ``REQUESTING_OFFICER`` requesting officer's name and rank
3. ``JURISDICTION``       police station / district / state
4. ``ACCOUNT_NUMBER``     the account to act on (on the traced money trail at this bank, if a case is known)
5. ``SPECIFIC_AMOUNT``    a specific amount to hold
6. ``TRANSACTION_REFS``   UTR / transaction ids of the disputed credits (credited to that account on the trail)
7. ``LEGAL_PROVISION``    the provision relied on (a BNSS section: 94, 106, 107 or 168)
8. ``DATE_VALIDITY``      date of the request and its validity (not expired)

The result lists the missing or unclear fields and the *identifiable amount*: the disputed amount the bank can tie
to the request on the money trail, ``min(plan hold for the account, victim money carried by the cited credits,
amount requested)``. The hold is limited to that amount (SOP 9.1 / Annexure-I; SOP 5(ix)): a request for
whole-account action, or for more than can be identified, is answered with an amount-specific hold plus a
clarification letter. Account-level action is only ever a recorded, supervisor-approved exception
(``DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST``), never a reply to an incomplete request.

The letter is template-bound (``wapsi/draft/templates/lea_clarification.j2``): every amount, account number, UTR
and date in it comes from the request or the case data. The request's free text is untrusted: instruction-like
spans are quoted back in ``untrusted_spans`` and never acted on.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from wapsi import money
from wapsi.citations import KNOWN_IDS
from wapsi.models import HOLDING_DISPOSITIONS, CaseBundle
from wapsi.ports import Guard, Ledger
from wapsi.timeutil import IST, ensure_utc


class LeaField(StrEnum):
    COMPLAINT_REF = "COMPLAINT_REF"
    REQUESTING_OFFICER = "REQUESTING_OFFICER"
    JURISDICTION = "JURISDICTION"
    ACCOUNT_NUMBER = "ACCOUNT_NUMBER"
    SPECIFIC_AMOUNT = "SPECIFIC_AMOUNT"
    TRANSACTION_REFS = "TRANSACTION_REFS"
    LEGAL_PROVISION = "LEGAL_PROVISION"
    DATE_VALIDITY = "DATE_VALIDITY"


FIELD_LABELS: dict[LeaField, str] = {
    LeaField.COMPLAINT_REF: "NCRP acknowledgement number or FIR reference",
    LeaField.REQUESTING_OFFICER: "Name and rank of the requesting officer",
    LeaField.JURISDICTION: "Jurisdiction (police station, district and state)",
    LeaField.ACCOUNT_NUMBER: "Account number to act on",
    LeaField.SPECIFIC_AMOUNT: "Specific amount to be held",
    LeaField.TRANSACTION_REFS: "Transaction references (UTR / transaction IDs) of the disputed credits",
    LeaField.LEGAL_PROVISION: "Legal provision relied on (for example Section 168 read with Section 94 BNSS)",
    LeaField.DATE_VALIDITY: "Date of the request and its validity",
}
CHECKLIST: tuple[LeaField, ...] = tuple(LeaField)
LETTER_TEMPLATE = "lea_clarification.j2"
LETTER_CITATIONS = ("SOP-9-1", "SOP-ANNEX-I", "SOP-5-IX", "BNSS-94", "BNSS-168")

# a BNSS section the SOP's hold / seizure / attachment steps rest on
_PROVISION_RE = re.compile(
    r"\b(?:s(?:ec(?:tion)?)?s?\.?\s*|u/s\.?\s*)(94|106|107|168)\b[^.;\n]{0,40}?(?:BNSS|Bharatiya\s+Nagarik)"
    r"|(?:BNSS|Bharatiya\s+Nagarik\s+Suraksha\s+Sanhita)[^.;\n]{0,40}?\b(94|106|107|168)\b",
    re.IGNORECASE,
)


class LeaHoldRequest(BaseModel):
    """A police / LEA hold request as received (fields the officer typed in or a parser extracted)."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    ncrp_ack_no: str | None = None
    fir_ref: str | None = None
    officer_name: str | None = None
    officer_rank: str | None = None
    jurisdiction: str | None = None  # police station, district, state
    account_id: str | None = None
    amount: float | None = None
    utrs: list[str] = Field(default_factory=list)
    legal_provision: str | None = None
    request_date: date | None = None
    valid_until: date | None = None
    asks_account_level: bool = False  # asks to freeze / debit-block / stop the whole account
    text: str = ""  # UNTRUSTED free text of the request: quoted back, never followed


class LeaFieldCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: LeaField
    label: str
    ok: bool
    problem: str = ""


class LeaRequestCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    case_id: str | None = None
    complete: bool
    fields: list[LeaFieldCheck]
    missing: list[LeaField]
    account_id: str | None = None
    requested_amount: float | None = None
    identifiable_amount: float = 0.0
    hold_limited: bool = False  # the hold is below what was asked (or the request asked for account-level action)
    asks_account_level: bool = False
    matched_utrs: list[str] = Field(default_factory=list)
    unmatched_utrs: list[str] = Field(default_factory=list)
    basis: str = ""  # how the identifiable amount was worked out, in words
    untrusted_spans: list[str] = Field(default_factory=list)
    letter: str = ""
    citation_ids: list[str] = Field(default_factory=list)


def _s(x: str | None) -> str:
    return " ".join(str(x or "").split())


def _fmt_date(d: date | None) -> str:
    return d.strftime("%d-%m-%Y") if d is not None else ""


def _today(now: datetime | None) -> date | None:
    return ensure_utc(now).astimezone(IST).date() if now is not None else None


def _canon_account(acct: str) -> str:
    found = money.find_account_ids(acct)
    return found[0][1] if found else acct.strip().upper()


def _check_fields(
    req: LeaHoldRequest, bundle: CaseBundle | None, now: datetime | None
) -> tuple[list[LeaFieldCheck], list[str], list[str]]:
    trace = bundle.trace if bundle is not None else None
    acct = _canon_account(req.account_id) if _s(req.account_id) else ""
    utrs = [u for u in dict.fromkeys(_s(u).upper() for u in req.utrs) if u]
    matched: list[str] = []
    unmatched: list[str] = list(utrs)
    if trace is not None and acct:
        credited = {e.utr.upper() for e in trace.edges if e.to_account == acct and e.tainted_total > 0}
        credited |= {e.txn_id.upper() for e in trace.edges if e.to_account == acct and e.tainted_total > 0}
        matched = [u for u in utrs if u in credited]
        unmatched = [u for u in utrs if u not in credited]

    out: list[LeaFieldCheck] = []

    def add(f: LeaField, problem: str) -> None:
        out.append(LeaFieldCheck(field=f, label=FIELD_LABELS[f], ok=not problem, problem=problem))

    # 1 complaint reference
    ack, fir = _s(req.ncrp_ack_no), _s(req.fir_ref)
    problem = ""
    if not ack and not fir:
        problem = "neither an NCRP acknowledgement number nor an FIR reference is given"
    elif ack and bundle is not None and ack not in bundle.case.ncrp_ids and not fir:
        problem = "the NCRP acknowledgement number does not match a complaint of this case"
    add(LeaField.COMPLAINT_REF, problem)
    # 2 officer name and rank
    name, rank = _s(req.officer_name), _s(req.officer_rank)
    problem = ""
    if not name and not rank:
        problem = "the requesting officer's name and rank are not given"
    elif not name:
        problem = "the requesting officer's name is not given"
    elif not rank:
        problem = "the requesting officer's rank is not given"
    add(LeaField.REQUESTING_OFFICER, problem)
    # 3 jurisdiction
    add(LeaField.JURISDICTION, "" if _s(req.jurisdiction) else "the police station and district are not given")
    # 4 account number
    problem = ""
    if not acct:
        problem = "no account number is given"
    elif trace is not None:
        st = trace.accounts.get(acct)
        if st is None or st.layer == 0:
            problem = "the account is not on the traced money trail of this case"
        elif not st.is_home_bank:
            problem = "the account is not maintained with this bank"
    add(LeaField.ACCOUNT_NUMBER, problem)
    # 5 specific amount
    amount = req.amount
    if amount is None or not math.isfinite(amount) or amount <= 0:
        add(LeaField.SPECIFIC_AMOUNT, "no specific amount to be held is given")
    else:
        add(LeaField.SPECIFIC_AMOUNT, "")
    # 6 transaction references
    problem = ""
    if not utrs:
        problem = "no UTR / transaction ID of the disputed credit is given"
    elif trace is not None and acct and not matched:
        problem = "none of the UTRs given matches a disputed credit into the account on the trail"
    add(LeaField.TRANSACTION_REFS, problem)
    # 7 legal provision
    prov = _s(req.legal_provision)
    problem = ""
    if not prov:
        problem = "the legal provision relied on is not stated"
    elif not _PROVISION_RE.search(prov):
        problem = "the provision stated is not a recognised BNSS section for a hold (94, 106, 107 or 168)"
    add(LeaField.LEGAL_PROVISION, problem)
    # 8 date and validity
    problem = ""
    today = _today(now)
    if req.request_date is None and req.valid_until is None:
        problem = "the date of the request and its validity are not given"
    elif req.request_date is None:
        problem = "the date of the request is not given"
    elif req.valid_until is None:
        problem = "the validity of the request is not given"
    elif req.valid_until < req.request_date:
        problem = "the validity ends before the date of the request"
    elif today is not None and req.valid_until < today:
        problem = "the request's validity has expired"
    elif today is not None and req.request_date > today:
        problem = "the request is dated in the future"
    add(LeaField.DATE_VALIDITY, problem)
    return out, matched, unmatched


def _identifiable(
    req: LeaHoldRequest, bundle: CaseBundle | None, matched: list[str]
) -> tuple[float, str]:
    """(identifiable amount, basis in words)."""
    if bundle is None or bundle.trace is None or bundle.plan is None:
        return 0.0, "no traced case to identify the disputed amount against"
    acct = _canon_account(req.account_id) if _s(req.account_id) else ""
    st = bundle.trace.accounts.get(acct) if acct else None
    if st is None or st.layer == 0 or not st.is_home_bank:
        return 0.0, "the account is not on the traced money trail at this bank"
    dec = next((d for d in bundle.plan.decisions if d.account_id == acct), None)
    if dec is None or dec.disposition not in HOLDING_DISPOSITIONS or dec.hold_amount <= 0:
        return 0.0, "the hold plan finds no victim money that can be held in the account"
    amount = money.r2(dec.hold_amount)
    basis = ["the victim money still in the account, capped at its balance, as the hold plan computes it"]
    if matched:
        carried = money.r2(
            sum(
                e.tainted_total
                for e in bundle.trace.edges
                if e.to_account == acct and (e.utr.upper() in matched or e.txn_id.upper() in matched)
            )
        )
        if carried < amount - money.EPSILON:
            basis.append("the victim money carried by the credits cited")
        amount = min(amount, carried)
    req_amt = req.amount if req.amount is not None and math.isfinite(req.amount) and req.amount > 0 else None
    if req_amt is not None and money.r2(req_amt) < amount - money.EPSILON:
        basis.append("the amount requested")
        amount = money.r2(req_amt)
    return money.r2(max(amount, 0.0)), "; limited by ".join(basis)


def _screen(text: str, guard: Guard | None) -> list[str]:
    if not text.strip():
        return []
    if guard is None:
        from wapsi.verify.injection import RulesGuard

        guard = RulesGuard()
    result = guard.screen(text)
    return list(result.quoted_spans) if result.flagged else []


def check_lea_request(
    request: LeaHoldRequest,
    bundle: CaseBundle | None = None,
    ledger: Ledger | None = None,
    *,
    now: datetime | None = None,
    guard: Guard | None = None,
    bank_name: str | None = None,
) -> LeaRequestCheck:
    """Check ``request`` against the eight-field checklist and render the reply / clarification letter.

    ``bundle`` (traced and planned) ties the account, UTRs and amount to the money trail; without it nothing is
    identifiable and the letter asks for the missing fields only. ``now`` enables the validity check.
    """
    fields, matched, unmatched = _check_fields(request, bundle, now)
    missing = [f.field for f in fields if not f.ok]
    identifiable, basis = _identifiable(request, bundle, matched)
    req_amt = request.amount if request.amount is not None and math.isfinite(request.amount) else None
    requested = money.r2(req_amt) if req_amt is not None and req_amt > 0 else None
    hold_limited = request.asks_account_level or requested is None or identifiable < requested - money.EPSILON
    acct = _canon_account(request.account_id) if _s(request.account_id) else None
    if bank_name is None and ledger is not None:
        try:
            home = ledger.home_bank_id()
            bank = ledger.banks().get(home)
            bank_name = bank.name if bank else home
        except (LookupError, NotImplementedError, OSError, RuntimeError, ValueError):
            bank_name = None

    check = LeaRequestCheck(
        request_id=request.request_id,
        case_id=bundle.case.case_id if bundle is not None else None,
        complete=not missing,
        fields=fields,
        missing=missing,
        account_id=acct,
        requested_amount=requested,
        identifiable_amount=identifiable,
        hold_limited=hold_limited,
        asks_account_level=request.asks_account_level,
        matched_utrs=matched,
        unmatched_utrs=unmatched,
        basis=basis,
        untrusted_spans=_screen(request.text, guard),
    )
    letter = render_letter(check, request, bank_name=bank_name)
    cited = [m.group(1) for m in re.finditer(r"\[([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\]", letter)]
    unknown = sorted({c for c in cited if c not in KNOWN_IDS})
    if unknown:  # never invent a clause
        raise ValueError(f"clarification letter cites unknown ids: {unknown}")
    return check.model_copy(update={"letter": letter, "citation_ids": list(dict.fromkeys(cited))})


def render_letter(check: LeaRequestCheck, request: LeaHoldRequest, *, bank_name: str | None = None) -> str:
    """The reply to the requesting officer (template-bound; numbers from the request and the case data)."""
    from wapsi.draft.render import neutralize, render

    def words(x: str | None) -> str:  # typed-in text: collapsed and stripped of template / citation syntax
        return neutralize(_s(x)).replace("{", "(").replace("}", ")").replace("[", "(").replace("]", ")")

    refs = ", ".join(words(x) for x in (request.ncrp_ack_no, request.fir_ref) if _s(x)) or "not stated"
    if request.request_date is not None:
        refs += f"; request dated {_fmt_date(request.request_date)}"
    if request.valid_until is not None:
        refs += f", valid until {_fmt_date(request.valid_until)}"
    officer = ", ".join(x for x in (words(request.officer_name), words(request.officer_rank)) if x)
    to_line = ", ".join(x for x in (officer or "The Investigating Officer", words(request.jurisdiction)) if x)
    c = {
        "request_id": words(request.request_id),
        "title_suffix": "" if check.complete else ": clarification needed",
        "bank_name": words(bank_name) or "the bank",
        "to_line": to_line,
        "ref_line": refs,
        "account_id": check.account_id or "",
        "complete": check.complete,
        "missing": [{"label": f.label, "problem": f.problem} for f in check.fields if not f.ok],
        "identifiable": money.fmt_inr(check.identifiable_amount),
        "has_identifiable": check.identifiable_amount > 0,
        "requested": money.fmt_inr(check.requested_amount) if check.requested_amount is not None else "",
        "over_requested": check.requested_amount is not None
        and check.identifiable_amount < check.requested_amount - money.EPSILON,
        "asks_account_level": check.asks_account_level,
        "matched_utrs": ", ".join(check.matched_utrs),
        "unmatched_utrs": ", ".join(words(u) for u in check.unmatched_utrs),
        "basis": check.basis,
    }
    return render(LETTER_TEMPLATE, c=c)


def missing_labels(check: LeaRequestCheck) -> list[str]:
    return [FIELD_LABELS[f] for f in check.missing]


def summary(check: LeaRequestCheck) -> dict[str, object]:
    """Compact, JSON-ready summary for an audit event."""
    return {
        "complete": check.complete,
        "missing": [f.value for f in check.missing],
        "account_id": check.account_id,
        "requested_amount": check.requested_amount,
        "identifiable_amount": check.identifiable_amount,
        "hold_limited": check.hold_limited,
        "asks_account_level": check.asks_account_level,
        "untrusted_spans": list(check.untrusted_spans),
    }

