"""Innocent-holder split (SOP 5(xii)): BONA_FIDE_RECIPIENT vs UNWITTING_CONDUIT.

Rule: no automatic STR; the officer decides; STR recommended where the account was used as a conduit. Golden sets
and fixtures written before the split keep the legacy label INNOCENT_HOLDER.
"""

from __future__ import annotations

import pandas as pd
import pytest

import wapsi.classify as classify_mod
from wapsi.classify import classify_trace
from wapsi.classify.rules import CONDUIT_REASON, CONDUIT_SHARE, conduit_indicators
from wapsi.config import ClassifyConfig, TraceConfig, WapsiConfig
from wapsi.fixtures import showcase as sc
from wapsi.graph import build_hold_plan, trace_case
from wapsi.graph.holdplan import REASON_CONDUIT, REASON_STR_RECOMMENDED
from wapsi.models import (
    INNOCENT_ROLES,
    AccountState,
    AccountType,
    ClassificationResult,
    Disposition,
    GoldenCase,
    Role,
    legacy_role,
)
from wapsi.money import eq

# --------------------------------------------------------------------------- the role enum stays backwards compatible


def test_bona_fide_recipient_keeps_the_legacy_wire_value():
    assert Role.BONA_FIDE_RECIPIENT is Role.INNOCENT_HOLDER
    assert Role.BONA_FIDE_RECIPIENT.value == "INNOCENT_HOLDER"
    assert Role("INNOCENT_HOLDER") is Role.BONA_FIDE_RECIPIENT
    assert Role("BONA_FIDE_RECIPIENT") is Role.BONA_FIDE_RECIPIENT  # the new name is accepted as input too
    assert Role("UNWITTING_CONDUIT") is Role.UNWITTING_CONDUIT
    assert INNOCENT_ROLES == {Role.BONA_FIDE_RECIPIENT, Role.UNWITTING_CONDUIT}


def test_legacy_role_maps_both_innocent_roles_to_the_golden_label():
    assert legacy_role(Role.UNWITTING_CONDUIT) == Role.INNOCENT_HOLDER
    assert legacy_role(Role.BONA_FIDE_RECIPIENT) == Role.INNOCENT_HOLDER
    for r in (Role.MULE, Role.MERCHANT, Role.INTERMEDIARY, Role.UNKNOWN, Role.VICTIM, Role.LEGIT):
        assert legacy_role(r) == r


def test_old_golden_rows_still_load():
    row = {
        "case_id": "C-1", "world": "demo", "ncrp_ids": ["N1"], "typology": "T1", "ring_id": "R1",
        "expected_totals": {"stolen": 100.0}, "expected_roles": {"HB01-1": "INNOCENT_HOLDER", "HB01-2": "MULE"},
        "expected_dispositions": {"HB01-1": "HOLD_AMOUNT_NO_ESCALATION"}, "expected_hold": {"HB01-1": 100.0},
        "expected_process": "P1",
    }
    g = GoldenCase.model_validate(row)
    assert g.expected_roles["HB01-1"] == Role.BONA_FIDE_RECIPIENT
    assert g.model_dump(mode="json")["expected_roles"]["HB01-1"] == "INNOCENT_HOLDER"


# --------------------------------------------------------------------------- conduit_indicators


def _state(received: float, sent: float) -> AccountState:
    return AccountState(account_id="HB01-X", bank_id="HB01", account_type=AccountType.INDIVIDUAL, is_home_bank=True,
                        layer=1, balance_now=0.0, tainted_received_total=received, tainted_sent_total=sent)


def test_conduit_indicators_threshold():
    assert conduit_indicators(None) == []
    assert conduit_indicators(_state(0.0, 0.0)) == []
    assert conduit_indicators(_state(10000.0, 0.0)) == []  # kept the money: bona fide recipient
    assert conduit_indicators(_state(10000.0, 10000.0 * CONDUIT_SHARE - 1)) == []
    assert conduit_indicators(_state(10000.0, 10000.0 * CONDUIT_SHARE)) == [CONDUIT_REASON]
    assert conduit_indicators(_state(10000.0, 9000.0)) == [CONDUIT_REASON]


