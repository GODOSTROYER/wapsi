"""The graph engine must reproduce every hand-computed number of the showcase worlds."""

from __future__ import annotations

import pytest

from wapsi.config import TraceConfig, WapsiConfig
from wapsi.fixtures import bundles
from wapsi.fixtures import showcase as sc
from wapsi.graph import build_hold_plan, trace_case
from wapsi.graph.holdplan import DISPOSITION_CITATIONS, forward_hold_vectors
from wapsi.models import ClassificationResult, Disposition, TerminalKind
from wapsi.money import eq


def _classes(roles):
    return {a: ClassificationResult(account_id=a, role=r, score=0.5) for a, r in roles.items()}


@pytest.fixture(scope="module")
def ledger():
    return sc.showcase_ledger()


@pytest.fixture(scope="module")
def run1(ledger):
    trace = trace_case(ledger, sc.SHOWCASE_1["ncrp_ids"], TraceConfig())
    plan = build_hold_plan(trace, _classes(sc.SHOWCASE_1["roles"]), ledger, WapsiConfig())
    return trace, plan


@pytest.fixture(scope="module")
def run2(ledger):
    trace = trace_case(ledger, sc.SHOWCASE_2["ncrp_ids"], TraceConfig())
    plan = build_hold_plan(trace, _classes(sc.SHOWCASE_2["roles"]), ledger, WapsiConfig())
    return trace, plan


# --------------------------------------------------------------------------- showcase 1


def test_showcase1_totals(run1):
    trace, _ = run1
    for k, v in sc.SHOWCASE_1["totals"].items():
        assert eq(getattr(trace.totals, k), v), (k, getattr(trace.totals, k), v)
    assert trace.warnings == []
    assert trace.case_id == f"CASE-{sc.N1}"
    assert trace.victim_seed == {sc.N1: 480000.0}
    assert trace.computed_at.tzinfo is not None


def test_showcase1_accounts(run1):
    trace, _ = run1
    exp = sc.SHOWCASE_1
    assert set(trace.accounts) == set(exp["layers"])
    for acct, layer in exp["layers"].items():
        assert trace.accounts[acct].layer == layer, acct
    for acct, v in exp["tainted_now"].items():
        st = trace.accounts[acct]
        assert eq(st.tainted_now_total, v), acct
        assert st.tainted_now == {sc.N1: v}, acct
    for acct, v in exp["balance_now"].items():
        assert eq(trace.accounts[acct].balance_now, v), acct
    for acct, v in exp["tainted_received"].items():
        assert eq(trace.accounts[acct].tainted_received_total, v), acct
    assert trace.accounts[sc.K].terminal == TerminalKind.MERCHANT
    assert trace.accounts[sc.P].terminal == TerminalKind.NODAL_POOL_ESCROW
    assert trace.accounts[sc.C].terminal is None  # followed through CFCFRMS
    assert trace.accounts[sc.A].parents == [sc.V1]
    assert trace.accounts[sc.D].parents == [sc.B]
    assert trace.accounts[sc.V1].first_tainted_at is None


def test_showcase1_edges(run1):
    trace, _ = run1
    got = {e.txn_id: e for e in trace.edges}
    assert set(got) == set(sc.SHOWCASE_1["edges"])
    for txn_id, (depth, tainted) in sc.SHOWCASE_1["edges"].items():
        assert got[txn_id].depth == depth, txn_id
        assert eq(got[txn_id].tainted_total, tainted), txn_id
        assert got[txn_id].tainted == {sc.N1: tainted}
    exits = sorted(e.txn_id for e in trace.edges if e.to_account is None)
    assert exits == sc.SHOWCASE_1["cashed_out_txns"]
    assert [e.txn_id for e in trace.edges] == sorted(got, key=lambda t: (got[t].ts, t))
    assert got["TX-011"].via_consortium and not got["TX-010"].via_consortium


