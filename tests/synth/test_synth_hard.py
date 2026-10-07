"""Hard world (``--size hard``): determinism, exact ground-truth invariants, the confounder typologies, and the
evaluation harness on it. Classification is meant to be imperfect here; taint and holds must stay exact."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from evals import harness
from wapsi.models import HOLDING_DISPOSITIONS, Disposition, Role
from wapsi.money import eq, r2
from wapsi.synth.export import write_frames
from wapsi.synth.golden import golden_cases, write_golden
from wapsi.synth.reference import INTERMEDIARY, ReferenceWorld, replay_order, ts_us
from wapsi.synth.rings import HARD_TYPOLOGIES, TYPOLOGIES
from wapsi.synth.world import WorldConfig, build_world

PARTS = ["holdable", "cashed_out", "outbound", "intermediary", "merchant_settled", "unlocated"]
SMALL = {"n_accounts": 900, "n_rings": 15, "activity": 0.4}


def _hashes(path: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.rglob("*.*")) if p.is_file()
    }


@pytest.fixture(scope="module")
def hard_golden(hard_world):
    frames, cases = hard_world
    return golden_cases(frames, cases, "hard")


# --------------------------------------------------------------------------- determinism


def test_determinism_by_seed(tmp_path):
    """Same seed -> byte-identical Parquet and golden set; another seed -> a different world."""
    outs = []
    for name, seed in (("a", 11), ("b", 11), ("c", 12)):
        frames, cases = build_world(WorldConfig(name="hard-small", size="hard", seed=seed, overrides=SMALL))
        out = tmp_path / name
        write_frames(frames, out)
        write_golden(frames, cases, out / "golden", "hard-small")
        outs.append(_hashes(out))
    assert outs[0] == outs[1]
    assert outs[0]["transactions.parquet"] != outs[2]["transactions.parquet"]
    assert outs[0]["cases.jsonl"] != outs[2]["cases.jsonl"]


def test_hard_dir_matches_in_process_world(hard_dir, hard_world):
    frames, _ = hard_world
    tx = pd.read_parquet(hard_dir / "transactions.parquet")
    assert len(tx) == len(frames["transactions"]) and tx["txn_id"].equals(frames["transactions"]["txn_id"])


# --------------------------------------------------------------------------- ground truth stays exact


def test_reference_clean(hard_world):
    """No warnings (no edge under min_edge_amount, no depth truncation), layers <= 5, unique flow rows."""
    frames, cases = hard_world
    assert all(not c.warnings for c in cases)
    assert max(s.layer for c in cases for s in c.accounts.values()) <= 5
    flows = frames["labels_flows"]
    assert not flows.duplicated(["txn_id", "ncrp_id"]).any() and (flows["tainted_amount_truth"] > 0).all()


def test_conservation_and_hold_bounds(hard_golden, hard_world):
    """Per case: parts sum to the stolen amount; holds + outbound requests never exceed the victims' loss; per
    victim, recoverable <= what that victim lost; every hold <= tainted and <= balance."""
    _, cases = hard_world
    ref = {c.case_id: c for c in cases}
    for g in hard_golden:
        t = g.expected_totals
        assert eq(sum(getattr(t, k) for k in PARTS), t.stolen), g.case_id
        assert abs(t.unlocated) <= 0.05 and min(getattr(t, k) for k in PARTS[:-1]) >= 0, g.case_id
        holds = r2(sum(g.expected_hold.values()))
        assert holds + sum(g.expected_outbound_request.values()) <= t.stolen + 0.01, g.case_id
        c = ref[g.case_id]
        for n, rec in g.expected_per_victim_recoverable.items():  # r9: not even a paisa of rounding over the loss
            assert round(rec * 100) <= round(c.victim_seed[n] * 100), (g.case_id, n)
        for d in c.decisions:
            assert (
                d.hold_amount <= d.tainted_amount + 0.01 and d.hold_amount <= max(d.balance_now, 0.0) + 0.01
            )
            assert (d.hold_amount > 0) == (d.disposition in HOLDING_DISPOSITIONS), (g.case_id, d.account_id)
        # ring accounts are quiet after their trail: every holdable / intermediary rupee is held, less the paisa
        # rounding residue taken back so no victim is shown more than they lost
        assert eq(t.holdable + t.intermediary, holds + sum(c.rounding_trim.values())), g.case_id
    # the hard world exercises that residue (r9): at least one case has a paisa taken back
    assert any(c.rounding_trim for c in cases)


def _running_min(frames: dict[str, pd.DataFrame], replay: bool) -> pd.Series:
    """Minimum running balance per account; ``replay`` applies the same-second credit-before-debit rule."""
    tx, acc = frames["transactions"], frames["accounts"]
    ts = ts_us(tx["ts"])
    order = np.lexsort((tx["txn_id"].to_numpy(), ts))
    if replay:
        ids = pd.Index(acc["account_id"])
        frm = ids.get_indexer(tx["from_account"].to_numpy()[order])
        to = ids.get_indexer(tx["to_account"].to_numpy()[order])
        perm = replay_order(ts[order], frm, to)
        if perm is not None:
            order = order[perm]
    rank = np.empty(len(tx), dtype=np.int64)
    rank[order] = np.arange(len(tx))
    debit = pd.DataFrame({"acct": tx["from_account"], "rank": rank, "v": -tx["amount"]})
    credit = pd.DataFrame({"acct": tx["to_account"], "rank": rank, "v": tx["amount"]})
    e = pd.concat([debit, credit]).dropna(subset=["acct"]).sort_values(["acct", "rank"], kind="mergesort")
    e["run"] = e.groupby("acct")["v"].cumsum()
    low = e.groupby("acct")["run"].min()
    opening = dict(zip(acc["account_id"], acc["opening_balance"], strict=True))
    return low + low.index.map(opening).to_numpy()


def test_no_negative_balances_in_replay_order(hard_world):
    """No account ever goes negative when same-second credits are applied before the debits they fund. Replaying by
    (ts, txn_id) alone would overdraw the T15 relays: the ordering stress is really present."""
    frames, _ = hard_world
    low = _running_min(frames, replay=True)
    assert (low >= -0.005).all(), low[low < -0.005].head()
    naive = _running_min(frames, replay=False)
    stressed = naive[naive < -0.005]
    assert len(stressed) > 0
    la = frames["labels_accounts"].set_index("account_id")
    assert set(la.loc[stressed.index, "typology"]) == {"T15_INSTANT_RELAY"}


def test_replay_order_unit():
    """Same second: debit X->Z (lower txn_id) and credit Y->X; the credit is replayed first. Untouched otherwise."""
    ts = np.array([5, 5, 5, 9])
    frm = np.array([0, 1, 3, 2])
    to = np.array([2, 0, 4, 1])  # row0 debits 0; row1 credits 0 -> row1 first
    perm = replay_order(ts, frm, to)
    assert perm is not None and perm.tolist() == [1, 0, 2, 3]
    assert replay_order(np.array([5, 5]), np.array([1, 0]), np.array([0, 2])) is None  # already credit-first
    assert replay_order(np.array([1, 2]), np.array([0, 0]), np.array([1, 1])) is None


def test_trace_window_contains_every_tracked_move(hard_world):
    """Tracked non-terminal accounts have no activity between the case window end and world_end, so the reference
    balances (as of world_end) equal the product's balances at the end of the trace window."""
    frames, cases = hard_world
    ref = ReferenceWorld(frames)
    for c in cases:
        for a in c.accounts.values():
            if a.layer == 0 or a.account_type in INTERMEDIARY or a.account_type == "MERCHANT":
                continue
            code = ref.code[a.account_id]
            assert eq(ref.balance_at_us(code, c.window_end_us + 1), a.balance_now), (c.case_id, a.account_id)


