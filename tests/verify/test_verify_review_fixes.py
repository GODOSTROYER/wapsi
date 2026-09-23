"""Regression tests for the verifier review findings: amounts in words and odd spellings (F1c), identifier and
date spellings (F7), freeze instructions next to amounts (F5), update-only merchants without proof of delivery
(F4), structured-payload amounts and dates, and positional binding through the draft skeleton."""

from __future__ import annotations

import pytest

from wapsi.adapters.llm_fake import FakeLLM
from wapsi.draft.drafter import draft_case
from wapsi.fixtures import showcase as sc
from wapsi.models import Disposition, DraftKind, Invoice, OrderStatus
from wapsi.verify import summarize, verify_draft, verify_plan
from wapsi.verify.policy import freeze_instructions
from wapsi.verify.report import code_of

from .test_verify_drafts import failing, spans

# --------------------------------------------------------------------------- F1c: amounts however written


@pytest.mark.parametrize(
    "sentence,span",
    [
        ("Hold 9,99,999 rupees in this account.", "9,99,999 rupees"),
        ("Hold INR  9,99,999.00 in this account.", "INR  9,99,999.00"),
        ("Hold ₹43,200.00 crore in this account.", "₹43,200.00 crore"),
        ("Hold ₹1.2 lakh in this account.", "₹1.2 lakh"),
        ("Hold Rs.9,99,999/- in this account.", "Rs.9,99,999/-"),
        ("Hold 480,001 in this account.", "480,001"),
        ("Hold ₹९,९९,९९९ in this account.", "₹९,९९,९९९"),
        ("Hold ₹１，２００.００ in this account.", "₹１，２００.００"),
        (
            "Hold rupees nine lakh ninety nine thousand in this account.",
            "rupees nine lakh ninety nine thousand",
        ),
        ("Nine lakh fifty thousand rupees is held at this bank.", "Nine lakh fifty thousand rupees"),
        ("Rupees two crore rest at other banks.", "Rupees two crore"),
        ("Hold Rs. five lakh only.", "Rs. five lakh only"),
        ("Do lakh rupaye hold karein.", "Do lakh rupaye"),
    ],
)
def test_unmatched_amount_spellings_fail(b1, ledger, hold_a, with_body, sentence, span):
    report = verify_draft(with_body(hold_a, hold_a.body + sentence + "\n"), b1, ledger)
    assert not report.passed
    assert "body_amount_unmatched" in failing(report, "numeric")
    assert span in spans(report, "body_amount_unmatched")


@pytest.mark.parametrize(
    "sentence",
    [
        "The victim lost four lakh eighty thousand rupees.",
        "The victim lost Rs.4,80,000/- only.",
        "The victim lost INR 480,000.",
        "The victim lost ₹4.8 lakh.",
        "The hold is ₹४३,२००.००.",
    ],
)
def test_matching_amount_spellings_pass(b1, ledger, hold_a, with_body, sentence):
    report = verify_draft(with_body(hold_a, hold_a.body + sentence + "\n"), b1, ledger)
    assert report.passed, summarize(report)


# --------------------------------------------------------------------------- F7: identifiers and dates


@pytest.mark.parametrize(
    "sentence,code,span",
    [
        ("Also hold hb01-9999999999.", "unknown_account", "HB01-9999999999"),
        ("Also hold HB01 9999999999.", "unknown_account", "HB01-9999999999"),
        ("Credit UTR 4253 1000 9999 is disputed.", "unknown_utr", "425310009999"),
        ("Credit UTR N260261234567890 is disputed.", "unknown_utr", "N260261234567890"),
        ("Credit UTR hb01n26026123456 is disputed.", "unknown_utr", "HB01N26026123456"),
        ("Hold until 10/09/2027.", "unknown_date", "10/09/2027"),
        ("Hold until 10.09.2027.", "unknown_date", "10.09.2027"),
        ("Hold until 10 Sep 2027.", "unknown_date", "10 Sep 2027"),
        ("Hold until September 10, 2027.", "unknown_date", "September 10, 2027"),
        ("Hold until 2027-09-10.", "unknown_date", "2027-09-10"),
        ("Hold until 31/02/2026.", "unknown_date", "31/02/2026"),
    ],
)
def test_unknown_identifier_spellings_fail(b1, ledger, hold_a, with_body, sentence, code, span):
    report = verify_draft(with_body(hold_a, hold_a.body + sentence + "\n"), b1, ledger)
    assert not report.passed
    assert code in failing(report, "numeric")
    assert span in spans(report, code)