def test_showcase1_plan(run1):
    _, plan = run1
    exp = sc.SHOWCASE_1
    dec = {d.account_id: d for d in plan.decisions}
    assert set(dec) == set(exp["dispositions"])
    for acct, disp in exp["dispositions"].items():
        d = dec[acct]
        assert d.disposition == disp, acct
        assert eq(d.hold_amount, exp["hold"][acct]), acct
        assert eq(d.forward_hold_amount, exp["forward_hold"].get(acct, 0.0)), acct
        assert d.role == exp["roles"][acct]
        assert d.reason_codes[0] == disp.value
        assert d.requires_human is False
        assert d.hold_amount <= d.tainted_amount + 0.005
    for acct, amount in exp["outbound_request"].items():
        assert eq(dec[acct].tainted_amount, amount) and dec[acct].hold_amount == 0.0
    assert "FORWARD_HOLD_FIRST_LAYER" in dec[sc.A].reason_codes
    assert dec[sc.A].per_victim == {sc.N1: 43200.0}
    assert {"SOP-9-1-I", "SOP-DEF-FIRST-LAYER"} <= set(dec[sc.A].citation_ids)
    assert "SOP-5-X" not in dec[sc.A].citation_ids
    # SOP 9.1(iii): the pool account holds the disputed amount only; no suspension, seizure or forward hold
    p = dec[sc.P]
    assert p.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY and p.hold_amount == 19200.0
    assert p.per_victim == {sc.N1: 19200.0} and p.forward_hold_amount == 0.0
    assert p.reason_codes == ["HOLD_AMOUNT_INTERMEDIARY", "NO_SUSPENSION_OR_SEIZURE"]
    assert p.citation_ids == ["SOP-9-1-III", "SOP-9-1", "SOP-ANNEX-I"]
    assert eq(plan.total_hold, exp["total_hold"])
    assert eq(plan.total_forward_hold, exp["total_forward_hold"])
    assert eq(plan.total_recoverable, exp["total_recoverable"])
    assert eq(plan.baseline_freeze_amount, exp["baseline_freeze_amount"])
    assert plan.baseline_freeze_accounts == exp["baseline_freeze_accounts"]
    assert plan.over_hold_amount == exp["over_hold_amount"]
    assert plan.restoration_process == exp["process"]
    assert plan.str_ground == exp["str_ground"]
    assert plan.per_victim_recoverable == {sc.N1: exp["total_recoverable"]}
    # deterministic ordering: layer, then account id
    assert [(d.layer, d.account_id) for d in plan.decisions] == sorted(
        (d.layer, d.account_id) for d in plan.decisions
    )


# --------------------------------------------------------------------------- showcase 2


def test_showcase2_trace(run2):
    trace, _ = run2
    exp = sc.SHOWCASE_2
    for k, v in exp["totals"].items():
        assert eq(getattr(trace.totals, k), v), k
    assert set(trace.accounts) == set(exp["layers"])
    for acct, layer in exp["layers"].items():
        assert trace.accounts[acct].layer == layer
    assert trace.accounts[sc.M].tainted_now == exp["tainted_now_by_victim"][sc.M]
    assert eq(trace.accounts[sc.M].balance_now, exp["balance_now"][sc.M])
    assert eq(trace.accounts[sc.M].tainted_received_total, exp["tainted_received"][sc.M])
    got = {e.txn_id: e for e in trace.edges}
    assert set(got) == set(exp["edges"])
    for txn_id, (depth, tainted) in exp["edges"].items():
        assert got[txn_id].depth == depth and eq(got[txn_id].tainted_total, tainted), txn_id
    for txn_id, by_victim in exp["edge_taint_by_victim"].items():
        assert got[txn_id].tainted == by_victim
    assert got["TX-101"].tainted == {sc.N2: 100000.0}
    assert got["TX-102"].tainted == {sc.N3: 60000.0}
    assert trace.victim_seed == {sc.N2: 100000.0, sc.N3: 60000.0}


