"""EXCEPTIONAL_ACCOUNT_LEVEL_HOLD: amount-specific holds by default; account-level action only as a recorded,
supervisor-approved exception (SOP 5(ix), 9.1).

Never emitted automatically; created only by an officer request carrying >= 1 reason from the closed
``AccountLevelReason`` list and applied only after a different supervisor approves it (maker-checker, bound to the
request's fingerprint). The verifier fails a plan / draft with an account-level hold lacking reasons or approval.
"""

from __future__ import annotations

import pytest

from wapsi.adapters.llm_fake import FakeLLM
from wapsi.adapters.memory import RecordingActionSink
from wapsi.citations import KNOWN_IDS
from wapsi.config import TraceConfig, WapsiConfig
from wapsi.draft.drafter import draft_account_level_request
from wapsi.fixtures import showcase as sc
from wapsi.graph import build_hold_plan, trace_case
from wapsi.graph.holdplan import (
    DISPOSITION_CITATIONS,
    REASON_OFFICER_REQUESTED,
    REASON_SUPERVISOR_APPROVED,
    account_level_block,
    account_level_reasons,
    apply_account_level_hold,
)
from wapsi.models import (
    AccountLevelReason,
    ActionKind,
    Approval,
    ApprovalDecision,
    ClassificationResult,
    Disposition,
    DraftKind,
)
from wapsi.money import eq
from wapsi.pipeline.run import (
    SIMULATED_CFCFRMS,
    approve,
    draft_fingerprint,
    process_case,
    request_account_level_hold,
)
from wapsi.timeutil import utcnow
from wapsi.verify import verify_draft, verify_plan
from wapsi.verify.report import code_of

FRAUDSTER = AccountLevelReason.FRAUDSTER_CONTROLLED_ACCOUNT.value


def _errors(report) -> set[str]:
    return {code_of(c) for c in report.checks if not c.passed and c.severity == "error"}


@pytest.fixture(scope="module")
def ledger():
    return sc.showcase_ledger()


@pytest.fixture(scope="module")
def processed(ledger):
    return process_case(ledger, [sc.N1], llm=FakeLLM())


@pytest.fixture()
def bundle(processed):
    return processed.model_copy(deep=True)


def _decision(bundle, acct):
    return next(d for d in bundle.plan.decisions if d.account_id == acct)


# --------------------------------------------------------------------------- never automatic


@pytest.mark.parametrize("which", [1, 2])
def test_hold_plan_never_emits_it(ledger, which):
    spec = sc.SHOWCASE_1 if which == 1 else sc.SHOWCASE_2
    trace = trace_case(ledger, spec["ncrp_ids"], TraceConfig())
    classes = {a: ClassificationResult(account_id=a, role=r, score=0.99) for a, r in spec["roles"].items()}
    plan = build_hold_plan(trace, classes, ledger, WapsiConfig())
    assert all(d.disposition != Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD for d in plan.decisions)
    assert all(Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD.value not in d.reason_codes for d in plan.decisions)


def test_it_cites_only_existing_sop_clauses():
    cites = DISPOSITION_CITATIONS[Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD]
    assert cites == ["SOP-5-IX", "SOP-9-1"]
    assert set(cites) <= KNOWN_IDS


# --------------------------------------------------------------------------- recorded reasons (closed list)


def test_reasons_come_from_the_closed_list():
    assert account_level_reasons(["fraudster_controlled_account", FRAUDSTER, " IDENTITY_THEFT_ACCOUNT "]) == [
        AccountLevelReason.FRAUDSTER_CONTROLLED_ACCOUNT,
        AccountLevelReason.IDENTITY_THEFT_ACCOUNT,
    ]
    with pytest.raises(ValueError, match="at least one recorded reason"):
        account_level_reasons([])
    with pytest.raises(ValueError, match="not a recorded account-level reason"):
        account_level_reasons(["SUSPICIOUS_VIBES"])


