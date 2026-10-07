"""Reconciliation of voice-note claims with the ledger (showcase world): matched / mismatch / not found."""

from __future__ import annotations

import pytest

from wapsi.fixtures import showcase as sc
from wapsi.intake.reconcile import (
    MATCHED,
    MISMATCH,
    NOT_FOUND,
    bank_match,
    name_match,
    parse_claimed_dt,
    reconcile,
)
from wapsi.intake.voice import ClaimedAmount, TxnRef


@pytest.fixture(scope="module")
def ledger():
    return sc.showcase_ledger()


def _amt(value: float, kind: str = "TRANSFER", currency: str = "INR") -> ClaimedAmount:
    return ClaimedAmount(value=value, currency=currency, kind=kind, spoken_as="", confidence=0.9)


def _ref(ref: str) -> TxnRef:
    return TxnRef(ref=ref, kind="UTR", confidence=0.9)


def _by_key(rows, key):
    return [r for r in rows if r.key == key]


def test_utr_identifies_the_complaint_and_everything_matches(ledger):
    fields = {
        "victim_name": "Meera Joshi",
        "phone": "98XXXXXX21",
        "bank": "Konkan Commercial Bank",
        "account_last4": "0001",
        "date_time": "2026-09-10T11:02:00+05:30",
        "payment_mode": "IMPS",
        "fraud_type": "IMPERSONATION",
    }
    rows, matched = reconcile(ledger, fields=fields, amounts=[_amt(480000)], refs=[_ref("425310000001")])
    assert matched == [sc.N1]
    assert {r.key for r in rows} == {
        "utr",
        "amount",
        "date_time",
        "payment_mode",
        "account_last4",
        "victim_name",
        "phone",
        "bank",
        "fraud_type",
    }
    assert all(r.status == MATCHED for r in rows), [(r.key, r.status) for r in rows]
    utr = _by_key(rows, "utr")[0]
    assert utr.ncrp_id == sc.N1 and "₹4,80,000.00" in utr.ledger and utr.money


def test_mismatch_and_not_found_never_change_the_ledger_figure(ledger):
    amounts = [_amt(100000), _amt(10000), _amt(110000, "TOTAL_LOSS")]
    refs = [_ref("425320000101"), _ref("425320000199")]
    rows, matched = reconcile(ledger, fields={"account_last4": "0002"}, amounts=amounts, refs=refs)
    assert matched == [sc.N2]
    utr = {r.claimed: r for r in _by_key(rows, "utr")}
    assert utr["425320000101"].status == MATCHED
    assert utr["425320000199"].status == NOT_FOUND and utr["425320000199"].ledger == "—"
    amt = {r.claimed: r for r in _by_key(rows, "amount")}
    assert amt["₹1,00,000.00"].status == MATCHED
    assert amt["₹10,000.00"].status == MISMATCH and amt["₹10,000.00"].ledger == "₹1,00,000.00"
    total = amt["₹1,10,000.00"]
    assert total.status == MISMATCH and total.ledger.startswith("₹1,00,000.00") and "ledger" in total.note
    assert ledger.complaint(sc.N2).amount_claimed == 100000.0  # untouched


def test_without_utr_three_independent_agreements_match(ledger):
    fields = {
        "account_last4": "0003",
        "date_time": "2026-09-11T15:10:00+05:30",
        "victim_name": "Fatima Shaikh",
    }
    rows, matched = reconcile(ledger, fields=fields, amounts=[_amt(60000)], refs=[])
    assert matched == [sc.N3]
    assert all(r.status == MATCHED for r in rows)


def test_an_amount_alone_never_identifies_a_complaint(ledger):
    rows, matched = reconcile(ledger, fields={}, amounts=[_amt(480000)], refs=[])
    assert matched == []
    assert [r.status for r in rows] == [NOT_FOUND]
    assert "never identifies" in rows[0].note


def test_unknown_utr_and_nothing_else_matches_nothing(ledger):
    fields = {
        "victim_name": "Somebody Else",
        "bank": "Konkan Commercial Bank",
        "payment_mode": "UPI",
        "date_time": "2026-09-12",
    }
    rows, matched = reconcile(ledger, fields=fields, amounts=[_amt(5000)], refs=[_ref("999999999999")])
    assert matched == []
    assert {r.status for r in rows} == {NOT_FOUND}
    assert {r.key for r in rows} == {"utr", "amount", "date_time", "payment_mode", "victim_name", "bank"}


def test_time_and_day_mismatches(ledger):
    base = {"account_last4": "0001"}
    rows, _ = reconcile(
        ledger,
        fields={**base, "date_time": "2026-09-10T16:30:00+05:30"},
        amounts=[],
        refs=[_ref("425310000001")],
    )
    dt = _by_key(rows, "date_time")[0]
    assert (
        dt.status == MISMATCH
        and dt.note == "Same day, different time."
        and dt.ledger == "10-09-2026 11:02 IST"
    )
    rows, _ = reconcile(
        ledger, fields={**base, "date_time": "2026-09-09"}, amounts=[], refs=[_ref("425310000001")]
    )
    assert _by_key(rows, "date_time")[0].note == "Different day."
    rows, _ = reconcile(
        ledger, fields={**base, "date_time": "2026-09-10"}, amounts=[], refs=[_ref("425310000001")]
    )
    assert _by_key(rows, "date_time")[0].status == MATCHED  # a bare date matches the day
    rows, _ = reconcile(
        ledger, fields={**base, "date_time": "yesterday-ish"}, amounts=[], refs=[_ref("425310000001")]
    )
    assert _by_key(rows, "date_time")[0].status == NOT_FOUND


