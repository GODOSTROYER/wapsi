"""Summarise the latest golden-set evaluation against the build-plan targets and the previous report.

Part of the `wapsi-eval` skill (also used by the CoCo SessionStart hook). Run from the repo root.

    uv run python .claude/skills/wapsi-eval/scripts/kpi_summary.py                 # evals/reports/latest.json
    uv run python .claude/skills/wapsi-eval/scripts/kpi_summary.py --run --world demo
    uv run python .claude/skills/wapsi-eval/scripts/kpi_summary.py --brief          # one line (hooks)
    uv run python .claude/skills/wapsi-eval/scripts/kpi_summary.py --sql            # INSERT for WAPSI.APP.EVAL_RUNS

Report schema: docs/eval-report.md. Exit codes: 0 all targets met, 1 a target missed or (with
--fail-on-regression) a metric regressed, 2 no report / harness failed. --brief always exits 0.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
REPORTS = ROOT / "evals" / "reports"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# (label, path in report, kind, target op, target value, higher_is_better); targets from WAPSI_BUILD_PLAN §5
KPIS = [
    ("Traced-amount recall", "metrics.traced_amount_recall", "pct", ">=", 0.95, True),
    ("Over-hold", "metrics.over_hold_inr", "inr", "<=", 0.0, False),
    ("False freezes", "metrics.false_freeze_count", "int", "<=", 0, False),
    ("Hold exact match", "metrics.hold_exact_match_rate", "pct", None, None, True),
    ("Disposition accuracy", "metrics.disposition_accuracy", "pct", None, None, True),
    ("Process (P1/P2) accuracy", "metrics.process_accuracy", "pct", None, None, True),
    ("Innocent-holder precision", "metrics.role_precision.INNOCENT_HOLDER", "pct", ">=", 0.90, True),
    ("Innocent-holder recall", "metrics.role_recall.INNOCENT_HOLDER", "pct", ">=", 0.80, True),
    ("Merchant precision", "metrics.role_precision.MERCHANT", "pct", ">=", 0.95, True),
    ("Mule F1 (from P/R)", "derived.mule_f1", "pct", None, None, True),
    ("Draft verification pass rate", "metrics.draft_verification_pass_rate", "pct", None, None, True),
    ("STR/draft numeric accuracy", "metrics.draft_numeric_accuracy", "pct", ">=", 1.0, True),
    ("Citation validity", "metrics.citation_validity", "pct", ">=", 1.0, True),
    ("Injection flag recall", "metrics.injection_flag_recall", "pct", None, None, True),
    ("Injection leaks", "metrics.injection_leaks", "int", "<=", 0, False),
    (
        "LLM-only hallucination rate (baseline)",
        "metrics.llm_only_hallucination_rate",
        "pct",
        None,
        None,
        False,
    ),
    ("p95 time to hold plan", "metrics.p95_time_to_hold_plan_s", "sec", "<", 90.0, False),
]
BRIEF = [
    "metrics.traced_amount_recall",
    "metrics.over_hold_inr",
    "metrics.false_freeze_count",
    "metrics.draft_numeric_accuracy",
    "metrics.citation_validity",
    "metrics.injection_leaks",
    "metrics.p95_time_to_hold_plan_s",
]


def get(report: dict, path: str):
    if path == "derived.mule_f1":
        p, r = get(report, "metrics.role_precision.MULE"), get(report, "metrics.role_recall.MULE")
        return None if p is None or r is None or p + r == 0 else 2 * p * r / (p + r)
    cur = report
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def fmt(value, kind: str) -> str:
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{value * 100:.1f}%"
    if kind == "inr":
        try:
            sys.path.insert(0, str(ROOT / "src"))
            from wapsi.money import fmt_inr

            return fmt_inr(value)
        except ImportError:
            return f"₹{value:,.2f}"
    if kind == "sec":
        return f"{value:.2f} s"
    return str(int(value))


def fmt_delta(cur, prev, kind: str) -> str:
    if cur is None or prev is None:
        return ""
    d = cur - prev
    if abs(d) < 1e-9:
        return "="
    if kind == "pct":
        return f"{d * 100:+.1f} pp"
    if kind == "sec":
        return f"{d:+.2f} s"
    if kind == "inr":
        return f"{d:+,.2f}"
    return f"{d:+d}" if isinstance(d, int) else f"{d:+g}"


def meets(value, op, target) -> bool | None:
    if op is None or value is None:
        return None
    return {"<=": value <= target, ">=": value >= target - 1e-9, "<": value < target}[op]


def regressed(cur, prev, higher_is_better: bool) -> bool:
    if cur is None or prev is None:
        return False
    return cur < prev - 1e-9 if higher_is_better else cur > prev + 1e-9


def find_reports(world: str | None, latest: dict) -> dict | None:
    """Previous report for the same world: second-newest <world>-*.json (latest.json mirrors the newest)."""
    world = world or latest.get("world")
    runs = sorted(
        (p for p in REPORTS.glob(f"{world}-*.json") if not p.stem.endswith("-summary")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for p in runs:
        try:
            rep = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rep.get("generated_at") != latest.get("generated_at"):
            return rep | {"_path": str(p.relative_to(ROOT))}
    return None


def run_harness(world: str, quick: bool) -> int:
    cmd = [sys.executable, "-m", "evals.harness", "--world", world] + (["--quick"] if quick else [])
    print(f"$ {' '.join(cmd[1:])}", flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", default=str(REPORTS / "latest.json"))
    ap.add_argument("--previous", help="explicit previous report (default: second-newest for the same world)")
    ap.add_argument("--run", action="store_true", help="run the harness first")
    ap.add_argument("--world", default="demo")
    ap.add_argument("--quick", action="store_true", help="with --run: first 10 cases only")
    ap.add_argument("--brief", action="store_true", help="one line, never fails (SessionStart hook)")
    ap.add_argument("--sql", action="store_true", help="print an INSERT for WAPSI.APP.EVAL_RUNS")
    ap.add_argument("--fail-on-regression", action="store_true")
    args = ap.parse_args(argv)

    if args.run and run_harness(args.world, args.quick) != 0:
        print("harness failed; no summary")
        return 2
    path = Path(args.report)
    if not path.exists():
        msg = f"No evaluation report at {path}. Run: uv run python -m evals.harness --world {args.world}"
        print(f"Wapsi KPIs: {msg}" if args.brief else msg)
        return 0 if args.brief else 2
    latest = json.loads(path.read_text(encoding="utf-8"))
    prev = (
        json.loads(Path(args.previous).read_text(encoding="utf-8"))
        if args.previous
        else find_reports(None, latest)
    )

    if args.sql:
        body = json.dumps(latest, ensure_ascii=False)
        print(
            "INSERT INTO WAPSI.APP.EVAL_RUNS (world, generated_at, cases, report)\n"
            f"SELECT '{latest.get('world', '')}', '{latest.get('generated_at', '')}'::TIMESTAMP_TZ, "
            f"{int(latest.get('cases', 0))}, PARSE_JSON($${body}$$);"
        )
        return 0

    rows, missed, worse = [], 0, 0
    for label, key, kind, op, target, hib in KPIS:
        cur = get(latest, key)
        ok = meets(cur, op, target)
        missed += ok is False
        reg = regressed(cur, get(prev, key) if prev else None, hib)
        worse += reg
        rows.append((label, key, kind, op, target, cur, ok, reg, get(prev, key) if prev else None))

    when = latest.get("generated_at", "?")
    try:
        when = datetime.fromisoformat(when).strftime("%d-%m-%Y %H:%M UTC")
    except ValueError:
        pass
    targets = [r for r in rows if r[6] is not None]
    met = sum(1 for r in targets if r[6])
    if args.brief:
        parts = [f"{r[0]} {fmt(r[5], r[2])}" for r in rows if r[1] in BRIEF]
        print(
            f"Wapsi KPIs ({latest.get('world')}, {latest.get('cases', '?')} cases, {when}): "
            + " · ".join(parts)
            + f" — {met}/{len(targets)} targets met"
            + (f", {worse} regression(s) vs previous" if worse else "")
        )
        return 0

    print(
        f"## Wapsi golden-set KPIs — world `{latest.get('world')}`, {latest.get('cases', '?')} cases "
        f"({latest.get('adversarial_cases', 0)} adversarial), {when}"
    )
    cfg = latest.get("config", {})
    print(
        f"\nConfig: rule {cfg.get('rule', '?')} · LLM {cfg.get('llm', '?')} · scorer {cfg.get('scorer', '?')}"
    )
    print(f"Previous: {prev.get('_path', 'explicit file') if prev else 'none found'}\n")
    print("| KPI | Value | Target | Status | Δ vs previous |")
    print("|---|---:|---|---|---:|")
    for label, _key, kind, op, target, cur, ok, reg, pv in rows:
        tgt = f"{op} {fmt(target, kind)}" if op else "report"
        status = "met" if ok else ("MISSED" if ok is False else "—")
        delta = fmt_delta(cur, pv, kind) + (" (worse)" if reg else "")
        print(f"| {label} | {fmt(cur, kind)} | {tgt} | {status} | {delta} |")
    base = latest.get("baseline", {})
    if base:
        print(
            f"\nWhole-account freeze baseline (comparison only): "
            f"{fmt(base.get('whole_account_freeze_inr'), 'inr')} "
            f"across {base.get('accounts_frozen_baseline', '?')} accounts; Wapsi holds "
            f"{fmt(base.get('wapsi_hold_inr'), 'inr')} across {base.get('accounts_held', '?')}."
        )
    fails = latest.get("failures", [])
    if fails:
        print(f"\nFailures ({len(fails)}; first 10):")
        for f in fails[:10]:
            print(f"- {f.get('case_id')}: {f.get('check')} — {f.get('detail')}")
    per = latest.get("per_typology", {})
    if per:
        print("\n| Typology | Cases | Hold exact | Disposition acc. |\n|---|---:|---:|---:|")
        for t, v in sorted(per.items()):
            print(
                f"| {t} | {v.get('cases', '?')} | {fmt(v.get('hold_exact_match_rate'), 'pct')} "
                f"| {fmt(v.get('disposition_accuracy'), 'pct')} |"
            )
    print(f"\n{met}/{len(targets)} targets met; {worse} metric(s) worse than the previous report.")
    if missed:
        return 1
    return 1 if (args.fail_on_regression and worse) else 0


if __name__ == "__main__":
    raise SystemExit(main())
