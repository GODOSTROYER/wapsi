"""CLI: ``uv run python -m wapsi.synth --size demo --seed 7 --out data/generated/demo``.

Writes the Parquet world to ``--out`` and the golden set to ``data/golden/<name>/`` (for ``full`` only when the
golden file is under 2 MB; otherwise it stays in ``<out>/golden``).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
import tracemalloc
from pathlib import Path

from wapsi.synth.export import write_frames
from wapsi.synth.golden import write_golden
from wapsi.synth.world import WorldConfig, build_world

GOLDEN_LIMIT_BYTES = 2_000_000


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wapsi.synth", description=__doc__)
    ap.add_argument("--size", choices=["showcase", "demo", "full"], default="demo")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--name", default=None, help="world name (default: the size)")
    ap.add_argument("--out", default=None, help="output directory (default: data/generated/<name>)")
    ap.add_argument("--golden-dir", default="data/golden", help="golden root; '' to skip")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--trace-memory", action="store_true", help="report peak Python heap (slower)")
    args = ap.parse_args(argv)

    name = args.name or args.size
    out = Path(args.out or f"data/generated/{name}")
    if args.trace_memory:
        tracemalloc.start()
    t0 = time.perf_counter()
    frames, cases = build_world(WorldConfig(name=name, size=args.size, seed=args.seed, days=args.days))
    t_gen = time.perf_counter() - t0
    write_frames(frames, out)
    local_golden = out / "golden"
    golden = write_golden(frames, cases, local_golden, name)
    t_all = time.perf_counter() - t0
    size = (local_golden / "cases.jsonl").stat().st_size
    dest = None
    if args.golden_dir and (args.size != "full" or size < GOLDEN_LIMIT_BYTES):
        dest = Path(args.golden_dir) / name
        dest.mkdir(parents=True, exist_ok=True)
        for f in ("cases.jsonl", "SUMMARY.md"):
            shutil.copyfile(local_golden / f, dest / f)
    meta = dict(zip(frames["meta"]["key"], frames["meta"]["value"], strict=True))
    print(
        f"world={name} size={args.size} seed={meta['seed']} accounts={meta['n_accounts']} "
        f"transactions={meta['n_transactions']} complaints={meta['n_complaints']} cases={len(golden)} "
        f"adversarial={meta['n_adversarial']} reference_warnings={meta.get('reference_warnings')}"
    )
    print(
        f"generate+reference {t_gen:.1f}s, total {t_all:.1f}s -> {out}"
        + (f"; golden -> {dest} ({size / 1024:.0f} KiB)" if dest else f"; golden kept in {local_golden}")
    )
    if args.trace_memory:
        _, peak = tracemalloc.get_traced_memory()
        print(f"peak traced heap {peak / 2**30:.2f} GiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