# --------------------------------------------------------------------------- classify_trace


class _LowScorer:
    """Every account in the innocent band (score 0.05)."""

    name = "low"

    def score(self, features: pd.DataFrame) -> pd.Series:
        return pd.Series(0.05, index=features.index)

    def contributions(self, features: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(0.0, index=features.index, columns=[*features.columns, "bias"])


@pytest.fixture(scope="module")
def ledger():
    return sc.showcase_ledger()


@pytest.fixture(scope="module")
def trace1(ledger):
    return trace_case(ledger, sc.SHOWCASE_1["ncrp_ids"], TraceConfig())


def test_innocent_band_splits_into_recipient_and_conduit(ledger, trace1, monkeypatch):
    # switch the safety guard off to see the split on its own: A passed 4,36,800 of 4,80,000 on; L kept its 4,800
    monkeypatch.setattr(classify_mod, "innocent_blockers", lambda row: [])
    out = classify_trace(ledger, trace1, ClassifyConfig(), model=_LowScorer())
    assert out[sc.L].role == Role.BONA_FIDE_RECIPIENT
    assert out[sc.A].role == Role.UNWITTING_CONDUIT
    assert out[sc.A].reasons[0] == CONDUIT_REASON
    assert CONDUIT_REASON not in out[sc.L].reasons
    # hard rules still decide merchants, intermediaries and victims
    assert out[sc.K].role == Role.MERCHANT and out[sc.P].role == Role.INTERMEDIARY and out[sc.V1].role == Role.VICTIM


def test_guard_still_wins_over_the_split(ledger, trace1):
    out = classify_trace(ledger, trace1, ClassifyConfig(), model=_LowScorer())
    # A forwarded fast to new counterparties and shares a device: the guard sends it to the officer
    assert out[sc.A].role == Role.UNKNOWN
    assert out[sc.L].role == Role.BONA_FIDE_RECIPIENT


def test_showcase_classification_is_unchanged(ledger, trace1):
    out = classify_trace(ledger, trace1, ClassifyConfig())
    assert out[sc.L].role == Role.INNOCENT_HOLDER  # the fixture's label, same wire value
    assert all(c.role != Role.UNWITTING_CONDUIT for c in out.values())


# --------------------------------------------------------------------------- hold plan reason codes


def _classes(roles):
    return {a: ClassificationResult(account_id=a, role=r, score=0.5) for a, r in roles.items()}


def test_bona_fide_plan_output_is_the_legacy_output(ledger, trace1):
    plan = build_hold_plan(trace1, _classes(sc.SHOWCASE_1["roles"]), ledger, WapsiConfig())
    d = next(x for x in plan.decisions if x.account_id == sc.L)
    assert d.disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION
    assert d.reason_codes == ["HOLD_AMOUNT_NO_ESCALATION"]
    assert not d.requires_human


def test_conduit_holds_the_amount_and_recommends_str_for_the_officer(ledger, trace1):
    roles = dict(sc.SHOWCASE_1["roles"]) | {sc.L: Role.UNWITTING_CONDUIT}
    base = build_hold_plan(trace1, _classes(sc.SHOWCASE_1["roles"]), ledger, WapsiConfig())
    plan = build_hold_plan(trace1, _classes(roles), ledger, WapsiConfig())
    d = next(x for x in plan.decisions if x.account_id == sc.L)
    assert d.disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION  # hold the amount, no escalation
    assert eq(d.hold_amount, sc.SHOWCASE_1["hold"][sc.L])
    assert REASON_CONDUIT in d.reason_codes and REASON_STR_RECOMMENDED in d.reason_codes
    assert "ESCALATE_S106_CANDIDATE" not in d.reason_codes
    assert d.requires_human  # the officer decides on the STR
    assert "No automatic STR" in d.notes
    # the split never moves money
    assert eq(plan.total_hold, base.total_hold) and eq(plan.total_recoverable, base.total_recoverable)
    assert eq(plan.total_hold, 142848.0) and eq(plan.total_recoverable, 334848.0)
