"""Compare heuristic-v2 vs XGBoost vs GNN on held-out worlds, through the real pipeline.

    uv run python -m editions.amd.gnn.evaluate --gnn editions/amd/gnn/checkpoints/cpu-dev-base \
        --worlds data/generated/heldout-404 data/generated/heldout-505 \
        --secondary-worlds data/generated/heldout-101 data/generated/heldout-202 data/generated/heldout-303

For every golden case of every world and every scorer, ``wapsi.pipeline.run.process_case`` runs trace -> classify
(scorer plugged in through ``scorer=``) -> hold plan -> plan verification (drafting off; drafts do not depend on the
scorer beyond roles). Metrics follow ``evals.harness``: role precision / recall (MULE, INNOCENT_HOLDER) over the
golden ``expected_roles``, disposition accuracy, hold exact-match, over-hold; plus mule F1 and the scorer's ROC-AUC on
the accounts the scorer decides (golden role MULE vs INNOCENT_HOLDER / LEGIT).

Scorers: ``heuristic-v2`` (core default), ``xgb-demo`` (the core's XGBoost artefact trained on the demo world),
``xgb-same-train`` (XGBoost retrained here on exactly the GNN's training worlds, for a like-for-like comparison) and
``gnn``. ``--ablate-external`` also runs every scorer with the third-party signal columns (``fri_risk``,
``mulehunter_score``, ``ncrp_prior_reports``) zeroed, using a GNN / XGBoost trained without them
(``--gnn-ablated``): how much does each model lean on those synthetic oracle-like scores?

Anti-overfitting: the GNN's test worlds must not appear in its checkpoint's train / val worlds (checked). Numbers are
written with an explicit label; CPU dev numbers are NOT the MI300X results.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from wapsi.classify.scoring import HeuristicScorer, XgbScorer
from wapsi.config import WapsiConfig
from wapsi.models import Disposition, GoldenCase, Role

EXTERNAL_SIGNALS = ("fri_risk", "mulehunter_score", "ncrp_prior_reports")
DEFAULT_XGB = Path("evals/reports/classify-xgb-demo-newfeat.json")
RESULTS = Path("editions/amd/gnn/results")


class AblatedScorer:
    """Wraps a scorer and zeroes some feature columns before it sees them (signal-availability ablation)."""

    def __init__(self, inner, columns: Sequence[str], suffix: str = "-noext") -> None:
        self.inner = inner
        self.columns = tuple(columns)
        self.name = f"{inner.name}{suffix}"

    def _strip(self, features: pd.DataFrame) -> pd.DataFrame:
        f = features.copy()
        for c in self.columns:
            if c in f:
                f[c] = 0.0
        return f

    def score(self, features: pd.DataFrame) -> pd.Series:
        return self.inner.score(self._strip(features))

    def contributions(self, features: pd.DataFrame) -> pd.DataFrame:
        return self.inner.contributions(self._strip(features))


def golden_cases(world: Path) -> list[GoldenCase]:
    for p in (world / "golden" / "cases.jsonl", Path("data/golden") / world.name / "cases.jsonl"):
        if p.exists():
            return [
                GoldenCase.model_validate_json(x)
                for x in p.read_text(encoding="utf-8").splitlines()
                if x.strip()
            ]
    raise SystemExit(f"no golden cases for {world}")


def train_xgb_on(worlds: Sequence[str | Path], drop: Sequence[str] = (), seed: int = 7) -> XgbScorer:
    """XGBoost role model on the same worlds as the GNN (``wapsi.classify.train`` frame per world, concatenated)."""
    from wapsi.adapters.parquet_ledger import ParquetLedger
    from wapsi.classify.train import build_training_frame, train_xgb

    feats, labels = [], []
    for w in worlds:
        f, y = build_training_frame(ParquetLedger(w), w, seed=seed)
        f.index = [f"{Path(w).name}:{a}" for a in f.index]
        y.index = f.index
        feats.append(f)
        labels.append(y)
    x = pd.concat(feats)
    for c in drop:
        x[c] = 0.0
    return train_xgb(x, pd.concat(labels), seed=seed, name="xgb-same-train" + ("-noext" if drop else ""))


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def evaluate_world(world: Path, scorers: dict, ledger, *, limit: int | None = None) -> dict[str, dict]:
    from wapsi.adapters.llm_fake import FakeLLM
    from wapsi.pipeline.run import process_case

    cases = golden_cases(world)[:limit]
    cfg = WapsiConfig()
    llm = FakeLLM()
    out = {}
    for sname, make in scorers.items():
        scorer = make(ledger)
        c: Counter = Counter()
        auc_y, auc_s, t_cls, fails = [], [], [], []
        for g in cases:
            bundle = process_case(
                ledger, g.ncrp_ids, llm=llm, cfg=cfg, scorer=scorer, case_id=g.case_id, draft=False
            )
            timing = next(
                (e.details.get("timings", {}) for e in bundle.audit if e.action == "case_processed"), {}
            )
            t_cls.append(timing.get("classify_s", 0.0))
            for acct, exp in g.expected_roles.items():
                cr = bundle.classifications.get(acct)
                got = cr.role if cr else Role.UNKNOWN
                for r in (Role.MULE, Role.INNOCENT_HOLDER):
                    c[f"{r.value}_tp"] += int(got == r and exp == r)
                    c[f"{r.value}_fp"] += int(got == r and exp != r)
                    c[f"{r.value}_fn"] += int(got != r and exp == r)
                if cr and exp in (Role.MULE, Role.INNOCENT_HOLDER, Role.LEGIT):
                    auc_y.append(int(exp == Role.MULE))
                    auc_s.append(cr.score)
                if got == Role.UNKNOWN and exp in (Role.MULE, Role.INNOCENT_HOLDER):
                    c["to_officer"] += 1
            decisions = {d.account_id: d for d in bundle.plan.decisions}
            for acct, exp in g.expected_dispositions.items():
                got = decisions[acct].disposition if acct in decisions else Disposition.NO_ACTION
                c["disp_n"] += 1
                c["disp_ok"] += int(got == exp)
                if got != exp:
                    fails.append(f"{g.case_id} {acct}: {got.value} (expected {exp.value})")
            for acct in set(g.expected_hold) | {a for a, d in decisions.items() if d.hold_amount > 0}:
                exp_h = g.expected_hold.get(acct, 0.0)
                got_h = decisions[acct].hold_amount if acct in decisions else 0.0
                c["hold_n"] += 1
                c["hold_ok"] += int(abs(exp_h - got_h) <= 0.005)
                c["over_hold_paise"] += int(round(max(0.0, got_h - exp_h) * 100))
            c["str_ok"] += int(bundle.plan.str_ground == g.expected_str_ground)
            c["cases"] += 1
        m = {}
        for r in ("MULE", "INNOCENT_HOLDER"):
            tp, fp, fn = c[f"{r}_tp"], c[f"{r}_fp"], c[f"{r}_fn"]
            p, rec = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
            m[r] = {
                "precision": p,
                "recall": rec,
                "support": tp + fn,
                "f1": round(2 * p * rec / (p + rec), 4) if p and rec else 0.0,
                "tp": tp,
                "fp": fp,
                "fn": fn,
            }
        auc = None
        if 0 < sum(auc_y) < len(auc_y):
            from sklearn.metrics import roc_auc_score

            auc = round(float(roc_auc_score(auc_y, auc_s)), 4)
        out[sname] = {
            "scorer": scorer.name,
            "cases": c["cases"],
            "mule": m["MULE"],
            "innocent_holder": m["INNOCENT_HOLDER"],
            "scorer_auc_mule_vs_legit": auc,
            "sent_to_officer_unknown": c["to_officer"],
            "disposition_accuracy": _ratio(c["disp_ok"], c["disp_n"]),
            "disposition_n": c["disp_n"],
            "hold_exact_match_rate": _ratio(c["hold_ok"], c["hold_n"]),
            "over_hold_inr": c["over_hold_paise"] / 100,
            "str_ground_accuracy": _ratio(c["str_ok"], c["cases"]),
            "mean_classify_s": round(float(np.mean(t_cls)), 4) if t_cls else None,
            "disposition_failures": fails[:20],
        }
    return out


def pooled(per_world: dict[str, dict[str, dict]]) -> dict[str, dict]:
    """Micro-averages over worlds (recomputed from supports)."""
    out: dict[str, dict] = {}
    names = next(iter(per_world.values())).keys() if per_world else []
    for s in names:
        rows = [w[s] for w in per_world.values()]
        agg = {}
        for key in ("mule", "innocent_holder"):
            tp, fp, fn = (sum(r[key][k] for r in rows) for k in ("tp", "fp", "fn"))
            p, rec = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
            agg[key] = {
                "precision": p,
                "recall": rec,
                "support": tp + fn,
                "f1": round(2 * p * rec / (p + rec), 4) if p and rec else 0.0,
                "tp": tp,
                "fp": fp,
                "fn": fn,
            }
        dn = sum(r["disposition_n"] for r in rows)
        agg["disposition_accuracy"] = _ratio(
            sum(r["disposition_accuracy"] * r["disposition_n"] for r in rows), dn
        )
        agg["disposition_n"] = dn
        agg["cases"] = sum(r["cases"] for r in rows)
        agg["sent_to_officer_unknown"] = sum(r["sent_to_officer_unknown"] for r in rows)
        agg["over_hold_inr"] = round(sum(r["over_hold_inr"] for r in rows), 2)
        out[s] = agg
    return out


def table(pool: dict[str, dict]) -> str:
    lines = [
        "| scorer | mule P | mule R | mule F1 | innocent P | innocent R | disposition acc | to officer |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s, r in pool.items():
        lines.append(
            f"| {s} | {r['mule']['precision']} | {r['mule']['recall']} | {r['mule']['f1']} | "
            f"{r['innocent_holder']['precision']} | {r['innocent_holder']['recall']} | "
            f"{r['disposition_accuracy']} | {r['sent_to_officer_unknown']} |"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m editions.amd.gnn.evaluate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--gnn", required=True, help="GNN checkpoint directory")
    ap.add_argument("--gnn-ablated", default=None, help="GNN checkpoint trained without external signals")
    ap.add_argument(
        "--worlds", nargs="+", default=["data/generated/heldout-404", "data/generated/heldout-505"]
    )
    ap.add_argument("--secondary-worlds", nargs="*", default=[])
    ap.add_argument("--xgb", default=str(DEFAULT_XGB))
    ap.add_argument("--no-xgb-same-train", action="store_true")
    ap.add_argument("--ablate-external", action="store_true")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--limit", type=int, default=None, help="cases per world (smoke)")
    ap.add_argument("--label", default="CPU dev numbers (development laptop, CPU only) - NOT MI300X results")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    import torch

    from editions.amd.gnn.model import configure_cpu_threads, resolve_device
    from editions.amd.gnn.scorer import GnnScorer
    from wapsi.adapters.parquet_ledger import ParquetLedger

    device = resolve_device(args.device)
    configure_cpu_threads(device)
    ckpt_cfg = json.loads((Path(args.gnn) / "config.json").read_text(encoding="utf-8"))
    used = {Path(w).resolve() for w in ckpt_cfg["worlds"]["train"] + ckpt_cfg["worlds"]["val"]}
    for w in [*args.worlds, *args.secondary_worlds]:
        if Path(w).resolve() in used:
            print(f"error: {w} was used to train/validate the GNN checkpoint", file=sys.stderr)
            return 2

    scorers: dict = {"heuristic-v2": lambda _l: HeuristicScorer()}
    if args.xgb and Path(args.xgb).exists():
        xgb_demo = XgbScorer.load(args.xgb)
        scorers["xgb-demo"] = lambda _l: xgb_demo
    t0 = time.perf_counter()
    if not args.no_xgb_same_train:
        xgb_same = train_xgb_on(ckpt_cfg["worlds"]["train"])
        scorers["xgb-same-train"] = lambda _l: xgb_same
    scorers["gnn"] = lambda ledger: GnnScorer.load(args.gnn, ledger=ledger, device=str(device))
    if args.ablate_external:
        scorers["heuristic-v2-noext"] = lambda _l: AblatedScorer(HeuristicScorer(), EXTERNAL_SIGNALS)
        if not args.no_xgb_same_train:
            xgb_noext = train_xgb_on(ckpt_cfg["worlds"]["train"], drop=EXTERNAL_SIGNALS)
            scorers["xgb-same-train-noext"] = lambda _l: AblatedScorer(xgb_noext, EXTERNAL_SIGNALS, suffix="")
        if args.gnn_ablated:
            scorers["gnn-noext"] = lambda ledger: AblatedScorer(
                GnnScorer.load(args.gnn_ablated, ledger=ledger, device=str(device)),
                EXTERNAL_SIGNALS,
                suffix="-noext",
            )
    prep_s = time.perf_counter() - t0

    result: dict = {
        "label": args.label,
        "date": datetime.now(UTC).date().isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "torch": torch.__version__,
            "device": str(device),
            "threads": torch.get_num_threads(),
        },
        "gnn_checkpoint": {
            "path": args.gnn,
            "name": ckpt_cfg.get("name"),
            "model": ckpt_cfg.get("model"),
            "graph_config": ckpt_cfg.get("graph_config"),
            "worlds": ckpt_cfg.get("worlds"),
            "drop_features": ckpt_cfg.get("drop_features", []),
        },
        "gnn_ablated_checkpoint": args.gnn_ablated,
        "protocol": (
            "GNN weights fitted on the train worlds, early stopping on the val worlds, test worlds scored "
            "once. xgb-same-train uses the same train worlds. heldout-101..505 existed before this work and "
            "their heuristic / XGBoost reports are in evals/reports, so they are not untouched for the "
            "heuristic's hand-tuning; they were never used to fit or tune the GNN."
        ),
        "scorer_prep_s": round(prep_s, 1),
        "primary": {},
        "secondary": {},
    }
    for group, worlds in (("primary", args.worlds), ("secondary", args.secondary_worlds)):
        per_world = {}
        for w in worlds:
            w = Path(w)
            t = time.perf_counter()
            per_world[w.name] = evaluate_world(w, scorers, ParquetLedger(w), limit=args.limit)
            print(f"{w.name}: {time.perf_counter() - t:.1f}s")
        if per_world:
            pool = pooled(per_world)
            result[group] = {"worlds": per_world, "pooled": pool}
            print(f"\n## {group}: {', '.join(per_world)} ({result['label']})\n{table(pool)}\n")
    out = Path(args.out) if args.out else RESULTS / f"cpu-dev-{result['date']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
