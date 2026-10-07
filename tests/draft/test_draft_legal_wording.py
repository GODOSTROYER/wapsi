"""Legal-accuracy wording in the draft templates.

- amount-specific holds by default; account-level action only as a recorded, supervisor-approved exception (never
  an absolute "never freeze")
- the comparison is the "whole-account freeze baseline"
- every draft that shows a CFCFRMS action carries the simulated-CFCFRMS-interface disclaimer
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from wapsi.adapters.llm_fake import FakeLLM
from wapsi.fixtures import showcase as sc
from wapsi.models import DraftKind
from wapsi.pipeline.run import process_case

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "wapsi" / "draft" / "templates"
ABSOLUTE = re.compile(
    r"no account is frozen|never (?:a |the )?(?:whole[- ]account |entire account )?freez"
    r"|never (?:hold |freeze )?the whole account|would have immobilised",
    re.IGNORECASE,
)
SIMULATED = "Simulated CFCFRMS interface"


@pytest.fixture(scope="module")
def drafts():
    ledger = sc.showcase_ledger()
    out = []
    for spec in (sc.SHOWCASE_1, sc.SHOWCASE_2):
        b = process_case(ledger, spec["ncrp_ids"], llm=FakeLLM())
        assert all(r.passed for r in b.verifications.values())
        out += b.drafts
    return out


def test_templates_carry_no_absolute_never_freeze():
    for path in sorted(TEMPLATES.glob("*.j2")):
        text = path.read_text(encoding="utf-8")
        assert not ABSOLUTE.search(text), (path.name, ABSOLUTE.search(text).group(0))


def test_rendered_drafts_carry_no_absolute_never_freeze(drafts):
    for d in drafts:
        assert not ABSOLUTE.search(d.body), (d.draft_id, ABSOLUTE.search(d.body).group(0))


def test_cfcfrms_drafts_carry_the_simulated_interface_disclaimer(drafts):
    cf = [d for d in drafts if d.kind in (DraftKind.CFCFRMS_UPDATE, DraftKind.OUTBOUND_REQUEST)]
    assert {d.kind for d in cf} == {DraftKind.CFCFRMS_UPDATE, DraftKind.OUTBOUND_REQUEST}
    for d in cf:
        assert f"> {SIMULATED}" in d.body, d.draft_id
        assert "not connected to the CFCFRMS portal" in d.body


def test_cfcfrms_update_names_the_whole_account_freeze_baseline(drafts):
    upd = next(d for d in drafts if d.kind == DraftKind.CFCFRMS_UPDATE and sc.N1 in d.body)
    assert "| Whole-account freeze baseline (comparison only, not recommended) | ₹5,03,50,000.00 |" in upd.body
    assert "Holds are amount-specific by default [SOP-5-IX] [SOP-9-1]" in upd.body
    assert "Account-level action is taken only as a recorded, supervisor-approved exception" in upd.body


def test_hold_instruction_states_the_default_and_the_exception(drafts):
    hold = next(d for d in drafts if d.kind == DraftKind.HOLD_INSTRUCTION and d.account_id == sc.A)
    assert "This is an amount-specific hold, not a freeze of the account" in hold.body
    assert "Account-level action is taken only as a recorded, supervisor-approved exception." in hold.body


def test_outbound_request_asks_for_the_amount_without_an_absolute(drafts):
    out = next(d for d in drafts if d.kind == DraftKind.OUTBOUND_REQUEST)
    assert "please hold the amount rather than the whole account" in out.body
    assert "unwarranted freezing of accounts is discouraged [SOP-5-IX]" in out.body


def test_no_template_cites_the_repealed_rbi_master_directions():
    for path in sorted(TEMPLATES.glob("*.j2")):
        assert "RBI-FRM-MD-2024" not in path.read_text(encoding="utf-8"), path.name