def test_known_identifier_spellings_pass(b1, ledger, hold_a, with_body):
    sentence = (
        "Same account hb01-1000000001, same credit 4253 1000 0001, received on 10/09/2026 (10 Sep 2026, "
        "September 10, 2026).\n"
    )
    report = verify_draft(with_body(hold_a, hold_a.body + sentence), b1, ledger)
    assert report.passed, summarize(report)


def test_cited_document_dates_are_known(b1, ledger, hold_a, with_body):
    text = "The FIU-IND circular of 02.06.2025 applies [FIU-CIRC-2025-06-02].\n"
    cited = with_body(hold_a, hold_a.body + text, citation_ids=[*hold_a.citation_ids, "FIU-CIRC-2025-06-02"])
    assert verify_draft(cited, b1, ledger).passed
    uncited = with_body(hold_a, hold_a.body + "A circular of 02.06.2025 applies.\n")
    assert "unknown_date" in failing(verify_draft(uncited, b1, ledger), "numeric")


# --------------------------------------------------------------------------- structured payload


def test_structured_amounts_must_be_case_amounts(b1, ledger, str_draft):
    ok = {
        **str_draft.structured,
        "totals": {"stolen": 480000.0, "hold": 142848.0},
        "timelines_days": {"x": 7},
    }
    assert verify_draft(str_draft.model_copy(update={"structured": ok}), b1, ledger).passed
    for bad in (
        {"accounts": [{"account_id": sc.A, "amount_held": 99999.0}]},
        {"totals": {"hold": 1.0}},
        {"per_victim": {sc.N1: 5.0}},
        {"grounds": {"narrative": "Nine lakh fifty thousand rupees moved."}},
        {"grounds": {"narrative": "₹9,99,999.00 moved."}},
    ):
        report = verify_draft(
            str_draft.model_copy(update={"structured": {**str_draft.structured, **bad}}), b1, ledger
        )
        assert "structured_amount_unmatched" in failing(report, "numeric"), bad


def test_structured_dates_and_identifier_spellings(b1, ledger, str_draft):
    ok = {
        **str_draft.structured,
        "transactions": [{"utr": "425310000001", "ts": "2026-09-10T05:32:00+00:00"}],
    }
    assert verify_draft(str_draft.model_copy(update={"structured": ok}), b1, ledger).passed
    bad = {
        **str_draft.structured,
        "transactions": [{"utr": "4253 1000 0777", "ts": "2027-01-01T00:00:00+00:00"}],
        "accounts": [{"account_id": "hb01-1000000777"}],
    }
    report = verify_draft(str_draft.model_copy(update={"structured": bad}), b1, ledger)
    assert {"unknown_utr", "unknown_account", "unknown_date"} <= failing(report, "numeric")


# --------------------------------------------------------------------------- F5: freeze next to amounts


@pytest.mark.parametrize(
    "sentence",
    [
        "Freeze account HB01-1000000001 with balance ₹45,000.00 immediately.",
        "Freeze the account; the hold is ₹43,200.00.",
        "Please freeze the said account holding ₹45,000.00.",
        "Block account HB01-1000000001 and hold ₹43,200.00.",
        "Hold account HB01-1000000001 (balance ₹45,000.00).",
        "Put the account on hold for ₹43,200.00.",
        "Mark the account debit-frozen; balance ₹45,000.00.",
        "Freeze the amount in the whole account.",
        "Freeze the balance of ₹45,000.00.",
        "Freeze ₹43,200.00 by a debit freeze on the account.",
    ],
)
def test_freeze_of_an_account_is_caught_despite_amounts(b1, ledger, hold_a, with_body, sentence):
    assert freeze_instructions(sentence), sentence
    report = verify_draft(with_body(hold_a, hold_a.body + sentence + "\n"), b1, ledger)
    assert "whole_account_freeze" in failing(report, "policy")


