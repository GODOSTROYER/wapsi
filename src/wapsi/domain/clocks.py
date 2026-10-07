"""Statutory and SOP clocks for a case.

Rules (citation ids from ``wapsi.citations``):

| Clock | Start | Due | Citation |
|---|---|---|---|
| ``STR_DUE`` | approval time of the case's STR draft (APPROVED), else ``trace.computed_at`` (provisional) | 7 working days | PMLR-8 |
| ``HOLD_LAPSE`` | hold start (see below), per holding decision | 90 calendar days | SOP-10-1 |
| ``HOLD_LAPSE_WARNING`` | hold start | day 75 (LEA intimated 15 days before lapse) | SOP-10-1 |
| ``FORWARD_HOLD_REVIEW`` | hold start, per decision with ``forward_hold_amount > 0`` | 90 calendar days (warns day 75) | SOP-9-1-I |
| ``GRIEVANCE_FILING`` | hold start, ``HOLD_AMOUNT_NO_ESCALATION`` only (bank files after EDD) | 7 calendar days | SOP-10-1 |
| ``IO_DECISION`` | grievance filing due | 15 calendar days | SOP-10-1 |
| ``DGO_ESCALATION`` | IO decision due | 15 calendar days | SOP-10-1 |
| ``P1_NOTICE`` | complaint ``reported_at`` (P1/P2 cases, victims with recoverable money) | 7 calendar days | SOP-11-P1/P2 |
| ``P1_REPLY`` | notice due | 15 calendar days | SOP-11-P1/P2 |
| ``P1_REMIT`` | reply due | 15 calendar days | SOP-11-P1/P2 |
| ``BANK_DATA`` | ``trace.computed_at`` | 2 calendar days | SOP-10-1 |

Hold start = ``executed_at`` of a HOLD_INSTRUCTION action for the account, else the approval time of the
account's HOLD_INSTRUCTION draft, else ``trace.computed_at`` (provisional).

Hold-lapse start point (a documented choice). The SOP gives two starting points for the 90 calendar days.
Annexure-I (the s.168 / s.94 BNSS notice the bank acts on), item 3, lets the bank remove a contested hold "after
completion of 90 calendar days from the date of amount put on hold". SOP Para 10.1 counts the 90 calendar days
from "the grievance being submitted by the bank" and has the bank intimate the LEA 15 calendar days before they
expire. Wapsi counts from the hold date (Annexure-I). A grievance can only be submitted after the hold is placed,
so the hold-date lapse is never later than the Para 10.1 lapse: the clock (and the day-75 LEA intimation) shows
the earliest date the deadline can fall, and it needs no grievance date, which the case record does not hold.
Every ``HOLD_LAPSE`` / ``HOLD_LAPSE_WARNING`` clock says so in ``Clock.notes`` (``LAPSE_START_NOTE``).

Forward holds (SOP Para 9.1(i): a hold on future credits of a first-layer account whose balance is below the
disputed amount) have no lapse of their own in the SOP. Each one gets a ``FORWARD_HOLD_REVIEW`` clock on the same
90-calendar-day timeline, counted from the same hold start and warning at day 75 (the LEA intimation point of an
amount hold), so a forward hold is reviewed and never left in place indefinitely (``FORWARD_REVIEW_NOTE``). An
account with both an amount hold and a forward hold gets both sets of clocks.

``Clock.subject`` is only the identifier the clock applies to (account, draft, NCRP id or case id); how the clock
was counted (provisional start, lapse start point, forward-hold review) is in ``Clock.notes``.

Every due time is 23:59:59 IST at the end of the due day (UTC-aware). ``warn_at`` is explicit for
``HOLD_LAPSE_WARNING`` (7 days before its due day) and ``FORWARD_HOLD_REVIEW`` (day 75); every other clock warns
once 80% of its duration has elapsed. Clock ids are deterministic: ``<case_id>:<KIND>[:<subject key>]``. A clock
already marked DONE in ``bundle.clocks`` stays DONE; ``STR_DUE`` is also DONE once a STR_EXPORT action has
executed.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from wapsi.domain.calendar import add_working_days, end_of_day_ist
from wapsi.models import (
    HOLDING_DISPOSITIONS,
    ActionKind,
    ActionStatus,
    ApprovalDecision,
    CaseBundle,
    Clock,
    ClockKind,
    ClockStatus,
    Disposition,
    DraftKind,
    RestorationProcess,
)
from wapsi.timeutil import ensure_utc

STR_WORKING_DAYS = 7
HOLD_LAPSE_DAYS = 90
LEA_INTIMATION_DAY = 75  # LEA must be intimated 15 days before the 90-day lapse
LEA_INTIMATION_NOTICE_DAYS = 7  # warn this many days before the intimation deadline
GRIEVANCE_FILING_DAYS = 7
IO_DECISION_DAYS = 15
DGO_ESCALATION_DAYS = 15
P1_NOTICE_DAYS = 7
P1_REPLY_DAYS = 15
P1_REMIT_DAYS = 15
BANK_DATA_DAYS = 2
WARN_FRACTION = 0.8

PROVISIONAL_NOTE = "provisional: counted from trace computation; STR not yet approved"
HOLD_PROVISIONAL_NOTE = "provisional: hold not yet executed; counted from trace computation"
LAPSE_START_NOTE = (
    "90 calendar days counted from the hold date (SOP Annexure-I, item 3); SOP Para 10.1 counts them from the "
    "bank's grievance submission, which is never earlier, so this is the earliest the hold can lapse"
)
FORWARD_REVIEW_NOTE = (
    "forward hold on future credits (SOP Para 9.1(i)); the SOP sets no lapse for it, so it is reviewed on the "
    "90-calendar-day hold timeline, counted from the hold date, with a reminder at day 75"
)


def join_notes(*notes: str) -> str:
    """('a', '', 'b') -> 'a; b': the notes of one clock, in order."""
    return "; ".join(n for n in notes if n)


def clock_id(case_id: str, kind: ClockKind, key: str | None = None) -> str:
    return f"{case_id}:{kind}" + (f":{key}" if key else "")


def _calendar_due(start: datetime, days: int) -> datetime:
    return end_of_day_ist(ensure_utc(start) + timedelta(days=days))


def _warn_at(start: datetime, due: datetime) -> datetime:
    return start + (due - start) * WARN_FRACTION


def _clock(
    case_id: str,
    kind: ClockKind,
    key: str | None,
    subject: str | None,
    start: datetime,
    due: datetime,
    citation_id: str,
    *,
    business_days: bool = False,
    warn_at: datetime | None = None,
    notes: str = "",
) -> Clock:
    start = ensure_utc(start)
    due = ensure_utc(due)
    return Clock(
        clock_id=clock_id(case_id, kind, key),
        case_id=case_id,
        kind=kind,
        subject=subject,
        start=start,
        due=due,
        warn_at=ensure_utc(warn_at) if warn_at else _warn_at(start, due),
        business_days=business_days,
        citation_id=citation_id,
        notes=notes,
    )


def _earliest(times: Iterable[datetime | None]) -> datetime | None:
    vals = [ensure_utc(t) for t in times if t is not None]
    return min(vals) if vals else None


def _approval_time(bundle: CaseBundle, draft_ids: set[str]) -> datetime | None:
    return _earliest(
        a.at for a in bundle.approvals if a.draft_id in draft_ids and a.decision == ApprovalDecision.APPROVED
    )


def _fallback_start(bundle: CaseBundle) -> datetime:
    return ensure_utc(bundle.trace.computed_at if bundle.trace else bundle.case.opened_at)


def hold_start(bundle: CaseBundle, account_id: str) -> tuple[datetime, str]:
    """(start, basis) where basis is ``executed`` | ``approved`` | ``computed``."""
    executed = _earliest(
        a.executed_at
        for a in bundle.actions
        if a.kind == ActionKind.HOLD_INSTRUCTION
        and a.status != ActionStatus.FAILED
        and a.payload.get("account_id") == account_id
    )
    if executed:
        return executed, "executed"
    drafts = {
        d.draft_id
        for d in bundle.drafts
        if d.kind == DraftKind.HOLD_INSTRUCTION and d.account_id == account_id
    }
    approved = _approval_time(bundle, drafts)
    if approved:
        return approved, "approved"
    return _fallback_start(bundle), "computed"


def _needs_str(bundle: CaseBundle) -> bool:
    if any(d.kind == DraftKind.STR for d in bundle.drafts):
        return True
    return bundle.plan is not None and bundle.plan.str_ground is not None


def _str_clocks(bundle: CaseBundle) -> list[Clock]:
    if not _needs_str(bundle):
        return []
    case_id = bundle.case.case_id
    str_drafts = sorted(d.draft_id for d in bundle.drafts if d.kind == DraftKind.STR)
    approved = _approval_time(bundle, set(str_drafts))
    label = str_drafts[0] if str_drafts else "STR"
    if approved:
        start, notes = approved, ""
    else:
        if bundle.trace is None:
            return []
        start, notes = ensure_utc(bundle.trace.computed_at), PROVISIONAL_NOTE
    due = add_working_days(start, STR_WORKING_DAYS)
    return [
        _clock(case_id, ClockKind.STR_DUE, None, label, start, due, "PMLR-8", business_days=True, notes=notes)
    ]


def _hold_clocks(bundle: CaseBundle) -> list[Clock]:
    if bundle.plan is None:
        return []
    case_id = bundle.case.case_id
    out: list[Clock] = []
    for dec in sorted(bundle.plan.decisions, key=lambda d: d.account_id):
        holding = dec.disposition in HOLDING_DISPOSITIONS
        forward = dec.forward_hold_amount > 0
        if not holding and not forward:
            continue
        acct = dec.account_id
        start, basis = hold_start(bundle, acct)
        provisional = HOLD_PROVISIONAL_NOTE if basis == "computed" else ""
        lapse = _calendar_due(start, HOLD_LAPSE_DAYS)
        intimate = _calendar_due(start, LEA_INTIMATION_DAY)
        if forward:
            out.append(
                _clock(
                    case_id,
                    ClockKind.FORWARD_HOLD_REVIEW,
                    acct,
                    acct,
                    start,
                    lapse,
                    "SOP-9-1-I",
                    warn_at=intimate,
                    notes=join_notes(provisional, FORWARD_REVIEW_NOTE),
                )
            )
        if not holding:
            continue
        lapse_notes = join_notes(provisional, LAPSE_START_NOTE)
        out.append(_clock(case_id, ClockKind.HOLD_LAPSE, acct, acct, start, lapse, "SOP-10-1", notes=lapse_notes))
        out.append(
            _clock(
                case_id,
                ClockKind.HOLD_LAPSE_WARNING,
                acct,
                acct,
                start,
                intimate,
                "SOP-10-1",
                warn_at=intimate - timedelta(days=LEA_INTIMATION_NOTICE_DAYS),
                notes=lapse_notes,
            )
        )
        if dec.disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION:
            filing = _calendar_due(start, GRIEVANCE_FILING_DAYS)
            io = _calendar_due(filing, IO_DECISION_DAYS)
            dgo = _calendar_due(io, DGO_ESCALATION_DAYS)
            out += [
                _clock(case_id, ClockKind.GRIEVANCE_FILING, acct, acct, start, filing, "SOP-10-1", notes=provisional),
                _clock(case_id, ClockKind.IO_DECISION, acct, acct, filing, io, "SOP-10-1", notes=provisional),
                _clock(case_id, ClockKind.DGO_ESCALATION, acct, acct, io, dgo, "SOP-10-1", notes=provisional),
            ]
    return out


def _restoration_clocks(bundle: CaseBundle) -> list[Clock]:
    process = bundle.plan.restoration_process if bundle.plan else RestorationProcess.P1
    if process not in (RestorationProcess.P1, RestorationProcess.P2):
        return []  # P3-P5 run on court timelines
    cite = "SOP-11-P1" if process == RestorationProcess.P1 else "SOP-11-P2"
    recoverable = bundle.plan.per_victim_recoverable if bundle.plan else None
    case_id = bundle.case.case_id
    out: list[Clock] = []
    for c in sorted(bundle.complaints, key=lambda c: c.ncrp_id):
        if recoverable is not None and recoverable.get(c.ncrp_id, 0.0) <= 0:
            continue
        notice = _calendar_due(c.reported_at, P1_NOTICE_DAYS)
        reply = _calendar_due(notice, P1_REPLY_DAYS)
        remit = _calendar_due(reply, P1_REMIT_DAYS)
        n = c.ncrp_id
        out.append(_clock(case_id, ClockKind.P1_NOTICE, n, n, c.reported_at, notice, cite))
        out.append(_clock(case_id, ClockKind.P1_REPLY, n, n, notice, reply, cite))
        out.append(_clock(case_id, ClockKind.P1_REMIT, n, n, reply, remit, cite))
    return out


def _bank_data_clocks(bundle: CaseBundle) -> list[Clock]:
    if bundle.trace is None:
        return []
    start = ensure_utc(bundle.trace.computed_at)
    return [
        _clock(
            bundle.case.case_id,
            ClockKind.BANK_DATA,
            None,
            bundle.case.case_id,
            start,
            _calendar_due(start, BANK_DATA_DAYS),
            "SOP-10-1",
        )
    ]


def clocks_for_bundle(bundle: CaseBundle, now: datetime) -> list[Clock]:
    """All clocks the case currently implies, with statuses evaluated at ``now``."""
    clocks = (
        _str_clocks(bundle) + _hold_clocks(bundle) + _restoration_clocks(bundle) + _bank_data_clocks(bundle)
    )
    done = {c.clock_id for c in bundle.clocks if c.status == ClockStatus.DONE}
    if any(a.kind == ActionKind.STR_EXPORT and a.status == ActionStatus.DONE for a in bundle.actions):
        done.add(clock_id(bundle.case.case_id, ClockKind.STR_DUE))
    clocks = [c.model_copy(update={"status": ClockStatus.DONE}) if c.clock_id in done else c for c in clocks]
    return evaluate_clocks(clocks, now)


def clock_status(clock: Clock, now: datetime) -> ClockStatus:
    if clock.status == ClockStatus.DONE:
        return ClockStatus.DONE
    now = ensure_utc(now)
    if now > ensure_utc(clock.due):
        return ClockStatus.OVERDUE
    if clock.warn_at is not None and now >= ensure_utc(clock.warn_at):
        return ClockStatus.WARNING
    return ClockStatus.OPEN


def evaluate_clocks(clocks: Iterable[Clock], now: datetime) -> list[Clock]:
    """OPEN / WARNING / OVERDUE at ``now``; DONE is preserved. Sorted by (due, clock_id)."""
    out = [c.model_copy(update={"status": clock_status(c, now)}) for c in clocks]
    return sorted(out, key=lambda c: (ensure_utc(c.due), c.clock_id))