# --------------------------------------------------------------------------- confounders are present


def test_hard_typologies_and_roles(hard_golden, hard_world):
    frames, _ = hard_world
    assert {g.typology for g in hard_golden} == set(TYPOLOGIES) | set(HARD_TYPOLOGIES)
    roles = {r for g in hard_golden for r in g.expected_roles.values()}
    assert {Role.MULE, Role.INNOCENT_HOLDER, Role.VICTIM, Role.MERCHANT, Role.INTERMEDIARY} <= roles
    # victim-to-victim: a complainant's account appears on another complaint's trail (never held as a mule's)
    victims_on_trail = [
        (g.case_id, a) for g in hard_golden for a, r in g.expected_roles.items() if r == Role.VICTIM
    ]
    assert victims_on_trail
    for g in hard_golden:
        for a, r in g.expected_roles.items():
            if r == Role.VICTIM:
                assert a not in g.expected_escalation_candidates
    # innocent holders are no longer only T6 sellers / landlords
    la = frames["labels_accounts"]
    inn = set(la.loc[la["role_truth"] == "INNOCENT_HOLDER", "typology"])
    assert {"T6_TRIANGULATION", "T13_SALARY_POOL", "T14_FESTIVAL_SELLER"} <= inn
    disp = {d for g in hard_golden for d in g.expected_dispositions.values()}
    assert {
        Disposition.HOLD_AMOUNT,
        Disposition.HOLD_AMOUNT_NO_ESCALATION,
        Disposition.OUTBOUND_CFCFRMS_REQUEST,
    } <= disp


