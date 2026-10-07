"""Display helpers: short Indian currency, IST times, plain-English labels and PII masking.

Everything here is pure (no Streamlit) so it can be unit-tested and reused by every edition.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from wapsi.models import (
    AccountLevelReason,
    AccountType,
    ApportionmentRule,
    ApprovalDecision,
    CaseStatus,
    Channel,
    ClockKind,
    ClockStatus,
    Disposition,
    DraftKind,
    RestorationProcess,
    RiskLevel,
    Role,
    StrGround,
    TerminalKind,
)
from wapsi.money import fmt_inr
from wapsi.timeutil import ensure_utc, fmt_ist, to_ist

DASH = "—"
_LAKH = Decimal(100_000)
_CRORE = Decimal(10_000_000)
_CENT = Decimal("0.01")

# --------------------------------------------------------------------------- money


def inr_short(x: float | None) -> str:
    """Short Indian format: ₹43,200 · ₹2.88 L · ₹5.04 Cr (half-up rounding, like a banker reads it)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return DASH
    d = Decimal(str(x)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    d = abs(d)
    if d >= _LAKH:
        lakhs = (d / _LAKH).quantize(_CENT, rounding=ROUND_HALF_UP)
        if lakhs < 100:
            return f"{sign}₹{lakhs} L"
        crores = (d / _CRORE).quantize(_CENT, rounding=ROUND_HALF_UP)
        return f"{sign}₹{crores} Cr"
    return f"{sign}₹{fmt_inr(float(d), symbol=False, paise=False)}"


def inr(x: float | None) -> str:
    """Full rupees with paise, exactly as drafts render them: ₹4,80,000.00."""
    return DASH if x is None else fmt_inr(x)


def inr_table(x: float | None) -> str:
    """Full Indian grouping; paise only when there are any (₹43,200 · ₹1,152.50)."""
    if x is None:
        return DASH
    whole = abs(x - round(x)) < 0.005
    return fmt_inr(x, paise=not whole)


def pct(x: float | None, digits: int = 0) -> str:
    if x is None:
        return DASH
    return f"{x * 100:.{digits}f}%"


def ratio_text(big: float, small: float) -> str:
    """'407×' — how many times larger ``big`` is than ``small``."""
    if small <= 0:
        return DASH
    r = big / small
    return f"{r:,.0f}×" if r >= 10 else f"{r:.1f}×"


# --------------------------------------------------------------------------- time


def ist(ts: datetime | None) -> str:
    return DASH if ts is None else fmt_ist(ts)


def ist_date(ts: datetime | None) -> str:
    return DASH if ts is None else fmt_ist(ts, with_time=False)


def ist_clock(ts: datetime | None) -> str:
    return DASH if ts is None else to_ist(ts).strftime("%H:%M")


def days_between(later: datetime, earlier: datetime) -> float:
    return (ensure_utc(later) - ensure_utc(earlier)).total_seconds() / 86400.0


def relative_due(due: datetime, now: datetime) -> str:
    """'due today' · 'due in 3 days' · 'overdue by 2 days'."""
    days = days_between(due, now)
    if days < 0:
        n = int(-days)
        return "overdue by less than a day" if n == 0 else f"overdue by {n} day{'s' if n != 1 else ''}"
    whole = int(days)
    if whole == 0:
        return "due today"
    return f"due in {whole} day{'s' if whole != 1 else ''}"


def relative_past(ts: datetime, now: datetime) -> str:
    days = days_between(now, ts)
    if days < 0:
        return "just now"
    if days < 1 / 24:
        return f"{max(1, int(days * 1440))} min ago"
    if days < 1:
        return f"{int(days * 24)} h ago"
    n = int(days)
    return f"{n} day{'s' if n != 1 else ''} ago"


# --------------------------------------------------------------------------- labels (no raw enums in the UI)

DISPOSITION_LABELS: dict[Disposition, str] = {
    Disposition.HOLD_AMOUNT: "Hold victim amount",
    Disposition.HOLD_AMOUNT_NO_ESCALATION: "Hold, no escalation",
    Disposition.FORWARD_HOLD: "Forward hold",
    Disposition.NO_HOLD_UPDATE_ONLY: "No hold, update only",
    Disposition.HOLD_IN_POOL: "Hold in merchant pool",
    # SOP Para 9.1(iii): hold the disputed amount in the nodal / pool / escrow account; no suspension or seizure
    Disposition.HOLD_AMOUNT_INTERMEDIARY: "Hold amount · pool/nodal account — no suspension",
    Disposition.OUTBOUND_CFCFRMS_REQUEST: "Request other bank",
    Disposition.CASHED_OUT: "Cashed out",
    Disposition.ESCALATE_S106_CANDIDATE: "s.106 escalation candidate",
    Disposition.NO_ACTION: "No action",
    # never automatic: an officer's recorded reasons plus a supervisor's approval (SOP Para 5(ix), 9.1)
    Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD: "Account-level hold · approved exception",
}

ROLE_LABELS: dict[Role, str] = {
    Role.VICTIM: "Victim",
    Role.MULE: "Suspected mule",
    # SOP Para 5(xii), split in two (Role.INNOCENT_HOLDER is the legacy alias of BONA_FIDE_RECIPIENT)
    Role.BONA_FIDE_RECIPIENT: "Possible innocent holder · bona fide recipient",
    Role.UNWITTING_CONDUIT: "Possible innocent holder · unwitting conduit",
    Role.MERCHANT: "Merchant",
    Role.INTERMEDIARY: "Pool / nodal account",
    Role.LEGIT: "Legitimate",
    Role.UNKNOWN: "Unclassified",
}

DRAFT_KIND_LABELS: dict[DraftKind, str] = {
    DraftKind.HOLD_INSTRUCTION: "Hold instruction",
    DraftKind.STR: "STR",
    DraftKind.RESTORATION_PACK: "Restoration pack",
    DraftKind.EDD_MEMO: "EDD memo",
    DraftKind.OUTBOUND_REQUEST: "CFCFRMS requests",
    DraftKind.CFCFRMS_UPDATE: "CFCFRMS update",
    DraftKind.CUSTOMER_NOTICE: "Customer notice",
    DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST: "Account-level hold request",
}
DRAFT_KIND_ORDER: list[DraftKind] = list(DRAFT_KIND_LABELS)

CLOCK_KIND_LABELS: dict[ClockKind, str] = {
    ClockKind.STR_DUE: "STR due",
    ClockKind.GRIEVANCE_FILING: "Grievance filing window",
    ClockKind.IO_DECISION: "IO decision",
    ClockKind.DGO_ESCALATION: "DGO escalation",
    ClockKind.BANK_DATA: "Bank data to LEA",
    ClockKind.HOLD_LAPSE: "Hold lapses",
    ClockKind.HOLD_LAPSE_WARNING: "Tell LEA before hold lapses",
    ClockKind.FORWARD_HOLD_REVIEW: "Review forward hold",
    ClockKind.P1_NOTICE: "IO notice to holder",
    ClockKind.P1_REPLY: "Holder reply",
    ClockKind.P1_REMIT: "Bank remits to victim",
}

CLOCK_STATUS_LABELS: dict[ClockStatus, str] = {
    ClockStatus.OPEN: "On track",
    ClockStatus.WARNING: "Due soon",
    ClockStatus.OVERDUE: "Overdue",
    ClockStatus.DONE: "Done",
}

CASE_STATUS_LABELS: dict[CaseStatus, str] = {
    CaseStatus.NEW: "New notice",
    CaseStatus.TRACED: "Trail traced",
    CaseStatus.PLANNED: "Recommended — pending approval",
    CaseStatus.DRAFTED: "Drafts ready for checks",
    CaseStatus.VERIFIED: "Verified — pending approval",
    CaseStatus.APPROVED: "Approved",
    CaseStatus.CLOSED: "Closed",
}

APPROVAL_LABELS: dict[ApprovalDecision, str] = {
    ApprovalDecision.APPROVED: "Approved",
    ApprovalDecision.REJECTED: "Rejected",
    ApprovalDecision.CHANGES_REQUESTED: "Changes requested",
}

PROCESS_LABELS: dict[RestorationProcess, str] = {
    RestorationProcess.P1: "Process 1 · single victim",
    RestorationProcess.P2: "Process 2 · multiple victims, pro-rata",
    RestorationProcess.P3: "Process 3 · competent court",
    RestorationProcess.P4: "Process 4 · attachment / forfeiture",
    RestorationProcess.P5: "Process 5 · as directed by court",
}

STR_GROUND_LABELS: dict[StrGround, str] = {
    StrGround.PROCEEDS_OF_CRIME: "Proceeds of crime",
    StrGround.UNUSUAL_COMPLEXITY: "Unusual or unjustified complexity",
    StrGround.NO_ECONOMIC_RATIONALE: "No economic rationale",
    StrGround.TERROR_FINANCING: "Terror financing",
}

RULE_LABELS: dict[ApportionmentRule, str] = {
    ApportionmentRule.PRO_RATA: "Pro-rata · SOP Para 5(xiii)",
    ApportionmentRule.FIFO: "First in, first out",
    ApportionmentRule.LIFO: "Last in, first out",
}

RISK_LABELS: dict[RiskLevel, str] = {
    RiskLevel.NONE: "None",
    RiskLevel.MEDIUM: "Medium",
    RiskLevel.HIGH: "High",
    RiskLevel.VERY_HIGH: "Very high",
}

ACCOUNT_TYPE_LABELS: dict[AccountType, str] = {
    AccountType.INDIVIDUAL: "Individual",
    AccountType.BUSINESS: "Business",
    AccountType.MERCHANT: "Merchant",
    AccountType.NODAL: "Nodal",
    AccountType.POOL: "Pool",
    AccountType.ESCROW: "Escrow",
}

TERMINAL_LABELS: dict[TerminalKind, str] = {
    TerminalKind.ATM_CASH_OUT: "ATM cash-out",
    TerminalKind.CRYPTO_EXIT: "Crypto exit",
    TerminalKind.OTHER_BANK: "Other bank",
    TerminalKind.NODAL_POOL_ESCROW: "Pool / nodal account",
    TerminalKind.MERCHANT: "Merchant",
}

CHANNEL_LABELS: dict[Channel, str] = {
    Channel.CASH_DEPOSIT: "Cash deposit",
    Channel.INTERNAL: "Internal",
    Channel.CRYPTO: "Crypto",
}

CHECK_LABELS: dict[str, str] = {
    "numeric": "Numbers",
    "citations": "Citations",
    "policy": "Policy",
    "injection": "Untrusted text",
    "format": "Format",
}
CHECK_ORDER = ["numeric", "citations", "policy", "injection"]

# closed list of reasons an officer may record for an exceptional account-level hold
ACCOUNT_LEVEL_REASON_LABELS: dict[AccountLevelReason, str] = {
    AccountLevelReason.FRAUDSTER_CONTROLLED_ACCOUNT: "Account wholly controlled by the fraudster",
    AccountLevelReason.CONTINUING_FRAUD_EVIDENCE: "Evidence that the fraud is continuing through the account",
    AccountLevelReason.LEA_ORDER_ACCOUNT_LEVEL: "LEA order that specifies account-level action",
    AccountLevelReason.IDENTITY_THEFT_ACCOUNT: "Account opened or operated with a stolen identity",
}

REASON_CODE_LABELS: dict[str, str] = {
    "FORWARD_HOLD_FIRST_LAYER": "Forward hold on future credits (first-layer account)",
    "ESCALATE_S106_CANDIDATE": "s.106 escalation candidate: FIR and 2+ earlier reports — human decision",
    **{d.value: label for d, label in DISPOSITION_LABELS.items()},
    "UNWITTING_CONDUIT": "Unwitting conduit: the account was used as a pass-through by someone else",
    "STR_RECOMMENDED_OFFICER_DECISION": "STR recommended — no automatic STR; the officer decides",
    "OFFICER_REQUESTED_EXCEPTION": "Account-level hold requested by an officer",
    "SUPERVISOR_APPROVED_EXCEPTION": "Account-level hold approved by a supervisor",
    **{r.value: label for r, label in ACCOUNT_LEVEL_REASON_LABELS.items()},
}

# Wapsi is not connected to the CFCFRMS portal: every CFCFRMS action it shows is prepared, not sent
SIMULATED_CFCFRMS_NOTE = ("Simulated CFCFRMS interface: Wapsi prepares CFCFRMS updates and requests and records "
                          "them here; nothing is sent to the portal until the nodal officer submits it there.")

APPROVAL_REASON_LABELS: dict[ApprovalDecision, dict[str, str]] = {
    ApprovalDecision.APPROVED: {
        "CHECKS_PASSED": "Checks passed and matches the hold plan",
        "VERIFIED_ON_LEDGER": "Re-checked against the ledger",
        "APPROVED_WITH_NOTE": "Approved with a note",
    },
    ApprovalDecision.CHANGES_REQUESTED: {
        "AMOUNT_QUERY": "An amount needs a second look",
        "WORDING": "Wording needs changes",
        "MISSING_EVIDENCE": "Evidence is missing",
        "CITATION_QUERY": "A citation needs checking",
    },
    ApprovalDecision.REJECTED: {
        "NOT_VICTIM_MONEY": "Account doesn't hold victim money",
        "WRONG_ACCOUNT": "Wrong account",
        "DUPLICATE_NOTICE": "Duplicate notice",
        "LEA_WITHDRAWN": "LEA withdrew the request",
    },
}
ALL_APPROVAL_REASONS: dict[str, str] = {k: v for m in APPROVAL_REASON_LABELS.values() for k, v in m.items()}
REASON_CODE_LABELS.update(ALL_APPROVAL_REASONS)

AUDIT_ACTION_LABELS: dict[str, str] = {
    "case_opened": "Case opened",
    "case_processed": "Case processed",
    "untrusted_instruction_detected": "Instruction-like text found in complaint",
    "viewed": "Viewed",
    "role_switched": "Switched role",
    "draft_approved": "Draft approved",
    "draft_rejected": "Draft rejected",
    "draft_changes_requested": "Changes requested on draft",
    "plan_approved": "Hold plan approved",
    "plan_rejected": "Hold plan rejected",
    "plan_changes_requested": "Changes requested on hold plan",
    "pipeline_run": "Drafts generated",
    "pipeline_unavailable": "Draft generation unavailable",
    "pipeline_failed": "Draft generation failed",
    "action_done": "Action recorded",
    "action_pending": "Action queued",
    "action_failed": "Action failed",
    "account_level_hold_requested": "Account-level hold requested",
    "account_level_hold_applied": "Account-level hold applied (approved exception)",
    "draft_withdrawn": "Draft withdrawn",
    "draft_verified": "Draft checked",
    "lea_request_checked": "Police / LEA hold request checked",
    "trace_built": "Money trail traced",
    "hold_plan_built": "Hold plan built",
    "draft_created": "Draft created",
    "audit_chain_verified": "Audit chain verified",
}

_ALL_MAPS: tuple[dict, ...] = (
    DISPOSITION_LABELS, ROLE_LABELS, DRAFT_KIND_LABELS, CLOCK_KIND_LABELS, CLOCK_STATUS_LABELS,
    CASE_STATUS_LABELS, APPROVAL_LABELS, PROCESS_LABELS, STR_GROUND_LABELS, RULE_LABELS, RISK_LABELS,
    ACCOUNT_TYPE_LABELS, TERMINAL_LABELS, CHANNEL_LABELS,
)


def humanize(code: str) -> str:
    """'ACTIVE_PENDING' -> 'Active pending' (fallback for codes without a curated label)."""
    words = re.split(r"[_\-.\s]+", str(code).strip())
    text = " ".join(w for w in words if w).lower()
    return text[:1].upper() + text[1:] if text else DASH


def label(value: object) -> str:
    """Plain-English label for any Wapsi enum value (falls back to a humanised code)."""
    if value is None:
        return DASH
    if isinstance(value, Channel):
        return channel_label(value)
    for mapping in _ALL_MAPS:
        if value in mapping and (not isinstance(value, Enum) or type(value) is type(next(iter(mapping)))):
            return mapping[value]
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str) and value in REASON_CODE_LABELS:
        return REASON_CODE_LABELS[value]
    if isinstance(value, str) and value in AUDIT_ACTION_LABELS:
        return AUDIT_ACTION_LABELS[value]
    if isinstance(value, str) and value.upper() == value and any(c.isalpha() for c in value):
        return humanize(value)
    return str(value)


