"""Regression tests for the drafter review findings: numbers and currency in LLM words (F1a), the narrowed
legal-reference exemption (F1b), innocent holders at other banks in the STR (F8), skeletons for positional
binding, and optional transaction details (``_load_txns``)."""

from __future__ import annotations

import json

import pytest

from wapsi.adapters.llm_fake import FakeLLM
from wapsi.draft.drafter import INNOCENT_LABEL, draft_case
from wapsi.draft.factsheet import build_slots
from wapsi.draft.prose import CASE_SUMMARY_TASK, number_word_problems, validate_prose, write_prose
from wapsi.draft.render import PLACEHOLDER_RE, TrackingSlots, fill
from wapsi.fixtures import showcase as sc
from wapsi.models import DraftKind, Role
from wapsi.verify import summarize, verify_draft

OFFERED = {"total_stolen": "₹4,80,000.00", "total_hold": "₹1,42,848.00", "ncrp_ids": sc.N1}


def codes(text: str) -> set[str]:
    return {c for c, _ in validate_prose(text, OFFERED, [])}


class _Const:
    name = "const-llm"

    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, messages, *, temperature=0.0, max_tokens=1024, json_schema=None):
        return json.dumps({"text": self.text})


# --------------------------------------------------------------------------- F1a: words in LLM prose

REVIEW_TEXT = (
    "The complaint ⟦ncrp_ids⟧ reports a loss; nine lakh fifty thousand rupees is held at this bank and rupees two "
    "crore rest at other banks. Section 480000 applies."
)


def test_review_prose_is_rejected():
    assert {"currency_words", "number_words", "raw_digits", "unmatched_amount"} <= codes(REVIEW_TEXT)


@pytest.mark.parametrize(
    "text,code",
    [
        ("Nine lakh fifty thousand is held at this bank.", "number_words"),
        ("An amount of forty-three thousand two hundred is held.", "number_words"),
        ("Ek lakh is held.", "number_words"),
        ("Do lakh is held.", "number_words"),
        ("Paanch hazaar moved.", "number_words"),
        ("Hundreds of accounts were used.", "number_words"),
        ("The three were used.", "number_words"),
        ("Nine held in trust.", "number_words"),
        ("A hundred and five transfers.", "number_words"),
        ("Two rupees accounts.", "currency_words"),
        ("The loss is ⟦total_stolen⟧ in rupees.", "currency_words"),
        ("Rs. ⟦total_hold⟧ is held.", "currency_words"),
        ("INR ⟦total_hold⟧ is held.", "currency_words"),
        ("Fifty paise were left.", "currency_words"),
        ("₹ ⟦total_hold⟧ is held.", "currency_words"),
    ],
)
def test_number_and_currency_words_are_rejected(text, code):
    assert code in codes(text), text


@pytest.mark.parametrize(
    "text",
    [
        "One complaint (⟦ncrp_ids⟧) reports a loss of ⟦total_stolen⟧.",
        "Within hours the money passed through three accounts held with this bank.",
        "Two complaints report ⟦total_stolen⟧ stolen; ⟦total_hold⟧ is held in two mule accounts.",
        "The account has one earlier complaint and no earlier reports; one of the accounts was opened recently.",
        "The money moved through four layers within twenty-five minutes across twelve transfers.",
        "Do not freeze any account; Mr Das and a teen are not involved.",
        "Victims lost ⟦total_stolen⟧ over two days.",
    ],
)
def test_counts_in_words_are_allowed(text):
    assert codes(text) == set(), validate_prose(text, OFFERED, [])


def test_number_word_problems_reports_phrases():
    currency, words_ = number_word_problems(
        "nine lakh fifty thousand rupees moved; it went through three accounts"
    )
    assert currency == ["rupees"] and words_ == ["nine lakh fifty thousand"]