def test_eligibility(processed):
    trace = processed.trace
    ok = {d.account_id for d in processed.plan.decisions
          if account_level_block(d, trace.accounts.get(d.account_id)) is None}
    assert ok == {sc.A, sc.B, sc.D, sc.L}  # home-bank accounts with an amount-specific hold
    why = {d.account_id: account_level_block(d, trace.accounts.get(d.account_id)) for d in processed.plan.decisions}
    assert "SOP 9.1(iii)" in why[sc.P]  # nodal / pool / escrow: never
    assert "merchant" in why[sc.K]
    assert "another bank" in why[sc.C]
    assert account_level_block(None, None) is not None


# --------------------------------------------------------------------------- apply_account_level_hold


def test_apply_keeps_the_money_and_records_reasons(processed):
    plan = apply_account_level_hold(processed.plan, sc.A, [FRAUDSTER], requested_by="asha", approved_by="ravi")
    d = next(x for x in plan.decisions if x.account_id == sc.A)
    before = _decision(processed, sc.A)
    assert d.disposition == Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD
    assert eq(d.hold_amount, 43200.0) and eq(d.forward_hold_amount, 96000.0)  # still the amount-specific figures
    assert d.per_victim == before.per_victim
    assert d.reason_codes[:4] == [Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD.value, REASON_OFFICER_REQUESTED,
                                  FRAUDSTER, REASON_SUPERVISOR_APPROVED]
    assert d.requires_human
    assert {"SOP-5-IX", "SOP-9-1"} <= set(d.citation_ids)
    assert eq(plan.total_hold, 142848.0) and eq(plan.total_recoverable, 334848.0)
    assert _decision(processed, sc.A).disposition == Disposition.HOLD_AMOUNT  # the input plan is not mutated


def test_apply_enforces_maker_checker_and_eligibility(processed):
    with pytest.raises(PermissionError, match="maker-checker"):
        apply_account_level_hold(processed.plan, sc.A, [FRAUDSTER], requested_by="asha", approved_by="ASHA")
    with pytest.raises(ValueError, match="at least one"):
        apply_account_level_hold(processed.plan, sc.A, [], requested_by="asha", approved_by="ravi")
    with pytest.raises(ValueError, match="no amount-specific hold"):
        apply_account_level_hold(processed.plan, sc.P, [FRAUDSTER], requested_by="asha", approved_by="ravi")
    with pytest.raises(KeyError):
        apply_account_level_hold(processed.plan, "HB01-0000000000", [FRAUDSTER], requested_by="a", approved_by="b")


# --------------------------------------------------------------------------- the verifier


def test_verifier_fails_an_account_level_hold_without_approval(bundle, ledger):
    bundle.plan = apply_account_level_hold(bundle.plan, sc.A, [FRAUDSTER], requested_by="asha", approved_by="ravi")
    assert "account_level_unapproved" in _errors(verify_plan(bundle, ledger))


def test_verifier_fails_an_account_level_hold_without_reasons(bundle, ledger):
    d = _decision(bundle, sc.A)
    forged = d.model_copy(update={"disposition": Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD, "requires_human": True,
                                  "reason_codes": [Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD.value]})
    bundle.plan.decisions = [forged if x.account_id == sc.A else x for x in bundle.plan.decisions]
    errs = _errors(verify_plan(bundle, ledger))
    assert {"account_level_without_reasons", "account_level_unapproved"} <= errs


def test_verifier_fails_account_level_on_a_merchant_or_other_bank(bundle, ledger):
    for acct, code in ((sc.K, "account_level_merchant"), (sc.C, "account_level_other_bank")):
        d = _decision(bundle, acct)
        forged = d.model_copy(update={"disposition": Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD,
                                      "reason_codes": [FRAUDSTER], "requires_human": True})
        bundle.plan.decisions = [forged if x.account_id == acct else x for x in bundle.plan.decisions]
        assert code in _errors(verify_plan(bundle, ledger)), acct