def reason_label(code: str) -> str:
    return REASON_CODE_LABELS.get(code, humanize(code))


def audit_action_label(action: str) -> str:
    return AUDIT_ACTION_LABELS.get(action, humanize(action))


def check_label(name: str) -> str:
    return CHECK_LABELS.get(name, humanize(name))


def channel_label(ch: Channel | str) -> str:
    try:
        return CHANNEL_LABELS.get(Channel(ch), Channel(ch).value)
    except ValueError:
        return str(ch)


# --------------------------------------------------------------------------- masking

MASK = "•"


def mask_name(name: str | None) -> str:
    """'Meera Joshi' -> 'M•••• J••••' (keeps initials and word lengths)."""
    if not name:
        return DASH if name is None else ""
    return " ".join(w[0] + MASK * (len(w) - 1) if len(w) > 1 else w for w in name.split())


def mask_mobile(mobile: str | None) -> str:
    """'9876543210' / '98XXXXXX21' -> '••••••••10' (last two characters only)."""
    if not mobile:
        return DASH if mobile is None else ""
    s = mobile.strip()
    keep = 2 if len(s) > 2 else 0
    return MASK * (len(s) - keep) + s[len(s) - keep:]


_MOBILE_RE = re.compile(r"(?<![\w-])(?:\+91[\s-]?)?[6-9][0-9X]{9}(?![\w-])")


