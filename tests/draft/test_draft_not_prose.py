"""Degenerate model output is not prose: an endpoint can answer HTTP 200 with a run of symbols (observed on a
shared endpoint on 2026-10-14). The validator rejects it, the drafter falls back, the draft still verifies."""

from __future__ import annotations

import json

import pytest

from wapsi.draft.drafter import draft_case
from wapsi.draft.prose import not_prose_reason, parse_response, validate_prose
from wapsi.fixtures import showcase as sc
from wapsi.fixtures.bundles import showcase_bundle
from wapsi.verify import verify_draft

OFFERED = {"total_stolen": "₹4,80,000.00", "ncrp_ids": sc.N1}
# shapes the endpoint really returned for a prose request
SLASHES = '{"' + "/" * 300
MIXED = '```0010101010101010' + "/" * 40 + "0" + "/" * 200
SYMBOLS = '"e1iyeseyysee\\---y\\---//y\\---//’s\\//y l\\---//’//’s’//’//\\---//’s\\//y'
UNBROKEN = "_y" * 60


@pytest.mark.parametrize("text", [SLASHES, MIXED, SYMBOLS, UNBROKEN, "x " + "=" * 12,
                                  '{"text": "The complaint reports a loss and the money moved'])
def test_degenerate_answers_are_not_prose(text):
    assert not_prose_reason(parse_response(text)), text
    assert [code for code, _ in validate_prose(parse_response(text), OFFERED, [])] == ["not_prose"]


@pytest.mark.parametrize("text", [
    "The victim reports a loss.",
    "Victims lost ⟦total_stolen⟧ over two days.",
    "One complaint (⟦ncrp_ids⟧) reports a loss of ⟦total_stolen⟧; see Sections 94 and 168 and Para 5(xii).",
    "The money moved through four layers within twenty-five minutes across twelve transfers, and the accounts "
    "were opened shortly before the fraud; shared devices were detected.",
    "⟦total_stolen⟧ ⟦ncrp_ids⟧",  # placeholders are never counted against the text
    "खाते में राशि रोकी गई है और पीड़ित की शिकायत दर्ज की गई है, आगे की जांच जारी है।",  # vowel signs count as letters
])
def test_real_prose_passes(text):
    assert not_prose_reason(text) is None
    assert "not_prose" not in [code for code, _ in validate_prose(text, OFFERED, [])]


def test_feedback_names_the_problem():
    (code, message), = validate_prose(SLASHES, OFFERED, [])
    assert code == "not_prose" and "repeats one symbol" not in message  # JSON syntax is reported first
    assert "starts with JSON or code-fence syntax" in message and "plain English sentences" in message
    assert "repeats one symbol" in validate_prose("Hold " + "/" * 20, OFFERED, [])[0][1]
    # digits are left to the content rules, which name the actual problem
    assert [c for c, _ in validate_prose("UTR 999999999999.", OFFERED, [])] != ["not_prose"]
    assert "raw_digits" in [c for c, _ in validate_prose("Paid 111111111111 and 222222222222 and 333333333333 and "
                                                         "444444444444.", OFFERED, [])]


def test_a_garbling_model_falls_back_and_the_draft_verifies():
    class Garbling:
        name = "Gemma-4-31B-IT on AMD @ developer.amd.com.cn"

        def __init__(self):
            self.calls = 0

        def complete(self, messages, **kw):
            self.calls += 1
            return SLASHES

    ledger = sc.showcase_ledger()
    b = showcase_bundle(1)
    llm = Garbling()
    b.drafts = draft_case(b, ledger, llm)
    prose_drafts = [d for d in b.drafts if d.llm_model]
    assert len(prose_drafts) == 3 and llm.calls == 6  # one retry with feedback each, then the fixed paragraph
    for d in prose_drafts:
        assert d.structured["llm_fallback"] is True and "/////" not in d.body
        assert "attempt 1: not_prose" in d.structured["llm_issues"]
        assert verify_draft(d, b, ledger).passed
    assert "/////" not in json.dumps([d.model_dump(mode="json") for d in b.drafts])