def test_confounder_features(hard_world):
    """Signals are patchy, amounts are structured just under Rs 50,000 and some mules dwell past a day."""
    frames, cases = hard_world
    sig = frames["external_signals"]
    assert 0.1 < 1 - len(sig) / len(frames["accounts"]) < 0.4  # accounts with no external-signal row
    tx = frames["transactions"]
    just_under = tx["amount"].between(49_000, 49_999.99) & tx["channel"].isin(["IMPS", "UPI"])
    assert just_under.sum() >= 5
    la = frames["labels_accounts"].set_index("account_id")
    dwell = 0
    for c in cases:
        for a in c.accounts.values():
            if a.layer >= 1 and la.loc[a.account_id, "role_truth"] == "MULE" and a.sent:
                first_out = min(e.ts_us for e in c.edges if e.from_account == a.account_id)
                if first_out - a.first_tainted_us > 24 * 3600 * 1_000_000:
                    dwell += 1
    assert dwell >= 3


# --------------------------------------------------------------------------- the harness on the hard world


def test_harness_runs_on_hard_world(hard_dir):
    """Money stays exact on the hard world; the classifier does not saturate."""
    rep = harness.evaluate("hard", world_dir=hard_dir, draft=False)
    m = rep["metrics"]
    assert rep["cases"] >= 40 and rep["config"]["scorer"] == "heuristic-v2"
    assert m["hold_exact_match_rate"] == 1.0 and m["over_hold_inr"] == 0.0 and m["false_freeze_count"] == 0
    assert m["traced_amount_recall"] == 1.0 and m["totals_abs_error_inr"] <= 0.02 * rep["cases"]
    assert m["process_accuracy"] == 1.0
    c = rep["classification"]
    assert c["accounts"] > 150 and c["mule_auc"] is not None
    assert m["disposition_accuracy"] < 0.99 and c["mule_auc"] < 0.99 and c["best_single_feature_auc"] < 0.97
    assert set(rep["per_typology"]) >= set(HARD_TYPOLOGIES)


def test_harness_cli_hard_report_name(hard_dir, tmp_path):
    """Hard worlds write <world>.json (never latest.json); drafts still verify on the hard world."""
    out = tmp_path / "reports"
    rc = harness.main(["--world", "hard", "--world-dir", str(hard_dir), "--out", str(out), "--limit", "3"])
    assert rc == 0
    assert (out / "hard.json").exists() and not (out / "latest.json").exists()
    rep = json.loads((out / "hard.json").read_text(encoding="utf-8"))
    assert rep["cases"] == 3 and rep["metrics"]["draft_verification_pass_rate"] == 1.0
    assert rep["metrics"]["citation_validity"] == 1.0 and "classification" in rep


def test_scorer_specs():
    assert harness.report_name("demo", "heuristic") == "latest"
    assert harness.report_name("demo", "xgb:models/x.json") == "demo-xgb"
    assert harness.report_name("hard-101", "gnn:ckpt") == "hard-101"
    assert harness.make_scorer("heuristic", None).name == "heuristic-v2"
    with pytest.raises(SystemExit):
        harness.make_scorer("bogus", None)