def mask_text(text: str, names: Iterable[str] = (), mobiles: Iterable[str] = ()) -> str:
    """Mask known names and mobile numbers (plus anything mobile-shaped) inside free text."""
    if not text:
        return text
    out = text
    for n in sorted({n for n in names if n and len(n) > 1}, key=len, reverse=True):
        out = re.sub(re.escape(n), mask_name(n), out, flags=re.IGNORECASE)
    for m in sorted({m for m in mobiles if m}, key=len, reverse=True):
        out = out.replace(m, mask_mobile(m))
    return _MOBILE_RE.sub(lambda mo: mask_mobile(mo.group(0)), out)


# --------------------------------------------------------------------------- viewer / roles

ROLES: tuple[str, ...] = ("analyst", "supervisor", "auditor")
ROLE_TITLES: dict[str, str] = {"analyst": "Analyst", "supervisor": "Supervisor", "auditor": "Auditor"}
ROLE_ACTORS: dict[str, str] = {
    "analyst": "nodal.officer@hb01",
    "supervisor": "supervisor@hb01",
    "auditor": "auditor@hb01",
}
ROLE_BLURBS: dict[str, str] = {
    "analyst": "Prepares hold plans and drafts",
    "supervisor": "Approves or returns drafts",
    "auditor": "Read-only; personal details masked",
}


