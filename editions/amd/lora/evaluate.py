"""Base vs fine-tuned (vs teacher) on Wapsi's prose tasks for held-out cases.

    # event week: base and merged fine-tune served by vLLM on the MI300X, Gemini as an external reference
    python -m editions.amd.lora.evaluate \
        --model "base=openai:http://localhost:8000/v1|google/gemma-4-31B-it" \
        --model "tuned=openai:http://localhost:8001/v1|wapsi-gemma4-31b-lora" \
        --teacher "openai:https://generativelanguage.googleapis.com/v1beta/openai|gemini-2.5-flash|GEMINI_API_KEY"

    # offline: a local HF checkpoint and the FakeLLM behind the real OpenAI-compatible client
    uv run python -m editions.amd.lora.evaluate --hf models/lora/tiny-merged --model fake=mock:fake --limit-cases 3

Each held-out case (worlds with generator seeds never used for training) is drafted by the production drafter with
the model under test -- same request, same retry-with-feedback, same deterministic fallback -- in three variants:
the clean case, a complaint with tempting raw numbers, and a complaint with an injected instruction (test-only
templates, unseen in training). Metrics per model (overall, per task, per variant):

- ``acceptance_rate``: prose accepted on the first attempt (no retry, no fallback);
  ``accepted_after_retry_rate``, ``fallback_rate``, ``llm_error_rate``;
- ``prose_verifier_pass_rate`` (drafts carrying prose) and ``all_drafts_verifier_pass_rate``;
- ``placeholder_coverage``: share of the task's key facts mentioned as placeholders in the first answer;
- first-answer violations: ``invented_number_rate`` (digits, number words or amounts / accounts / UTRs that are not
  offered values), ``forbidden_token_rate`` (digits, currency words, number words), ``injection_obeyed_rate``
  (canary phrase or copied complaint text), per issue code rates, ``json_valid_rate``;
- style: ``str_ground_named_rate`` (Rule 2(1)(g) ground named), ``length_in_range_rate``;
- ``latency_s`` per call (mean / p50 / p95).

Writes ``eval.json`` and ``eval.md`` (small) to ``--out``; ``--details`` writes per-request rows (JSONL).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from editions.amd.lora.common import (
    ALL_CODES,
    FORBIDDEN_CODES,
    NUMBER_CODES,
    RESULTS,
    TASKS,
    VARIANTS,
    ProseRecord,
    canary_hits,
    describe,
    discover_heldout_worlds,
    ground_named,
    is_json_answer,
    issue_codes,
    key_coverage,
    length_in_range,
    load_search,
    load_world,
    make_llm,
    make_variant,
    obeyed_injection,
    plan_case,
    ratio,
    redact_spec,
    run_case,
    split_list,
    word_count,
    write_json,
    write_jsonl,
)

SCHEMA = "wapsi.lora.eval/1"
HEADLINE = [
    "acceptance_rate",
    "accepted_after_retry_rate",
    "fallback_rate",
    "prose_verifier_pass_rate",
    "all_drafts_verifier_pass_rate",
    "placeholder_coverage",
    "invented_number_rate",
    "forbidden_token_rate",
    "injection_obeyed_rate",
    "json_valid_rate",
    "str_ground_named_rate",
    "length_in_range_rate",
    "llm_error_rate",
    "latency_s_mean",
    "latency_s_p95",
]


def request_row(
    rec: ProseRecord, *, model: str, world: str, case_id: str, variant: str, canaries: list[str]
) -> dict[str, Any]:
    text = rec.first_text
    codes = (
        issue_codes(text, rec.offered, rec.narratives) if rec.first_response is not None else ["no_answer"]
    )
    cov, _, missing = key_coverage(rec.task, text, rec.offered)
    hits = canary_hits(text)
    return {
        "model": model,
        "world": world,
        "case_id": case_id,
        "variant": variant,
        "task": rec.task,
        "account_id": rec.account_id,
        "llm_error": rec.first_response is None,
        "accepted_first_try": (not rec.fallback) and rec.llm_attempts == 1,
        "accepted_after_retry": (not rec.fallback) and rec.llm_attempts > 1,
        "fallback": rec.fallback,
        "verifier_passed": rec.verifier_passed,
        "verifier_errors": rec.verifier_errors,
        "json_valid": is_json_answer(rec.first_response),
        "codes": codes,
        "coverage": cov if cov is not None else (1.0 if rec.first_response is not None else 0.0),
        "missing_facts": missing,
        "words": word_count(text),
        "length_ok": length_in_range(rec.task, text),
        "ground_named": ground_named(text, rec.ground) if rec.task == "str_grounds" else None,
        "injection_obeyed": bool(text) and obeyed_injection(text, rec.narratives),
        "canary_hits": hits,
        "canary_expected": bool(canaries),
        "latencies": [round(c.latency_s, 4) for c in rec.calls],
        "first_chars": len(rec.first_response or ""),
        "unaligned": rec.unaligned,
    }


def aggregate(rows: list[dict[str, Any]], draft_pass: list[bool] | None = None) -> dict[str, Any]:
    n = len(rows)
    lat = [x for r in rows for x in r["latencies"]]
    str_rows = [r for r in rows if r["task"] == "str_grounds"]
    code_rate = {c: ratio(sum(1 for r in rows if c in r["codes"]), n) for c in ALL_CODES}
    out = {
        "requests": n,
        "acceptance_rate": ratio(sum(r["accepted_first_try"] for r in rows), n),
        "accepted_after_retry_rate": ratio(sum(r["accepted_after_retry"] for r in rows), n),
        "fallback_rate": ratio(sum(r["fallback"] for r in rows), n),
        "llm_error_rate": ratio(sum(r["llm_error"] for r in rows), n),
        "prose_verifier_pass_rate": ratio(sum(r["verifier_passed"] for r in rows), n),
        "placeholder_coverage": round(sum(r["coverage"] for r in rows) / n, 4) if n else None,
        "invented_number_rate": ratio(sum(any(c in r["codes"] for c in NUMBER_CODES) for r in rows), n),
        "forbidden_token_rate": ratio(sum(any(c in r["codes"] for c in FORBIDDEN_CODES) for r in rows), n),
        "injection_obeyed_rate": ratio(sum(r["injection_obeyed"] for r in rows), n),
        "canary_rate_on_injected": ratio(
            sum(bool(r["canary_hits"]) for r in rows if r["canary_expected"]),
            sum(1 for r in rows if r["canary_expected"]),
        ),
        "json_valid_rate": ratio(sum(r["json_valid"] for r in rows), n),
        "str_ground_named_rate": ratio(sum(bool(r["ground_named"]) for r in str_rows), len(str_rows)),
        "length_in_range_rate": ratio(sum(r["length_ok"] for r in rows), n),
        "issue_code_rates": code_rate,
        "latency_s_mean": describe(lat)["mean"],
        "latency_s_p50": describe(lat)["p50"],
        "latency_s_p95": describe(lat)["p95"],
        "calls": len(lat),
        "first_answer_chars_mean": round(sum(r["first_chars"] for r in rows) / n, 1) if n else None,
    }
    if draft_pass is not None:
        out["drafts"] = len(draft_pass)
        out["all_drafts_verifier_pass_rate"] = ratio(sum(draft_pass), len(draft_pass))
    return out


def to_markdown(rep: dict[str, Any]) -> str:
    models = list(rep["models"])
    lines = [
        "# Wapsi prose fine-tune: evaluation",
        "",
        f"Generated {rep['generated_at']} - {rep['cases']} held-out cases from {', '.join(rep['worlds'])} - "
        f"variants {', '.join(rep['variants'])}. Measured values from this run only.",
        "",
        "| Metric | " + " | ".join(models) + " |",
        "|---|" + "---|" * len(models),
    ]
    for key in HEADLINE:
        vals = [rep["models"][m]["overall"].get(key) for m in models]
        lines.append(f"| {key} | " + " | ".join("n/a" if v is None else f"{v}" for v in vals) + " |")
    lines += [
        "",
        "## Acceptance rate by variant",
        "",
        "| Variant | " + " | ".join(models) + " |",
        "|---|" + "---|" * len(models),
    ]
    for v in rep["variants"]:
        vals = [rep["models"][m]["by_variant"].get(v, {}).get("acceptance_rate") for m in models]
        lines.append(f"| {v} | " + " | ".join("n/a" if x is None else f"{x}" for x in vals) + " |")
    lines += [
        "",
        "## Acceptance rate by task",
        "",
        "| Task | " + " | ".join(models) + " |",
        "|---|" + "---|" * len(models),
    ]
    for t in TASKS:
        vals = [rep["models"][m]["by_task"].get(t, {}).get("acceptance_rate") for m in models]
        lines.append(f"| {t} | " + " | ".join("n/a" if x is None else f"{x}" for x in vals) + " |")
    lines += ["", "Specs: " + "; ".join(f"`{m}` = `{rep['models'][m]['spec']}`" for m in models), ""]
    return "\n".join(lines)


def parse_models(args: argparse.Namespace) -> list[tuple[str, str]]:
    models: list[tuple[str, str]] = []
    for item in args.model or []:
        label, sep, spec = item.partition("=")
        if not sep or not label or not spec:
            raise SystemExit(f"--model must be LABEL=SPEC, got {item!r}")
        models.append((label, spec))
    for label, spec in (("base", args.base), ("tuned", args.tuned), ("teacher", args.teacher)):
        if spec:
            models.append((label, spec))
    for path in args.hf or []:
        models.append((f"hf-{Path(path).name}", f"hf:{path}"))
    labels = [m[0] for m in models]
    if not models:
        raise SystemExit("give at least one --model / --base / --tuned / --teacher / --hf")
    if len(set(labels)) != len(labels):
        raise SystemExit(f"duplicate model labels: {labels}")
    return models


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    t0 = time.perf_counter()
    models = parse_models(args)
    variants = split_list(args.variants)
    if not variants or any(v not in VARIANTS for v in variants):
        raise SystemExit(f"--variants must be a subset of {VARIANTS}")
    specs = discover_heldout_worlds() if args.heldout_worlds == "auto" else split_list(args.heldout_worlds)
    if not specs:
        raise SystemExit("no held-out worlds (generate data/generated/heldout-* or pass --heldout-worlds)")
    worlds = [load_world(s, "heldout", allow_unverified=args.allow_unverified_world) for s in specs]
    search = load_search()

    jobs = []  # (world, case_id, variant, bundle, info)
    for w in worlds:
        for case_id, ncrp_ids in w.cases[: args.limit_cases] if args.limit_cases else w.cases:
            bundle = plan_case(w, case_id, ncrp_ids)
            for v in variants:
                vb, info = make_variant(bundle, w.ledger, v, "test", f"{args.seed}|{w.name}|{case_id}")
                jobs.append((w, case_id, v, vb, info))
    n_cases = len({(j[0].name, j[1]) for j in jobs})
    print(f"[eval] {n_cases} held-out cases x {len(variants)} variants from {[w.name for w in worlds]}")

    kw = {
        "timeout": args.timeout,
        "device": args.device,
        "dtype": args.dtype,
        "max_new_tokens": args.max_new_tokens,
        "chat_template_kwargs": args.chat_template_kwargs,
    }
    report_models: dict[str, Any] = {}
    all_rows: list[dict[str, Any]] = []
    for label, spec in models:
        llm = make_llm(spec, name=label, **kw)
        t = time.perf_counter()

        def one(job, llm=llm, label=label):
            w, case_id, variant, vb, info = job
            try:
                records, passed = run_case(vb, w.ledger, llm, search=search)
            except Exception as exc:  # noqa: BLE001 - count, never crash the comparison
                return [], [], f"{type(exc).__name__}: {str(exc)[:200]}"
            rows = [
                request_row(
                    r, model=label, world=w.name, case_id=case_id, variant=variant, canaries=info.canaries
                )
                for r in records
            ]
            return rows, list(passed.values()), None

        if args.workers > 1:
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                results = list(ex.map(one, jobs))
        else:
            results = [one(j) for j in jobs]
        rows = [r for res in results for r in res[0]]
        draft_pass = [p for res in results for p in res[1]]
        errors = [res[2] for res in results if res[2]]
        by_variant = {v: aggregate([r for r in rows if r["variant"] == v]) for v in variants}
        by_task = {
            tk: aggregate([r for r in rows if r["task"] == tk])
            for tk in TASKS
            if any(r["task"] == tk for r in rows)
        }
        report_models[label] = {
            "spec": redact_spec(spec),
            "name": getattr(llm, "name", label),
            "overall": aggregate(rows, draft_pass),
            "by_variant": by_variant,
            "by_task": by_task,
            "case_errors": len(errors),
            "case_error_examples": errors[:3],
            "seconds": round(time.perf_counter() - t, 1),
        }
        ov = report_models[label]["overall"]
        print(
            f"[eval] {label}: acceptance={ov['acceptance_rate']} fallback={ov['fallback_rate']} "
            f"verifier={ov['prose_verifier_pass_rate']} coverage={ov['placeholder_coverage']} "
            f"invented={ov['invented_number_rate']} injection={ov['injection_obeyed_rate']} "
            f"({report_models[label]['seconds']}s)"
        )
        all_rows.extend(rows)
        close = getattr(llm, "close", None)
        if callable(close):
            close()

    report = {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "Measured by this run. Fake / tiny models only check the pipeline; they are not model results.",
        "worlds": [w.name for w in worlds],
        "world_seeds": {w.name: w.seed for w in worlds},
        "cases": n_cases,
        "variants": variants,
        "injection_templates": "test pool (never used in training)",
        "params": {
            "limit_cases": args.limit_cases,
            "max_new_tokens": args.max_new_tokens,
            "workers": args.workers,
            "seed": args.seed,
        },
        "metric_definitions": {
            "acceptance_rate": "first answer passes the drafter's checks (no retry, no fallback)",
            "placeholder_coverage": "mean share of the task's key facts present as placeholders in the first answer",
            "invented_number_rate": "first answer has raw digits, number words, or amounts/accounts/UTRs not offered",
            "forbidden_token_rate": "first answer has digits, currency words/symbols or number words",
            "injection_obeyed_rate": "first answer contains an injected canary phrase or copies complaint text",
            "prose_verifier_pass_rate": "final draft carrying the prose passes verify_draft",
        },
        "models": report_models,
        "seconds": round(time.perf_counter() - t0, 1),
    }
    out = Path(args.out)
    write_json(out / "eval.json", report)
    (out / "eval.md").write_text(to_markdown(report), encoding="utf-8")
    if args.details:
        write_jsonl(Path(args.details), all_rows)
    return report


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--model", action="append", help="LABEL=SPEC (repeatable); SPEC as in --teacher of dataset.py"
    )
    ap.add_argument("--base", default=None, help="SPEC of the base model (label 'base')")
    ap.add_argument("--tuned", default=None, help="SPEC of the merged fine-tune (label 'tuned')")
    ap.add_argument(
        "--teacher", default=None, help="SPEC of a reference model, e.g. Gemini (label 'teacher')"
    )
    ap.add_argument("--hf", action="append", help="local HF checkpoint path (repeatable)")
    ap.add_argument("--heldout-worlds", default="auto")
    ap.add_argument("--limit-cases", type=int, default=None, help="first N cases per held-out world")
    ap.add_argument("--variants", default="clean,tempting,injection")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--dtype", default="auto")
    ap.add_argument("--max-new-tokens", type=int, default=700)
    ap.add_argument("--chat-template-kwargs", default=None, help="JSON for local HF models")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--workers", type=int, default=1, help="parallel cases per model (endpoints)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--out", default=None, help="output dir (default editions/amd/lora/results/eval-<UTC stamp>)"
    )
    ap.add_argument("--details", default=None, help="per-request JSONL (put it under data/generated/)")
    ap.add_argument("--allow-unverified-world", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if isinstance(args.chat_template_kwargs, str):
        args.chat_template_kwargs = json.loads(args.chat_template_kwargs)
    if args.out is None:
        args.out = str(RESULTS / f"eval-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    rep = evaluate(args)
    print(f"[eval] wrote {Path(args.out) / 'eval.json'} and eval.md ({rep['seconds']}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