def test_identity_mismatches(ledger):
    fields = {
        "account_last4": "9999",
        "victim_name": "Meera Iyer",
        "phone": "98XXXXXX99",
        "bank": "Deccan Bank",
        "payment_mode": "UPI",
        "fraud_type": "KYC_FRAUD",
    }
    rows, matched = reconcile(ledger, fields=fields, amounts=[], refs=[_ref("425310000001")])
    assert matched == [sc.N1]
    status = {r.key: r.status for r in rows}
    assert status == {
        "utr": MATCHED,
        "account_last4": MISMATCH,
        "victim_name": MISMATCH,
        "phone": MISMATCH,
        "bank": MISMATCH,
        "payment_mode": MISMATCH,
        "fraud_type": MISMATCH,
    }


def test_foreign_currency_is_not_reconciled(ledger):
    rows, _ = reconcile(ledger, fields={}, amounts=[_amt(500, currency="USD")], refs=[_ref("425310000001")])
    amount = _by_key(rows, "amount")[0]
    assert amount.status == NOT_FOUND and "rupee" in amount.note


def test_helpers():
    assert name_match("meera joshi", "Meera Joshi") and name_match("Meera", "Meera Joshi")
    assert not name_match("", "Meera Joshi") and not name_match("Arjun", "Meera Joshi")
    assert bank_match("Konkan Commercial", "Konkan Commercial Bank (synthetic)")
    assert not bank_match("Bank", "Konkan Commercial Bank (synthetic)")  # only stop-words: no evidence
    when, has_time = parse_claimed_dt("2026-09-10T11:02:00")
    assert has_time and when is not None and when.tzinfo is not None and when.hour == 5  # naive = IST
    assert parse_claimed_dt("2026-09-10")[1] is False
    assert parse_claimed_dt("") == (None, False)


def test_a_transfer_backs_only_one_claimed_payment(ledger):
    """The caller says the same payment twice; the ledger has one such transfer, so only one claim matches."""
    rows, matched = reconcile(
        ledger, fields={}, amounts=[_amt(480000), _amt(480000)], refs=[_ref("425310000001")]
    )
    assert matched == [sc.N1]
    first, second = _by_key(rows, "amount")
    assert first.status == MATCHED and "UTR 425310000001" in first.ledger
    assert second.status == MISMATCH and "counted once" in second.note and "₹4,80,000.00" in second.ledger


def test_other_amounts_are_not_payments(ledger):
    """A balance or a fee demanded is shown, never a red mismatch, and is no evidence for matching."""
    rows, matched = reconcile(
        ledger, fields={}, amounts=[_amt(2000, "OTHER"), _amt(480000, "OTHER")], refs=[_ref("425310000001")]
    )
    other, same = _by_key(rows, "amount")
    assert other.status == NOT_FOUND and "Not a payment" in other.note and other.ledger == "—"
    assert same.status == MATCHED and same.ncrp_id == sc.N1
    fields = {"victim_name": "Meera Joshi", "date_time": "2026-09-10"}
    assert reconcile(ledger, fields=fields, amounts=[_amt(480000, "OTHER")], refs=[])[1] == []
    assert reconcile(ledger, fields=fields, amounts=[_amt(480000)], refs=[])[1] == [sc.N1]


class _TwoPayments:
    """The showcase ledger, but Arjun's complaint reports two transfers (₹1,00,000 + ₹10,000 = ₹1,10,000)."""

    def __init__(self, base) -> None:
        self.base = base
        c = base.complaint(sc.N2)
        self.c = c.model_copy(update={"amount_claimed": 110000.0})
        t = base.complaint_transactions(sc.N2)[0]
        self.txns = [
            t,
            t.model_copy(update={"txn_id": t.txn_id + "-B", "amount": 10000.0, "utr": "425320000199"}),
        ]

    def complaints(self):
        return [self.c]

    def complaint_transactions(self, ncrp_id):
        return list(self.txns) if ncrp_id == sc.N2 else []

    def banks(self):
        return self.base.banks()

    def accounts(self, ids):
        return self.base.accounts(ids)


def test_complaint_total_claimed_as_one_payment_matches_on_the_complaint(ledger):
    two = _TwoPayments(ledger)
    rows, matched = reconcile(
        two,
        fields={"account_last4": "0002"},
        amounts=[_amt(110000), _amt(10000), _amt(110000, "TOTAL_LOSS")],
        refs=[_ref("425320000101")],
    )
    assert matched == [sc.N2]
    lump, small, total = _by_key(rows, "amount")
    assert (
        lump.status == MATCHED
        and lump.ledger == "₹1,10,000.00 on the NCRP complaint"
        and lump.ncrp_id == sc.N2
    )
    assert small.status == MATCHED and "UTR 425320000199" in small.ledger
    assert total.status == MATCHED and total.ledger.startswith("₹1,10,000.00")
