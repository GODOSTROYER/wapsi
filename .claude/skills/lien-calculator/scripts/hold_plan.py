"""Build and explain the amount-specific hold plan for an NCRP case, with SOP citations and the baseline comparison.

Part of the `lien-calculator` skill. Run from the repo root.

    uv run python .claude/skills/lien-calculator/scripts/hold_plan.py 31009260000101 --world showcase-memory
    uv run python .claude/skills/lien-calculator/scripts/hold_plan.py 31109260000102 --world showcase-memory --group
    uv run python .claude/skills/lien-calculator/scripts/hold_plan.py <ncrp_id> --world demo --group --json

Pipeline: trace_case -> classify_trace -> build_hold_plan, then an independent re-check of the hard rules in
AGENTS.md (convention 6) and, when available, wapsi.verify.verify_plan.
Exit codes: 0 plan passes every check, 1 a policy check failed, 2 usage / data error.
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

from wapsi.config import WapsiConfig  # noqa: E402
from wapsi.models import (  # noqa: E402
    INTERMEDIARY_ACCOUNT_TYPES,
    ApportionmentRule,
    Disposition,
    HoldPlan,
    RestorationProcess,
    Role,
    TraceResult,
)
from wapsi.money import EPSILON, eq, fmt_inr, r2  # noqa: E402
from wapsi.timeutil import fmt_ist  # noqa: E402

ESCALATE = "ESCALATE_S106_CANDIDATE"
# Dispositions are compared by value so the script keeps working while the core moves from the legacy
# BLOCK_NODAL_POOL_ESCROW ("never hold") to HOLD_AMOUNT_INTERMEDIARY (SOP 9.1(iii): hold the disputed amount).
INTERMEDIARY_HOLD = "HOLD_AMOUNT_INTERMEDIARY"
LEGACY_BLOCK = "BLOCK_NODAL_POOL_ESCROW"
BALANCE_CAPPED = {"HOLD_AMOUNT", "HOLD_AMOUNT_NO_ESCALATION", INTERMEDIARY_HOLD}
ZERO_HOLD = {"NO_HOLD_UPDATE_ONLY", "CASHED_OUT", "OUTBOUND_CFCFRMS_REQUEST", LEGACY_BLOCK}
RULE_TEXT = {
    ApportionmentRule.PRO_RATA: "pro-rata, SOP Para 5(xiii) [SOP-5-XIII]",
    ApportionmentRule.FIFO: "FIFO (override; SOP default is pro-rata, Para 5(xiii))",
    ApportionmentRule.LIFO: "LIFO (override; SOP default is pro-rata, Para 5(xiii))",
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


def resolve_ids(ledger, ncrp_ids: list[str], group: bool) -> list[str]:
    for n in ncrp_ids:
        ledger.complaint(n)  # KeyError for unknown ids
    if not group:
        return sorted(set(ncrp_ids))
    from wapsi.pipeline.run import group_complaints

    out = set(ncrp_ids)
    for g in group_complaints(ledger, ledger.complaints()):
        if out & set(g):
            out |= set(g)
    return sorted(out)


def utrs_into(trace: TraceResult, account_id: str) -> tuple[list[str], list[str]]:
    edges = sorted(
        (e for e in trace.edges if e.to_account == account_id and e.tainted_total > 0),
        key=lambda e: (e.ts, e.txn_id),
    )
    dates = list(dict.fromkeys(fmt_ist(e.ts, with_time=False) for e in edges))
    return sorted(e.utr for e in edges), dates


def policy_checks(trace: TraceResult, plan: HoldPlan, ledger, cfg: WapsiConfig) -> list[tuple[bool, str]]:
    """Independent re-check of AGENTS.md convention 6. Returns [(passed, message)]."""
    out: list[tuple[bool, str]] = []

    def check(ok: bool, msg: str) -> None:
        out.append((bool(ok), msg))

    fir = any(ledger.complaint(n).fir_number for n in trace.ncrp_ids)
    signals = ledger.external_signals([d.account_id for d in plan.decisions])
    for d in plan.decisions:
        st = trace.accounts.get(d.account_id)
        a = d.account_id
        disp = d.disposition.value
        check(
            st is not None and st.layer > 0, f"{a}: decision is for a traced, non-victim account (layer > 0)"
        )
        check(
            d.hold_amount <= d.tainted_amount + EPSILON or disp == "OUTBOUND_CFCFRMS_REQUEST",
            f"{a}: hold {fmt_inr(d.hold_amount)} <= tainted {fmt_inr(d.tainted_amount)} (SOP 5(ix), 9.1)",
        )
        if disp in BALANCE_CAPPED:
            check(
                d.hold_amount <= d.balance_now + EPSILON,
                f"{a}: hold {fmt_inr(d.hold_amount)} <= balance {fmt_inr(d.balance_now)}",
            )
        if st is not None and st.account_type in INTERMEDIARY_ACCOUNT_TYPES:
            expected = r2(min(d.tainted_amount, d.balance_now))
            check(
                disp == INTERMEDIARY_HOLD and eq(d.hold_amount, expected),
                f"{a}: {st.account_type.value} account -> {INTERMEDIARY_HOLD}, hold the disputed amount "
                f"{fmt_inr(expected)} (SOP 9.1(iii)); got {disp} {fmt_inr(d.hold_amount)}",
            )
            check(
                d.forward_hold_amount == 0 and ESCALATE not in d.reason_codes,
                f"{a}: no forward hold and no s.106 flag on a {st.account_type.value} account (SOP 9.1(i), 9.1(iii))",
            )
        if d.forward_hold_amount > 0:
            check(d.layer == 1, f"{a}: forward hold only on first-layer accounts (SOP 9.1(i))")
        if disp in ZERO_HOLD:
            check(d.hold_amount == 0, f"{a}: {disp} holds 0")
        check(
            disp != LEGACY_BLOCK, f"{a}: legacy {LEGACY_BLOCK} must not be emitted (use {INTERMEDIARY_HOLD})"
        )
        check(
            disp != ESCALATE,
            f"{a}: ESCALATE_S106_CANDIDATE is a flag, never a disposition",
        )
        if d.role == Role.INNOCENT_HOLDER:
            check(
                d.disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION and ESCALATE not in d.reason_codes,
                f"{a}: innocent holder -> hold, no escalation (SOP 5(xii))",
            )
        if ESCALATE in d.reason_codes:
            prior = signals.get(a).ncrp_prior_reports if a in signals else 0
            check(
                d.requires_human and fir and prior >= cfg.escalation_min_prior_reports,
                f"{a}: s.106 flag needs requires_human, an FIR and >= {cfg.escalation_min_prior_reports} prior "
                f"reports (FIR={'yes' if fir else 'no'}, prior={prior})",
            )
    total_hold = r2(sum(d.hold_amount for d in plan.decisions))
    outbound = r2(
        sum(d.tainted_amount for d in plan.decisions if d.disposition.value == "OUTBOUND_CFCFRMS_REQUEST")
    )
    check(
        eq(plan.total_hold, total_hold),
        f"total_hold {fmt_inr(plan.total_hold)} = sum of holds {fmt_inr(total_hold)}",
    )
    check(
        eq(plan.total_recoverable, total_hold + outbound),
        f"total_recoverable {fmt_inr(plan.total_recoverable)} = holds + outbound requests {fmt_inr(total_hold + outbound)}",
    )
    check(eq(plan.over_hold_amount, 0.0), f"over-hold is {fmt_inr(plan.over_hold_amount)} (must be ₹0.00)")
    victims = sum(1 for v in plan.per_victim_recoverable.values() if v > EPSILON)
    expected = RestorationProcess.P2 if victims > 1 else RestorationProcess.P1
    check(
        plan.restoration_process == expected,
        f"restoration process {plan.restoration_process.value} (victims with recoverable money: {victims})",
    )
    return out


def render(
    trace: TraceResult,
    plan: HoldPlan,
    checks: list[tuple[bool, str]],
    external: str | None,
    all_checks: bool = False,
) -> str:
    held = [d for d in plan.decisions if d.hold_amount > 0]
    lines = [
        f"## Hold plan — {plan.case_id}",
        "",
        f"- Complaints: {', '.join(trace.ncrp_ids)} · stolen {fmt_inr(trace.totals.stolen)}",
        f"- Apportionment rule: {RULE_TEXT[plan.rule]}",
        f"- Restoration process: {plan.restoration_process.value}"
        f" [{'SOP-11-P2' if plan.restoration_process == RestorationProcess.P2 else 'SOP-11-P1'}]",
        f"- STR ground (PML Rule 2(1)(g)): {plan.str_ground.value if plan.str_ground else 'none (no automatic STR)'}",
        "",
        "| Account | Layer | Role | Disposition | Tainted | Balance | Hold | Forward hold | Citations | Flags |",
        "|---|---:|---|---|---:|---:|---:|---:|---|---|",
    ]
    for d in sorted(plan.decisions, key=lambda d: (d.layer, d.account_id)):
        flags = ", ".join(d.reason_codes) + (" · HUMAN REVIEW" if d.requires_human else "")
        lines.append(
            f"| {d.account_id} | {d.layer} | {d.role.value} | {d.disposition.value} | {fmt_inr(d.tainted_amount)} "
            f"| {fmt_inr(d.balance_now)} | {fmt_inr(d.hold_amount)} | {fmt_inr(d.forward_hold_amount)} "
            f"| {' '.join(f'[{c}]' for c in d.citation_ids)} | {flags} |"
        )
    lines += ["", "### Hold lines (amount-specific; wording comes from the HOLD_INSTRUCTION draft)", ""]
    for d in held:
        utrs, dates = utrs_into(trace, d.account_id)
        lines.append(
            f"- {d.account_id} ({d.bank_id}): hold {fmt_inr(d.hold_amount)} of tainted "
            f"{fmt_inr(d.tainted_amount)}; UTR {', '.join(utrs) or '-'}; credited {', '.join(dates) or '-'}"
        )
    for d in plan.decisions:
        if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST:
            lines.append(
                f"- {d.account_id} ({d.bank_id}): CFCFRMS request to the other bank for "
                f"{fmt_inr(d.tainted_amount)} (no hold by us)"
            )
    lines += [
        "",
        "### Totals",
        "",
        f"- Held now: {fmt_inr(plan.total_hold)} across {len(held)} account(s)",
        f"- Forward holds (first-layer only, not money in hand): {fmt_inr(plan.total_forward_hold)}",
        f"- Recoverable (holds + outbound requests): {fmt_inr(plan.total_recoverable)}",
        f"- Over-hold: {fmt_inr(plan.over_hold_amount)}",
    ]
    if plan.per_victim_recoverable:
        lines.append(
            "- Per victim: "
            + ", ".join(f"{k}: {fmt_inr(v)}" for k, v in sorted(plan.per_victim_recoverable.items()))
        )
    lines += [
        "",
        f"**Whole-account freeze baseline** (comparison only): {fmt_inr(plan.baseline_freeze_amount)} across the "
        f"{plan.baseline_freeze_accounts} home-bank account(s) that received tainted money; this plan holds "
        f"{fmt_inr(plan.total_hold)} across {len(held)} account(s).",
        "",
        "### Policy re-check (AGENTS.md convention 6)",
        "",
    ]
    passed = sum(ok for ok, _ in checks)
    lines.append(f"{passed}/{len(checks)} checks passed.")
    lines += [f"- {'PASS' if ok else 'FAIL'}: {msg}" for ok, msg in checks if all_checks or not ok]
    if external:
        lines += ["", f"verify_plan: {external}"]
    lines += ["", "Status: Recommended, pending supervisor approval."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ncrp_ids", nargs="+")
    ap.add_argument("--world", default="demo")
    ap.add_argument(
        "--rule", choices=[r.value for r in ApportionmentRule], default=ApportionmentRule.PRO_RATA.value
    )
    ap.add_argument("--group", action="store_true", help="add complaints that share first-layer accounts")
    ap.add_argument("--json", action="store_true", help="print HoldPlan JSON")
    ap.add_argument("--all-checks", action="store_true", help="list passing policy checks too")
    args = ap.parse_args(argv)

    try:
        from wapsi.classify import classify_trace
        from wapsi.graph.holdplan import build_hold_plan
        from wapsi.graph.trace import trace_case
    except (ImportError, AttributeError) as exc:
        print(f"wapsi core not importable in this checkout ({type(exc).__name__}: {exc})")
        return 2

    ledger = load_ledger(args.world)
    try:
        ids = resolve_ids(ledger, args.ncrp_ids, args.group)
    except KeyError as exc:
        print(f"unknown NCRP id in world {args.world}: {exc}")
        return 2
    cfg = WapsiConfig()
    cfg.trace.rule = ApportionmentRule(args.rule)
    trace = trace_case(ledger, ids, cfg.trace)
    classes = classify_trace(ledger, trace, cfg.classify)
    plan = build_hold_plan(trace, classes, ledger, cfg)
    checks = policy_checks(trace, plan, ledger, cfg)

    external = None
    try:
        from wapsi.models import Case, CaseBundle
        from wapsi.timeutil import utcnow
        from wapsi.verify.report import label_of
        from wapsi.verify.runner import verify_plan

        bundle = CaseBundle(
            case=Case(case_id=plan.case_id, ncrp_ids=ids, opened_at=utcnow()),
            complaints=[ledger.complaint(n) for n in ids],
            trace=trace,
            classifications=classes,
            plan=plan,
        )
        rep = verify_plan(bundle, ledger, cfg=cfg)
        bad = [c for c in rep.checks if not c.passed and c.severity == "error"]
        external = "PASS" if rep.passed else "FAIL: " + "; ".join(f"{c.name}: {label_of(c)}" for c in bad)
        if not rep.passed:
            checks.append((False, "wapsi.verify.verify_plan failed"))
    except ImportError:
        external = "not available in this checkout (skipped)"

    if args.json:
        print(plan.model_dump_json(indent=2))
    else:
        print(render(trace, plan, checks, external, all_checks=args.all_checks))
    return 0 if all(ok for ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