@pytest.mark.parametrize(
    "sentence",
    [
        "Place a freeze on ₹43,200.00 only.",
        "Freeze the disputed amount of ₹43,200.00 only.",
        "Freeze ₹43,200.00 in A/c No. HB01-1000000001.",
        "Mark lien on the amount of ₹43,200.00.",
        "Hold ₹43,200.00 in A/c No. HB01-1000000001.",
        "Amount-specific holds cover ₹3,34,848.00, while freezing the touched accounts outright would have "
        "immobilised ₹5,03,50,000.00.",
        "The account is not frozen; only ₹43,200.00 is on hold.",
    ],
)
def test_amount_specific_or_descriptive_freeze_mentions_pass(sentence):
    assert freeze_instructions(sentence) == [], sentence


# --------------------------------------------------------------------------- F4: update-only merchants


class _InvoiceLedger:
    """The showcase ledger with the merchant's invoices replaced."""

    def __init__(self, base, invoices):
        self._base, self._invoices = base, invoices

    def __getattr__(self, name):
        return getattr(self._base, name)

    def invoices_for_transactions(self, txn_ids):
        wanted = set(txn_ids)
        return [i for i in self._invoices if i.txn_id in wanted]


def _merchant_invoices(ledger, b1):
    txns = [e.txn_id for e in b1.trace.edges if e.to_account == sc.K]
    return ledger.invoices_for_transactions(txns)


@pytest.mark.parametrize("status", [OrderStatus.UNDELIVERED, OrderStatus.CANCELLED])
def test_update_only_merchant_with_an_undelivered_order_is_an_error(b1, ledger, status):
    (delivered,) = _merchant_invoices(ledger, b1)
    extra = delivered.model_copy(update={"invoice_id": "INV-K-9999", "order_status": status, "amount": 100.0})
    report = verify_plan(b1, _InvoiceLedger(ledger, [delivered, extra]))
    assert not report.passed
    assert "update_only_undelivered_merchant" in failing(report)
    assert sc.K in spans(report, "update_only_undelivered_merchant")


def test_update_only_merchant_without_invoice_is_an_error(b1, ledger):
    report = verify_plan(b1, _InvoiceLedger(ledger, []))
    assert "update_only_undelivered_merchant" in failing(report)
    msg = next(c.message for c in report.checks if code_of(c) == "update_only_undelivered_merchant")
    assert "no invoice" in msg


def test_update_only_merchant_needs_the_invoice_of_that_merchant(b1, ledger):
    (delivered,) = _merchant_invoices(ledger, b1)
    other = delivered.model_copy(update={"merchant_account": "HB01-3000000099"})
    assert "update_only_undelivered_merchant" in failing(verify_plan(b1, _InvoiceLedger(ledger, [other])))


def test_update_only_merchant_with_all_goods_delivered_passes(b1, ledger):
    (delivered,) = _merchant_invoices(ledger, b1)
    second = Invoice(**{**delivered.model_dump(), "invoice_id": "INV-K-5522", "amount": 10.0})
    report = verify_plan(b1, _InvoiceLedger(ledger, [delivered, second]))
    assert report.passed, summarize(report)
    assert failing(report, severity="warning") == set()


def test_update_only_without_ledger_is_a_warning(b1):
    report = verify_plan(b1)
    assert "merchant_delivery_unverified" in failing(report, severity="warning")


def test_update_only_is_for_merchants(b1, ledger):
    dec = next(d for d in b1.plan.decisions if d.account_id == sc.C)
    dec.disposition = Disposition.NO_HOLD_UPDATE_ONLY
    dec.tainted_amount = 0.0
    assert "update_only_non_merchant" in failing(verify_plan(b1, ledger))


# --------------------------------------------------------------------------- positional binding (skeleton)


@pytest.fixture(scope="module")
def drafted():
    from wapsi.fixtures.bundles import showcase_bundle

    ledger = sc.showcase_ledger()
    out = {}
    for which in (1, 2):
        b = showcase_bundle(which)
        b.drafts = draft_case(b, ledger, FakeLLM())
        out[which] = b
    return ledger, out


def _get(bundle, kind, acct=None):
    return next(d for d in bundle.drafts if d.kind == kind and d.account_id == acct)


def test_every_drafted_draft_verifies_through_its_skeleton(drafted):
    ledger, bundles = drafted
    total = 0
    for b in bundles.values():
        for d in b.drafts:
            assert set(d.structured["skeleton"]) == {"title", "body"}
            report = verify_draft(d, b, ledger)
            assert report.passed, summarize(report)
            assert failing(report, severity="warning") == set(), summarize(report)
            total += 1
    assert total == 16 + 5


