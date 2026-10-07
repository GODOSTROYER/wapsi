"""Shared data contracts for every Wapsi module and edition.

Conventions (see AGENTS.md):
- Money is float rupees; round with ``wapsi.money.r2`` before storing or comparing.
- Timestamps are timezone-aware. Store UTC; display IST (``wapsi.timeutil``).
- Taint vectors are keyed by ``ncrp_id`` (one key per victim complaint).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- enums


class AccountType(StrEnum):
    INDIVIDUAL = "INDIVIDUAL"
    BUSINESS = "BUSINESS"
    MERCHANT = "MERCHANT"
    NODAL = "NODAL"
    POOL = "POOL"
    ESCROW = "ESCROW"


INTERMEDIARY_ACCOUNT_TYPES = frozenset({AccountType.NODAL, AccountType.POOL, AccountType.ESCROW})


class AccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    CLOSED = "CLOSED"


class Channel(StrEnum):
    UPI = "UPI"
    IMPS = "IMPS"
    NEFT = "NEFT"
    RTGS = "RTGS"
    ATM = "ATM"  # cash withdrawal; to_account is None
    POS = "POS"
    CRYPTO = "CRYPTO"  # transfer to a VASP / exchange; to_account is None
    CASH_DEPOSIT = "CASH_DEPOSIT"  # from_account is None
    INTERNAL = "INTERNAL"


class Role(StrEnum):
    VICTIM = "VICTIM"
    MULE = "MULE"
    # SOP §5(xii) innocent holders, split in two (AGENTS.md, hard rules):
    # - BONA_FIDE_RECIPIENT: received victim money and kept it (rent, salary, payment for goods). Hold the amount,
    #   no escalation, no STR by default, EDD memo. It keeps the legacy wire value "INNOCENT_HOLDER", so golden sets,
    #   SQL views and model labels written before the split still load; ``Role.INNOCENT_HOLDER`` is an alias.
    # - UNWITTING_CONDUIT: the account was used as a pass-through by someone else. Hold the amount, no escalation,
    #   STR recommended for the officer's decision (never automatic), EDD memo.
    BONA_FIDE_RECIPIENT = "INNOCENT_HOLDER"
    INNOCENT_HOLDER = "INNOCENT_HOLDER"  # legacy alias of BONA_FIDE_RECIPIENT
    UNWITTING_CONDUIT = "UNWITTING_CONDUIT"
    MERCHANT = "MERCHANT"
    INTERMEDIARY = "INTERMEDIARY"  # nodal / pool / escrow
    LEGIT = "LEGIT"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> Role | None:
        # the new name is accepted as input too ("BONA_FIDE_RECIPIENT" -> the legacy wire value)
        if isinstance(value, str) and value.strip().upper() == "BONA_FIDE_RECIPIENT":
            return cls.BONA_FIDE_RECIPIENT
        return None


# both innocent-holder roles (SOP §5(xii)): never an s.106 escalation flag, never named in an automatic STR
INNOCENT_ROLES = frozenset({Role.BONA_FIDE_RECIPIENT, Role.UNWITTING_CONDUIT})


def legacy_role(role: Role) -> Role:
    """Map a role onto the vocabulary of golden sets written before the innocent-holder split (both innocent
    roles -> ``INNOCENT_HOLDER``)."""
    return Role.INNOCENT_HOLDER if role in INNOCENT_ROLES else role


class TerminalKind(StrEnum):
    ATM_CASH_OUT = "ATM_CASH_OUT"
    CRYPTO_EXIT = "CRYPTO_EXIT"
    OTHER_BANK = "OTHER_BANK"  # hop leaves the home bank and is not followed
    NODAL_POOL_ESCROW = "NODAL_POOL_ESCROW"
    MERCHANT = "MERCHANT"


class Disposition(StrEnum):
    HOLD_AMOUNT = "HOLD_AMOUNT"  # SOP §9.1 / Annexure-I: hold the disputed amount only
    # §5(xii) innocent holder: hold, EDD memo, no escalation; no automatic STR (the officer decides; recommended
    # for an unwitting conduit)
    HOLD_AMOUNT_NO_ESCALATION = "HOLD_AMOUNT_NO_ESCALATION"
    FORWARD_HOLD = "FORWARD_HOLD"  # §9.1(i): insufficient balance, first-layer accounts only
    NO_HOLD_UPDATE_ONLY = "NO_HOLD_UPDATE_ONLY"  # §9.2: merchant, goods delivered
    HOLD_IN_POOL = "HOLD_IN_POOL"  # §9.2: merchant, goods undelivered -> cancel, hold in pool/nodal
    # §9.1(iii): nodal / pool / escrow account: hold the disputed amount only; no forward hold, no suspension or
    # seizure of the account (LEAs "should normally refrain"), never an s.106 escalation flag
    HOLD_AMOUNT_INTERMEDIARY = "HOLD_AMOUNT_INTERMEDIARY"
    OUTBOUND_CFCFRMS_REQUEST = "OUTBOUND_CFCFRMS_REQUEST"  # money left for another bank
    CASHED_OUT = "CASHED_OUT"  # ATM / crypto exit; nothing to hold
    ESCALATE_S106_CANDIDATE = "ESCALATE_S106_CANDIDATE"  # needs FIR + multiple reports; human only
    NO_ACTION = "NO_ACTION"
    # SOP §5(ix), §9.1: amount-specific holds are the default. Account-level action is only a recorded exception:
    # never emitted by the hold plan; created by an officer request carrying >= 1 ``AccountLevelReason`` and applied
    # only after a supervisor approves that request (maker-checker, fingerprint-bound). ``hold_amount`` stays the
    # amount-specific hold (the recoverable money); the account-level restriction is on top of it.
    EXCEPTIONAL_ACCOUNT_LEVEL_HOLD = "EXCEPTIONAL_ACCOUNT_LEVEL_HOLD"


HOLDING_DISPOSITIONS = frozenset(
    {
        Disposition.HOLD_AMOUNT,
        Disposition.HOLD_AMOUNT_NO_ESCALATION,
        Disposition.HOLD_IN_POOL,
        Disposition.HOLD_AMOUNT_INTERMEDIARY,
        Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD,
    }
)


class AccountLevelReason(StrEnum):
    """Closed list of reasons an officer may record for an exceptional account-level hold (SOP §5(ix): unwarranted
    freezing is discouraged, so every account-level action carries at least one of these)."""

    FRAUDSTER_CONTROLLED_ACCOUNT = "FRAUDSTER_CONTROLLED_ACCOUNT"  # account wholly controlled by the fraudster
    CONTINUING_FRAUD_EVIDENCE = "CONTINUING_FRAUD_EVIDENCE"  # fresh victim credits / fraud still running
    LEA_ORDER_ACCOUNT_LEVEL = "LEA_ORDER_ACCOUNT_LEVEL"  # an LEA order that specifies account-level action
    IDENTITY_THEFT_ACCOUNT = "IDENTITY_THEFT_ACCOUNT"  # account opened with a stolen / fake identity


class ApportionmentRule(StrEnum):
    PRO_RATA = "PRO_RATA"  # default; SOP Para 5(xiii) / Annexure-V
    FIFO = "FIFO"
    LIFO = "LIFO"


class RestorationProcess(StrEnum):
    P1 = "P1"  # s.106 BNSS, single victim
    P2 = "P2"  # multiple victims / competing claims, pro-rata
    P3 = "P3"  # competent court, s.497/498/503 BNSS
    P4 = "P4"  # s.107 BNSS attachment / forfeiture
    P5 = "P5"  # process directed by the jurisdictional court


class StrGround(StrEnum):
    """PML (Maintenance of Records) Rules 2005, Rule 2(1)(g)."""

    PROCEEDS_OF_CRIME = "PROCEEDS_OF_CRIME"
    UNUSUAL_COMPLEXITY = "UNUSUAL_COMPLEXITY"
    NO_ECONOMIC_RATIONALE = "NO_ECONOMIC_RATIONALE"
    TERROR_FINANCING = "TERROR_FINANCING"


class RiskLevel(StrEnum):
    NONE = "NONE"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    VERY_HIGH = "VERY_HIGH"


class OrderStatus(StrEnum):
    DELIVERED = "DELIVERED"
    UNDELIVERED = "UNDELIVERED"
    CANCELLED = "CANCELLED"


class DraftKind(StrEnum):
    HOLD_INSTRUCTION = "HOLD_INSTRUCTION"
    CFCFRMS_UPDATE = "CFCFRMS_UPDATE"
    STR = "STR"
    RESTORATION_PACK = "RESTORATION_PACK"
    EDD_MEMO = "EDD_MEMO"
    OUTBOUND_REQUEST = "OUTBOUND_REQUEST"
    CUSTOMER_NOTICE = "CUSTOMER_NOTICE"
    # officer's request for an exceptional account-level hold; a supervisor approves it (maker-checker)
    ACCOUNT_LEVEL_HOLD_REQUEST = "ACCOUNT_LEVEL_HOLD_REQUEST"


class ClockKind(StrEnum):
    STR_DUE = "STR_DUE"  # 7 working days, PML Rule 8
    GRIEVANCE_FILING = "GRIEVANCE_FILING"  # 7 days, SOP §10.1
    IO_DECISION = "IO_DECISION"  # 15 days
    DGO_ESCALATION = "DGO_ESCALATION"  # 15 days
    BANK_DATA = "BANK_DATA"  # 2 days
    HOLD_LAPSE = "HOLD_LAPSE"  # 90 calendar days
    HOLD_LAPSE_WARNING = "HOLD_LAPSE_WARNING"  # day 75: intimate LEA
    # SOP 9.1(i) sets no lapse of its own for a forward hold on a first-layer account: it is reviewed on the same
    # 90-calendar-day timeline as an amount hold (SOP 10.1), so it is never left in place indefinitely
    FORWARD_HOLD_REVIEW = "FORWARD_HOLD_REVIEW"
    P1_NOTICE = "P1_NOTICE"  # IO notice to holder within 7 calendar days
    P1_REPLY = "P1_REPLY"  # holder reply within 15 days
    P1_REMIT = "P1_REMIT"  # bank remits within 15 days


class ClockStatus(StrEnum):
    OPEN = "OPEN"
    WARNING = "WARNING"
    OVERDUE = "OVERDUE"
    DONE = "DONE"


class CaseStatus(StrEnum):
    NEW = "NEW"
    TRACED = "TRACED"
    PLANNED = "PLANNED"
    DRAFTED = "DRAFTED"
    VERIFIED = "VERIFIED"
    APPROVED = "APPROVED"
    CLOSED = "CLOSED"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ActionKind(StrEnum):
    HOLD_INSTRUCTION = "HOLD_INSTRUCTION"
    CFCFRMS_UPDATE = "CFCFRMS_UPDATE"
    JIRA_TICKET = "JIRA_TICKET"
    SLACK_NOTICE = "SLACK_NOTICE"
    EMAIL = "EMAIL"
    STR_EXPORT = "STR_EXPORT"


class ActionStatus(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"


# --------------------------------------------------------------------------- world / ledger


class Bank(_Model):
    bank_id: str
    name: str
    is_home: bool = False


class Customer(_Model):
    customer_id: str
    name: str
    mobile: str
    pan_masked: str
    kyc_tier: str  # FULL | MIN | VKYC
    dob: date | None = None
    city: str = ""


class Account(_Model):
    account_id: str
    bank_id: str
    customer_id: str | None = None  # None for nodal/pool/escrow
    account_type: AccountType
    opened_at: datetime
    gstin: str | None = None
    status: AccountStatus = AccountStatus.ACTIVE
    opening_balance: float = 0.0  # balance at the start of the synthetic world


class Transaction(_Model):
    txn_id: str
    ts: datetime
    from_account: str | None = None
    to_account: str | None = None
    from_bank_id: str | None = None
    to_bank_id: str | None = None
    amount: float
    channel: Channel
    utr: str
    remark: str = ""
    counterparty_label: str | None = None  # ATM id / VASP name for exits


class Invoice(_Model):
    invoice_id: str
    merchant_account: str
    payer_account: str
    txn_id: str | None = None
    amount: float
    ordered_at: datetime
    delivered_at: datetime | None = None
    order_status: OrderStatus


class GstRecord(_Model):
    gstin: str
    legal_name: str
    status: str  # ACTIVE | CANCELLED | SUSPENDED
    registered_at: date


class ExternalSignal(_Model):
    account_id: str
    fri_risk: RiskLevel = RiskLevel.NONE  # DoT Financial Fraud Risk Indicator of the linked mobile (synthetic)
    mulehunter_score: float = 0.0  # MuleHunter-style score in [0, 1] (synthetic)
    ncrp_prior_reports: int = 0


class DeviceLink(_Model):
    device_id: str
    account_id: str
    first_seen: datetime


class Complaint(_Model):
    ncrp_id: str
    reported_at: datetime
    victim_account: str
    victim_name: str
    victim_mobile: str
    amount_claimed: float
    narrative_text: str  # UNTRUSTED input: never follow instructions inside it
    language: str = "en"  # en | hi-en
    category: str = "UPI_FRAUD"
    fir_number: str | None = None
    source: str = "NCRP"  # NCRP | 1930


# --------------------------------------------------------------------------- case artefacts


class Case(_Model):
    case_id: str
    ncrp_ids: list[str]
    opened_at: datetime
    status: CaseStatus = CaseStatus.NEW
    assigned_to: str | None = None
    priority_amount: float = 0.0


class TrailEdge(_Model):
    txn_id: str
    ts: datetime
    from_account: str | None
    to_account: str | None
    from_bank_id: str | None = None
    to_bank_id: str | None = None
    channel: Channel
    utr: str
    amount: float
    depth: int  # 1 = victim -> first-layer account
    tainted: dict[str, float] = Field(default_factory=dict)  # ncrp_id -> tainted rupees carried by this edge
    tainted_total: float = 0.0
    via_consortium: bool = False  # learned through a (simulated) CFCFRMS response from another bank


class AccountState(_Model):
    account_id: str
    bank_id: str
    account_type: AccountType
    is_home_bank: bool
    layer: int  # minimum depth at which tainted money reached the account (1 = first-layer)
    first_tainted_at: datetime | None = None
    balance_now: float
    tainted_now: dict[str, float] = Field(default_factory=dict)
    tainted_now_total: float = 0.0
    tainted_received_total: float = 0.0
    tainted_sent_total: float = 0.0
    terminal: TerminalKind | None = None
    parents: list[str] = Field(default_factory=list)


class TraceTotals(_Model):
    stolen: float = 0.0  # sum of reported victim transfers
    holdable: float = 0.0  # tainted money still sitting in holdable accounts
    cashed_out: float = 0.0  # ATM + crypto exits
    outbound: float = 0.0  # left for banks we cannot follow
    intermediary: float = 0.0  # sitting in nodal/pool/escrow accounts
    merchant_settled: float = 0.0  # paid to merchants for delivered goods
    unlocated: float = 0.0  # stolen - everything above (should be ~0)


class TraceResult(_Model):
    case_id: str
    ncrp_ids: list[str]
    rule: ApportionmentRule
    window_start: datetime
    window_end: datetime
    victim_seed: dict[str, float]  # ncrp_id -> reported amount traced
    edges: list[TrailEdge]
    accounts: dict[str, AccountState]
    totals: TraceTotals
    warnings: list[str] = Field(default_factory=list)
    computed_at: datetime


class ClassificationResult(_Model):
    account_id: str
    role: Role
    score: float  # probability-like mule score in [0, 1]
    reasons: list[str] = Field(default_factory=list)  # plain-English, SOP vocabulary
    features: dict[str, float] = Field(default_factory=dict)
    model: str = "rules"


class HoldDecision(_Model):
    account_id: str
    bank_id: str
    disposition: Disposition
    hold_amount: float = 0.0
    forward_hold_amount: float = 0.0
    tainted_amount: float = 0.0
    balance_now: float = 0.0
    layer: int = 0
    role: Role = Role.UNKNOWN
    per_victim: dict[str, float] = Field(default_factory=dict)  # ncrp_id -> amount held for that victim
    citation_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    requires_human: bool = False
    notes: str = ""


class HoldPlan(_Model):
    case_id: str
    rule: ApportionmentRule
    decisions: list[HoldDecision]
    total_hold: float = 0.0
    total_forward_hold: float = 0.0
    total_recoverable: float = 0.0
    per_victim_recoverable: dict[str, float] = Field(default_factory=dict)
    over_hold_amount: float = 0.0  # held beyond tainted money; must be 0
    # whole-account freeze baseline (comparison only): what freezing every touched home account would immobilise
    baseline_freeze_amount: float = 0.0
    baseline_freeze_accounts: int = 0
    restoration_process: RestorationProcess = RestorationProcess.P1
    str_ground: StrGround | None = None


class Citation(_Model):
    citation_id: str
    source_title: str
    section: str
    quote: str = ""
    url: str = ""
    page: int | None = None


class SearchHit(_Model):
    citation_id: str | None = None
    chunk_id: str
    doc_id: str
    section: str
    text: str
    score: float


class Draft(_Model):
    draft_id: str
    case_id: str
    kind: DraftKind
    account_id: str | None = None
    title: str
    body: str
    slots: dict[str, str] = Field(default_factory=dict)  # values injected from tables, formatted as rendered
    structured: dict[str, Any] = Field(default_factory=dict)  # e.g. FINnet-style STR JSON
    citation_ids: list[str] = Field(default_factory=list)
    llm_model: str | None = None
    created_at: datetime
    version: int = 1


class CheckResult(_Model):
    name: str  # numeric | citations | policy | injection | format
    passed: bool
    severity: str = "error"  # error | warning
    message: str = ""  # human-readable text (no machine code prefix; see ``code``)
    spans: list[str] = Field(default_factory=list)
    # stable machine code (``slot_mismatch``, ``over_hold``, ``ok``, ...). Empty on records written before the field
    # existed, whose message still starts with "<code>: " (``wapsi.verify.report.code_of`` reads both)
    code: str = ""


class VerificationReport(_Model):
    draft_id: str
    case_id: str
    passed: bool
    checks: list[CheckResult]
    verified_at: datetime


class GuardResult(_Model):
    flagged: bool
    categories: list[str] = Field(default_factory=list)
    quoted_spans: list[str] = Field(default_factory=list)
    sanitized_text: str = ""


class Approval(_Model):
    approval_id: str
    case_id: str
    draft_id: str | None = None
    decision: ApprovalDecision
    approver: str
    role: str
    reason_code: str = ""
    comment: str = ""
    at: datetime
    draft_sha256: str | None = None  # fingerprint of the exact draft content the decision applies to


class Action(_Model):
    action_id: str
    case_id: str
    kind: ActionKind
    payload: dict[str, Any] = Field(default_factory=dict)
    status: ActionStatus = ActionStatus.PENDING
    created_at: datetime
    executed_at: datetime | None = None
    result: str = ""


class Clock(_Model):
    clock_id: str
    case_id: str
    kind: ClockKind
    subject: str | None = None  # account_id / draft_id / ncrp_id / case_id the clock applies to (identifier only)
    start: datetime
    due: datetime
    warn_at: datetime | None = None
    business_days: bool = False
    status: ClockStatus = ClockStatus.OPEN
    citation_id: str | None = None
    # how the clock was counted, for the officer ("provisional: hold not yet executed", the lapse start point, ...).
    # Records written before the field existed carry the note in ``subject`` as "<id> (<note>)".
    notes: str = ""


class AuditEvent(_Model):
    at: datetime
    actor: str
    role: str
    action: str
    object_type: str
    object_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    # hash-chain link (``wapsi.audit``), stamped when the event is appended to a case's log; None = not sealed.
    # hash = SHA-256 over canonical JSON of (seq, ts, actor, role, event_type, case_id, payload_digest, prev_hash)
    seq: int | None = None
    case_id: str | None = None
    prev_hash: str | None = None
    hash: str | None = None


class GoldenCase(_Model):
    """One line of data/golden/<world>/cases.jsonl. Expected values assume ApportionmentRule.PRO_RATA and the
    default TraceConfig. Amounts in rupees."""

    case_id: str
    world: str
    ncrp_ids: list[str]
    typology: str  # T1_FANOUT_CASHOUT ... T10_NODAL_POOL, see wapsi.synth
    ring_id: str
    is_adversarial: bool = False
    expected_totals: TraceTotals
    expected_roles: dict[str, Role]  # account_id -> role (accounts that received tainted money)
    expected_dispositions: dict[str, Disposition]
    expected_hold: dict[str, float]  # account_id -> hold_amount
    expected_outbound_request: dict[str, float] = Field(default_factory=dict)
    expected_per_victim_recoverable: dict[str, float] = Field(default_factory=dict)
    expected_process: RestorationProcess
    expected_str_ground: StrGround | None = None
    expected_escalation_candidates: list[str] = Field(default_factory=list)
    notes: str = ""


class CaseBundle(_Model):
    """Everything known about one case; what the UI and every edition read/write."""

    case: Case
    complaints: list[Complaint]
    trace: TraceResult | None = None
    classifications: dict[str, ClassificationResult] = Field(default_factory=dict)
    plan: HoldPlan | None = None
    drafts: list[Draft] = Field(default_factory=list)
    verifications: dict[str, VerificationReport] = Field(default_factory=dict)  # draft_id -> report
    approvals: list[Approval] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    clocks: list[Clock] = Field(default_factory=list)
    audit: list[AuditEvent] = Field(default_factory=list)
