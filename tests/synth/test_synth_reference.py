"""The independent reference implementation must reproduce every number in wapsi.fixtures.showcase."""

from __future__ import annotations

from datetime import datetime

import pytest

from wapsi.fixtures import showcase as sc
from wapsi.models import Disposition, RestorationProcess, StrGround
from wapsi.money import eq
from wapsi.synth.export import ledger_to_frames
from wapsi.synth.reference import ReferenceWorld
from wapsi.timeutil import IST

SHOWCASE_NOW = datetime(2026, 9, 30, tzinfo=IST)


@pytest.fixture(scope="module")
def ref(showcase_memory):
    roles = {a: r.value for a, r in {**sc.SHOWCASE_1["roles"], **sc.SHOWCASE_2["roles"]}.items()}
    return ReferenceWorld(ledger_to_frames(showcase_memory), roles=roles, as_of=SHOWCASE_NOW)


@pytest.fixture(scope="module")
def case1(ref):
    return ref.trace([sc.N1])


@pytest.fixture(scope="module")
def case2(ref):
    return ref.trace([sc.N2, sc.N3])


def _close(actual: dict, expected: dict) -> None:
    assert set(actual) == set(expected), (sorted(actual), sorted(expected))
    for k, v in expected.items():
        assert eq(actual[k], v), (k, actual[k], v)


def test_showcase1_totals(case1):
    exp = sc.SHOWCASE_1
    _close(case1.totals, exp["totals"])
    assert case1.victim_seed == {sc.N1: 480000.0}


def test_showcase1_accounts(case1):
    exp = sc.SHOWCASE_1
    layers = {a: s.layer for a, s in case1.accounts.items()}
    assert layers == exp["layers"]
    _close({a: s.tainted_now_total for a, s in case1.accounts.items() if s.layer}, exp["tainted_now"])
    _close({a: s.balance_now for a, s in case1.accounts.items() if s.layer}, exp["balance_now"])
    _close({a: s.received_total for a, s in case1.accounts.items() if s.layer}, exp["tainted_received"])


def test_showcase1_edges(case1):
    got = {e.txn_id: (e.depth, e.tainted_total) for e in case1.edges}
    assert set(got) == set(sc.SHOWCASE_1["edges"])
    for txn, (depth, taint) in sc.SHOWCASE_1["edges"].items():
        assert got[txn][0] == depth, txn
        assert eq(got[txn][1], taint), txn
    exits = sorted(e.txn_id for e in case1.edges if e.to_account is None)
    assert exits == sc.SHOWCASE_1["cashed_out_txns"]


def test_showcase1_plan(case1):
    exp = sc.SHOWCASE_1
    assert {d.account_id: d.disposition for d in case1.decisions} == exp["dispositions"]
    _close({d.account_id: d.hold_amount for d in case1.decisions}, exp["hold"])
    _close(
        {d.account_id: d.forward_hold_amount for d in case1.decisions if d.forward_hold_amount},
        exp["forward_hold"],
    )
    _close(
        {
            d.account_id: d.tainted_amount
            for d in case1.decisions
            if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST
        },
        exp["outbound_request"],
    )
    assert all(
        d.hold_amount == 0 for d in case1.decisions if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST
    )
    assert "FORWARD_HOLD_FIRST_LAYER" in case1.decision(sc.A).reason_codes
    assert eq(case1.total_hold, exp["total_hold"])
    assert eq(case1.total_forward_hold, exp["total_forward_hold"])
    assert eq(case1.total_recoverable, exp["total_recoverable"])
    assert case1.per_victim_recoverable == {sc.N1: exp["total_recoverable"]}
    assert eq(case1.baseline_freeze_amount, exp["baseline_freeze_amount"])
    assert case1.baseline_freeze_accounts == exp["baseline_freeze_accounts"]
    assert eq(case1.over_hold_amount, exp["over_hold_amount"])
    assert case1.process == exp["process"] == RestorationProcess.P1
    assert case1.str_ground == exp["str_ground"] == StrGround.PROCEEDS_OF_CRIME
    assert case1.escalation_candidates == []  # D has 2 prior reports but N1 carries no FIR
    assert case1.warnings == []


def test_showcase2(case2):
    exp = sc.SHOWCASE_2
    _close(case2.totals, exp["totals"])
    m = case2.accounts[sc.M]
    assert m.tainted_now == exp["tainted_now_by_victim"][sc.M]
    assert eq(m.balance_now, exp["balance_now"][sc.M])
    assert {a: s.layer for a, s in case2.accounts.items()} == exp["layers"]
    assert eq(m.received_total, exp["tainted_received"][sc.M])
    got = {e.txn_id: e for e in case2.edges}
    for txn, (depth, taint) in exp["edges"].items():
        assert got[txn].depth == depth and eq(got[txn].tainted_total, taint)
    assert got["TX-103"].tainted == exp["edge_taint_by_victim"]["TX-103"]
    d = case2.decision(sc.M)
    assert d.disposition == exp["dispositions"][sc.M] == Disposition.HOLD_AMOUNT
    assert eq(d.hold_amount, exp["hold"][sc.M])
    assert eq(d.forward_hold_amount, exp["forward_hold"][sc.M])
    assert d.forward_per_victim == exp["forward_hold_by_victim"][sc.M]
    assert d.per_victim == exp["tainted_now_by_victim"][sc.M]
    assert d.requires_human is exp["requires_human"][sc.M]
    assert set(exp["reason_codes_include"][sc.M]) <= set(d.reason_codes)
    assert case2.per_victim_recoverable == exp["per_victim_recoverable"]
    assert eq(case2.baseline_freeze_amount, exp["baseline_freeze_amount"])
    assert case2.baseline_freeze_accounts == exp["baseline_freeze_accounts"]
    assert case2.process == exp["process"] == RestorationProcess.P2
    assert case2.escalation_candidates == [sc.M]


def test_escalation_needs_fir(showcase_memory):
    """Same world, but N2's FIR removed: the flag must disappear while the hold stays."""
    frames = ledger_to_frames(showcase_memory)
    frames["complaints"].loc[frames["complaints"]["ncrp_id"] == sc.N2, "fir_number"] = None
    roles = {sc.M: "MULE"}
    case = ReferenceWorld(frames, roles=roles, as_of=SHOWCASE_NOW).trace([sc.N2, sc.N3])
    d = case.decision(sc.M)
    assert d.disposition == Disposition.HOLD_AMOUNT and not d.requires_human
    assert "ESCALATE_S106_CANDIDATE" not in d.reason_codes


def test_innocent_and_unknown_roles(showcase_memory):
    """Without roles the landlord is a plain HOLD_AMOUNT; the rule depends on the role only."""
    frames = ledger_to_frames(showcase_memory)
    case = ReferenceWorld(frames, roles={}, as_of=SHOWCASE_NOW).trace([sc.N1])
    assert case.decision(sc.L).disposition == Disposition.HOLD_AMOUNT
    assert case.str_ground is None  # no known mule at the home bank


def test_seed_claimed_twice_rejected(showcase_memory):
    frames = ledger_to_frames(showcase_memory)
    import pandas as pd

    frames["complaint_txns"] = pd.concat(
        [frames["complaint_txns"], pd.DataFrame({"ncrp_id": [sc.N2], "txn_id": ["TX-001"]})],
        ignore_index=True,
    )
    with pytest.raises(ValueError):
        ReferenceWorld(frames, as_of=SHOWCASE_NOW).trace([sc.N1, sc.N2])
