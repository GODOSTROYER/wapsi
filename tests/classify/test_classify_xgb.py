import json
import math
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("xgboost")
pytest.importorskip("sklearn")

from wapsi.classify import XgbScorer, classify_trace  # noqa: E402
from wapsi.classify.features import FEATURE_COLUMNS  # noqa: E402
from wapsi.classify.train import build_training_frame, evaluate, format_report, main, train_xgb  # noqa: E402
from wapsi.config import ClassifyConfig  # noqa: E402
from wapsi.fixtures import showcase as sc  # noqa: E402
from wapsi.fixtures.bundles import showcase_bundle  # noqa: E402
from wapsi.models import Role  # noqa: E402


def synthetic_frame(n_per_class: int = 60, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    rows, labels = [], []

    def base():
        return dict.fromkeys(FEATURE_COLUMNS, 0.0)

    for _ in range(n_per_class):
        r = base()
        r.update(age_days=rng.uniform(1, 40), pass_through_ratio_24h=rng.uniform(0.7, 1.0),
                 median_forward_minutes=rng.uniform(2, 90), mulehunter_score=rng.uniform(0.5, 1.0),
                 kyc_min=float(rng.random() < 0.7), in_amount_24h=rng.uniform(2e4, 5e5),
                 atm_share_24h=rng.uniform(0, 0.6), device_shared_count=float(rng.integers(0, 4)))  # fmt: skip
        rows.append(r)
        labels.append(Role.MULE)
    for _ in range(n_per_class):
        r = base()
        r.update(age_days=rng.uniform(400, 4000), pass_through_ratio_24h=rng.uniform(0, 0.2),
                 median_forward_minutes=10080.0, mulehunter_score=rng.uniform(0, 0.2),
                 recurring_rent_like_90d=float(rng.integers(0, 4)), history_days=rng.uniform(60, 180),
                 in_amount_24h=rng.uniform(1e3, 3e4))  # fmt: skip
        rows.append(r)
        labels.append(Role.INNOCENT_HOLDER)
    for _ in range(n_per_class):
        r = base()
        r.update(age_days=rng.uniform(300, 3000), is_merchant=1.0, gst_active=float(rng.random() < 0.9),
                 merchant_delivered_share=rng.uniform(0.6, 1.0), n_distinct_payers_7d=float(rng.integers(5, 60)),
                 history_days=180.0, mulehunter_score=rng.uniform(0, 0.3))  # fmt: skip
        rows.append(r)
        labels.append("MERCHANT")
    idx = pd.Index([f"HB01-{i:010d}" for i in range(len(rows))], name="account_id")
    return pd.DataFrame(rows, index=idx)[list(FEATURE_COLUMNS)], pd.Series(labels, index=idx)


@pytest.fixture
def workdir():
    # tempfile instead of tmp_path: the shared pytest-of-<user> temp root is not always accessible on Windows
    with tempfile.TemporaryDirectory(prefix="wapsi-classify-") as d:
        yield Path(d)


@pytest.fixture(scope="module")
def trained():
    x, y = synthetic_frame()
    return train_xgb(x, y, num_boost_round=40, seed=3, name="xgb-test"), x, y


def test_train_and_predict(trained):
    scorer, x, y = trained
    assert scorer.classes == ["INNOCENT_HOLDER", "MERCHANT", "MULE"]
    assert scorer.feature_names == list(FEATURE_COLUMNS)
    proba = scorer.predict_proba(x)
    assert np.allclose(proba.sum(axis=1), 1.0, atol=1e-5)
    acc = (scorer.predict_role(x).to_numpy() == y.map(str).to_numpy()).mean()
    assert acc > 0.95
    score = scorer.score(x)
    assert score.between(0, 1).all()
    assert score[y == Role.MULE].mean() > 0.8 > score[y != Role.MULE].mean()


def test_contributions_sum_to_margin(trained):
    import xgboost as xgb

    scorer, x, _ = trained
    contrib = scorer.contributions(x.head(10))
    assert list(contrib.columns) == list(FEATURE_COLUMNS) + ["bias"]
    margin = scorer.booster.predict(
        xgb.DMatrix(x.head(10).to_numpy(), feature_names=list(FEATURE_COLUMNS)), output_margin=True
    )
    mule = scorer.classes.index("MULE")
    assert np.allclose(contrib.sum(axis=1).to_numpy(), np.asarray(margin)[:, mule], atol=1e-3)


def test_save_load_round_trip(trained, workdir):
    scorer, x, _ = trained
    path = scorer.save(workdir / "models" / "xgb_test.json")
    assert path.exists() and json.loads(path.read_text(encoding="utf-8"))  # valid JSON model
    loaded = XgbScorer.load(path)
    assert loaded.classes == scorer.classes
    assert loaded.name == "xgb-test"
    assert loaded.feature_names == scorer.feature_names
    pd.testing.assert_frame_equal(loaded.predict_proba(x), scorer.predict_proba(x))
    pd.testing.assert_frame_equal(loaded.contributions(x.head(5)), scorer.contributions(x.head(5)))
    # column order / extra columns in the input do not matter
    shuffled = x[list(reversed(x.columns))].assign(extra=1.0)
    pd.testing.assert_series_equal(loaded.score(shuffled), scorer.score(x))


def test_load_rejects_foreign_model(workdir):
    import xgboost as xgb

    d = xgb.DMatrix(np.array([[0.0], [1.0]]), label=np.array([0, 1]))
    booster = xgb.train({"objective": "binary:logistic"}, d, num_boost_round=1)
    path = workdir / "foreign.json"
    booster.save_model(str(path))
    with pytest.raises(ValueError):
        XgbScorer.load(path)


def test_train_needs_two_classes():
    x, _ = synthetic_frame(5)
    with pytest.raises(ValueError):
        train_xgb(x, ["MULE"] * len(x))


def test_evaluate_reports_per_class(trained):
    scorer, x, y = trained
    metrics = evaluate(scorer, x, y)
    assert set(metrics["per_class"]) == {"INNOCENT_HOLDER", "MERCHANT", "MULE"}
    for m in metrics["per_class"].values():
        assert {"precision", "recall", "f1-score", "support"} <= set(m)
    assert metrics["mule_auc"] > 0.95
    text = format_report(metrics)
    assert "MULE" in text and "macro-F1" in text


def test_classify_trace_with_xgb(trained):
    scorer, _, _ = trained
    ledger = sc.showcase_ledger()
    res = classify_trace(ledger, showcase_bundle(1).trace, ClassifyConfig(), model=scorer)
    assert res[sc.K].role == Role.MERCHANT and res[sc.K].model == "rules+xgb-test"
    assert res[sc.V1].role == Role.VICTIM
    for r in res.values():
        assert r.reasons and all("nan" not in t.lower().split() for t in r.reasons)
        assert 0 <= r.score <= 1 and all(math.isfinite(v) for v in r.features.values())
    assert res[sc.A].model == "xgb-test"
    assert res[sc.A].score > res[sc.L].score


# --------------------------------------------------------------------------- CLI


def test_cli_missing_world(workdir, capsys):
    rc = main(["--world", str(workdir / "nope"), "--out", str(workdir / "m.json")])
    assert rc == 2
    assert "wapsi.synth" in capsys.readouterr().err
    (workdir / "partial").mkdir()
    rc = main(["--world", str(workdir / "partial"), "--out", str(workdir / "m.json")])
    assert rc == 2
    assert "labels_accounts" in capsys.readouterr().err


def _write_showcase_world(path):
    """Parquet ground-truth tables for the showcase world (the ledger itself is injected)."""
    path.mkdir(parents=True, exist_ok=True)
    ledger = sc.showcase_ledger()
    txns = ledger.all_transactions()
    pd.DataFrame(
        {"txn_id": [t.txn_id for t in txns], "ts": pd.to_datetime([t.ts for t in txns], utc=True),
         "to_account": [t.to_account for t in txns]}
    ).to_parquet(path / "transactions.parquet")  # fmt: skip
    flows = [(txn_id, sc.N1, amt) for txn_id, (_, amt) in sc.SHOWCASE_1["edges"].items()]
    flows += [("TX-101", sc.N2, 100000.0), ("TX-102", sc.N3, 60000.0)]
    flows += [("TX-103", n, a) for n, a in sc.SHOWCASE_2["edge_taint_by_victim"]["TX-103"].items()]
    pd.DataFrame(flows, columns=["txn_id", "ncrp_id", "tainted_amount_truth"]).to_parquet(
        path / "labels_flows.parquet"
    )
    roles = {**{a: r.value for a, r in sc.SHOWCASE_1["roles"].items()}, sc.M: "MULE"}
    roles.update({v: "VICTIM" for v in (sc.V1, sc.V2, sc.V3)})
    roles.update({a: "LEGIT" for a in [sc.TENANT, *sc.PAYERS]})
    pd.DataFrame(
        {"account_id": list(roles), "role_truth": list(roles.values()), "ring_id": None, "typology": None}
    ).to_parquet(path / "labels_accounts.parquet")
    return ledger


def test_build_training_frame_and_cli(workdir, capsys):
    world_dir = workdir / "world"
    ledger = _write_showcase_world(world_dir)
    feats, labels = build_training_frame(ledger, world_dir, normal_ratio=10.0, seed=1)
    assert set(feats.columns) == set(FEATURE_COLUMNS)
    assert not ({sc.V1, sc.V2, sc.V3} & set(feats.index))  # victims are never training rows
    assert {sc.A, sc.B, sc.C, sc.D, sc.E, sc.K, sc.L, sc.M, sc.P} <= set(feats.index)
    assert set(labels.loc[[sc.TENANT, *sc.PAYERS]]) == {"LEGIT"}
    # tainted rows mirror classify_trace: as_of = case start, burst from the first tainted receipt
    assert feats.loc[sc.A, "in_amount_24h"] == 480000.0
    assert feats.loc[sc.A, "age_days"] == pytest.approx(11.04, abs=0.01)
    assert feats.loc[sc.E, "age_days"] == pytest.approx(8.08, abs=0.01)
    assert feats.loc[sc.A, "shared_device_with_seed_accounts"] == 2.0

    out = workdir / "models" / "xgb_showcase.json"
    rc = main(
        ["--world", str(world_dir), "--out", str(out), "--rounds", "5", "--normal-ratio", "10"],
        ledger_factory=lambda _p: ledger,
    )
    assert rc == 0
    printed = capsys.readouterr().out
    assert "precision" in printed and "wrote" in printed
    metrics = json.loads((workdir / "models" / "xgb_showcase.metrics.json").read_text(encoding="utf-8"))
    assert metrics["name"] == "xgb:xgb_showcase" and metrics["test"]["per_class"]
    loaded = XgbScorer.load(out)
    assert "MULE" in loaded.classes
