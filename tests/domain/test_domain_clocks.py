from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from wapsi.domain.clocks import clock_id, clocks_for_bundle, evaluate_clocks
from wapsi.fixtures import showcase as sc
from wapsi.fixtures.bundles import showcase_bundle
from wapsi.models import (
    Action,
    ActionKind,
    ActionStatus,
    Approval,
    ApprovalDecision,
    ClockKind,
    ClockStatus,
    Draft,
    DraftKind,
    RestorationProcess,
)
from wapsi.timeutil import IST

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=IST)


def eod(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 23, 59, 59, tzinfo=IST)


def by_id(clocks):
    return {c.clock_id: c for c in clocks}


def one(clocks, kind: ClockKind, key: str | None = None):
    matches = [c for c in clocks if c.kind == kind and (key is None or c.clock_id.endswith(f":{key}"))]
    assert len(matches) == 1, matches
    return matches[0]


@pytest.fixture
def b1():
    return showcase_bundle(1)


@pytest.fixture
def b2():
    return showcase_bundle(2)


def test_str_due_provisional(b1, b2):
    c = one(clocks_for_bundle(b1, NOW), ClockKind.STR_DUE)
    assert c.clock_id == "CASE-31009260000101:STR_DUE"
    assert c.start == datetime(2026, 9, 10, 13, 45, tzinfo=IST)  # trace.computed_at
    assert c.due == eod(2026, 9, 19)  # 7 working days; Sat 12 (2nd) and Sun 13 skipped, Sat 19 (3rd) counted
    assert c.business_days and c.citation_id == "PMLR-8"
    assert "provisional" in c.subject
    assert c.status == ClockStatus.OPEN
    assert one(clocks_for_bundle(b2, NOW), ClockKind.STR_DUE).due == eod(2026, 9, 21)


def test_str_due_from_approval(b1):
    approved_at = datetime(2026, 9, 14, 10, 0, tzinfo=IST)
    b1.drafts.append(
        Draft(
            draft_id="D-STR",
            case_id=b1.case.case_id,
            kind=DraftKind.STR,
            title="STR",
            body="",
            created_at=datetime(2026, 9, 14, 9, 0, tzinfo=IST),
        )
    )
    b1.approvals += [
        Approval(
            approval_id="AP-0",
            case_id=b1.case.case_id,
            draft_id="D-STR",
            decision=ApprovalDecision.REJECTED,
            approver="po",
            role="principal_officer",
            at=datetime(2026, 9, 12, 10, 0, tzinfo=IST),
        ),
        Approval(
            approval_id="AP-1",
            case_id=b1.case.case_id,
            draft_id="D-STR",
            decision=ApprovalDecision.APPROVED,
            approver="po",
            role="principal_officer",
            at=approved_at,
        ),
    ]
    c = one(clocks_for_bundle(b1, NOW), ClockKind.STR_DUE)
    assert c.start == approved_at
    assert c.due == eod(2026, 9, 22)  # 15,16,17,18,19(3rd Sat),21,22
    assert c.subject == "D-STR"
    assert c.clock_id == "CASE-31009260000101:STR_DUE"  # stable across approval


def test_str_done_after_export(b1):
    b1.actions.append(
        Action(
            action_id="A1",
            case_id=b1.case.case_id,
            kind=ActionKind.STR_EXPORT,
            status=ActionStatus.DONE,
            created_at=NOW,
            executed_at=NOW,
        )
    )
    c = one(clocks_for_bundle(b1, datetime(2026, 10, 1, tzinfo=IST)), ClockKind.STR_DUE)
    assert c.status == ClockStatus.DONE


def test_hold_clocks_only_for_holding_decisions(b1):
    clocks = clocks_for_bundle(b1, NOW)
    lapse_subjects = {c.clock_id.rsplit(":", 1)[1] for c in clocks if c.kind == ClockKind.HOLD_LAPSE}
    # P (pool) holds the disputed amount under SOP 9.1(iii); not K (update only) or C/E (outbound)
    assert lapse_subjects == {sc.A, sc.B, sc.D, sc.L, sc.P}
    warn_subjects = {c.clock_id.rsplit(":", 1)[1] for c in clocks if c.kind == ClockKind.HOLD_LAPSE_WARNING}
    assert warn_subjects == lapse_subjects


