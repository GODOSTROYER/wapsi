"""Golden set: conservation, rule consistency, and the committed files under data/golden."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wapsi.fixtures import showcase as sc
from wapsi.models import HOLDING_DISPOSITIONS, Disposition, GoldenCase, RestorationProcess, Role
from wapsi.money import eq, r2
from wapsi.synth.golden import golden_cases, load_golden, write_golden
from wapsi.synth.rings import TYPOLOGIES

GOLDEN_ROOT = Path(__file__).resolve().parents[2] / "data" / "golden"
PARTS = ["holdable", "cashed_out", "outbound", "intermediary", "merchant_settled", "unlocated"]


@pytest.fixture(scope="module")
def demo_golden(demo_world) -> list[GoldenCase]:
    frames, cases = demo_world
    return golden_cases(frames, cases, "demo")


def test_conservation(demo_golden):
    for g in demo_golden:
        t = g.expected_totals
        assert eq(sum(getattr(t, k) for k in PARTS), t.stolen), g.case_id
        assert abs(t.unlocated) <= 0.05, g.case_id  # rounding only
        assert t.stolen > 0 and min(getattr(t, k) for k in PARTS[:-1]) >= 0


def test_rules(demo_golden, demo_world):
    frames, _ = demo_world
    comp = frames["complaints"].set_index("ncrp_id")
    sig = frames["external_signals"].set_index("account_id")
    types = frames["accounts"].set_index("account_id")["account_type"]
    for g in demo_golden:
        notes = json.loads(g.notes)
        assert set(g.expected_dispositions) == set(g.expected_roles) == set(g.expected_hold)
        for acct, disp in g.expected_dispositions.items():
            hold = g.expected_hold[acct]
            assert disp != Disposition.ESCALATE_S106_CANDIDATE
            if disp not in HOLDING_DISPOSITIONS:
                assert hold == 0.0, (g.case_id, acct, disp)
            else:
                assert hold > 0
            if types[acct] in ("NODAL", "POOL", "ESCROW"):
                assert disp == Disposition.HOLD_AMOUNT_INTERMEDIARY  # SOP 9.1(iii): amount-specific hold
                assert g.expected_roles[acct] == Role.INTERMEDIARY
                assert acct not in notes["forward_hold"] and acct not in g.expected_escalation_candidates
            if g.expected_roles[acct] == Role.INNOCENT_HOLDER:
                assert disp in (Disposition.HOLD_AMOUNT_NO_ESCALATION, Disposition.NO_ACTION)
                assert acct not in g.expected_escalation_candidates
            if types[acct] == "MERCHANT":
                assert disp in (Disposition.NO_HOLD_UPDATE_ONLY, Disposition.HOLD_IN_POOL)
        assert set(g.expected_outbound_request) == {
            a for a, d in g.expected_dispositions.items() if d == Disposition.OUTBOUND_CFCFRMS_REQUEST
        }
        assert all(not a.startswith("HB01") for a in g.expected_outbound_request)
        holds = r2(sum(g.expected_hold.values()))
        assert eq(holds, notes["total_hold"])
        assert eq(holds + sum(g.expected_outbound_request.values()), notes["total_recoverable"])
        assert eq(sum(g.expected_per_victim_recoverable.values()), notes["total_recoverable"])
        assert set(g.expected_per_victim_recoverable) == set(g.ncrp_ids)
        n_rec = sum(v > 0 for v in g.expected_per_victim_recoverable.values())
        assert g.expected_process == (RestorationProcess.P2 if n_rec > 1 else RestorationProcess.P1)
        # every holdable / intermediary rupee is held (ring accounts have no later outflows)
        assert eq(g.expected_totals.holdable + g.expected_totals.intermediary, holds)
        for acct in g.expected_escalation_candidates:
            assert g.expected_roles[acct] == Role.MULE
            assert g.expected_dispositions[acct] in (Disposition.HOLD_AMOUNT, Disposition.FORWARD_HOLD)
            assert sig.loc[acct, "ncrp_prior_reports"] >= 2
            assert comp.loc[g.ncrp_ids, "fir_number"].notna().any()
        for acct in notes["forward_hold"]:
            assert g.expected_dispositions[acct] in (Disposition.HOLD_AMOUNT, Disposition.FORWARD_HOLD)
        has_mule = any(r == Role.MULE and a.startswith("HB01") for a, r in g.expected_roles.items())
        assert (g.expected_str_ground is not None) == has_mule
        if g.typology == "T6_TRIANGULATION":
            assert g.expected_str_ground is None
        if g.typology == "T7_MULTI_VICTIM":
            assert len(g.ncrp_ids) >= 2 and g.expected_process == RestorationProcess.P2
        if g.typology == "T10_NODAL_POOL":
            assert g.expected_totals.intermediary > 0


def test_typology_coverage(demo_golden):
    assert {g.typology for g in demo_golden} == set(TYPOLOGIES)
    assert sum(g.is_adversarial for g in demo_golden) >= 1
    disp = {d for g in demo_golden for d in g.expected_dispositions.values()}
    assert {
        Disposition.HOLD_AMOUNT,
        Disposition.HOLD_AMOUNT_NO_ESCALATION,
        Disposition.NO_HOLD_UPDATE_ONLY,
        Disposition.HOLD_AMOUNT_INTERMEDIARY,
        Disposition.OUTBOUND_CFCFRMS_REQUEST,
    } <= disp


def test_flows_match_golden(demo_world):
    frames, cases = demo_world
    flows = frames["labels_flows"]
    seeds = frames["complaint_txns"].merge(frames["transactions"][["txn_id", "amount"]], on="txn_id")
    by = flows.merge(seeds, on=["txn_id", "ncrp_id"])
    assert len(by) == len(seeds) and (by["tainted_amount_truth"] == by["amount"]).all()
    exits = frames["transactions"].loc[frames["transactions"]["channel"].isin(["ATM", "CRYPTO"]), "txn_id"]
    for c in cases:
        f = flows[flows["ncrp_id"].isin(c.ncrp_ids) & flows["txn_id"].isin(exits)]
        assert eq(f["tainted_amount_truth"].sum(), c.totals["cashed_out"]), c.case_id


def test_showcase_golden_matches_fixture(showcase_world, tmp_path):
    frames, cases = showcase_world
    golden = {g.case_id: g for g in write_golden(frames, cases, tmp_path, "showcase")}
    g1, g2 = golden[f"CASE-{sc.N1}"], golden[f"CASE-{sc.N2}"]
    e1 = sc.SHOWCASE_1
    assert g1.expected_totals.model_dump() == e1["totals"]
    assert g1.expected_roles == e1["roles"]
    assert g1.expected_dispositions == e1["dispositions"]
    assert g1.expected_hold == e1["hold"]
    assert g1.expected_outbound_request == e1["outbound_request"]
    assert g1.expected_per_victim_recoverable == {sc.N1: e1["total_recoverable"]}
    assert g1.expected_process == e1["process"] and g1.expected_str_ground == e1["str_ground"]
    assert json.loads(g1.notes)["forward_hold"] == e1["forward_hold"]
    assert json.loads(g1.notes)["baseline_freeze_amount"] == e1["baseline_freeze_amount"]
    e2 = sc.SHOWCASE_2
    assert g2.ncrp_ids == e2["ncrp_ids"]
    assert g2.expected_hold == e2["hold"] and g2.expected_roles == e2["roles"]
    assert g2.expected_per_victim_recoverable == e2["per_victim_recoverable"]
    assert g2.expected_process == e2["process"] and g2.expected_escalation_candidates == [sc.M]
    assert load_golden(tmp_path) == sorted(golden.values(), key=lambda g: g.case_id)


@pytest.mark.parametrize("world", ["showcase", "demo"])
def test_committed_golden_is_current(world, request, tmp_path):
    """data/golden/<world>/cases.jsonl must equal what the generator produces now (seed 7)."""
    committed = GOLDEN_ROOT / world / "cases.jsonl"
    if not committed.exists():
        pytest.skip(f"{committed} not generated yet")
    frames, cases = request.getfixturevalue(f"{world}_world")
    write_golden(frames, cases, tmp_path, world)
    assert (tmp_path / "cases.jsonl").read_text(encoding="utf-8") == committed.read_text(encoding="utf-8")
    assert (GOLDEN_ROOT / world / "SUMMARY.md").exists()
