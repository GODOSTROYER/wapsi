"""Verifier policy for the innocent-holder split: no automatic STR; the officer decides; STR recommended where the
account was used as a conduit (BONA_FIDE_RECIPIENT vs UNWITTING_CONDUIT, SOP 5(xii))."""

from __future__ import annotations

import pytest

from wapsi.adapters.llm_fake import FakeLLM
from wapsi.config import WapsiConfig
from wapsi.draft.drafter import draft_case
from wapsi.fixtures import showcase as sc
from wapsi.graph import build_hold_plan
from wapsi.graph.holdplan import REASON_CONDUIT, REASON_STR_RECOMMENDED
from wapsi.models import Disposition, DraftKind, Role
from wapsi.money import eq
from wapsi.pipeline.run import process_case
from wapsi.verify import verify_draft, verify_plan
from wapsi.verify.report import code_of


def _errors(report) -> set[str]:
    return {code_of(c) for c in report.checks if not c.passed and c.severity == "error"}


@pytest.fixture(scope="module")
def ledger():
    return sc.showcase_ledger()


@pytest.fixture(scope="module")
def bona_fide(ledger):
    return process_case(ledger, [sc.N1], llm=FakeLLM())


@pytest.fixture(scope="module")
def conduit(ledger, bona_fide):
    """Showcase 1 with L re-labelled UNWITTING_CONDUIT: re-planned and re-drafted."""
    b = bona_fide.model_copy(deep=True)
    b.classifications[sc.L] = b.classifications[sc.L].model_copy(update={"role": Role.UNWITTING_CONDUIT})
    b.plan = build_hold_plan(b.trace, b.classifications, ledger, WapsiConfig())
    b.drafts = draft_case(b, ledger, FakeLLM())
    b.verifications = {d.draft_id: verify_draft(d, b, ledger) for d in b.drafts}
    return b


def _draft(b, kind, acct=None):
    return next(d for d in b.drafts if d.kind == kind and (acct is None or d.account_id == acct))


def test_both_variants_pass_and_hold_the_same_money(bona_fide, conduit, ledger):
    assert verify_plan(bona_fide, ledger).passed and verify_plan(conduit, ledger).passed
    assert all(r.passed for r in conduit.verifications.values()), {
        k: [c.message for c in r.checks if not c.passed] for k, r in conduit.verifications.items() if not r.passed
    }
    assert eq(conduit.plan.total_hold, 142848.0) and eq(conduit.plan.total_recoverable, 334848.0)
    assert eq(bona_fide.plan.total_hold, conduit.plan.total_hold)


def test_edd_memo_bona_fide_recipient(bona_fide):
    edd = _draft(bona_fide, DraftKind.EDD_MEMO, sc.L)
    assert edd.structured["assessment"] == "INNOCENT_HOLDER"  # legacy wire value of BONA_FIDE_RECIPIENT
    assert edd.structured["recommendation"] == "HOLD_NO_ESCALATION_NO_STR"
    assert edd.structured["str_recommended"] is False and edd.structured["str_decision"] == "OFFICER"
    assert "bona fide recipient" in edd.body and "no STR by default" in edd.body


def test_edd_memo_unwitting_conduit(conduit):
    edd = _draft(conduit, DraftKind.EDD_MEMO, sc.L)
    assert edd.structured["assessment"] == "UNWITTING_CONDUIT"
    assert edd.structured["recommendation"] == "HOLD_NO_ESCALATION_STR_OFFICER_DECISION"
    assert edd.structured["str_recommended"] is True and edd.structured["str_decision"] == "OFFICER"
    assert "pass-through by someone else" in edd.body
    assert "There is no automatic STR; the officer decides" in edd.body


def test_hold_instruction_and_cfcfrms_update_wording(bona_fide, conduit):
    hold_b = _draft(bona_fide, DraftKind.HOLD_INSTRUCTION, sc.L)
    hold_c = _draft(conduit, DraftKind.HOLD_INSTRUCTION, sc.L)
    assert "bona fide recipient" in hold_b.body and "no STR by default" in hold_b.body
    assert "unwitting conduit" in hold_c.body and "an STR is recommended and the officer decides" in hold_c.body
    upd = _draft(conduit, DraftKind.CFCFRMS_UPDATE)
    assert "unwitting conduit: STR recommended, the officer decides; no automatic STR" in upd.body
    assert "UNWITTING_CONDUIT" in upd.body and "BONA_FIDE_RECIPIENT" not in upd.body
    assert "BONA_FIDE_RECIPIENT" in _draft(bona_fide, DraftKind.CFCFRMS_UPDATE).body


def test_neither_innocent_role_is_named_in_the_automatic_str(bona_fide, conduit):
    for b in (bona_fide, conduit):
        s = _draft(b, DraftKind.STR)
        assert sc.L not in s.body


def test_conduit_decision_must_recommend_str_and_need_a_human(conduit, ledger):
    b = conduit.model_copy(deep=True)
    d = next(x for x in b.plan.decisions if x.account_id == sc.L)
    assert d.disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION
    assert REASON_CONDUIT in d.reason_codes and REASON_STR_RECOMMENDED in d.reason_codes and d.requires_human
    stripped = d.model_copy(update={"reason_codes": [c for c in d.reason_codes if c != REASON_STR_RECOMMENDED],
                                    "requires_human": False})
    b.plan.decisions = [stripped if x.account_id == sc.L else x for x in b.plan.decisions]
    errs = _errors(verify_plan(b, ledger))
    assert {"conduit_str_not_recommended", "conduit_without_human"} <= errs


def test_bona_fide_recipient_gets_no_str_recommendation(bona_fide, ledger):
    b = bona_fide.model_copy(deep=True)
    d = next(x for x in b.plan.decisions if x.account_id == sc.L)
    forged = d.model_copy(update={"reason_codes": [*d.reason_codes, REASON_STR_RECOMMENDED]})
    b.plan.decisions = [forged if x.account_id == sc.L else x for x in b.plan.decisions]
    assert "bona_fide_str_recommended" in _errors(verify_plan(b, ledger))


def test_edd_memo_str_flag_must_match_the_role(bona_fide, conduit, ledger):
    edd_b = _draft(bona_fide, DraftKind.EDD_MEMO, sc.L)
    bad_b = edd_b.model_copy(update={"structured": {**edd_b.structured, "str_recommended": True}})
    assert "edd_bona_fide_str" in _errors(verify_draft(bad_b, bona_fide, ledger))
    edd_c = _draft(conduit, DraftKind.EDD_MEMO, sc.L)
    bad_c = edd_c.model_copy(update={"structured": {**edd_c.structured, "str_recommended": False}})
    assert "edd_conduit_str_missing" in _errors(verify_draft(bad_c, conduit, ledger))


def test_conduit_is_never_escalated(conduit, ledger):
    b = conduit.model_copy(deep=True)
    d = next(x for x in b.plan.decisions if x.account_id == sc.L)
    forged = d.model_copy(update={"reason_codes": [*d.reason_codes, "ESCALATE_S106_CANDIDATE"]})
    b.plan.decisions = [forged if x.account_id == sc.L else x for x in b.plan.decisions]
    assert not verify_plan(b, ledger).passed