def test_verifier_rejects_self_approval_and_changed_requests(bundle, ledger):
    req = request_account_level_hold(bundle, ledger, sc.A, [FRAUDSTER], requested_by="asha", role="analyst")
    bundle.plan = apply_account_level_hold(bundle.plan, sc.A, [FRAUDSTER], requested_by="asha", approved_by="ravi")
    # an approval recorded by the requester herself (bypassing approve()) does not count
    bundle.approvals.append(Approval(approval_id="APR-self", case_id=bundle.case.case_id, draft_id=req.draft_id,
                                     decision=ApprovalDecision.APPROVED, approver="asha", role="supervisor",
                                     at=utcnow(), draft_sha256=draft_fingerprint(req)))
    assert "account_level_unapproved" in _errors(verify_plan(bundle, ledger))
    # an approval of different content does not cover the request on record
    bundle.approvals.append(Approval(approval_id="APR-old", case_id=bundle.case.case_id, draft_id=req.draft_id,
                                     decision=ApprovalDecision.APPROVED, approver="ravi", role="supervisor",
                                     at=utcnow(), draft_sha256="0" * 64))
    assert "account_level_unapproved" in _errors(verify_plan(bundle, ledger))
    # the right approval passes; reasons that differ from the approved request do not
    bundle.approvals.append(Approval(approval_id="APR-ok", case_id=bundle.case.case_id, draft_id=req.draft_id,
                                     decision=ApprovalDecision.APPROVED, approver="ravi", role="supervisor",
                                     at=utcnow(), draft_sha256=draft_fingerprint(req)))
    assert verify_plan(bundle, ledger).passed
    other = AccountLevelReason.IDENTITY_THEFT_ACCOUNT.value
    d = _decision(bundle, sc.A)
    bundle.plan.decisions = [d.model_copy(update={"reason_codes": [c if c != FRAUDSTER else other
                                                                   for c in d.reason_codes]})
                             if x.account_id == sc.A else x for x in bundle.plan.decisions]
    assert "account_level_reason_mismatch" in _errors(verify_plan(bundle, ledger))


def test_request_draft_checks(bundle, ledger):
    req = draft_account_level_request(bundle, ledger, sc.A, [FRAUDSTER], requested_by="asha")
    assert verify_draft(req, bundle, ledger).passed
    assert req.structured["reasons"] == [FRAUDSTER] and req.structured["supervisor_approval_required"] is True
    bad = req.model_copy(update={"structured": {**req.structured, "reasons": ["GUT_FEELING"]}})
    errs = _errors(verify_draft(bad, bundle, ledger))
    assert {"account_level_without_reasons", "account_level_unknown_reason"} <= errs
    anon = req.model_copy(update={"structured": {**req.structured, "requested_by": ""}})
    assert "account_level_no_requester" in _errors(verify_draft(anon, bundle, ledger))
    # a request on the pool account is not eligible
    pool = req.model_copy(update={"account_id": sc.P})
    assert "account_level_not_eligible" in _errors(verify_draft(pool, bundle, ledger))


def test_drafter_refuses_missing_reasons_and_ineligible_accounts(bundle, ledger):
    with pytest.raises(ValueError, match="at least one"):
        draft_account_level_request(bundle, ledger, sc.A, [], requested_by="asha")
    with pytest.raises(ValueError, match="SOP 9.1"):
        draft_account_level_request(bundle, ledger, sc.P, [FRAUDSTER], requested_by="asha")
    with pytest.raises(ValueError, match="requesting officer"):
        draft_account_level_request(bundle, ledger, sc.A, [FRAUDSTER], requested_by=" ")


def test_freeze_wording_is_allowed_only_under_an_approved_exception(bundle, ledger):
    hold = next(d for d in bundle.drafts if d.kind == DraftKind.HOLD_INSTRUCTION and d.account_id == sc.A)
    freeze = hold.model_copy(update={"body": hold.body + "\n\nFreeze the entire account immediately.\n"})
    assert "whole_account_freeze" in _errors(verify_draft(freeze, bundle, ledger))
    req = request_account_level_hold(bundle, ledger, sc.A, [FRAUDSTER], requested_by="asha", role="analyst")
    approve(bundle, req.draft_id, approver="ravi", role="supervisor", ledger=ledger)
    hold2 = next(d for d in bundle.drafts if d.kind == DraftKind.HOLD_INSTRUCTION and d.account_id == sc.A)
    freeze2 = hold2.model_copy(update={"body": hold2.body + "\n\nFreeze the entire account immediately.\n"})
    rep = verify_draft(freeze2, bundle, ledger)
    assert "whole_account_freeze" not in _errors(rep)
    assert any(code_of(c) == "account_level_instruction" and c.severity == "warning" for c in rep.checks)