def test_swapped_slot_values_fail(drafted):
    ledger, bundles = drafted
    b = bundles[1]
    hold = _get(b, DraftKind.HOLD_INSTRUCTION, sc.A)
    # the hold amount replaced by another legitimate slot value (the disputed amount)
    swapped = hold.model_copy(update={"body": hold.body.replace("**₹43,200.00**", "**₹4,80,000.00**")})
    report = verify_draft(swapped, b, ledger)
    assert not report.passed
    assert failing(report) == {"body_skeleton_mismatch"}
    msg = next(c.message for c in report.checks if code_of(c) == "body_skeleton_mismatch")
    assert "'**₹4,80,000.00**' where '**₹43,200.00**' is expected" in msg
    assert spans(report, "body_skeleton_mismatch") == ["**₹4,80,000.00**"]


def test_reordered_utr_list_fails(drafted):
    ledger, bundles = drafted
    b = bundles[2]
    hold = _get(b, DraftKind.HOLD_INSTRUCTION, sc.M)
    utrs = hold.slots["utrs"]
    first, second = utrs.split(", ")
    assert utrs in hold.body
    reordered = hold.model_copy(update={"body": hold.body.replace(utrs, f"{second}, {first}")})
    report = verify_draft(reordered, b, ledger)
    assert failing(report) == {"body_skeleton_mismatch"}


def test_title_and_moved_values_are_bound(drafted):
    ledger, bundles = drafted
    b = bundles[1]
    notice = _get(b, DraftKind.CUSTOMER_NOTICE, sc.A)
    retitled = notice.model_copy(update={"title": f"Customer notice: {sc.B}"})
    assert "body_skeleton_mismatch" in failing(verify_draft(retitled, b, ledger))
    # a legitimate sentence appended to the body is still a change the skeleton does not allow
    appended = notice.model_copy(update={"body": notice.body + "Held: ₹43,200.00.\n"})
    assert failing(verify_draft(appended, b, ledger)) == {"body_skeleton_mismatch"}


def test_skeleton_edited_consistently_still_recomputes_values(drafted):
    ledger, bundles = drafted
    b = bundles[1]
    hold = _get(b, DraftKind.HOLD_INSTRUCTION, sc.A)
    skel = dict(hold.structured["skeleton"])
    skel["body"] = skel["body"].replace("**⟦hold_amount⟧**", "**⟦favourite_amount⟧**")
    bad = hold.model_copy(update={"structured": {**hold.structured, "skeleton": skel}})
    assert "skeleton_unresolvable" in failing(verify_draft(bad, b, ledger))


def test_missing_or_malformed_skeleton(drafted):
    ledger, bundles = drafted
    b = bundles[1]
    hold = _get(b, DraftKind.HOLD_INSTRUCTION, sc.A)
    rest = {k: v for k, v in hold.structured.items() if k != "skeleton"}
    assert "body_skeleton_missing" in failing(
        verify_draft(hold.model_copy(update={"structured": rest}), b, ledger)
    )
    # hand-written drafts (no skeleton) fall back to the any-slot amount check
    hand = hold.model_copy(update={"structured": {**rest, "hand_written": True}})
    assert verify_draft(hand, b, ledger).passed
    # the flag does not excuse a draft that carries a skeleton
    both = hold.model_copy(
        update={"structured": {**hold.structured, "hand_written": True}, "body": hold.body + "x\n"}
    )
    assert "body_skeleton_mismatch" in failing(verify_draft(both, b, ledger))
    broken = hold.model_copy(update={"structured": {**rest, "skeleton": {"body": 1}}})
    assert "body_skeleton_malformed" in failing(verify_draft(broken, b, ledger))


def test_innocent_account_in_str_slot_keys_is_caught(drafted):
    ledger, bundles = drafted
    b = bundles[1]
    s = _get(b, DraftKind.STR)
    leaked = s.model_copy(update={"slots": {**s.slots, f"{sc.L}/disputed_amount": "₹4,800.00"}})
    assert "innocent_in_str" in failing(verify_draft(leaked, b, ledger), "policy")
