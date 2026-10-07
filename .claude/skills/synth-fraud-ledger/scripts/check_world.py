"""Check a generated Wapsi world (Parquet) before anything reads it.

Part of the `synth-fraud-ledger` skill. Run from the repo root.

    uv run python .claude/skills/synth-fraud-ledger/scripts/check_world.py --world data/generated/demo
    uv run python .claude/skills/synth-fraud-ledger/scripts/check_world.py --world showcase

--world: a directory with <table>.parquet files, or a name under data/generated/.
Checks: every product and ground-truth table is present with the columns and types of
wapsi.synth.export.SCHEMAS (docs/data-contract.md); row counts equal META (n_accounts, n_transactions,
n_complaints) and the golden set has META n_cases cases; exactly one home bank and it equals META home_bank_id;
every complaint transaction exists (no orphans); transaction and complaint timestamps are UTC and inside META
world_start..world_end; ground truth lives only in labels_*.parquet; the world loads through ParquetLedger and a
complaint resolves to its transactions.
Exit codes: 0 every check passed, 1 a check failed, 2 usage / missing input.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
for extra in (ROOT, ROOT / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import pyarrow.parquet as pq  # noqa: E402

from wapsi.synth.export import LABEL_TABLES, PRODUCT_TABLES, SCHEMAS  # noqa: E402

COUNTS = {"accounts": "n_accounts", "transactions": "n_transactions", "complaints": "n_complaints"}
TIME_COLUMNS = {"transactions": "ts", "complaints": "reported_at"}
# showcase fixture anchor: the victim transfer of case 1 is stored as UTC (11:02 IST)
SHOWCASE_ANCHOR = ("TX-001", "2026-09-10T05:32:00+00:00")


def resolve(world: str) -> Path:
    """A directory path as given, or a bare world name under data/generated/."""
    path = Path(world)
    if not path.is_dir() and len(path.parts) == 1:
        path = ROOT / "data" / "generated" / world
    return path


def check(world: Path) -> list[tuple[bool, str]]:
    results: list[tuple[bool, str]] = []
    add = results.append

    print(f"World: {world}")
    print(f"{'table':<20}{'rows':>12}  status")
    for t in [*PRODUCT_TABLES, *LABEL_TABLES]:
        p = world / f"{t}.parquet"
        if not p.exists():
            add((False, f"{t}: {p.name} missing"))
            print(f"{t:<20}{'-':>12}  MISSING")
            continue
        schema = pq.read_schema(p).remove_metadata()
        ok = schema.equals(SCHEMAS[t])
        if not ok:
            want = {f.name: str(f.type) for f in SCHEMAS[t]}
            got = {f.name: str(f.type) for f in schema}
            diff = sorted(set(want.items()) ^ set(got.items()))
            add((False, f"{t}: columns differ from wapsi.synth.export.SCHEMAS: {diff}"))
        print(f"{t:<20}{pq.read_metadata(p).num_rows:>12}  {'OK' if ok else 'SCHEMA MISMATCH'}")
    if results:
        return results
    add((True, f"schema: {len(PRODUCT_TABLES) + len(LABEL_TABLES)} tables match wapsi.synth.export.SCHEMAS"))

    meta = dict(pq.read_table(world / "meta.parquet").to_pandas().values.tolist())
    for t, key in COUNTS.items():
        n = pq.read_metadata(world / f"{t}.parquet").num_rows
        add((str(n) == str(meta.get(key)), f"{t}: {n} rows, META {key} = {meta.get(key)}"))

    golden = world / "golden" / "cases.jsonl"
    if golden.exists():
        n = sum(1 for line in golden.read_text(encoding="utf-8").splitlines() if line.strip())
        add(
            (
                str(n) == str(meta.get("n_cases")),
                f"golden: {n} cases in {golden.name}, META n_cases = {meta.get('n_cases')}",
            )
        )
    else:
        add((False, f"golden: {golden} missing (regenerate with `python -m wapsi.synth`)"))

    banks = pq.read_table(world / "banks.parquet").to_pandas()
    homes = banks.loc[banks["is_home"].fillna(False).astype(bool), "bank_id"].tolist()
    add(
        (
            len(homes) == 1 and homes[0] == meta.get("home_bank_id"),
            f"home bank: {homes} (exactly one, equal to META home_bank_id = {meta.get('home_bank_id')})",
        )
    )

    txn_ids = set(pq.read_table(world / "transactions.parquet", columns=["txn_id"]).column(0).to_pylist())
    links = pq.read_table(world / "complaint_txns.parquet").to_pandas()
    orphans = sorted(set(links["txn_id"]) - txn_ids)
    add((not orphans, f"complaint transactions: {len(links)} links, {len(orphans)} orphan(s) {orphans[:5]}"))

    start = datetime.fromisoformat(meta["world_start"])
    end = datetime.fromisoformat(meta["world_end"])
    for t, col in TIME_COLUMNS.items():
        s = pq.read_table(world / f"{t}.parquet", columns=[col]).column(0).to_pandas()
        tz = str(getattr(s.dtype, "tz", None))
        lo, hi = s.min(), s.max()
        inside = tz == "UTC" and (s.empty or (lo >= start and hi <= end))
        add((inside, f"{t}.{col}: tz {tz}, {lo} .. {hi} within {meta['world_start']} .. {meta['world_end']}"))

    leaks = [
        f"{t}.{name}"
        for t in PRODUCT_TABLES
        for name in pq.read_schema(world / f"{t}.parquet").names
        if name.endswith("_truth")
    ]
    add(
        (
            not leaks,
            f"ground truth only in labels_*.parquet (truth columns in product tables: {leaks or 'none'})",
        )
    )

    if meta.get("world") == "showcase":
        tx = pq.read_table(world / "transactions.parquet", columns=["txn_id", "ts"]).to_pandas()
        row = tx[tx["txn_id"] == SHOWCASE_ANCHOR[0]]
        got = row["ts"].iloc[0].isoformat() if len(row) else None
        add(
            (
                got == SHOWCASE_ANCHOR[1],
                f"showcase anchor {SHOWCASE_ANCHOR[0]}: {got} (expected {SHOWCASE_ANCHOR[1]})",
            )
        )

    try:
        from wapsi.adapters.parquet_ledger import ParquetLedger

        ledger = ParquetLedger(world)
        complaints = ledger.complaints()
        first = complaints[0].ncrp_id if complaints else None
        txns = ledger.complaint_transactions(first) if first else []
        add(
            (
                ledger.home_bank_id() == meta.get("home_bank_id") and bool(txns),
                f"ParquetLedger: home bank {ledger.home_bank_id()}, {len(complaints)} complaints, "
                f"complaint {first} -> {len(txns)} transaction(s)",
            )
        )
    except Exception as exc:  # report, do not hide
        add((False, f"ParquetLedger failed to load the world: {type(exc).__name__}: {exc}"))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--world", required=True, help="world directory or a name under data/generated/")
    args = ap.parse_args(argv)

    world = resolve(args.world)
    if not (world / "transactions.parquet").exists():
        print(f"no world at {world} (run `uv run python -m wapsi.synth --size <size> --out {world}`)")
        return 2
    results = check(world)
    print()
    for ok, msg in results:
        print(f"{'PASS' if ok else 'FAIL'}  {msg}")
    passed = sum(ok for ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
