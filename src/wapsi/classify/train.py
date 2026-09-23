"""Train the XGBoost role baseline on a generated world.

    uv run python -m wapsi.classify.train --world data/generated/demo --out models/xgb_demo.json

Training set: every account that received tainted money (``labels_flows``) plus a seeded random sample of other
non-victim accounts. Labels come from ``labels_accounts.role_truth`` (ground truth; never read by the product).
Features mirror ``classify_trace``: ``as_of`` = start of the earliest complaint whose money reached the account,
burst window from the account's first tainted receipt; normal accounts get a random ``as_of`` inside the world
and a burst window starting there. Seeds for ``shared_device_with_seed_accounts`` are all tainted accounts.

Writes the model JSON and ``<out stem>.metrics.json`` (per-class precision / recall / F1 on a stratified hold-out).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from wapsi.classify.features import FEATURE_COLUMNS, account_features
from wapsi.classify.scoring import XgbScorer
from wapsi.config import ClassifyConfig
from wapsi.models import Role
from wapsi.ports import Ledger

REQUIRED_TABLES = ("labels_accounts", "labels_flows", "transactions")
DEFAULT_PARAMS = {
    "objective": "multi:softprob",
    "eval_metric": "mlogloss",
    "max_depth": 4,
    "eta": 0.1,
    "subsample": 0.9,
    "colsample_bytree": 0.9,
    "min_child_weight": 1.0,
    "tree_method": "hist",
    "nthread": 1,  # deterministic
}


def train_xgb(
    features_df: pd.DataFrame,
    labels: pd.Series | Sequence[str],
    *,
    num_boost_round: int = 200,
    params: dict | None = None,
    seed: int = 7,
    name: str = "xgb",
    balance: bool = True,
) -> XgbScorer:
    """Fit a multi-class role model. ``labels`` align with ``features_df`` rows (Role values or strings)."""
    import xgboost as xgb

    y = pd.Series(
        list(labels) if not isinstance(labels, pd.Series) else labels.to_numpy(), index=features_df.index
    )
    y = y.map(lambda v: v.value if isinstance(v, Role) else str(v))
    classes = sorted(y.unique())
    if len(classes) < 2:
        raise ValueError(f"need at least two classes to train, got {classes}")
    x = features_df.reindex(columns=list(FEATURE_COLUMNS)).astype(float).fillna(0.0)
    y_idx = y.map({c: i for i, c in enumerate(classes)}).to_numpy()
    weights = None
    if balance:
        counts = y.value_counts()
        weights = y.map(lambda c: len(y) / (len(classes) * counts[c])).to_numpy(dtype=float)
    dtrain = xgb.DMatrix(x.to_numpy(), label=y_idx, weight=weights, feature_names=list(FEATURE_COLUMNS))
    p = {**DEFAULT_PARAMS, **(params or {}), "num_class": len(classes), "seed": seed}
    booster = xgb.train(p, dtrain, num_boost_round=num_boost_round)
    return XgbScorer(booster, classes, name=name)


def evaluate(scorer: XgbScorer, features_df: pd.DataFrame, labels: pd.Series) -> dict:
    """Per-class precision / recall / F1 / support plus the confusion matrix."""
    from sklearn.metrics import classification_report, confusion_matrix

    y_true = [v.value if isinstance(v, Role) else str(v) for v in labels]
    y_pred = scorer.predict_role(features_df).astype(str).tolist()
    classes = sorted(set(y_true) | set(y_pred))
    report = classification_report(y_true, y_pred, labels=classes, output_dict=True, zero_division=0)
    per_class = {
        c: {k: round(float(report[c][k]), 4) for k in ("precision", "recall", "f1-score", "support")}
        for c in classes
    }
    out = {
        "classes": classes,
        "per_class": per_class,
        "accuracy": round(float(np.mean(np.array(y_true) == np.array(y_pred))), 4) if y_true else 0.0,
        "macro_f1": round(float(report["macro avg"]["f1-score"]), 4),
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels=classes).tolist(),
        "n": len(y_true),
    }
    if Role.MULE.value in classes and len(set(y_true)) > 1:
        from sklearn.metrics import roc_auc_score

        is_mule = np.array([v == Role.MULE.value for v in y_true])
        if 0 < is_mule.sum() < len(is_mule):
            out["mule_auc"] = round(float(roc_auc_score(is_mule, scorer.score(features_df).to_numpy())), 4)
    return out


def format_report(metrics: dict) -> str:
    lines = [f"{'class':<18}{'precision':>10}{'recall':>10}{'f1':>10}{'support':>10}"]
    for c in metrics["classes"]:
        m = metrics["per_class"][c]
        lines.append(
            f"{c:<18}{m['precision']:>10.3f}{m['recall']:>10.3f}{m['f1-score']:>10.3f}{int(m['support']):>10d}"
        )
    lines.append(f"accuracy {metrics['accuracy']:.3f}  macro-F1 {metrics['macro_f1']:.3f}  n={metrics['n']}")
    if "mule_auc" in metrics:
        lines.append(f"MULE vs rest ROC-AUC {metrics['mule_auc']:.3f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- world -> training frame


def _read(world: Path, table: str, columns: list[str] | None = None) -> pd.DataFrame:
    return pd.read_parquet(world / f"{table}.parquet", columns=columns)


def _utc(s: pd.Series) -> pd.Series:
    s = pd.to_datetime(s)
    return s.dt.tz_localize("UTC") if s.dt.tz is None else s.dt.tz_convert("UTC")


def build_training_frame(
    ledger: Ledger,
    world: str | Path,
    cfg: ClassifyConfig | None = None,
    *,
    normal_ratio: float = 3.0,
    max_normal: int = 5000,
    seed: int = 7,
) -> tuple[pd.DataFrame, pd.Series]:
    """(features, labels) for tainted accounts plus a sample of normal non-victim accounts."""
    world = Path(world)
    cfg = cfg or ClassifyConfig()
    labels = _read(world, "labels_accounts", ["account_id", "role_truth"]).drop_duplicates("account_id")
    role_of = labels.set_index("account_id")["role_truth"].astype(str)
    flows = _read(world, "labels_flows", ["txn_id", "ncrp_id", "tainted_amount_truth"])
    txns = _read(world, "transactions", ["txn_id", "ts", "to_account"])
    txns["ts"] = _utc(txns["ts"])

    tainted = flows[flows["tainted_amount_truth"] > 0].merge(txns, on="txn_id", how="inner")
    tainted = tainted[tainted["to_account"].notna()]
    ncrp_start = tainted.groupby("ncrp_id")["ts"].min()
    tainted = tainted.assign(case_start=tainted["ncrp_id"].map(ncrp_start))
    per_acct = tainted.groupby("to_account").agg(as_of=("case_start", "min"), burst=("ts", "min"))
    per_acct = per_acct[per_acct.index.isin(role_of.index)]
    per_acct = per_acct[role_of.reindex(per_acct.index) != Role.VICTIM.value]

    rng = np.random.default_rng(seed)
    pool = role_of[~role_of.index.isin(per_acct.index) & (role_of != Role.VICTIM.value)].index.to_numpy()
    n_normal = int(min(len(pool), max_normal, max(1, round(normal_ratio * len(per_acct)))))
    normal_ids = (
        sorted(rng.choice(pool, size=n_normal, replace=False).tolist()) if n_normal and len(pool) else []
    )
    lo, hi = txns["ts"].quantile(0.1), txns["ts"].quantile(0.9)
    span = max((hi - lo).total_seconds(), 0.0)
    offsets = pd.to_timedelta(np.floor(rng.uniform(0.0, span, size=len(normal_ids))), unit="s")
    normal_times = pd.Series(lo.floor("s") + offsets, index=normal_ids)

    def py(ts: pd.Timestamp):
        return ts.floor("us").to_pydatetime()

    as_of = {a: py(ts) for a, ts in per_acct["as_of"].items()}
    burst = {a: py(ts) for a, ts in per_acct["burst"].items()}
    for a, ts in normal_times.items():
        as_of[a] = burst[a] = py(ts)
    ids = list(per_acct.index) + normal_ids
    if not ids:
        raise ValueError(f"no training accounts found in {world}")
    feats = account_features(ledger, ids, as_of, cfg, seed_accounts=set(per_acct.index), burst_start=burst)
    return feats, role_of.reindex(feats.index).rename("role_truth")


def _split(y: pd.Series, test_size: float, seed: int) -> tuple[pd.Index, pd.Index]:
    from sklearn.model_selection import train_test_split

    counts = y.value_counts()
    stratify = y if counts.min() >= 2 and len(y) * test_size >= len(counts) else None
    tr, te = train_test_split(y.index.to_numpy(), test_size=test_size, random_state=seed, stratify=stratify)
    return pd.Index(tr), pd.Index(te)


def main(argv: Sequence[str] | None = None, ledger_factory: Callable[[Path], Ledger] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m wapsi.classify.train",
        description="Train the XGBoost role baseline on a generated world.",
    )
    ap.add_argument("--world", required=True, help="generated world directory (Parquet tables)")
    ap.add_argument("--out", required=True, help="model path, e.g. models/xgb_demo.json")
    ap.add_argument(
        "--normal-ratio", type=float, default=3.0, help="normal accounts sampled per tainted account"
    )
    ap.add_argument("--max-normal", type=int, default=5000)
    ap.add_argument("--rounds", type=int, default=200)
    ap.add_argument("--test-size", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    world, out = Path(args.world), Path(args.out)
    missing = [t for t in REQUIRED_TABLES if not (world / f"{t}.parquet").exists()]
    if not world.is_dir() or missing:
        what = "directory not found" if not world.is_dir() else f"missing tables: {', '.join(missing)}"
        print(
            f"error: world {world} is not usable ({what}). Generate it first:\n"
            f"  uv run python -m wapsi.synth --size demo --seed 7 --out {world}",
            file=sys.stderr,
        )
        return 2
    if ledger_factory is None:
        try:
            from wapsi.adapters.parquet_ledger import ParquetLedger
        except ImportError as exc:
            print(f"error: wapsi.adapters.parquet_ledger is unavailable ({exc})", file=sys.stderr)
            return 2
        ledger_factory = ParquetLedger
    try:
        import sklearn  # noqa: F401
        import xgboost  # noqa: F401
    except ImportError as exc:
        print(f"error: training needs the 'ml' extra (uv sync --extra ml): {exc}", file=sys.stderr)
        return 2

    ledger = ledger_factory(world)
    feats, labels = build_training_frame(
        ledger, world, normal_ratio=args.normal_ratio, max_normal=args.max_normal, seed=args.seed
    )
    print(f"training frame: {len(feats)} accounts; classes: {labels.value_counts().to_dict()}")
    tr, te = _split(labels, args.test_size, args.seed)
    scorer = train_xgb(
        feats.loc[tr], labels.loc[tr], num_boost_round=args.rounds, seed=args.seed, name=f"xgb:{out.stem}"
    )
    metrics = evaluate(scorer, feats.loc[te], labels.loc[te])
    print(format_report(metrics))
    scorer.save(out)
    metrics_path = out.with_name(f"{out.stem}.metrics.json")
    payload = {
        "model": str(out),
        "name": scorer.name,
        "world": str(world),
        "seed": args.seed,
        "rounds": args.rounds,
        "n_train": len(tr),
        "n_test": len(te),
        "train_class_counts": labels.loc[tr].value_counts().to_dict(),
        "features": list(FEATURE_COLUMNS),
        "test": metrics,
    }
    metrics_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out} and {metrics_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