# --------------------------------------------------------------------------- pipeline: maker-checker flow


def test_officer_request_then_supervisor_approval(bundle, ledger):
    sink = RecordingActionSink()
    req = request_account_level_hold(bundle, ledger, sc.A, [FRAUDSTER], requested_by="asha", role="analyst",
                                     note="Device linked to two other mule accounts")
    assert req.kind == DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST
    assert bundle.verifications[req.draft_id].passed
    # nothing changes until a supervisor approves
    assert _decision(bundle, sc.A).disposition == Disposition.HOLD_AMOUNT
    assert "Supervisor approval is required" in req.body and "[SOP-5-IX]" in req.body
    with pytest.raises(PermissionError, match="maker-checker"):
        approve(bundle, req.draft_id, approver="asha", role="supervisor", ledger=ledger)
    assert _decision(bundle, sc.A).disposition == Disposition.HOLD_AMOUNT

    approve(bundle, req.draft_id, approver="ravi", role="supervisor", ledger=ledger, sink=sink)
    d = _decision(bundle, sc.A)
    assert d.disposition == Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD and eq(d.hold_amount, 43200.0)
    assert eq(bundle.plan.total_hold, 142848.0) and eq(bundle.plan.total_recoverable, 334848.0)
    assert any(e.action == "account_level_hold_applied" for e in bundle.audit)
    assert verify_plan(bundle, ledger).passed
    # the hold instruction is redrafted (new version, fresh approval) and still verifies
    hold = next(x for x in bundle.drafts if x.kind == DraftKind.HOLD_INSTRUCTION and x.account_id == sc.A)
    assert hold.version == 2 and "exceptional account-level hold applies" in hold.body
    assert bundle.verifications[hold.draft_id].passed
    # the amount-only customer notice is withdrawn: the officer informs the holder
    assert not any(x.kind == DraftKind.CUSTOMER_NOTICE and x.account_id == sc.A for x in bundle.drafts)
    assert all(bundle.verifications[x.draft_id].passed for x in bundle.drafts if x.account_id == sc.A)


def test_rejected_request_changes_nothing(bundle, ledger):
    req = request_account_level_hold(bundle, ledger, sc.B, [FRAUDSTER], requested_by="asha", role="analyst")
    approve(bundle, req.draft_id, approver="ravi", role="supervisor", decision=ApprovalDecision.REJECTED,
            reason_code="NOT_VICTIM_MONEY")
    assert _decision(bundle, sc.B).disposition == Disposition.HOLD_AMOUNT
    # a fresh request after a rejection is a new version with its own id
    again = request_account_level_hold(bundle, ledger, sc.B, [FRAUDSTER], requested_by="asha", role="analyst")
    assert again.draft_id != req.draft_id and again.version == 2


def test_request_guards(bundle, ledger):
    with pytest.raises(PermissionError):
        request_account_level_hold(bundle, ledger, sc.A, [FRAUDSTER], requested_by="audit", role="auditor")
    with pytest.raises(ValueError):
        request_account_level_hold(bundle, ledger, sc.A, [], requested_by="asha", role="analyst")
    with pytest.raises(ValueError):
        request_account_level_hold(bundle, ledger, sc.P, [FRAUDSTER], requested_by="asha", role="analyst")
    assert not any(d.kind == DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST for d in bundle.drafts)


def test_cfcfrms_actions_are_marked_simulated(bundle, ledger):
    sink = RecordingActionSink()
    upd = next(d for d in bundle.drafts if d.kind == DraftKind.CFCFRMS_UPDATE)
    approve(bundle, upd.draft_id, approver="ravi", role="supervisor", ledger=ledger, sink=sink)
    cf = [a for a in bundle.actions if a.kind == ActionKind.CFCFRMS_UPDATE]
    assert cf and all(a.payload.get("interface") == SIMULATED_CFCFRMS for a in cf)