@dataclass(frozen=True)
class Viewer:
    """Who is looking at the screen; decides masking and which controls are live."""

    role: str = "analyst"

    @property
    def actor(self) -> str:
        return ROLE_ACTORS.get(self.role, self.role)

    @property
    def title(self) -> str:
        return ROLE_TITLES.get(self.role, humanize(self.role))

    @property
    def masked(self) -> bool:
        return self.role == "auditor"

    @property
    def can_approve(self) -> bool:
        return self.role == "supervisor"

    @property
    def can_run(self) -> bool:
        return self.role != "auditor"

    def name(self, name: str | None) -> str:
        return mask_name(name) if self.masked else (name or DASH)

    def mobile(self, mobile: str | None) -> str:
        return mask_mobile(mobile) if self.masked else (mobile or DASH)

    def text(self, text: str, names: Iterable[str] = (), mobiles: Iterable[str] = ()) -> str:
        return mask_text(text, names, mobiles) if self.masked else text


def short_account(account_id: str | None) -> str:
    """'HB01-1000000001' -> 'HB01 ···0001' for tight spaces (full id stays in tooltips)."""
    if not account_id:
        return DASH
    if "-" in account_id and len(account_id) > 9:
        bank, num = account_id.split("-", 1)
        return f"{bank} ···{num[-4:]}"
    return account_id