def test_review_llm_falls_back_and_the_draft_verifies(ledger):
    from wapsi.fixtures.bundles import showcase_bundle

    b = showcase_bundle(1)
    b.drafts = draft_case(b, ledger, _Const(REVIEW_TEXT))
    upd = next(d for d in b.drafts if d.kind == DraftKind.CFCFRMS_UPDATE)
    assert upd.structured["llm_fallback"] is True
    assert "attempt 1: number_words" in upd.structured["llm_issues"]
    assert "lakh" not in upd.body and "rupees" not in upd.body.lower()
    for d in b.drafts:
        assert verify_draft(d, b, ledger).passed, d.draft_id


def test_fake_llm_prose_passes_the_stricter_checks(bundle1, bundle2, ledger):
    for b in (bundle1, bundle2):
        for d in draft_case(b, ledger, FakeLLM()):
            if "llm_fallback" in d.structured:
                assert d.structured["llm_fallback"] is False, (d.draft_id, d.structured["llm_issues"])


# --------------------------------------------------------------------------- F1b: legal references


@pytest.mark.parametrize(
    "text",
    [
        "Section 106 applies.",
        "Sections 94 and 168 apply.",
        "See s. 106, Sec. 107 and ss. 94, 168.",
        "Rule 2(1)(g) and Para 5(xii), Para 9.1(iii) apply.",
    ],
)
def test_short_legal_references_keep_their_digits(text):
    assert "raw_digits" not in codes(text)


@pytest.mark.parametrize(
    "text",
    [
        "Section 480000 applies.",
        "Section 4,80,000 applies.",
        "Section 1234 applies.",
        "Rule 2(1)(g) of the Rules, 2005 applies.",
        "The Sanhita, 2023 applies.",
        "Process 2 applies.",
        "Annexure 480000 applies.",
        "Para 9.1234 applies.",
        "Section 106/480000 applies.",
    ],
)
def test_long_numbers_are_never_legal_references(text):
    assert "raw_digits" in codes(text)


# --------------------------------------------------------------------------- F8: innocent holders in the STR


def _innocent_c(bundle):
    bundle.classifications[sc.C] = bundle.classifications[sc.C].model_copy(
        update={"role": Role.INNOCENT_HOLDER}
    )
    for d in bundle.plan.decisions:
        if d.account_id == sc.C:
            d.role = Role.INNOCENT_HOLDER
    return bundle


def test_other_bank_innocent_holder_is_not_named_in_the_str(bundle1, ledger):
    b = _innocent_c(bundle1)
    b.drafts = draft_case(b, ledger, FakeLLM())
    s = next(d for d in b.drafts if d.kind == DraftKind.STR)
    blob = json.dumps([s.title, s.body, s.slots, s.structured], ensure_ascii=False)
    assert sc.C not in blob
    assert f"{INNOCENT_LABEL}, not identified" in s.body  # transaction sheet
    assert f"A non-subject account ({INNOCENT_LABEL}) received" in s.body  # fund flow
    assert not any(x["account_id"] == sc.C for x in s.structured["beneficiaries"])
    assert sc.E in blob  # other-bank mules are still reported
    report = verify_draft(s, b, ledger)
    assert report.passed, summarize(report)
    # other drafts still name the account (the outbound request, the CFCFRMS update)
    outbound = next(d for d in b.drafts if d.kind == DraftKind.OUTBOUND_REQUEST and d.account_id == sc.C)
    assert sc.C in outbound.body and verify_draft(outbound, b, ledger).passed


def test_innocent_by_decision_only_is_masked_too(bundle1, ledger):
    b = bundle1
    del b.classifications[sc.C]
    for d in b.plan.decisions:
        if d.account_id == sc.C:
            d.role = Role.INNOCENT_HOLDER
    s = next(d for d in draft_case(b, ledger, FakeLLM()) if d.kind == DraftKind.STR)
    assert sc.C not in json.dumps([s.body, s.slots, s.structured], ensure_ascii=False)


def test_home_bank_innocent_holder_is_masked_by_trail_edge(bundle1, ledger):
    s = next(d for d in draft_case(bundle1, ledger, FakeLLM()) if d.kind == DraftKind.STR)
    assert sc.L not in json.dumps([s.body, s.slots, s.structured], ensure_ascii=False)
    assert not any(k.startswith(f"{sc.L}/") for k in s.slots)
    assert any(k.startswith("edge_tainted:") for k in s.slots)


