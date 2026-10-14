"""Prose that speaks of freezing an account never reaches a draft.

On 2026-10-14 a real model wrote "requests to other banks to freeze the outbound funds" into a prose field; the
verifier was right to fail the draft (`whole_account_freeze`), but the draft was then not approvable. The prose
validator now applies the verifier's wording rule to the prose alone: one corrective retry, then the fixed
paragraph. The verifier keeps its own independent check of the finished draft.
"""

from __future__ import annotations

import json

import pytest

from wapsi.draft.drafter import draft_case
from wapsi.draft.prose import action_wording_problems, validate_prose
from wapsi.fixtures import showcase as sc
from wapsi.fixtures.bundles import showcase_bundle
from wapsi.verify import verify_draft

OFFERED = {"total_stolen": "₹4,80,000.00", "total_outbound": "₹1,92,000.00", "ncrp_ids": sc.N1}
SEEN_LIVE = ("The complaint reports a loss of ⟦total_stolen⟧, and the bank has sent requests to other banks to freeze "
             "the outbound funds of ⟦total_outbound⟧.")


@pytest.mark.parametrize("text", [
    SEEN_LIVE,
    "The bank should freeze the account of the first-layer holder without delay.",
    "Block the account and suspend internet banking for the pool account holder.",
    "The officer is asked to seize the account balance pending the inquiry.",
])
def test_prose_that_instructs_a_freeze_or_suspension_is_rejected(text):
    assert action_wording_problems(text)
    codes = [code for code, _ in validate_prose(text, OFFERED, [])]
    assert "freeze_wording" in codes, codes


@pytest.mark.parametrize("text", [
    "The complaint reports a loss of ⟦total_stolen⟧; the amount traced to each account is held, and requests were "
    "sent to other banks for ⟦total_outbound⟧.",
    "Only the victim's money is held in each account; no account is frozen and digital banking is not suspended.",
    "A whole-account freeze would have immobilised far more than the amount that is held.",
    "The bank placed a hold on the disputed amount and asked the other bank to hold the same amount.",
])
def test_amount_specific_and_negated_wording_passes(text):
    assert action_wording_problems(text) == []
    assert "freeze_wording" not in [code for code, _ in validate_prose(text, OFFERED, [])]


def test_feedback_quotes_the_sentence_and_says_what_to_write():
    (code, message), = [p for p in validate_prose(SEEN_LIVE, OFFERED, []) if p[0] == "freeze_wording"]
    assert "freeze the outbound funds" in message and "amount is held" in message


class _Freezing:
    """A model that speaks of freezing in its first answer to every field and corrects itself when told why."""

    name = "Gemma-4-31B-IT on AMD @ developer.amd.com.cn"

    def __init__(self, corrects: bool):
        self.corrects = corrects
        self.calls = 0
        self.retries = 0

    def complete(self, messages, *, temperature=0.0, max_tokens=1024, json_schema=None) -> str:
        self.calls += 1
        told = any(m["role"] == "user" and "freezing, blocking" in m["content"] for m in messages)
        if told:
            self.retries += 1
        if told and self.corrects:
            return json.dumps({"text": "The amount traced to each account is held and the other banks were asked to "
                                       "hold the amount that reached them."})
        return json.dumps({"text": "The bank has asked the other banks to freeze the outbound funds at once."})


@pytest.mark.parametrize("corrects", [True, False])
def test_a_model_that_writes_of_freezing_still_gives_verified_drafts(corrects):
    ledger = sc.showcase_ledger()
    bundle = showcase_bundle(1)
    llm = _Freezing(corrects)
    bundle.drafts = draft_case(bundle, ledger, llm)
    written = [d for d in bundle.drafts if d.llm_model]
    assert written and llm.retries > 0, "the model was told why its answer was refused"
    for draft in bundle.drafts:
        assert "freeze the outbound funds" not in draft.body, draft.kind  # the sentence never reaches a draft
        report = verify_draft(draft, bundle, ledger)
        assert report.passed, (draft.kind, report.checks)
    for draft in written:
        assert "attempt 1: freeze_wording" in draft.structured["llm_issues"]
        # corrected: the model's second answer is used; not corrected: the fixed paragraph replaces it
        assert draft.structured["llm_fallback"] is (not corrects)
