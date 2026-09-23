"""Maker-checker hardening and complaint grouping (review findings F3 and F6)."""

from datetime import datetime

import pytest

from wapsi.adapters.llm_fake import FakeLLM
from wapsi.adapters.memory import InMemoryLedger, RecordingActionSink
from wapsi.fixtures import showcase as sc
from wapsi.models import (
    Account,
    AccountType,
    ActionKind,
    ApprovalDecision,
    Bank,
    CaseStatus,
    Channel,
    Complaint,
    DraftKind,
    Transaction,
)
from wapsi.pipeline.run import approve, draft_fingerprint, group_complaints, process_case
from wapsi.timeutil import IST


@pytest.fixture()
def bundle():
    return process_case(sc.showcase_ledger(), [sc.N1], llm=FakeLLM())


def _hold(b, acct=sc.A):
    return next(d for d in b.drafts if d.kind == DraftKind.HOLD_INSTRUCTION and d.account_id == acct)


def test_rejected_draft_cannot_be_approved_later(bundle):
    d = _hold(bundle)
    approve(bundle, d.draft_id, approver="sup", role="supervisor", decision=ApprovalDecision.REJECTED,
            reason_code="WRONG_ACCOUNT")
    with pytest.raises(ValueError, match="rejected"):
        approve(bundle, d.draft_id, approver="sup", role="supervisor")


def test_double_approval_is_idempotent(bundle):
    d = _hold(bundle)
    sink = RecordingActionSink()
    approve(bundle, d.draft_id, approver="sup", role="supervisor", sink=sink)
    approve(bundle, d.draft_id, approver="sup", role="supervisor", sink=sink)
    holds = [a for a in sink.actions if a.kind == ActionKind.HOLD_INSTRUCTION]
    assert len(holds) == 1
    assert sum(1 for a in bundle.approvals if a.draft_id == d.draft_id) == 1
    assert any(e.action == "approval_duplicate_ignored" for e in bundle.audit)


def test_edited_draft_cannot_ride_on_old_verification(bundle):
    d = _hold(bundle)
    idx = bundle.drafts.index(d)
    bundle.drafts[idx] = d.model_copy(update={"body": d.body + "\nFreeze the entire account immediately."})
    with pytest.raises(ValueError, match="changed after it was verified"):
        approve(bundle, d.draft_id, approver="sup", role="supervisor")


def test_edited_draft_is_reverified_when_ledger_given(bundle):
    d = _hold(bundle)
    idx = bundle.drafts.index(d)
    bundle.drafts[idx] = d.model_copy(update={"body": d.body.replace("₹43,200.00", "₹4,70,000.00")})
    with pytest.raises(ValueError, match="fails verification"):
        approve(bundle, d.draft_id, approver="sup", role="supervisor", ledger=sc.showcase_ledger())
    assert not bundle.verifications[d.draft_id].passed


def test_approval_records_fingerprint_and_status(bundle):
    ledger = sc.showcase_ledger()
    holds = [d for d in bundle.drafts if d.kind == DraftKind.HOLD_INSTRUCTION]
    for d in holds:
        approve(bundle, d.draft_id, approver="sup", role="supervisor", ledger=ledger, reason_code="OK")
    assert bundle.case.status == CaseStatus.APPROVED
    assert all(a.draft_sha256 == draft_fingerprint(next(x for x in bundle.drafts if x.draft_id == a.draft_id))
               for a in bundle.approvals)


def _chain_ledger():
    """V1 -> V2 (complaint N1); V2 (own money) -> M (complaint N2): one case, not two."""
    t = lambda h, m: datetime(2026, 9, 12, h, m, tzinfo=IST)  # noqa: E731
    accounts = [
        Account(account_id="OB02-5100000001", bank_id="OB02", account_type=AccountType.INDIVIDUAL,
                opened_at=datetime(2015, 1, 1, tzinfo=IST), opening_balance=100000.0),
        Account(account_id="HB01-5100000002", bank_id="HB01", account_type=AccountType.INDIVIDUAL,
                opened_at=datetime(2016, 1, 1, tzinfo=IST), opening_balance=30000.0),
        Account(account_id="HB01-5100000003", bank_id="HB01", account_type=AccountType.INDIVIDUAL,
                opened_at=datetime(2026, 9, 1, tzinfo=IST)),
    ]
    txns = [
        Transaction(txn_id="C1", ts=t(10, 0), from_account="OB02-5100000001", to_account="HB01-5100000002",
                    from_bank_id="OB02", to_bank_id="HB01", amount=50000.0, channel=Channel.IMPS, utr="111111111111"),
        Transaction(txn_id="C2", ts=t(10, 30), from_account="HB01-5100000002", to_account="HB01-5100000003",
                    from_bank_id="HB01", to_bank_id="HB01", amount=60000.0, channel=Channel.IMPS, utr="222222222222"),
    ]
    complaints = [
        Complaint(ncrp_id="N-CHAIN-1", reported_at=t(12, 0), victim_account="OB02-5100000001", victim_name="A",
                  victim_mobile="9X", amount_claimed=50000.0, narrative_text="Sent money after a scam call."),
        Complaint(ncrp_id="N-CHAIN-2", reported_at=t(13, 0), victim_account="HB01-5100000002", victim_name="B",
                  victim_mobile="9X", amount_claimed=60000.0, narrative_text="Tricked into a transfer."),
    ]
    return InMemoryLedger(
        banks=[Bank(bank_id="HB01", name="Home", is_home=True), Bank(bank_id="OB02", name="Other")],
        accounts=accounts, transactions=txns, complaints=complaints,
        complaint_txns=[("N-CHAIN-1", "C1"), ("N-CHAIN-2", "C2")],
    )


def test_victim_to_victim_chain_is_one_case():
    ledger = _chain_ledger()
    assert group_complaints(ledger, ledger.complaints()) == [["N-CHAIN-1", "N-CHAIN-2"]]