def test_hold_lapse_90_and_75_days(b1):
    clocks = clocks_for_bundle(b1, NOW)
    lapse = one(clocks, ClockKind.HOLD_LAPSE, sc.A)
    warning = one(clocks, ClockKind.HOLD_LAPSE_WARNING, sc.A)
    start = datetime(2026, 9, 10, 13, 45, tzinfo=IST)
    assert lapse.start == warning.start == start
    assert lapse.due == eod(2026, 12, 9)  # 10-09 + 90 days
    assert warning.due == eod(2026, 11, 24)  # day 75: intimate the LEA 15 days before the lapse
    assert warning.warn_at == eod(2026, 11, 17)  # explicit: a week before the intimation deadline
    assert lapse.warn_at == start + (lapse.due - start) * 0.8
    assert not lapse.business_days and lapse.citation_id == warning.citation_id == "SOP-10-1"
    assert "provisional" in lapse.subject


@pytest.mark.parametrize(
    "now,lapse_status,warning_status",
    [
        (datetime(2026, 9, 16, 12, 0, tzinfo=IST), ClockStatus.OPEN, ClockStatus.OPEN),
        (datetime(2026, 11, 18, 9, 0, tzinfo=IST), ClockStatus.OPEN, ClockStatus.WARNING),
        (datetime(2026, 11, 23, 9, 0, tzinfo=IST), ClockStatus.WARNING, ClockStatus.WARNING),
        (datetime(2026, 11, 24, 23, 59, 59, tzinfo=IST), ClockStatus.WARNING, ClockStatus.WARNING),
        (datetime(2026, 11, 25, 0, 0, tzinfo=IST), ClockStatus.WARNING, ClockStatus.OVERDUE),
        (datetime(2026, 12, 10, 0, 0, tzinfo=IST), ClockStatus.OVERDUE, ClockStatus.OVERDUE),
    ],
)
def test_hold_clock_statuses(b1, now, lapse_status, warning_status):
    clocks = clocks_for_bundle(b1, now)
    assert one(clocks, ClockKind.HOLD_LAPSE, sc.A).status == lapse_status
    assert one(clocks, ClockKind.HOLD_LAPSE_WARNING, sc.A).status == warning_status


def test_hold_start_from_executed_action(b2):
    executed = datetime(2026, 9, 12, 10, 0, tzinfo=IST)
    b2.actions.append(
        Action(
            action_id="A1",
            case_id=b2.case.case_id,
            kind=ActionKind.HOLD_INSTRUCTION,
            payload={"account_id": sc.M},
            status=ActionStatus.DONE,
            created_at=executed - timedelta(minutes=5),
            executed_at=executed,
        )
    )
    lapse = one(clocks_for_bundle(b2, NOW), ClockKind.HOLD_LAPSE, sc.M)
    assert lapse.start == executed
    assert lapse.due == eod(2026, 12, 11)
    assert lapse.subject == sc.M


def test_hold_start_from_approval(b2):
    approved = datetime(2026, 9, 11, 18, 0, tzinfo=IST)
    b2.drafts.append(
        Draft(
            draft_id="D-HOLD-M",
            case_id=b2.case.case_id,
            kind=DraftKind.HOLD_INSTRUCTION,
            account_id=sc.M,
            title="t",
            body="",
            created_at=approved - timedelta(hours=1),
        )
    )
    b2.approvals.append(
        Approval(
            approval_id="AP",
            case_id=b2.case.case_id,
            draft_id="D-HOLD-M",
            decision=ApprovalDecision.APPROVED,
            approver="no",
            role="nodal_officer",
            at=approved,
        )
    )
    lapse = one(clocks_for_bundle(b2, NOW), ClockKind.HOLD_LAPSE, sc.M)
    assert lapse.start == approved and lapse.due == eod(2026, 12, 10)


def test_grievance_chain_for_innocent_holder(b1):
    clocks = clocks_for_bundle(b1, NOW)
    filing = one(clocks, ClockKind.GRIEVANCE_FILING)
    io = one(clocks, ClockKind.IO_DECISION)
    dgo = one(clocks, ClockKind.DGO_ESCALATION)
    assert filing.clock_id.endswith(sc.L) and io.clock_id.endswith(sc.L) and dgo.clock_id.endswith(sc.L)
    assert filing.due == eod(2026, 9, 17)
    assert io.start == filing.due and io.due == eod(2026, 10, 2)
    assert dgo.start == io.due and dgo.due == eod(2026, 10, 17)
    assert {filing.citation_id, io.citation_id, dgo.citation_id} == {"SOP-10-1"}
    # 80% of the filing window (10-09 13:45 -> 17-09 23:59:59) elapses on 16-09 at about 12:20 IST
    assert filing.status == ClockStatus.OPEN
    later = one(clocks_for_bundle(b1, datetime(2026, 9, 16, 13, 0, tzinfo=IST)), ClockKind.GRIEVANCE_FILING)
    assert later.status == ClockStatus.WARNING


