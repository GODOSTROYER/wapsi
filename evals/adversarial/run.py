"""Evaluate ``RulesGuard`` on the independent adversarial corpus.

    uv run python -m evals.adversarial.run                # metrics on dev and test, dev errors printed
    uv run python -m evals.adversarial.run --show-test    # also print test errors (for reporting, not tuning)
    uv run python -m evals.adversarial.run --out PATH     # default evals/reports/guard-latest.json

The split is deterministic and stratified by (label, style): within each stratum rows are ordered by
sha256(id) and the first 60% go to ``dev``. Tune only on dev; report test.

``corpus.jsonl`` was written before the rules were changed, but by the same author, so its test split guards
against tuning on errors, not against author bias. ``holdout_blind.jsonl`` (if present) was written by a
separate agent that never saw the guard or the corpus; it is scored as a whole (``blind_holdout``) and its
errors are printed only with ``--show-test``.

If ``data/generated/demo`` exists, the demo world's complaints are screened too as a sanity check (not a
tuning target): flagged adversarial and benign counts go into ``demo_sanity``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from wapsi.verify.injection import RulesGuard

ROOT = Path(__file__).resolve().parents[2]
CORPUS = Path(__file__).with_name("corpus.jsonl")
HOLDOUT = Path(__file__).with_name("holdout_blind.jsonl")
DEFAULT_OUT = ROOT / "evals" / "reports" / "guard-latest.json"
DEV_SHARE = 0.6


def load(path: Path = CORPUS) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def split(rows: list[dict], dev_share: float = DEV_SHARE) -> dict[str, list[dict]]:
    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        strata[(r["label"], r["style"])].append(r)
    out: dict[str, list[dict]] = {"dev": [], "test": []}
    for key in sorted(strata):
        group = sorted(strata[key], key=lambda r: hashlib.sha256(r["id"].encode()).hexdigest())
        n_dev = round(len(group) * dev_share)
        out["dev"] += group[:n_dev]
        out["test"] += group[n_dev:]
    return out


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def evaluate(rows: list[dict], guard: RulesGuard) -> dict:
    tp = fp = fn = tn = 0
    by_style: dict[str, Counter] = defaultdict(Counter)
    cats: Counter = Counter()
    errors = []
    t0 = time.perf_counter()
    for r in rows:
        res = guard.screen(r["text"])
        adv = r["label"] == "adversarial"
        s = by_style[f"{r['label']}:{r['style']}"]
        s["n"] += 1
        s["flagged"] += int(res.flagged)
        cats.update(res.categories)
        if adv and res.flagged:
            tp += 1
        elif adv:
            fn += 1
            errors.append({"id": r["id"], "kind": "miss", "style": r["style"], "text": r["text"]})
        elif res.flagged:
            fp += 1
            errors.append(
                {
                    "id": r["id"],
                    "kind": "false_positive",
                    "style": r["style"],
                    "text": r["text"],
                    "categories": res.categories,
                    "spans": res.quoted_spans,
                }
            )
        else:
            tn += 1
    elapsed = time.perf_counter() - t0
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f1 = round(2 * precision * recall / (precision + recall), 4) if precision and recall else 0.0
    return {
        "n": len(rows),
        "n_adversarial": tp + fn,
        "n_benign": fp + tn,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positive_rate": _ratio(fp, fp + tn),
        "ms_per_text": round(1000 * elapsed / max(1, len(rows)), 3),
        "flag_rate_by_style": {k: _ratio(v["flagged"], v["n"]) for k, v in sorted(by_style.items())},
        "categories": dict(cats.most_common()),
        "errors": errors,
    }


def demo_sanity(guard: RulesGuard) -> dict | None:
    world = ROOT / "data" / "generated" / "demo"
    golden = ROOT / "data" / "golden" / "demo" / "cases.jsonl"
    if not (world.exists() and golden.exists()):
        return None
    import pandas as pd

    from wapsi.adapters.parquet_ledger import ParquetLedger

    ledger = ParquetLedger(str(world))
    cases = [json.loads(line) for line in golden.read_text(encoding="utf-8").splitlines() if line.strip()]
    adv = {n for c in cases if c.get("is_adversarial") for n in c["ncrp_ids"]}
    ids = pd.read_parquet(world / "complaints.parquet")["ncrp_id"].tolist()
    flagged = {n for n in ids if guard.screen(ledger.complaint(n).narrative_text).flagged}
    return {
        "adversarial": len(adv),
        "adversarial_flagged": len(adv & flagged),
        "benign": len(set(ids) - adv),
        "benign_flagged": len(flagged - adv),
        "missed": sorted(adv - flagged),
    }


def _summary(name: str, m: dict) -> str:
    return (
        f"{name:5s} n={m['n']:3d} (adv {m['n_adversarial']}, benign {m['n_benign']})  "
        f"P={m['precision']}  R={m['recall']}  F1={m['f1']}  FPR={m['false_positive_rate']}  "
        f"tp={m['tp']} fp={m['fp']} fn={m['fn']}  {m['ms_per_text']} ms/text"
    )


def _print_errors(name: str, m: dict) -> None:
    for e in m["errors"]:
        extra = f"  {e.get('categories')} {e.get('spans')}" if e["kind"] == "false_positive" else ""
        print(f"  [{name}] {e['kind']:14s} {e['id']} ({e['style']}): {e['text']!r}{extra}")


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, default=CORPUS)
    ap.add_argument("--holdout", type=Path, default=HOLDOUT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--show-test", action="store_true", help="print test-split errors (reporting only)")
    ap.add_argument("--no-demo", action="store_true", help="skip the demo-world sanity check")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    rows = load(args.corpus)
    parts = split(rows)
    guard = RulesGuard()
    report = {
        "corpus": str(args.corpus.relative_to(ROOT) if args.corpus.is_relative_to(ROOT) else args.corpus),
        "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        "n": len(rows),
        "n_adversarial": sum(r["label"] == "adversarial" for r in rows),
        "n_benign": sum(r["label"] == "benign" for r in rows),
        "styles": dict(Counter(f"{r['label']}:{r['style']}" for r in rows).most_common()),
        "languages": dict(Counter(f"{r['label']}:{r['language']}" for r in rows).most_common()),
        "split": {"method": "stratified by (label, style), sha256(id) order", "dev_share": DEV_SHARE},
        "dev": evaluate(parts["dev"], guard),
        "test": evaluate(parts["test"], guard),
        "blind_holdout": None,
        "demo_sanity": None if args.no_demo else demo_sanity(guard),
    }
    if args.holdout.exists():
        holdout = load(args.holdout)
        report["blind_holdout"] = {
            "file": args.holdout.name,
            "sha256": hashlib.sha256(args.holdout.read_bytes()).hexdigest(),
            "styles": dict(Counter(f"{r['label']}:{r['style']}" for r in holdout).most_common()),
            **evaluate(holdout, guard),
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"corpus: {report['n_adversarial']} adversarial, {report['n_benign']} benign")
    print(_summary("dev", report["dev"]))
    print(_summary("test", report["test"]))
    if report["blind_holdout"]:
        print(_summary("blind", report["blind_holdout"]))
    _print_errors("dev", report["dev"])
    if args.show_test:
        _print_errors("test", report["test"])
        if report["blind_holdout"]:
            _print_errors("blind", report["blind_holdout"])
    if report["demo_sanity"]:
        d = report["demo_sanity"]
        print(
            f"demo sanity: adversarial flagged {d['adversarial_flagged']}/{d['adversarial']}, "
            f"benign flagged {d['benign_flagged']}/{d['benign']}"
        )
    print(f"wrote {args.out}")
    return report


if __name__ == "__main__":
    main()