def test_second_pending_request_is_refused_before_anything_is_recorded(bundle, ledger):
    first = request_account_level_hold(bundle, ledger, sc.A, [FRAUDSTER], requested_by="asha", role="analyst")
    second = request_account_level_hold(bundle, ledger, sc.A, [FRAUDSTER], requested_by="meena", role="analyst")
    approve(bundle, first.draft_id, approver="ravi", role="supervisor", ledger=ledger)
    assert _decision(bundle, sc.A).disposition == Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD
    # the still-pending duplicate does not void the approved exception
    assert verify_plan(bundle, ledger).passed
    approvals, events = len(bundle.approvals), len(bundle.audit)
    with pytest.raises(ValueError, match="no amount-specific hold"):
        approve(bundle, second.draft_id, approver="ravi", role="supervisor", ledger=ledger)
    # nothing half-recorded: no approval, no audit event, the applied exception is untouched
    assert len(bundle.approvals) == approvals and len(bundle.audit) == events
    assert _decision(bundle, sc.A).disposition == Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD
    approve(bundle, second.draft_id, approver="ravi", role="supervisor", decision=ApprovalDecision.REJECTED,
            reason_code="DUPLICATE")
    assert verify_plan(bundle, ledger).passed  # nor does rejecting it


def test_approving_an_exception_needs_the_ledger(bundle, ledger):
    req = request_account_level_hold(bundle, ledger, sc.A, [FRAUDSTER], requested_by="asha", role="analyst")
    approvals = len(bundle.approvals)
    # without the ledger the hold instruction could not be redrafted nor the account's drafts re-verified
    with pytest.raises(ValueError, match="needs the ledger"):
        approve(bundle, req.draft_id, approver="ravi", role="supervisor")
    assert len(bundle.approvals) == approvals
    assert _decision(bundle, sc.A).disposition == Disposition.HOLD_AMOUNT


@pytest.mark.parametrize(("acct", "reason"), [(sc.A, FRAUDSTER), (sc.L, AccountLevelReason.IDENTITY_THEFT_ACCOUNT.value)])
def test_every_draft_still_verifies_after_an_exception(bundle, ledger, acct, reason):
    # the supervisor first rejected the amount-only hold instruction; its redraft gets its own decision
    hold = next(x for x in bundle.drafts if x.kind == DraftKind.HOLD_INSTRUCTION and x.account_id == acct)
    approve(bundle, hold.draft_id, approver="ravi", role="supervisor", decision=ApprovalDecision.REJECTED,
            reason_code="WRONG_ACCOUNT")
    req = request_account_level_hold(bundle, ledger, acct, [reason], requested_by="asha", role="analyst")
    approve(bundle, req.draft_id, approver="ravi", role="supervisor", ledger=ledger)
    # the CFCFRMS update lists every account's disposition: redrafted as a new version
    update = next(x for x in bundle.drafts if x.kind == DraftKind.CFCFRMS_UPDATE)
    assert update.version == 2 and "EXCEPTIONAL_ACCOUNT_LEVEL_HOLD" in update.body
    # an innocent holder's EDD memo (written for HOLD_AMOUNT_NO_ESCALATION) is withdrawn with the customer notice
    assert not any(x.kind in (DraftKind.EDD_MEMO, DraftKind.CUSTOMER_NOTICE) and x.account_id == acct
                   for x in bundle.drafts)
    assert verify_plan(bundle, ledger).passed
    for x in bundle.drafts:  # re-verified from scratch, not only the stored reports
        assert verify_draft(x, bundle, ledger).passed, x.draft_id
        assert bundle.verifications[x.draft_id].passed, x.draft_id
    for x in (hold.draft_id, update.draft_id):  # the new versions can be approved
        approve(bundle, x, approver="ravi", role="supervisor", ledger=ledger)
    assert all(a.decision == ApprovalDecision.APPROVED for a in bundle.approvals[-2:])
