"""Trace an NCRP complaint's money trail with the portable Wapsi core and print it as Markdown (or JSON).

Part of the `money-trail-tracer` skill. Run from the repo root.

    uv run python .claude/skills/money-trail-tracer/scripts/trace.py 31009260000101 --world showcase-memory
    uv run python .claude/skills/money-trail-tracer/scripts/trace.py <ncrp_id> --world demo --group
    uv run python .claude/skills/money-trail-tracer/scripts/trace.py <ncrp_id> [<ncrp_id> ...] --rule FIFO --json

--world: `showcase-memory` (hand-computed fixture), a name under data/generated/, or a directory path.
Exit codes: 0 ok, 1 totals do not reconcile, 2 usage / data error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
for extra in (ROOT, ROOT / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # ₹ on Windows consoles (cp1252)

from wapsi.config import TraceConfig  # noqa: E402
from wapsi.models import ApportionmentRule, TraceResult  # noqa: E402
from wapsi.money import eq, fmt_inr, r2  # noqa: E402
from wapsi.timeutil import fmt_ist  # noqa: E402

RULE_TEXT = {
    ApportionmentRule.PRO_RATA: "pro-rata (proportionate), SOP Para 5(xiii) and Annexure-V [SOP-5-XIII] [SOP-ANNEX-V]",
    ApportionmentRule.FIFO: "FIFO (configured override; the SOP default is pro-rata, Para 5(xiii) [SOP-5-XIII])",
    ApportionmentRule.LIFO: "LIFO (configured override; the SOP default is pro-rata, Para 5(xiii) [SOP-5-XIII])",
}


def load_ledger(world: str):
    if world in ("showcase-memory", "fixture"):
        from wapsi.fixtures.showcase import showcase_ledger

        return showcase_ledger()
    path = Path(world)
    if not path.exists():
        path = ROOT / "data" / "generated" / world
    if not path.exists():
        raise SystemExit(f"world not found: {world} (generate it with the synth-fraud-ledger skill)")
    from wapsi.adapters.parquet_ledger import ParquetLedger

    return ParquetLedger(path)


def _known(ledger, ncrp_id: str) -> bool:
    try:
        ledger.complaint(ncrp_id)
    except KeyError:
        return False
    return True


def expand_group(ledger, ncrp_ids: list[str]) -> list[str]:
    """Add every complaint that shares first-layer accounts with the given ones (one case = one group)."""
    from wapsi.pipeline.run import group_complaints

    wanted = set(ncrp_ids)
    out: set[str] = set(ncrp_ids)
    for group in group_complaints(ledger, ledger.complaints()):
        if wanted & set(group):
            out |= set(group)
    return sorted(out)


def reconcile(trace: TraceResult) -> tuple[float, float]:
    t = trace.totals
    located = r2(t.holdable + t.cashed_out + t.outbound + t.intermediary + t.merchant_settled)
    return located, r2(t.stolen - located)


def render(trace: TraceResult) -> str:
    t = trace.totals
    located, gap = reconcile(trace)
    lines = [
        f"## Money trail — {trace.case_id}",
        "",
        f"- Complaints (NCRP): {', '.join(trace.ncrp_ids)}",
        f"- Apportionment rule: {RULE_TEXT[trace.rule]}",
        f"- Window: {fmt_ist(trace.window_start)} to {fmt_ist(trace.window_end)}",
        f"- Accounts reached: {sum(1 for s in trace.accounts.values() if s.layer > 0)}"
        f" (plus {sum(1 for s in trace.accounts.values() if s.layer == 0)} victim account(s) at layer 0);"
        f" tainted edges: {len(trace.edges)}",
        "",
        "### Accounts by layer",
        "",
        "| Layer | Account | Bank | Type | Home | Tainted in | Tainted out | Tainted now | Balance now | Terminal |",
        "|---:|---|---|---|:-:|---:|---:|---:|---:|---|",
    ]
    for s in sorted(trace.accounts.values(), key=lambda s: (s.layer, s.account_id)):
        lines.append(
            f"| {s.layer} | {s.account_id} | {s.bank_id} | {s.account_type.value} | {'Y' if s.is_home_bank else ''} "
            f"| {fmt_inr(s.tainted_received_total)} | {fmt_inr(s.tainted_sent_total)} | {fmt_inr(s.tainted_now_total)} "
            f"| {fmt_inr(s.balance_now)} | {s.terminal.value if s.terminal else ''} |"
        )
    lines += [
        "",
        "### Tainted transfers (chronological)",
        "",
        "| Depth | Time (IST) | From | To | Channel | UTR | Amount | Tainted | Via CFCFRMS |",
        "|---:|---|---|---|---|---|---:|---:|:-:|",
    ]
    for e in sorted(trace.edges, key=lambda e: (e.ts, e.txn_id)):
        to = e.to_account or "(cash-out / exit)"
        lines.append(
            f"| {e.depth} | {fmt_ist(e.ts)} | {e.from_account or '(cash deposit)'} | {to} | {e.channel.value} | {e.utr} "
            f"| {fmt_inr(e.amount)} | {fmt_inr(e.tainted_total)} | {'Y' if e.via_consortium else ''} |"
        )
    lines += [
        "",
        "### Totals",
        "",
        "| Stolen | Holdable | Cashed out | Outbound (other banks) | Nodal/pool/escrow | Merchant settled | Unlocated |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        f"| {fmt_inr(t.stolen)} | {fmt_inr(t.holdable)} | {fmt_inr(t.cashed_out)} | {fmt_inr(t.outbound)} "
        f"| {fmt_inr(t.intermediary)} | {fmt_inr(t.merchant_settled)} | {fmt_inr(t.unlocated)} |",
        "",
        f"Reconciliation: located {fmt_inr(located)} + unlocated {fmt_inr(t.unlocated)} = stolen {fmt_inr(t.stolen)}"
        f" → {'OK' if eq(gap, t.unlocated) else 'MISMATCH (gap ' + fmt_inr(gap) + ')'}",
    ]
    if len(trace.victim_seed) > 1:
        lines += [
            "",
            "Per-victim seed: "
            + ", ".join(f"{k}: {fmt_inr(v)}" for k, v in sorted(trace.victim_seed.items())),
        ]
        lines += ["", "Tainted now by victim (Process 2 shares, SOP Para 5(xiii)):", ""]
        for s in sorted(trace.accounts.values(), key=lambda s: (s.layer, s.account_id)):
            if s.tainted_now_total > 0:
                split = ", ".join(f"{k}: {fmt_inr(v)}" for k, v in sorted(s.tainted_now.items()))
                lines.append(f"- {s.account_id}: {split}")
    if trace.warnings:
        lines += ["", "### Warnings", ""] + [f"- {w}" for w in trace.warnings]
    lines += [
        "",
        "Holds are amount-specific (tainted amount only, never the whole account; SOP Para 5(ix), 9.1)."
        " Use $lien-calculator for dispositions.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ncrp_ids", nargs="+", help="14-digit NCRP acknowledgement number(s)")
    ap.add_argument("--world", default="demo")
    ap.add_argument(
        "--rule", choices=[r.value for r in ApportionmentRule], default=ApportionmentRule.PRO_RATA.value
    )
    ap.add_argument("--group", action="store_true", help="add complaints that share first-layer accounts")
    ap.add_argument("--max-depth", type=int, default=None, help="override TraceConfig.max_depth (default 6)")
    ap.add_argument("--json", action="store_true", help="print TraceResult JSON instead of Markdown")
    args = ap.parse_args(argv)

    try:
        from wapsi.graph.trace import trace_case
    except (ImportError, AttributeError) as exc:
        print(f"wapsi core not importable in this checkout ({type(exc).__name__}: {exc})")
        return 2

    ledger = load_ledger(args.world)
    unknown = [n for n in args.ncrp_ids if not _known(ledger, n)]
    if unknown:
        print(f"unknown NCRP id(s) in world {args.world}: {', '.join(unknown)}")
        return 2
    ids = expand_group(ledger, args.ncrp_ids) if args.group else sorted(set(args.ncrp_ids))
    cfg = TraceConfig(rule=ApportionmentRule(args.rule))
    if args.max_depth is not None:
        if not 1 <= args.max_depth <= 10:
            print("--max-depth must be between 1 and 10 (depth guard)")
            return 2
        cfg = cfg.model_copy(update={"max_depth": args.max_depth})
    trace = trace_case(ledger, ids, cfg)
    if args.json:
        print(trace.model_dump_json(indent=2))
    else:
        print(render(trace))
    _, gap = reconcile(trace)
    return 0 if eq(gap, trace.totals.unlocated) else 1


if __name__ == "__main__":
    raise SystemExit(main())