# --------------------------------------------------------------------------- skeletons


def test_body_is_the_skeleton_filled_with_the_slots(bundle1, bundle2, ledger):
    for b in (bundle1, bundle2):
        for d in draft_case(b, ledger, FakeLLM()):
            skel = d.structured["skeleton"]
            assert fill(skel["body"], d.slots) == d.body and fill(skel["title"], d.slots) == d.title
            used = {m.group(1) for part in skel.values() for m in PLACEHOLDER_RE.finditer(part)}
            assert used <= set(d.slots), d.draft_id
            assert "⟦" not in d.body and "" not in d.body and "" not in skel["body"]


def test_prose_skeleton_keeps_placeholders(bundle2, ledger):
    slots = TrackingSlots(build_slots(bundle2, ledger))
    out = write_prose(_Const("Held ⟦total_hold⟧ of ⟦total_stolen⟧."), CASE_SUMMARY_TASK, {}, slots, [])
    assert out.skeleton == "Held ⟦total_hold⟧ of ⟦total_stolen⟧."
    assert out.text == "Held ₹1,20,000.00 of ₹1,60,000.00."
    fallback = write_prose(_Const("bad 1"), CASE_SUMMARY_TASK, {}, slots, [])
    assert fallback.fallback and "⟦total_stolen⟧" in fallback.skeleton
    assert fill(fallback.skeleton, slots) == fallback.text
    forged = write_prose(_Const("Held total_hold now."), CASE_SUMMARY_TASK, {}, slots, [])
    assert forged.skeleton == "Held total_hold now."


def test_literal_placeholders_in_data_do_not_become_slots(bundle1, ledger):
    b = bundle1
    b.complaints[0] = b.complaints[0].model_copy(update={"victim_name": "Asha ⟦hold_amount⟧ total_hold"})
    drafts = draft_case(b, ledger, FakeLLM())
    pack = next(d for d in drafts if d.kind == DraftKind.RESTORATION_PACK)
    assert "Asha (hold_amount) total_hold" in pack.body
    assert "⟦hold_amount⟧" not in pack.structured["skeleton"]["body"]
    assert verify_draft(pack, b, ledger).passed


def test_cleaned_amounts_are_bound_in_the_skeleton(bundle1, ledger):
    s = next(d for d in draft_case(bundle1, ledger, FakeLLM()) if d.kind == DraftKind.STR)
    # "Received ₹2,88,000 of victim money ..." (classifier reason for B) is bound to a slot, not copied literally
    line = next(
        x for x in s.structured["skeleton"]["body"].splitlines() if "of victim money and forwarded" in x
    )
    assert "₹" not in line and "⟦" in line
    person = next(p for p in s.structured["individuals"] if p["account_id"] == sc.B)
    assert any("₹2,88,000.00" in r for r in person["reasons"])


# --------------------------------------------------------------------------- optional transaction details


class _NoTxnLedger:
    def __init__(self, base, exc):
        self._base, self._exc = base, exc

    def __getattr__(self, name):
        return getattr(self._base, name)

    def transactions_touching(self, *args, **kwargs):
        raise self._exc

    def consortium_transactions(self, *args, **kwargs):
        raise self._exc


def test_missing_transaction_details_are_recorded(bundle1, ledger):
    drafts = draft_case(bundle1, _NoTxnLedger(ledger, NotImplementedError("no txns")), FakeLLM())
    assert len(drafts) == 16
    notes = drafts[0].structured["drafter_warnings"]
    assert len(notes) == 2 and all("NotImplementedError" in n for n in notes)
    for d in drafts:
        assert verify_draft(d, bundle1, ledger).passed, d.draft_id


def test_unexpected_ledger_errors_are_not_swallowed(bundle1, ledger):
    with pytest.raises(TypeError):
        draft_case(bundle1, _NoTxnLedger(ledger, TypeError("bug")), FakeLLM())