def test_showcase2_plan(run2, ledger):
    trace, plan = run2
    exp = sc.SHOWCASE_2
    (d,) = plan.decisions
    assert d.account_id == sc.M
    assert d.disposition == exp["dispositions"][sc.M] == Disposition.HOLD_AMOUNT
    assert eq(d.hold_amount, exp["hold"][sc.M])
    assert eq(d.forward_hold_amount, exp["forward_hold"][sc.M])
    assert d.per_victim == exp["tainted_now_by_victim"][sc.M]
    assert d.requires_human is exp["requires_human"][sc.M]
    for code in exp["reason_codes_include"][sc.M]:
        assert code in d.reason_codes
    assert d.reason_codes == ["HOLD_AMOUNT", "ESCALATE_S106_CANDIDATE", "FORWARD_HOLD_FIRST_LAYER"]
    assert {"BNSS-106", "CASE-KARTIK-CHATUR", "SOP-5-XIII", "SOP-11-P2"} <= set(d.citation_ids)
    assert plan.per_victim_recoverable == exp["per_victim_recoverable"]
    assert eq(plan.baseline_freeze_amount, exp["baseline_freeze_amount"])
    assert plan.baseline_freeze_accounts == exp["baseline_freeze_accounts"]
    assert plan.restoration_process == exp["process"]
    fwd = forward_hold_vectors(trace, ledger.home_bank_id(), follow_consortium=True)
    assert fwd[sc.M] == exp["forward_hold_by_victim"][sc.M]


def test_escalation_needs_enough_prior_reports(run2, ledger):
    trace, _ = run2
    cfg = WapsiConfig(escalation_min_prior_reports=3)
    (d,) = build_hold_plan(trace, _classes(sc.SHOWCASE_2["roles"]), ledger, cfg).decisions
    assert d.disposition == Disposition.HOLD_AMOUNT
    assert "ESCALATE_S106_CANDIDATE" not in d.reason_codes and not d.requires_human
    assert "BNSS-106" not in d.citation_ids


def test_showcase1_never_escalates(run1):
    # D has 2 prior NCRP reports but complaint N1 carries no FIR
    _, plan = run1
    assert all("ESCALATE_S106_CANDIDATE" not in d.reason_codes for d in plan.decisions)


# --------------------------------------------------------------------------- against the hand-built bundles


def test_citation_mapping_matches_fixture():
    for disp, cites in bundles._CITES.items():
        assert DISPOSITION_CITATIONS[disp] == cites


@pytest.mark.parametrize("which", [1, 2])
def test_matches_bundle(which, run1, run2):
    trace, plan = run1 if which == 1 else run2
    bundle = bundles.showcase_bundle(which)
    bt, bp = bundle.trace, bundle.plan

    skip = {"computed_at"}
    assert trace.model_dump(exclude=skip | {"edges", "accounts", "totals"}) == bt.model_dump(
        exclude=skip | {"edges", "accounts", "totals"}
    )
    assert trace.totals.model_dump() == pytest.approx(bt.totals.model_dump())
    assert [e.model_dump() for e in trace.edges] == [e.model_dump() for e in bt.edges]
    assert set(trace.accounts) == set(bt.accounts)
    for acct, st in bt.accounts.items():
        assert trace.accounts[acct].model_dump() == st.model_dump(), acct

    mine = {d.account_id: d for d in plan.decisions}
    theirs = {d.account_id: d for d in bp.decisions}
    assert set(mine) == set(theirs)
    for acct, want in theirs.items():
        got = mine[acct]
        assert got.model_dump(exclude={"notes", "citation_ids"}) == want.model_dump(
            exclude={"notes", "citation_ids"}
        ), acct
        assert got.citation_ids == want.citation_ids, acct
        if which == 2:
            assert {"SOP-5-XIII", "SOP-11-P2"} <= set(got.citation_ids), acct
    assert plan.model_dump(exclude={"decisions"}) == bp.model_dump(exclude={"decisions"})