def test_restoration_clocks(b1, b2):
    clocks = clocks_for_bundle(b1, NOW)
    notice = one(clocks, ClockKind.P1_NOTICE, sc.N1)
    reply = one(clocks, ClockKind.P1_REPLY, sc.N1)
    remit = one(clocks, ClockKind.P1_REMIT, sc.N1)
    assert notice.start == datetime(2026, 9, 10, 13, 40, tzinfo=IST)  # complaint reported_at
    assert notice.due == eod(2026, 9, 17)
    assert reply.due == eod(2026, 10, 2) and remit.due == eod(2026, 10, 17)
    assert notice.citation_id == "SOP-11-P1"
    clocks2 = clocks_for_bundle(b2, NOW)
    notices = [c for c in clocks2 if c.kind == ClockKind.P1_NOTICE]
    assert {c.subject for c in notices} == {sc.N2, sc.N3}
    assert {c.citation_id for c in notices} == {"SOP-11-P2"}
    assert one(clocks2, ClockKind.P1_NOTICE, sc.N2).due == eod(2026, 9, 18)


def test_no_restoration_clocks_for_court_process_or_nothing_recoverable(b1, b2):
    b1.plan.restoration_process = RestorationProcess.P3
    assert not [c for c in clocks_for_bundle(b1, NOW) if c.kind == ClockKind.P1_NOTICE]
    b2.plan.per_victim_recoverable = {sc.N2: 120000.0}
    assert {c.subject for c in clocks_for_bundle(b2, NOW) if c.kind == ClockKind.P1_NOTICE} == {sc.N2}


def test_bank_data_clock(b1):
    c = one(clocks_for_bundle(b1, NOW), ClockKind.BANK_DATA)
    assert c.due == eod(2026, 9, 12)
    assert c.status == ClockStatus.OVERDUE
    assert c.warn_at == c.start + (c.due - c.start) * 0.8


def test_ids_deterministic_and_unique(b1, b2):
    for b in (b1, b2):
        first = [c.clock_id for c in clocks_for_bundle(b, NOW)]
        second = [c.clock_id for c in clocks_for_bundle(b, datetime(2027, 1, 1, tzinfo=IST))]
        assert sorted(first) == sorted(second)
        assert len(first) == len(set(first))
    assert clock_id("C1", ClockKind.HOLD_LAPSE, sc.A) == f"C1:HOLD_LAPSE:{sc.A}"
    assert len(clocks_for_bundle(b1, NOW)) == 1 + 5 * 2 + 3 + 3 + 1
    assert len(clocks_for_bundle(b2, NOW)) == 1 + 2 + 2 * 3 + 1


def test_done_is_preserved(b1):
    clocks = clocks_for_bundle(b1, NOW)
    bank = by_id(clocks)["CASE-31009260000101:BANK_DATA"]
    b1.clocks = [bank.model_copy(update={"status": ClockStatus.DONE})]
    later = datetime(2027, 1, 1, tzinfo=IST)
    again = by_id(clocks_for_bundle(b1, later))
    assert again["CASE-31009260000101:BANK_DATA"].status == ClockStatus.DONE
    assert again[f"CASE-31009260000101:HOLD_LAPSE:{sc.A}"].status == ClockStatus.OVERDUE
    evaluated = evaluate_clocks(b1.clocks, later)
    assert evaluated[0].status == ClockStatus.DONE


def test_evaluate_clocks_sorted_by_due(b1):
    clocks = evaluate_clocks(reversed(clocks_for_bundle(b1, NOW)), NOW)
    dues = [c.due for c in clocks]
    assert dues == sorted(dues)
    assert clocks[0].kind == ClockKind.BANK_DATA


def test_no_trace_no_plan(b1):
    b1.trace = None
    b1.plan = None
    clocks = clocks_for_bundle(b1, NOW)
    # without a plan every complaint still gets P1 clocks; nothing else can be computed
    assert {c.kind for c in clocks} == {ClockKind.P1_NOTICE, ClockKind.P1_REPLY, ClockKind.P1_REMIT}
