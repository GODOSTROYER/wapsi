"""Golden-set evaluation. Usage:

    uv run python -m evals.harness --world demo            # data/generated/demo + data/golden/demo
    uv run python -m evals.harness --world showcase --llm fake
    uv run python -m evals.harness --world hard            # data/generated/hard (+ its golden/ folder)
    uv run python -m evals.harness --world hard --scorer heuristic --scorer xgb:models/xgb_demo.json

Writes evals/reports/<world>-<timestamp>.json/.md plus a stable copy (schema: docs/eval-report.md):
``latest.json`` for the default heuristic scorer on showcase / demo / full, ``<world>.json`` for hard worlds
(``hard.json``, ``hard-101.json``; never ``latest.json``), ``<world>-<xgb|gnn>.json`` otherwise; ``--report-name``
overrides it.

``--scorer`` (repeatable): ``heuristic`` (heuristic-v2, default), ``xgb:<model.json>`` (``wapsi.classify.train``),
``gnn:<checkpoint dir>`` (``editions/amd/gnn``, needs the ``amd`` extra). The first scorer's run is the report body;
with several, ``by_scorer`` holds each scorer's headline metrics. Every run adds a ``classification`` block
(per-account MULE-vs-rest ROC-AUC of the score and the best single-feature AUC on the scorer-decided accounts).

Golden sets are read from ``data/golden/<world>/cases.jsonl``, falling back to
``data/generated/<world>/golden/cases.jsonl`` (where ``python -m wapsi.synth --size hard`` writes them).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from wapsi.config import WapsiConfig
from wapsi.models import (
    HOLDING_DISPOSITIONS,
    Disposition,
    GoldenCase,
    Role,
    TraceTotals,
    legacy_role,
)
from wapsi.money import eq, find_amounts, fmt_inr, r2
from wapsi.timeutil import utcnow

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "evals" / "reports"
TOTAL_FIELDS = list(TraceTotals.model_fields)
ROLES_SCORED = [Role.MULE, Role.INNOCENT_HOLDER, Role.MERCHANT, Role.INTERMEDIARY]


# scorer-decided roles: merchants and nodal / pool / escrow accounts are decided by hard rules on account type
SCORED_TRUTH = {Role.MULE, Role.INNOCENT_HOLDER, Role.VICTIM}


def golden_path(world: str, world_dir: Path | None = None) -> Path:
    """data/golden/<world>/cases.jsonl, else <world dir>/golden/cases.jsonl (hard worlds keep it there)."""
    committed = ROOT / "data" / "golden" / world / "cases.jsonl"
    local = (world_dir or ROOT / "data" / "generated" / world) / "golden" / "cases.jsonl"
    if world_dir is None and committed.exists():
        return committed
    return local if local.exists() else committed


def load_golden(world: str, world_dir: Path | None = None) -> list[GoldenCase]:
    path = golden_path(world, world_dir)
    if not path.exists():
        size = "hard" if world.startswith("hard") else world
        raise SystemExit(f"golden set not found: {path} (run `uv run python -m wapsi.synth --size {size}`)")
    return [GoldenCase.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def load_ledger(world: str, world_dir: Path | None = None):
    if world == "showcase-memory":
        from wapsi.fixtures.showcase import showcase_ledger

        return showcase_ledger()
    from wapsi.adapters.parquet_ledger import ParquetLedger

    path = world_dir or ROOT / "data" / "generated" / world
    if not path.exists():
        raise SystemExit(f"world not found: {path} (run `uv run python -m wapsi.synth --size {world} --out {path}`)")
    return ParquetLedger(path)


def make_llm(kind: str):
    from wapsi.adapters.llm_fake import FakeLLM

    if kind == "fake":
        return FakeLLM()
    if kind == "fake-hallucinate":
        return FakeLLM(hallucinate=True)
    if kind.startswith("openai:"):  # openai:<base_url>|<model>
        from wapsi.adapters.llm_openai_compat import OpenAICompatLLM

        base_url, model = kind.removeprefix("openai:").split("|", 1)
        return OpenAICompatLLM(base_url=base_url, model=model)
    raise SystemExit(f"unknown --llm {kind}")


def make_scorer(spec: str, ledger):
    """``heuristic`` | ``xgb:<model.json>`` | ``gnn:<checkpoint dir>`` -> a ``wapsi.classify.Scorer``."""
    from wapsi.classify import HeuristicScorer

    if spec in ("heuristic", "heuristic-v2"):
        return HeuristicScorer()
    kind, _, path = spec.partition(":")
    if kind == "xgb" and path:
        from wapsi.classify import XgbScorer

        return XgbScorer.load(path)
    if kind == "gnn" and path:
        try:
            from editions.amd.gnn.scorer import GnnScorer
        except ImportError as exc:  # torch is in the optional 'amd' extra
            raise SystemExit(f"--scorer {spec} needs the 'amd' extra (uv sync --extra amd): {exc}") from exc
        return GnnScorer.load(path, ledger=ledger)
    raise SystemExit(f"unknown --scorer {spec} (heuristic | xgb:<model.json> | gnn:<checkpoint dir>)")


def scorer_kind(spec: str) -> str:
    return "heuristic" if spec in ("heuristic", "heuristic-v2") else spec.partition(":")[0]


def _ratio(num: float, den: float) -> float:
    return round(num / den, 4) if den else 1.0


def _auc(labels: list[bool], scores: list[float]) -> float | None:
    """ROC-AUC (Mann-Whitney, ties averaged); None without both classes."""
    import pandas as pd

    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    ranks = pd.Series(scores, dtype=float).fillna(0.0).rank(method="average").to_numpy()
    rank_pos = sum(r for r, y in zip(ranks, labels, strict=True) if y)
    return round((rank_pos - pos * (pos + 1) / 2) / (pos * neg), 4)


def classification_summary(rows: list[tuple[Role, Role, float, dict]]) -> dict:
    """Per-account view of the scorer on the accounts it decides (truth MULE / INNOCENT_HOLDER / VICTIM).

    ``rows``: (expected role, predicted role, score, features). ``best_single_feature_auc`` is the best
    ``max(AUC, 1 - AUC)`` of any one feature for MULE vs rest: how far a single threshold gets on this world."""
    rows = [r for r in rows if r[0] in SCORED_TRUTH]
    is_mule = [r[0] == Role.MULE for r in rows]
    best, best_name = None, None
    names = sorted({k for r in rows for k in r[3]})
    for k in names:
        a = _auc(is_mule, [float(r[3].get(k, 0.0)) for r in rows])
        if a is not None and (best is None or max(a, 1 - a) > best):
            best, best_name = round(max(a, 1 - a), 4), k
    pred = Counter(r[1].value for r in rows)
    return {
        "accounts": len(rows),
        "truth": dict(sorted(Counter(r[0].value for r in rows).items())),
        "predicted": dict(sorted(pred.items())),
        "unknown_rate": _ratio(pred.get(Role.UNKNOWN.value, 0), len(rows)) if rows else 0.0,
        "mule_auc": _auc(is_mule, [r[2] for r in rows]),
        "best_single_feature_auc": best,
        "best_single_feature": best_name,
        "victim_on_trail_called_mule": sum(1 for r in rows if r[0] == Role.VICTIM and r[1] == Role.MULE),
    }


def llm_only_hallucination(bundle, ledger, llm) -> tuple[int, int]:
    """Baseline: ask the LLM for a narrative with raw numbers (no slot discipline) and check them against slots."""
    from wapsi.draft.factsheet import build_factsheet, build_slots

    facts = build_factsheet(bundle, ledger)
    slots = build_slots(bundle, ledger)
    allowed = {r2(v) for s in slots.values() for _, v in find_amounts(s)}
    allowed |= {r2(d.hold_amount) for d in bundle.plan.decisions} | {r2(d.tainted_amount) for d in bundle.plan.decisions}
    allowed |= {r2(e.amount) for e in bundle.trace.edges} | {r2(e.tainted_total) for e in bundle.trace.edges}
    prompt = [
        {"role": "system", "content": "You are a bank fraud analyst. Write a short case summary with the amounts."},
        {"role": "user", "content": json.dumps(facts, default=str)[:12000]},
    ]
    text = llm.complete(prompt, temperature=0.0, max_tokens=400)
    amounts = [v for _, v in find_amounts(text)]
    wrong = sum(1 for v in amounts if r2(v) not in allowed)
    return (1 if wrong else 0), 1


def evaluate(
    world: str,
    llm_kind: str = "fake",
    limit: int | None = None,
    draft: bool = True,
    scorer: str = "heuristic",
    world_dir: Path | None = None,
    ledger=None,
    llm=None,
) -> dict:
    """``llm``: a ready ``LLM`` to draft with instead of ``make_llm(llm_kind)`` (e.g. a metered endpoint client);
    ``llm_kind`` then only names it (a name starting with ``fake`` skips the LLM-only baseline)."""
    from wapsi.pipeline.run import process_case
    from wapsi.verify.injection import RulesGuard
    from wapsi.verify.report import label_of

    ledger = ledger if ledger is not None else load_ledger(world, world_dir)
    golden = load_golden(world if world != "showcase-memory" else "showcase", world_dir)
    model = make_scorer(scorer, ledger)
    if limit:
        golden = golden[:limit]
    llm = llm if llm is not None else make_llm(llm_kind)
    # the LLM-only number-hallucination baseline needs a real model; the offline FakeLLM only writes placeholders
    baseline_llm = None if llm_kind.startswith("fake") else llm
    search = None
    try:
        from wapsi.search.index import LocalDocSearch

        search = LocalDocSearch.load()
    except Exception:  # index not built yet
        search = None
    guard = RulesGuard()
    cfg = WapsiConfig()

    m: dict = defaultdict(float)
    counts: Counter = Counter()
    role_tp: Counter = Counter()
    role_fp: Counter = Counter()
    role_fn: Counter = Counter()
    per_typ: dict[str, Counter] = defaultdict(Counter)
    failures: list[dict] = []
    t_plan: list[float] = []
    t_total: list[float] = []
    base_freeze = wapsi_hold = 0.0
    base_accts = held_accts = 0
    halluc = halluc_n = 0
    cls_rows: list[tuple[Role, Role, float, dict]] = []

    for g in golden:
        t = time.perf_counter()
        bundle = process_case(ledger, g.ncrp_ids, llm=llm, cfg=cfg, search=search, guard=guard,
                              scorer=model, case_id=g.case_id, draft=draft)
        t_total.append(time.perf_counter() - t)
        timing = next((e.details.get("timings", {}) for e in bundle.audit if e.action == "case_processed"), {})
        t_plan.append(timing.get("time_to_hold_plan_s", t_total[-1]))
        trace, plan = bundle.trace, bundle.plan
        typ = per_typ[g.typology]
        typ["cases"] += 1
        counts["cases"] += 1

        # totals
        located = sum(getattr(trace.totals, f) for f in TOTAL_FIELDS if f not in ("stolen", "unlocated"))
        m["located"] += located
        m["stolen"] += trace.totals.stolen
        err = sum(abs(getattr(trace.totals, f) - getattr(g.expected_totals, f)) for f in TOTAL_FIELDS)
        m["totals_abs_error_inr"] += err
        if err > 0.05:
            failures.append({"case_id": g.case_id, "check": "totals", "detail": f"abs error ₹{err:,.2f}"})

        # holds & dispositions
        decisions = {d.account_id: d for d in plan.decisions}
        for acct in set(g.expected_hold) | {a for a, d in decisions.items() if d.hold_amount > 0}:
            exp = g.expected_hold.get(acct, 0.0)
            got = decisions[acct].hold_amount if acct in decisions else 0.0
            counts["hold_n"] += 1
            typ["hold_n"] += 1
            if eq(exp, got):
                counts["hold_ok"] += 1
                typ["hold_ok"] += 1
            else:
                failures.append({"case_id": g.case_id, "check": "hold", "detail": f"{acct}: got {got} expected {exp}"})
            m["over_hold_inr"] += max(0.0, got - exp)
            if got > 0.005 and exp <= 0.005:
                counts["false_freeze"] += 1
        for acct, exp_disp in g.expected_dispositions.items():
            got_disp = decisions[acct].disposition if acct in decisions else Disposition.NO_ACTION
            counts["disp_n"] += 1
            typ["disp_n"] += 1
            if got_disp == exp_disp:
                counts["disp_ok"] += 1
                typ["disp_ok"] += 1
            else:
                failures.append({"case_id": g.case_id, "check": "disposition",
                                 "detail": f"{acct}: got {got_disp} expected {exp_disp}"})
        counts["process_ok"] += int(plan.restoration_process == g.expected_process)
        counts["ground_ok"] += int(plan.str_ground == g.expected_str_ground)
        flagged = {d.account_id for d in plan.decisions if "ESCALATE_S106_CANDIDATE" in d.reason_codes}
        exp_flag = set(g.expected_escalation_candidates)
        counts["esc_tp"] += len(flagged & exp_flag)
        counts["esc_fp"] += len(flagged - exp_flag)
        counts["esc_fn"] += len(exp_flag - flagged)

        # roles (golden sets use the legacy INNOCENT_HOLDER label: both innocent-holder roles score as it)
        for acct, exp_role in g.expected_roles.items():
            exp_role = legacy_role(exp_role)
            got_role = bundle.classifications.get(acct).role if acct in bundle.classifications else Role.UNKNOWN
            got_role = legacy_role(got_role)
            if acct in bundle.classifications:
                c = bundle.classifications[acct]
                cls_rows.append((exp_role, legacy_role(c.role), float(c.score), dict(c.features)))
            for r in ROLES_SCORED:
                if got_role == r and exp_role == r:
                    role_tp[r] += 1
                elif got_role == r:
                    role_fp[r] += 1
                elif exp_role == r:
                    role_fn[r] += 1

        # baseline
        base_freeze += plan.baseline_freeze_amount
        base_accts += plan.baseline_freeze_accounts
        wapsi_hold += plan.total_hold
        held_accts += sum(1 for d in plan.decisions if d.disposition in HOLDING_DISPOSITIONS and d.hold_amount > 0)

        # drafts & verification
        for d in bundle.drafts:
            rep = bundle.verifications.get(d.draft_id)
            counts["drafts"] += 1
            if rep and rep.passed:
                counts["drafts_ok"] += 1
            elif rep:
                failures.append({"case_id": g.case_id, "check": "verification", "detail":
                                 f"{d.draft_id}: " + "; ".join(label_of(c) for c in rep.checks if not c.passed)[:300]})
            if rep:
                num_errors = [c for c in rep.checks if c.name == "numeric" and not c.passed and c.severity == "error"]
                counts["numeric_ok"] += int(not num_errors)
                cit_errors = [c for c in rep.checks if c.name == "citations" and not c.passed and c.severity == "error"]
                counts["citation_ok"] += int(not cit_errors)
            if d.structured.get("llm_fallback"):
                counts["prose_fallback"] += 1

        # injection
        if g.is_adversarial:
            counts["adv"] += 1
            if any(e.action == "untrusted_instruction_detected" for e in bundle.audit):
                counts["adv_flagged"] += 1
            for c in bundle.complaints:
                spans = guard.screen(c.narrative_text).quoted_spans
                for d in bundle.drafts:
                    if any(s and s in d.body for s in spans):
                        counts["leaks"] += 1

        if draft and baseline_llm is not None:
            try:
                h, n = llm_only_hallucination(bundle, ledger, baseline_llm)
                halluc += h
                halluc_n += n
            except Exception as exc:  # baseline is best-effort
                failures.append({"case_id": g.case_id, "check": "llm_baseline", "detail": str(exc)[:200]})

    def pr(tp, fp, fn):
        return _ratio(tp, tp + fp), _ratio(tp, tp + fn)

    esc_p, esc_r = pr(counts["esc_tp"], counts["esc_fp"], counts["esc_fn"])
    metrics = {
        "traced_amount_recall": _ratio(m["located"], m["stolen"]),
        "totals_abs_error_inr": round(m["totals_abs_error_inr"], 2),
        "hold_exact_match_rate": _ratio(counts["hold_ok"], counts["hold_n"]),
        "over_hold_inr": round(m["over_hold_inr"], 2),
        "false_freeze_count": counts["false_freeze"],
        "disposition_accuracy": _ratio(counts["disp_ok"], counts["disp_n"]),
        "process_accuracy": _ratio(counts["process_ok"], counts["cases"]),
        "str_ground_accuracy": _ratio(counts["ground_ok"], counts["cases"]),
        "escalation_precision": esc_p,
        "escalation_recall": esc_r,
        "role_precision": {r.value: pr(role_tp[r], role_fp[r], role_fn[r])[0] for r in ROLES_SCORED},
        "role_recall": {r.value: pr(role_tp[r], role_fp[r], role_fn[r])[1] for r in ROLES_SCORED},
        "draft_verification_pass_rate": _ratio(counts["drafts_ok"], counts["drafts"]),
        "draft_numeric_accuracy": _ratio(counts["numeric_ok"], counts["drafts"]),
        "citation_validity": _ratio(counts["citation_ok"], counts["drafts"]),
        "llm_only_hallucination_rate": _ratio(halluc, halluc_n) if halluc_n else None,
        "llm_prose_fallback_rate": _ratio(counts["prose_fallback"], counts["drafts"]),
        "injection_flag_recall": _ratio(counts["adv_flagged"], counts["adv"]),
        "injection_leaks": counts["leaks"],
        "p50_time_to_hold_plan_s": round(statistics.median(t_plan), 4) if t_plan else None,
        "p95_time_to_hold_plan_s": round(_pct(t_plan, 95), 4) if t_plan else None,
        "p95_total_s": round(_pct(t_total, 95), 4) if t_total else None,
    }
    return {
        "world": world,
        "generated_at": utcnow().isoformat(),
        "config": {"rule": cfg.trace.rule.value, "llm": getattr(llm, "name", llm_kind),
                   "scorer": getattr(model, "name", scorer), "scorer_spec": scorer, "search": search is not None},
        "cases": counts["cases"],
        "adversarial_cases": counts["adv"],
        "metrics": metrics,
        "baseline": {"whole_account_freeze_inr": round(base_freeze, 2), "wapsi_hold_inr": round(wapsi_hold, 2),
                     "accounts_frozen_baseline": base_accts, "accounts_held": held_accts},
        "per_typology": {
            k: {"cases": v["cases"], "hold_exact_match_rate": _ratio(v["hold_ok"], v["hold_n"]),
                "disposition_accuracy": _ratio(v["disp_ok"], v["disp_n"])}
            for k, v in sorted(per_typ.items())
        },
        "classification": classification_summary(cls_rows),
        "failures": failures[:200],
    }


HEADLINE = [
    "hold_exact_match_rate", "totals_abs_error_inr", "over_hold_inr", "false_freeze_count", "disposition_accuracy",
    "str_ground_accuracy", "escalation_precision", "escalation_recall", "role_precision", "role_recall",
    "draft_verification_pass_rate", "draft_numeric_accuracy", "citation_validity",
]


def scorer_summary(rep: dict) -> dict:
    """Headline metrics of one run (for ``by_scorer``)."""
    return {"scorer": rep["config"]["scorer"], "scorer_spec": rep["config"].get("scorer_spec"),
            **{k: rep["metrics"][k] for k in HEADLINE}, "classification": rep["classification"]}


def report_name(world: str, scorer: str) -> str:
    if world.startswith("hard"):
        return world
    return "latest" if scorer_kind(scorer) == "heuristic" else f"{world}-{scorer_kind(scorer)}"


def _pct(values: list[float], p: float) -> float:
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return s[k]


def to_markdown(rep: dict) -> str:
    mt = rep["metrics"]
    lines = [f"# Wapsi evaluation — {rep['world']}", "", f"Generated {rep['generated_at']} · {rep['cases']} cases "
             f"({rep['adversarial_cases']} adversarial) · LLM `{rep['config']['llm']}`", "", "| Metric | Value |",
             "|---|---|"]
    for k, v in mt.items():
        lines.append(f"| {k} | {json.dumps(v)} |")
    b = rep["baseline"]
    lines += ["", "## Whole-account freeze baseline vs Wapsi", "",
              f"- Whole-account freeze baseline (comparison only): {fmt_inr(b['whole_account_freeze_inr'])} across "
              f"{b['accounts_frozen_baseline']} accounts",
              f"- Wapsi holds {fmt_inr(b['wapsi_hold_inr'])} across {b['accounts_held']} accounts", "",
              "## Per typology", "", "| Typology | Cases | Hold exact | Disposition acc |", "|---|---|---|---|"]
    for k, v in rep["per_typology"].items():
        lines.append(f"| {k} | {v['cases']} | {v['hold_exact_match_rate']} | {v['disposition_accuracy']} |")
    if rep.get("classification"):
        c = rep["classification"]
        lines += ["", "## Classification (scorer-decided accounts)", "",
                  f"- {c['accounts']} accounts, truth {c['truth']}, predicted {c['predicted']}",
                  f"- MULE-vs-rest ROC-AUC {c['mule_auc']}; best single feature {c['best_single_feature']} "
                  f"(AUC {c['best_single_feature_auc']}); sent to officer (UNKNOWN) {c['unknown_rate']}"]
    if rep.get("by_scorer"):
        lines += ["", "## By scorer", "", "| Scorer | MULE P | MULE R | INNOCENT P | INNOCENT R | Disposition acc | "
                  "Hold exact | MULE AUC |", "|---|---|---|---|---|---|---|---|"]
        for name, v in rep["by_scorer"].items():
            p, r = v["role_precision"], v["role_recall"]
            lines.append(f"| {name} | {p['MULE']} | {r['MULE']} | {p['INNOCENT_HOLDER']} | {r['INNOCENT_HOLDER']} | "
                         f"{v['disposition_accuracy']} | {v['hold_exact_match_rate']} | "
                         f"{v['classification']['mule_auc']} |")
    if rep["failures"]:
        lines += ["", f"## Failures (first {min(30, len(rep['failures']))})", ""]
        lines += [f"- `{f['case_id']}` {f['check']}: {f['detail']}" for f in rep["failures"][:30]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # ₹ on Windows cp1252 consoles
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--world", default="demo")
    ap.add_argument("--llm", default="fake")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-draft", action="store_true")
    ap.add_argument("--out", default=str(REPORTS))
    ap.add_argument("--quick", action="store_true", help="first 10 cases, for hooks/CI")
    ap.add_argument("--scorer", action="append", default=None,
                    help="heuristic (default) | xgb:<model.json> | gnn:<checkpoint dir>; repeat to compare")
    ap.add_argument("--world-dir", default=None, help="world directory (default data/generated/<world>)")
    ap.add_argument("--report-name", default=None,
                    help="stable report file stem (default: latest | <world> for hard worlds | <world>-<kind>)")
    args = ap.parse_args(argv)
    scorers = args.scorer or ["heuristic"]
    world_dir = Path(args.world_dir) if args.world_dir else None
    limit = 10 if args.quick else args.limit
    ledger = load_ledger(args.world, world_dir)
    runs = [evaluate(args.world, args.llm, limit=limit, draft=not args.no_draft, scorer=s, world_dir=world_dir,
                     ledger=ledger) for s in scorers]
    rep = runs[0]
    if len(runs) > 1:
        rep["by_scorer"] = {r["config"]["scorer"]: scorer_summary(r) for r in runs}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    (out / f"{args.world}-{stamp}.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    (out / f"{args.world}-{stamp}.md").write_text(to_markdown(rep), encoding="utf-8")
    name = args.report_name or report_name(args.world, scorers[0])
    (out / f"{name}.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(to_markdown(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
