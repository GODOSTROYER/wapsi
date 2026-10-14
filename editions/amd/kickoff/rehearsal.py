"""Tiny inputs for the CPU rehearsal of ``run_all.sh`` (``--rehearsal``): no GPU, no downloads, a few seconds each.

    uv run python -m editions.amd.kickoff.rehearsal worlds --out data/generated/kickoff-rehearsal/worlds

Writes four small synthetic worlds (same generator as ``python -m wapsi.synth``, fewer accounts) with their golden
sets: ``kr-train`` / ``kr-val`` / ``kr-test`` for the GNN and ``heldout-kr`` for the LoRA test split, plus a
240-row file in the documented AMLworld CSV layout (``aml-tiny.csv``, random values, no real data) so the AMLworld
pre-training code path runs too. Everything produced from these inputs is a plumbing check, never a result.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# name -> generator seed; seeds are disjoint from every committed or event-week world
WORLDS = {"kr-train": 9101, "kr-val": 9201, "kr-test": 9301, "heldout-kr": 9401}
AML_HEADER = (
    "Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,Amount Paid,"
    "Payment Currency,Payment Format,Is Laundering"
)
AML_FORMATS = ["ACH", "Wire", "Cheque", "Credit Card", "Cash", "Bitcoin", "Reinvestment"]


def tiny_amlworld_csv(path: Path, rows: int = 240, seed: int = 0) -> Path:
    """Random rows in the AMLworld HI-Small column layout (synthetic; the real file is downloaded at the event)."""
    rng = random.Random(seed)
    accts = [f"80{i:05X}0" for i in range(40)]
    lines = [AML_HEADER]
    for i in range(rows):
        a, b = rng.sample(range(len(accts)), 2)
        amt = round(rng.lognormvariate(7, 1.5), 2)
        day, minute = 1 + i // 60, i % 60
        lines.append(f"2022/09/{day:02d} 10:{minute:02d},{10 + a % 3},{accts[a]},{10 + b % 3},{accts[b]},{amt},"
                     f"US Dollar,{amt},US Dollar,{AML_FORMATS[i % len(AML_FORMATS)]},{int(i % 17 == 0)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def make_worlds(out: Path, *, n_accounts: int = 250, n_rings: int = 4) -> list[Path]:
    from editions.amd.lora.common import make_mini_world

    made = []
    for name, seed in WORLDS.items():
        target = out / name
        if (target / "transactions.parquet").exists() and (target / "golden" / "cases.jsonl").exists():
            print(f"{name}: exists")
        else:
            make_mini_world(target, name=name, seed=seed, n_accounts=n_accounts, n_rings=n_rings)
            print(f"{name}: generated (seed {seed})")
        made.append(target)
    csv = tiny_amlworld_csv(out / "aml-tiny.csv")
    print(f"{csv.name}: {sum(1 for _ in csv.open(encoding='utf-8')) - 1} synthetic rows")
    return made


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("worlds")
    w.add_argument("--out", required=True)
    w.add_argument("--accounts", type=int, default=250)
    args = ap.parse_args(argv)
    if args.cmd == "worlds":
        make_worlds(Path(args.out), n_accounts=args.accounts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
