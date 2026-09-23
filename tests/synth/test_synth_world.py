"""Synthetic world: determinism, schema conformance and world invariants (demo size, seed 7)."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wapsi.models import Channel, Role
from wapsi.synth.export import LABEL_TABLES, MODELS, PRODUCT_TABLES, SCHEMAS, load_frames, records
from wapsi.synth.reference import ts_us
from wapsi.synth.rings import TYPOLOGIES
from wapsi.synth.world import ADVERSARIAL_RATE, WorldConfig, generate_world


def _hashes(path: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.glob("*.parquet"))}


def _generate(out: Path, hash_seed: str) -> None:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    subprocess.run(
        [
            sys.executable,
            "-m",
            "wapsi.synth",
            "--size",
            "demo",
            "--seed",
            "7",
            "--out",
            str(out),
            "--golden-dir",
            "",
        ],
        check=True,
        env=env,
        capture_output=True,
    )


def test_determinism_byte_identical(tmp_path, demo_dir):
    """Same seed -> byte-identical Parquet, independent of the string hash seed and of the process."""
    a, b = tmp_path / "a", tmp_path / "b"
    _generate(a, "1")
    _generate(b, "12345")
    ha, hb = _hashes(a), _hashes(b)
    assert set(ha) == {f"{t}.parquet" for t in [*PRODUCT_TABLES, *LABEL_TABLES]}
    assert ha == hb
    assert ha == _hashes(demo_dir)  # in-process generation writes the same bytes
    assert (a / "golden" / "cases.jsonl").read_bytes() == (b / "golden" / "cases.jsonl").read_bytes()


def test_different_seed_differs():
    small = {"n_accounts": 400, "n_rings": 10, "activity": 0.3}
    f1 = generate_world(WorldConfig(size="demo", seed=1, overrides=small))
    f2 = generate_world(WorldConfig(size="demo", seed=2, overrides=small))
    assert not f1["transactions"]["amount"].equals(f2["transactions"]["amount"])
    assert set(f1["labels_complaints"]["typology"]) == set(TYPOLOGIES)


def test_sizes(demo_world):
    frames, cases = demo_world
    assert 2700 <= len(frames["accounts"]) <= 3300
    assert 50_000 <= len(frames["transactions"]) <= 75_000
    assert 38 <= len(frames["complaints"]) <= 55
    assert frames["meta"].set_index("key").loc["n_rings", "value"] == "30"
    assert len(cases) == frames["labels_complaints"]["case_id"].nunique()


def test_schema_conformance(demo_world):
    frames, _ = demo_world
    for table in [*PRODUCT_TABLES, *LABEL_TABLES]:
        df = frames[table]
        assert list(df.columns) == [f.name for f in SCHEMAS[table]], table
        model = MODELS.get(table)
        if model is None or df.empty:
            continue
        sample = df if len(df) <= 400 else df.sample(400, random_state=0)
        for row in records(sample):
            model.model_validate(row)
    la = frames["labels_accounts"]
    assert set(la["role_truth"]) <= {r.value for r in Role}
    assert la["account_id"].tolist() == frames["accounts"]["account_id"].tolist()


def test_loaded_types(demo_dir):
    frames = load_frames(demo_dir)
    tx = frames["transactions"]
    assert str(tx["ts"].dtype) == "datetime64[us, UTC]"
    assert tx["amount"].dtype == np.float64
    assert (tx["amount"].round(2) == tx["amount"]).all()
    assert str(frames["accounts"]["opened_at"].dtype) == "datetime64[us, UTC]"


def test_ids_and_formats(demo_world):
    frames, _ = demo_world
    acc, tx = frames["accounts"], frames["transactions"]
    assert acc["account_id"].is_unique
    assert acc["account_id"].str.fullmatch(r"(HB01|OB02|OB03)-[1-9]\d{9}").all()
    assert (acc["account_id"].str[:4] == acc["bank_id"]).all()
    assert tx["txn_id"].is_unique and tx["utr"].is_unique
    utr, ch = tx["utr"], tx["channel"]
    twelve = ch.isin(["UPI", "IMPS", "POS", "CRYPTO", "INTERNAL"])
    assert utr[twelve].str.fullmatch(r"\d{12}").all()
    assert utr[ch == "ATM"].str.fullmatch(r"ATM\d{9}").all()
    assert utr[ch == "CASH_DEPOSIT"].str.fullmatch(r"CSH\d{9}").all()
    assert utr[ch.isin(["NEFT", "RTGS"])].str.fullmatch(r"(HB01|OB02|OB03)[NR]\d{11}").all()
    comp = frames["complaints"]
    assert comp["ncrp_id"].str.fullmatch(r"\d{14}").all() and comp["ncrp_id"].is_unique
    assert frames["device_links"]["device_id"].str.fullmatch(r"DEV-\d{7}").all()
    gst = frames["gst_registry"]
    assert (
        gst["gstin"].str.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][0-9A-Z]").all() and gst["gstin"].is_unique
    )
    assert set(gst["status"]) >= {"ACTIVE", "CANCELLED"}
    assert set(acc["gstin"].dropna()) <= set(gst["gstin"])
    cust = frames["customers"]
    assert cust["mobile"].str.fullmatch(r"[6-9]\dX{6}\d{2}").all()
    assert cust["pan_masked"].str.fullmatch(r"X{5}\d{4}[A-Z]").all()
    assert set(acc["customer_id"].dropna()) <= set(cust["customer_id"])
    assert acc.loc[acc["account_type"].isin(["NODAL", "POOL", "ESCROW"]), "customer_id"].isna().all()
    assert frames["banks"]["name"].str.endswith("(synthetic)").all()


def test_transaction_shapes(demo_world):
    frames, _ = demo_world
    tx, acc = frames["transactions"], frames["accounts"]
    ch = tx["channel"]
    assert set(ch) <= {c.value for c in Channel}
    exits = ch.isin(["ATM", "CRYPTO"])
    assert tx.loc[exits, "to_account"].isna().all() and tx.loc[exits, "counterparty_label"].notna().all()
    assert tx.loc[ch == "CASH_DEPOSIT", "from_account"].isna().all()
    assert tx.loc[~exits, "to_account"].notna().all()
    assert tx.loc[ch != "CASH_DEPOSIT", "from_account"].notna().all()
    assert (tx["from_account"] != tx["to_account"]).all()
    assert (tx["amount"] > 0).all()
    assert (tx.loc[ch == "UPI", "amount"] <= 100_000).all()
    bank = dict(zip(acc["account_id"], acc["bank_id"], strict=True))
    assert (tx["from_bank_id"].fillna("-") == tx["from_account"].map(bank).fillna("-")).all()
    assert (tx["to_bank_id"].fillna("-") == tx["to_account"].map(bank).fillna("-")).all()
    meta = frames["meta"].set_index("key")["value"]
    start, end = pd.Timestamp(meta["world_start"]), pd.Timestamp(meta["world_end"])
    assert tx["ts"].min() >= start and tx["ts"].max() < end
    assert tx["ts"].is_monotonic_increasing and tx["txn_id"].is_monotonic_increasing
    opened = tx["from_account"].map(dict(zip(acc["account_id"], acc["opened_at"], strict=True)))
    assert (opened.isna() | (tx["ts"] > opened)).all()
    salary = tx["remark"].str.startswith("SALARY ")
    assert salary.sum() > 500 and tx.loc[salary, "channel"].isin(["NEFT", "RTGS"]).all()
    inv = frames["invoices"]
    assert set(inv["txn_id"]) <= set(tx["txn_id"])
    assert (inv["order_status"].value_counts(normalize=True)["DELIVERED"]) > 0.8
    assert inv.loc[inv["order_status"] != "DELIVERED", "delivered_at"].isna().all()


def _running_min(frames: dict[str, pd.DataFrame]) -> pd.Series:
    """Minimum running balance per account, replaying transactions in (ts, txn_id) order."""
    tx, acc = frames["transactions"], frames["accounts"]
    order = np.lexsort((tx["txn_id"].to_numpy(), ts_us(tx["ts"])))
    rank = np.empty(len(tx), dtype=np.int64)
    rank[order] = np.arange(len(tx))
    debit = pd.DataFrame({"acct": tx["from_account"], "rank": rank, "v": -tx["amount"]})
    credit = pd.DataFrame({"acct": tx["to_account"], "rank": rank, "v": tx["amount"]})
    e = pd.concat([debit, credit]).dropna(subset=["acct"]).sort_values(["acct", "rank"], kind="mergesort")
    e["run"] = e.groupby("acct")["v"].cumsum()
    low = e.groupby("acct")["run"].min()
    opening = dict(zip(acc["account_id"], acc["opening_balance"], strict=True))
    return low + low.index.map(opening).to_numpy()


def test_no_negative_balances(demo_world):
    """Background accounts never go negative; ring accounts (mules included) are balance-checked too."""
    frames, _ = demo_world
    low = _running_min(frames)
    roles = dict(
        zip(frames["labels_accounts"]["account_id"], frames["labels_accounts"]["role_truth"], strict=True)
    )
    non_mule = low[[roles[a] != "MULE" for a in low.index]]
    assert (non_mule >= -0.005).all(), non_mule[non_mule < 0].head()
    assert (low >= -0.005).all()


def test_every_typology_and_labels(demo_world):
    frames, cases = demo_world
    lc = frames["labels_complaints"]
    assert set(lc["typology"]) == set(TYPOLOGIES)
    assert lc["ncrp_id"].tolist() == frames["complaints"]["ncrp_id"].tolist()
    la = frames["labels_accounts"].set_index("account_id")
    comp = frames["complaints"].set_index("ncrp_id")
    assert (la.loc[comp["victim_account"], "role_truth"] == "VICTIM").all()
    t7 = lc[lc["typology"] == "T7_MULTI_VICTIM"].groupby("case_id").size()
    assert (t7 >= 2).all()
    mules = la[la["role_truth"] == "MULE"]
    assert mules["ring_id"].notna().all() and mules["typology"].notna().all()
    assert set(la.loc[la["role_truth"] == "INNOCENT_HOLDER", "typology"]) == {"T6_TRIANGULATION"}
    types = frames["accounts"].set_index("account_id")["account_type"]
    assert (la.loc[types[types == "MERCHANT"].index, "role_truth"] == "MERCHANT").all()
    assert (
        la.loc[types[types.isin(["NODAL", "POOL", "ESCROW"])].index, "role_truth"] == "INTERMEDIARY"
    ).all()


def test_adversarial_ratio(demo_world):
    frames, _ = demo_world
    lc, comp = frames["labels_complaints"], frames["complaints"]
    n = len(lc)
    assert lc["is_adversarial"].sum() == max(1, round(ADVERSARIAL_RATE * n))
    pattern = re.compile(
        r"ignore|AI|assistant|instruction|SYSTEM|OVERRIDE|Bot ko|release|unfreeze|lift", re.I
    )
    adv = comp["narrative_text"][lc["is_adversarial"].to_numpy()]
    assert adv.str.contains(pattern).all()
    clean = comp["narrative_text"][~lc["is_adversarial"].to_numpy()]
    assert not clean.str.contains(r"SYSTEM|AI system|assistant|OVERRIDE|Bot ko", regex=True).any()
    assert set(comp["language"]) == {"en", "hi-en"}
    assert set(comp["source"]) == {"1930", "NCRP"}
    assert comp["fir_number"].notna().any()


def test_complaints_match_transactions(demo_world):
    frames, _ = demo_world
    ct = frames["complaint_txns"].merge(frames["transactions"], on="txn_id", how="left")
    assert ct["amount"].notna().all()
    comp = frames["complaints"].set_index("ncrp_id")
    claimed = ct.groupby("ncrp_id")["amount"].sum()
    assert np.allclose(claimed.loc[comp.index], comp["amount_claimed"])
    assert (ct["from_account"] == ct["ncrp_id"].map(comp["victim_account"])).all()
    first_ts = ct.groupby("ncrp_id")["ts"].max()
    lag = (comp["reported_at"] - first_ts.loc[comp.index]).dt.total_seconds() / 3600
    assert ((lag >= 0.5) & (lag <= 6.0)).all()
    homes = frames["accounts"].set_index("account_id")["bank_id"]
    assert (ct["to_account"].map(homes) == "HB01").all()  # first-layer accounts are at the home bank


def test_reference_clean(demo_world):
    """Ground truth is clean: no edges under min_edge_amount, no depth truncation, taint only on ring accounts
    plus merchants / pools, and every flow row is unique."""
    frames, cases = demo_world
    assert all(not c.warnings for c in cases)
    assert max(s.layer for c in cases for s in c.accounts.values()) <= 5
    flows = frames["labels_flows"]
    assert not flows.duplicated(["txn_id", "ncrp_id"]).any()
    assert (flows["tainted_amount_truth"] > 0).all()
    la = frames["labels_accounts"].set_index("account_id")
    touched = {a for c in cases for a, s in c.accounts.items() if s.layer >= 1}
    assert set(la.loc[sorted(touched), "role_truth"]) <= {
        "MULE",
        "INNOCENT_HOLDER",
        "MERCHANT",
        "INTERMEDIARY",
    }
    ncrps = set(frames["complaints"]["ncrp_id"])
    assert set(flows["ncrp_id"]) == ncrps


@pytest.mark.parametrize("size", ["showcase"])
def test_showcase_world_frames(size, showcase_world):
    frames, cases = showcase_world
    assert len(frames["transactions"]) == 21 and len(cases) == 2
    assert frames["labels_complaints"]["case_id"].nunique() == 2
