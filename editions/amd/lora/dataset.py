"""Build the LoRA SFT dataset for Wapsi's prose fields by verifier-filtered distillation.

    uv run python -m editions.amd.lora.dataset --teacher fake                       # offline pipeline check
    uv run python -m editions.amd.lora.dataset --teacher "openai:http://localhost:8000/v1|google/gemma-4-31B-it" \
        --samples 2 --workers 16                                                      # event week, vLLM teacher

For every case of every synthetic world the pipeline is run with ``draft=False`` (trace, classify, plan), then the
real drafter (``wapsi.draft.drafter.draft_case``) is run with the TEACHER behind a recording wrapper, so each
request is byte-for-byte what production sends (STR grounds, EDD rationale, case summary). A teacher answer becomes
a training target only if:

1. the drafter accepted it (``validate_prose``: placeholders only, no digits / currency / number words, no copied
   complaint text, ...) without falling back, and the independent re-check agrees;
2. the substituted draft passes ``wapsi.verify.verify_draft`` (numbers, identifiers, citations, policy, injection);
3. it does not reproduce an injected instruction (canary phrases, copied complaint text);
4. it mentions the key facts (placeholder coverage >= ``--min-coverage``), names the Rule 2(1)(g) ground (STR) and
   stays near the requested length.

Hard negatives: each case is also drafted with a *tempting* complaint (wrong totals in digits and words, Hinglish
amounts, stray account numbers / UTRs / dates) and with an *injected instruction*; the kept targets still use
placeholders only and ignore the instruction. Corrections: when the teacher needed the drafter's retry, the
two-turn exchange is kept; a share of accepted answers also gets a synthetic wrong first answer plus the drafter's
exact feedback message (loss is computed on the final answer only).

Splits are by CASE and deterministic: test = held-out worlds only (different generator seeds); train/val = a hash
of ``world:case_id``. Outputs: ``train.jsonl`` / ``val.jsonl`` / ``test.jsonl`` (chat ``messages``),
``rejections.jsonl`` and ``manifest.json``. All inputs are synthetic (``meta.generator_version`` is checked).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from editions.amd.lora.common import (
    GENERATED,
    ROOT,
    SHOWCASE_MEMORY,
    TASKS,
    TEACHER_STYLE_GUIDE,
    VARIANTS,
    ProseRecord,
    VariantInfo,
    World,
    bucket,
    corrupt,
    describe,
    discover_heldout_worlds,
    feedback_message,
    ground_named,
    issue_codes,
    key_coverage,
    length_in_range,
    llm_slug,
    load_search,
    load_world,
    make_llm,
    make_variant,
    obeyed_injection,
    plan_case,
    ratio,
    redact_spec,
    run_case,
    sha256_file,
    sha256_text,
    split_list,
    word_count,
    write_json,
    write_jsonl,
)

SCHEMA = "wapsi.lora.dataset/1"
SPLITS = ("train", "val", "test")
_SECRET_RE = re.compile(
    r"(?:Bearer\s+[A-Za-z0-9._-]{12,}|\bsk-[A-Za-z0-9]{16,}|\bAIza[0-9A-Za-z_-]{20,}|\bhf_[A-Za-z0-9]{20,})"
)

FILTERS = [
    "drafter_accepted: validate_prose passed without falling back (retry allowed; re-checked independently)",
    "verifier_passed: verify_draft on the substituted draft has no blocking error",
    "injection_ignored: no canary phrase and no copied complaint text in the answer",
    "coverage: key-fact placeholder coverage >= min_coverage",
    "ground_named: STR grounds name the Rule 2(1)(g) ground in words",
    "length: word count within the task range widened by length_slack",
    "scrub: no secret-like tokens; synthetic worlds only (generator_version checked)",
    "dedupe: identical message lists kept once",
]


@dataclass
class Job:
    world: World
    case_id: str
    split: str
    variant: str
    sample: int
    bundle: Any
    info: VariantInfo


def _split_for(world: World, case_id: str, val_frac: float) -> str:
    if world.role == "heldout":
        return "test"
    return "val" if bucket(f"{world.name}:{case_id}") < int(val_frac * 10_000) else "train"


def judge(rec: ProseRecord, *, min_coverage: float, length_slack: float) -> list[str]:
    """Rejection reasons for a teacher answer (empty list = keep)."""
    reasons: list[str] = []
    if rec.unaligned:
        reasons.append("unaligned_call")
    if not rec.calls or all(c.error for c in rec.calls):
        reasons.append("llm_error")
    if rec.fallback:
        reasons.append("drafter_fallback")
    if not rec.verifier_passed:
        reasons.append("verifier_failed")
    text = rec.accepted_text
    if text is None:
        return reasons or ["no_answer"]
    if issue_codes(text, rec.offered, rec.narratives):
        reasons.append("prose_recheck_failed")
    if obeyed_injection(text, rec.narratives):
        reasons.append("injection_obeyed")
    cov, _, _ = key_coverage(rec.task, text, rec.offered)
    if cov is not None and cov < min_coverage:
        reasons.append("low_coverage")
    if rec.task == "str_grounds" and not ground_named(text, rec.ground):
        reasons.append("ground_not_named")
    if not length_in_range(rec.task, text, slack=length_slack):
        reasons.append("length_out_of_range")
    return reasons


def _answer(text: str) -> str:
    return json.dumps({"text": text}, ensure_ascii=False)


def examples_for(
    rec: ProseRecord, job: Job, *, teacher: str, corrections_frac: float, seed: int
) -> list[dict[str, Any]]:
    text = rec.accepted_text or ""
    target = {"role": "assistant", "content": _answer(text)}
    cov, keys, missing = key_coverage(rec.task, text, rec.offered)
    base_meta = {
        "prompt_chars": sum(len(m["content"]) for m in rec.request),
        "target_words": word_count(text),
        "coverage": cov,
        "key_facts": keys,
        "missing_facts": missing,
        "placeholders_offered": len(rec.offered),
        "teacher_attempts": rec.llm_attempts,
        "verifier_passed": rec.verifier_passed,
        "canaries": job.info.canaries,
    }
    common = {
        "split": job.split,
        "world": job.world.name,
        "case_id": job.case_id,
        "task": rec.task,
        "account_id": rec.account_id,
        "variant": job.variant,
        "sample": job.sample,
        "teacher": teacher,
    }

    def make(source: str, messages: list[dict[str, str]], **extra: Any) -> dict[str, Any]:
        rid = sha256_text(
            json.dumps(
                [job.world.name, job.case_id, job.variant, job.sample, rec.task, rec.account_id, source]
            )
        )[:16]
        return {"id": rid, **common, "source": source, "messages": messages, "meta": {**base_meta, **extra}}

    out = [
        make("teacher_first_try" if rec.llm_attempts == 1 else "teacher_after_retry", [*rec.request, target])
    ]
    if job.split == "test":
        return out
    if (
        rec.llm_attempts == 2
        and len(rec.calls) >= 2
        and rec.calls[0].response
        and len(rec.calls[1].messages) >= 4
    ):
        fb = rec.calls[1].messages[3]["content"]
        bad = {"role": "assistant", "content": rec.calls[0].response}
        out.append(
            make("teacher_retry_correction", [*rec.request, bad, {"role": "user", "content": fb}, target])
        )
    key = f"{seed}|{job.world.name}|{job.case_id}|{job.variant}|{job.sample}|{rec.task}|{rec.account_id}"
    if bucket(key + "|corr") < int(corrections_frac * 10_000):
        rng = random.Random(bucket(key, 2**31))
        injected = job.info.inserted if job.variant == "injection" else ""
        bad_kind = corrupt(text, rec.offered, rec.narratives, rng, injected=injected)
        if bad_kind is not None:
            bad_text, kind = bad_kind
            fb = feedback_message(bad_text, rec.offered, rec.narratives)
            messages = [
                *rec.request,
                {"role": "assistant", "content": _answer(bad_text)},
                {"role": "user", "content": fb},
                target,
            ]
            out.append(make("synthetic_correction", messages, corruption=kind))
    return out


def _count_table(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by = {k: Counter(r[k] for r in rows) for k in ("task", "variant", "source")}
    tv = Counter(f"{r['task']}/{r['variant']}" for r in rows)
    return {
        "examples": len(rows),
        "cases": len({f"{r['world']}:{r['case_id']}" for r in rows}),
        "worlds": dict(Counter(r["world"] for r in rows)),
        "by_task": dict(by["task"]),
        "by_variant": dict(by["variant"]),
        "by_source": dict(by["source"]),
        "by_task_variant": dict(sorted(tv.items())),
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    t0 = time.perf_counter()
    train_specs = split_list(args.train_worlds)
    heldout_specs = (
        discover_heldout_worlds() if args.heldout_worlds == "auto" else split_list(args.heldout_worlds)
    )
    overlap = set(train_specs) & set(heldout_specs)
    if overlap:
        raise SystemExit(f"worlds cannot be both train and held-out: {sorted(overlap)}")
    variants = split_list(args.variants)
    bad = [v for v in variants if v not in VARIANTS]
    if bad or not variants:
        raise SystemExit(f"--variants must be a subset of {VARIANTS}, got {variants}")

    worlds: list[World] = []
    for spec, role in [*[(s, "train") for s in train_specs], *[(s, "heldout") for s in heldout_specs]]:
        t = time.perf_counter()
        w = load_world(spec, role, allow_unverified=args.allow_unverified_world)
        worlds.append(w)
        print(
            f"[dataset] world {w.name} ({role}, seed {w.seed}): {len(w.cases)} cases, {time.perf_counter() - t:.1f}s"
        )
    train_seeds = {w.seed for w in worlds if w.role == "train"}
    held_seeds = {w.seed for w in worlds if w.role == "heldout"}
    if train_seeds & held_seeds and SHOWCASE_MEMORY not in heldout_specs:
        raise SystemExit(
            f"held-out worlds must use generator seeds unseen in training: shared {train_seeds & held_seeds}"
        )

    style = args.style_guide == "on" or (args.style_guide == "auto" and not args.teacher.startswith("fake"))
    suffix = TEACHER_STYLE_GUIDE if style else ""
    shared_teacher = None if args.teacher.startswith("fake") else make_llm(args.teacher, timeout=args.timeout)
    teacher_name = getattr(shared_teacher or make_llm(args.teacher), "name", args.teacher)
    search = load_search()

    # plan every case (CPU), then fan out drafting jobs (teacher-bound)
    jobs: list[Job] = []
    rejections: list[dict[str, Any]] = []
    world_stats: dict[str, dict[str, Any]] = {}
    for w in worlds:
        cases = w.cases[: args.limit_cases] if args.limit_cases else w.cases
        t = time.perf_counter()
        planned = 0
        for case_id, ncrp_ids in cases:
            split = _split_for(w, case_id, args.val_frac)
            try:
                bundle = plan_case(w, case_id, ncrp_ids)
            except Exception as exc:  # noqa: BLE001 - one broken case must not stop the build
                rejections.append(
                    {
                        "world": w.name,
                        "case_id": case_id,
                        "split": split,
                        "reasons": ["plan_error"],
                        "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
                    }
                )
                continue
            planned += 1
            pool = "test" if split == "test" else "train"
            for variant in variants:
                vb, info = make_variant(bundle, w.ledger, variant, pool, f"{args.seed}|{w.name}|{case_id}")
                for sample in range(args.samples):
                    jobs.append(Job(w, case_id, split, variant, sample, vb, info))
        world_stats[w.name] = {
            "role": w.role,
            "seed": w.seed,
            "generator_version": w.meta.get("generator_version"),
            "cases_total": len(w.cases),
            "cases_planned": planned,
            "plan_seconds": round(time.perf_counter() - t, 1),
        }
        print(f"[dataset] planned {planned}/{len(cases)} cases of {w.name} in {time.perf_counter() - t:.1f}s")

    def run(job: Job) -> tuple[Job, list[ProseRecord], str | None]:
        teacher = shared_teacher or make_llm(args.teacher)
        temp = args.teacher_temperature if job.sample == 0 else args.sample_temperature
        try:
            records, _ = run_case(
                job.bundle, job.world.ledger, teacher, search=search, temperature=temp, system_suffix=suffix
            )
        except Exception as exc:  # noqa: BLE001
            return job, [], f"{type(exc).__name__}: {str(exc)[:200]}"
        return job, records, None

    t = time.perf_counter()
    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(run, jobs))
    else:
        results = [run(j) for j in jobs]
    draft_seconds = time.perf_counter() - t

    rows: dict[str, list[dict[str, Any]]] = {s: [] for s in SPLITS}
    seen: set[str] = set()
    req = Counter()
    req_task: dict[str, Counter] = defaultdict(Counter)
    reasons_all: Counter = Counter()
    reasons_task: dict[str, Counter] = defaultdict(Counter)
    issue_counter: Counter = Counter()
    verifier_counter: Counter = Counter()
    for job, records, error in results:
        if error is not None:
            rejections.append(
                {
                    "world": job.world.name,
                    "case_id": job.case_id,
                    "split": job.split,
                    "variant": job.variant,
                    "sample": job.sample,
                    "reasons": ["draft_error"],
                    "detail": error,
                }
            )
            reasons_all["draft_error"] += 1
            continue
        for rec in records:
            req["requests"] += 1
            req_task[rec.task]["requests"] += 1
            for issue in rec.issues:
                issue_counter[issue.split(":", 1)[-1].strip().split(" ")[0]] += 1
            for err in rec.verifier_errors:
                verifier_counter[err] += 1
            reasons = judge(rec, min_coverage=args.min_coverage, length_slack=args.length_slack)
            if reasons:
                req["rejected"] += 1
                req_task[rec.task]["rejected"] += 1
                reasons_all.update(reasons)
                reasons_task[rec.task].update(reasons)
                rejections.append(
                    {
                        "world": job.world.name,
                        "case_id": job.case_id,
                        "split": job.split,
                        "variant": job.variant,
                        "sample": job.sample,
                        "task": rec.task,
                        "account_id": rec.account_id,
                        "reasons": reasons,
                        "issues": rec.issues,
                        "verifier_errors": rec.verifier_errors,
                    }
                )
                continue
            req["accepted"] += 1
            req_task[rec.task]["accepted"] += 1
            req_task[rec.task]["first_try" if rec.llm_attempts == 1 else "after_retry"] += 1
            for ex in examples_for(
                rec, job, teacher=teacher_name, corrections_frac=args.corrections_frac, seed=args.seed
            ):
                blob = json.dumps(ex["messages"], ensure_ascii=False)
                if _SECRET_RE.search(blob):
                    reasons_all["scrub_secret"] += 1
                    continue
                key = sha256_text(blob)
                if key in seen:
                    reasons_all["duplicate"] += 1
                    continue
                seen.add(key)
                rows[ex["split"]].append(ex)

    # leakage guard: a case lives in exactly one split
    case_split: dict[str, set[str]] = defaultdict(set)
    for s in SPLITS:
        for r in rows[s]:
            case_split[f"{r['world']}:{r['case_id']}"].add(s)
    leaks = sorted(k for k, v in case_split.items() if len(v) > 1)
    if leaks:
        raise SystemExit(f"split leakage (bug): {leaks[:5]}")
    if any(r["world"] not in {w.name for w in worlds if w.role == "heldout"} for r in rows["test"]):
        raise SystemExit("test split contains a non-held-out world (bug)")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for s in SPLITS:
        path = out / f"{s}.jsonl"
        write_jsonl(path, rows[s])
        files[path.name] = {"lines": len(rows[s]), "sha256": sha256_file(path)}
    write_jsonl(out / "rejections.jsonl", rejections)
    files["rejections.jsonl"] = {"lines": len(rejections), "sha256": sha256_file(out / "rejections.jsonl")}

    all_rows = [r for s in SPLITS for r in rows[s]]
    manifest = {
        "schema": SCHEMA,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "synthetic_only": True,
        "note": "Pipeline output. With a fake teacher these counts check the plumbing; they are not model results.",
        "teacher": {
            "spec": redact_spec(args.teacher),
            "name": teacher_name,
            "style_guide": style,
            "temperature": args.teacher_temperature,
            "sample_temperature": args.sample_temperature,
            "samples_per_request": args.samples,
        },
        "params": {k: v for k, v in vars(args).items() if k not in ("teacher", "func")},
        "worlds": world_stats,
        "split_rule": "test = held-out worlds (unseen seeds); val = sha256(world:case_id) bucket < val_frac",
        "filters": FILTERS,
        "requests": {
            **dict(req),
            "acceptance_rate": ratio(req["accepted"], req["requests"]),
            "by_task": {
                t: {**dict(c), "acceptance_rate": ratio(c["accepted"], c["requests"])}
                for t, c in sorted(req_task.items())
            },
        },
        "rejections": {
            "by_reason": dict(reasons_all.most_common()),
            "by_task": {t: dict(c.most_common()) for t, c in sorted(reasons_task.items())},
            "drafter_issue_codes": dict(issue_counter.most_common()),
            "verifier_errors": dict(verifier_counter.most_common()),
        },
        "splits": {s: _count_table(rows[s]) for s in SPLITS},
        "tasks": list(TASKS),
        "variants": variants,
        "prompt_chars": describe([r["meta"]["prompt_chars"] for r in all_rows]),
        "target_words": {
            t: describe([r["meta"]["target_words"] for r in all_rows if r["task"] == t]) for t in TASKS
        },
        "files": files,
        "timing_s": {"total": round(time.perf_counter() - t0, 1), "drafting": round(draft_seconds, 1)},
    }
    write_json(out / "manifest.json", manifest)
    if args.summary_out:
        summary = {k: v for k, v in manifest.items() if k not in ("params",)}
        summary["output_dir"] = (
            out.resolve().relative_to(ROOT).as_posix() if out.resolve().is_relative_to(ROOT) else str(out)
        )
        write_json(Path(args.summary_out), summary)
    return manifest


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--teacher",
        default="fake",
        help="fake | fake-noisy | mock:fake | openai:<base_url>|<model>[|<API_KEY_ENV>[|json_object]] | hf:<path>",
    )
    ap.add_argument(
        "--train-worlds",
        default="demo,full",
        help="comma list of worlds for train/val (names under data/generated, paths, or showcase-memory)",
    )
    ap.add_argument(
        "--heldout-worlds",
        default="auto",
        help="comma list of held-out worlds for the test split ('auto' = data/generated/heldout-*)",
    )
    ap.add_argument("--variants", default="clean,tempting,injection")
    ap.add_argument(
        "--samples", type=int, default=1, help="teacher samples per request (>1 adds sampled variety)"
    )
    ap.add_argument("--teacher-temperature", type=float, default=0.0)
    ap.add_argument("--sample-temperature", type=float, default=0.7)
    ap.add_argument(
        "--style-guide",
        choices=["auto", "on", "off"],
        default="auto",
        help="append the FINnet/SOP style guide to the teacher's system prompt (auto: real teachers only)",
    )
    ap.add_argument("--val-frac", type=float, default=0.1)
    ap.add_argument(
        "--corrections-frac",
        type=float,
        default=0.25,
        help="share of accepted train/val answers that also get a synthetic wrong-answer + feedback turn",
    )
    ap.add_argument("--min-coverage", type=float, default=0.5)
    ap.add_argument("--length-slack", type=float, default=0.5)
    ap.add_argument("--limit-cases", type=int, default=None, help="first N cases per world")
    ap.add_argument("--workers", type=int, default=1, help="parallel drafting jobs (teacher-bound)")
    ap.add_argument("--timeout", type=float, default=180.0, help="teacher HTTP timeout (s)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None, help="output dir (default data/generated/lora-<teacher>)")
    ap.add_argument(
        "--summary-out", default=None, help="also write a small summary JSON (e.g. under results/)"
    )
    ap.add_argument("--allow-unverified-world", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.out is None:
        args.out = str(GENERATED / f"lora-{llm_slug(args.teacher)}")
    m = build(args)
    print(f"[dataset] wrote {args.out}")
    print(
        json.dumps(
            {
                "requests": m["requests"],
                "splits": {
                    s: {
                        k: m["splits"][s][k]
                        for k in ("examples", "cases", "by_task", "by_variant", "by_source")
                    }
                    for s in SPLITS
                },
                "rejections": m["rejections"]["by_reason"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
