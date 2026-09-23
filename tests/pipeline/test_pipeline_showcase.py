import pytest

from wapsi.adapters.llm_fake import FakeLLM
from wapsi.adapters.memory import InMemoryCaseStore, RecordingActionSink
from wapsi.fixtures import showcase as sc
from wapsi.models import ActionKind, ApprovalDecision, CaseStatus, Disposition, DraftKind, Role
from wapsi.money import eq
from wapsi.pipeline.run import approve, group_complaints, pending_holds, process_case


@pytest.fixture(scope="module")
def ledger():
    return sc.showcase_ledger()


def test_grouping(ledger):
    assert group_complaints(ledger, ledger.complaints()) == [[sc.N1], [sc.N2, sc.N3]]


@pytest.mark.parametrize("which", [1, 2])
def test_end_to_end_matches_showcase(ledger, which):
    exp = sc.SHOWCASE_1 if which == 1 else sc.SHOWCASE_2
    store = InMemoryCaseStore()
    b = process_case(ledger, exp["ncrp_ids"], llm=FakeLLM(), store=store)

    assert b.case.status == CaseStatus.VERIFIED, [
        (k, [c.message for c in r.checks if not c.passed]) for k, r in b.verifications.items() if not r.passed
    ]
    for field, value in exp["totals"].items():
        assert eq(getattr(b.trace.totals, field), value), field
    decisions = {d.account_id: d for d in b.plan.decisions}
    for acct, disp in exp["dispositions"].items():
        assert decisions[acct].disposition == disp, acct
        assert eq(decisions[acct].hold_amount, exp["hold"][acct]), acct
    # roles come from the real classifier here, not from the fixture
    for acct, role in exp["roles"].items():
        got = b.classifications[acct].role
        if role == Role.MULE:
            assert got in (Role.MULE, Role.UNKNOWN), acct
        else:
            assert got == role, acct
    assert b.plan.restoration_process == exp["process"]
    assert b.clocks, "clocks should be created"
    assert store.get_bundle(b.case.case_id) is not None
    kinds = {d.kind for d in b.drafts}
    assert {
        DraftKind.HOLD_INSTRUCTION,
        DraftKind.STR,
        DraftKind.CFCFRMS_UPDATE,
        DraftKind.RESTORATION_PACK,
    } <= kinds
    if which == 1:
        assert eq(b.plan.total_hold, exp["total_hold"]) and eq(
            b.plan.total_recoverable, exp["total_recoverable"]
        )
        assert len(b.drafts) == 16


def test_approval_flow(ledger):
    b = process_case(ledger, [sc.N1], llm=FakeLLM())
    hold = next(d for d in b.drafts if d.kind == DraftKind.HOLD_INSTRUCTION and d.account_id == sc.A)
    with pytest.raises(PermissionError):
        approve(b, hold.draft_id, approver="analyst1", role="analyst")
    sink = RecordingActionSink()
    before = set(pending_holds(b))
    assert sc.A in before
    approve(b, hold.draft_id, approver="sup1", role="supervisor", reason_code="VERIFIED_OK", sink=sink)
    assert any(a.kind == ActionKind.HOLD_INSTRUCTION for a in sink.actions)
    assert sc.A not in pending_holds(b)
    assert b.approvals[-1].decision == ApprovalDecision.APPROVED
    assert any(e.action == "draft_approved" for e in b.audit)


def test_unverified_draft_cannot_be_approved(ledger):
    b = process_case(ledger, [sc.N1], llm=FakeLLM())
    d = b.drafts[0]
    b.verifications[d.draft_id] = b.verifications[d.draft_id].model_copy(update={"passed": False})
    with pytest.raises(ValueError):
        approve(b, d.draft_id, approver="sup1", role="supervisor")


def test_no_hold_for_merchant_amount_hold_for_pool(ledger):
    b = process_case(ledger, [sc.N1], llm=FakeLLM(), draft=False)
    decisions = {d.account_id: d for d in b.plan.decisions}
    assert decisions[sc.K].disposition == Disposition.NO_HOLD_UPDATE_ONLY
    p = decisions[sc.P]
    # SOP 9.1(iii): hold the disputed amount in the pool account; no forward hold, suspension, seizure or flag
    assert p.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY
    assert eq(p.hold_amount, 19200.0) and p.forward_hold_amount == 0.0
    assert "NO_SUSPENSION_OR_SEIZURE" in p.reason_codes and not p.requires_human
    assert "ESCALATE_S106_CANDIDATE" not in p.reason_codes
    assert b.plan.over_hold_amount == 0
    assert eq(b.plan.total_hold, sc.SHOWCASE_1["total_hold"])
    assert eq(b.plan.total_recoverable, sc.SHOWCASE_1["total_recoverable"])
    assert eq(b.plan.per_victim_recoverable[sc.N1], 334848.0)
    assert sc.P in pending_holds(b)


def test_showcase1_drafts_verify_cleanly(ledger):
    b = process_case(ledger, [sc.N1], llm=FakeLLM())
    assert b.case.status == CaseStatus.VERIFIED
    assert len(b.drafts) == 16
    kinds = {(d.kind, d.account_id) for d in b.drafts}
    assert {(DraftKind.HOLD_INSTRUCTION, sc.P), (DraftKind.CUSTOMER_NOTICE, sc.P)} <= kinds
    for draft_id, report in b.verifications.items():
        problems = [c.message for c in report.checks if not c.passed]
        assert report.passed and problems == [], (draft_id, problems)
    assert all("SOP-5-X" not in d.citation_ids for d in b.drafts if d.kind != DraftKind.RESTORATION_PACK)
