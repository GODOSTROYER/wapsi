"""Golden-set evaluation with the real model: the ``eval`` stage of ``run_all.sh`` (every mode).

    # endpoint mode (AMD Token Factory or any OpenAI-compatible endpoint; rate-limited, cached, metered)
    uv run python -m editions.amd.kickoff.golden_eval --endpoint --world demo --limit 20 --out <evidence dir>
    # a server on this machine (vLLM on the droplet or in the notebook)
    uv run python -m editions.amd.kickoff.golden_eval --llm-url http://127.0.0.1:8000/v1 --llm-model wapsi-llm \
        --world demo --out <evidence dir>
    # anywhere, offline (FakeLLM: a plumbing check, not a model result)
    uv run python -m editions.amd.kickoff.golden_eval --fake --world showcase-memory --limit 2 --out <dir>

Runs ``evals.harness.evaluate`` (the same scorer of record as ``python -m evals.harness``) with the model under test
drafting every prose field, and writes ``golden_eval.json`` + ``golden_eval.md`` into ``--out``: hold exact-match,
false freezes, draft numeric accuracy, citation validity, verifier pass rate, prose fallback rate, injection leaks,
and the harness's own LLM-only baseline (the same model asked for a narrative with raw numbers, no slots: how often
it states an amount that is not in the case). It never touches ``evals/reports/``.

Mule / innocent holder / merchant decisions in this report come from the transparent scorer named in ``config.scorer``
(heuristic by default, the GNN with ``--scorer gnn:<checkpoint>``): never from the language model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

STUB_LABEL = "stub LLM (offline plumbing check): not a model result"
KEEP = ("hold_exact_match_rate", "false_freeze_count", "disposition_accuracy", "draft_verification_pass_rate",
        "draft_numeric_accuracy", "citation_validity", "llm_only_hallucination_rate", "llm_prose_fallback_rate",
        "injection_flag_recall", "injection_leaks", "p50_time_to_hold_plan_s", "p95_total_s")


def to_markdown(rep: dict[str, Any]) -> str:
    m = rep["metrics"]
    lines = [f"# Golden-set evaluation with the model under test ({rep['world']}, {rep['cases']} cases)", "",
             f"- Label: **{rep['label']}**",
             f"- LLM: `{rep['config']['llm']}` · scorer: `{rep['config']['scorer']}` (roles and holds never come from "
             f"the LLM) · generated {rep['generated_at']}", "",
             "| Metric | Value |", "|---|---|"]
    lines += [f"| {k} | {json.dumps(m.get(k))} |" for k in KEEP]
    ep = rep.get("endpoint")
    if ep:
        lines += ["", f"Endpoint `{ep['endpoint_host']}`, model `{ep['model']}`: {ep['calls']} calls "
                  f"(+ {ep['cache_hits']} from the disk cache), {ep['prompt_tokens']:,} prompt + "
                  f"{ep['completion_tokens']:,} completion tokens, latency p50 {ep['latency_s']['p50']} s / p95 "
                  f"{ep['latency_s']['p95']} s, API-reported cost ${ep['cost_usd']}, HTTP 429s {ep['rate_limited_429']}."]
    if rep.get("failures"):
        lines += ["", f"## Failures (first {min(20, len(rep['failures']))})", ""]
        lines += [f"- `{f['case_id']}` {f['check']}: {f['detail']}" for f in rep["failures"][:20]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, *, transport: Any = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--world", default="demo", help="data/generated/<world> with data/golden/<world> (or showcase-memory)")
    ap.add_argument("--world-dir", default=None)
    ap.add_argument("--limit", type=int, default=None, help="first N golden cases (default: all)")
    ap.add_argument("--scorer", default="heuristic", help="heuristic | xgb:<model.json> | gnn:<checkpoint dir>")
    ap.add_argument("--endpoint", action="store_true",
                    help="metered client from WAPSI_LLM_BASE_URL / WAPSI_LLM_API_KEY / WAPSI_LLM_MODEL "
                         "(rate limit, 429 retry, disk cache, usage.cost)")
    ap.add_argument("--llm-url", default=None, help="OpenAI-compatible server (default with --endpoint: the env)")
    ap.add_argument("--llm-model", default=None)
    ap.add_argument("--no-cache", action="store_true", help="--endpoint: do not read or write the disk cache")
    ap.add_argument("--fake", action="store_true", help=f"offline {STUB_LABEL}")
    ap.add_argument("--label", default=None)
    ap.add_argument("--out", required=True, help="directory for golden_eval.json / golden_eval.md")
    ap.add_argument("--name", default="golden_eval")
    args = ap.parse_args(argv)

    from evals import harness

    llm: Any = None
    kind = "fake"
    if args.fake:
        label = args.label or STUB_LABEL
    elif args.endpoint:
        from editions.amd.kickoff.endpoint import MeteredLLM, measured_label

        llm = MeteredLLM.from_env(base_url=args.llm_url, model=args.llm_model, cache=not args.no_cache,
                                  transport=transport)
        kind = "endpoint"
        label = args.label or measured_label(llm.base_url, llm.model)
    else:
        from editions.amd.kickoff.ab_hallucination import endpoint_api_key
        from wapsi.adapters.llm_env import self_hosted_name  # "vllm-rocm:" only when AMD is confirmed
        from wapsi.adapters.llm_openai_compat import OpenAICompatLLM

        url = args.llm_url or os.environ.get("WAPSI_LLM_BASE_URL", "http://127.0.0.1:8000/v1")
        model = args.llm_model or os.environ.get("WAPSI_LLM_SERVED_NAME", "wapsi-llm")
        llm = OpenAICompatLLM(base_url=url, model=model, api_key=endpoint_api_key(), timeout=180.0,
                              json_mode=os.environ.get("WAPSI_LLM_JSON_MODE") or "json_schema",
                              name=self_hosted_name(model), transport=transport)
        kind = "openai"
        label = args.label or f"measured against {url} ({model})"

    world_dir = Path(args.world_dir) if args.world_dir else None
    print(f"golden-set evaluation on {args.world} with {getattr(llm, 'name', 'fake-llm')}", flush=True)
    rep = harness.evaluate(args.world, kind, limit=args.limit, scorer=args.scorer, world_dir=world_dir, llm=llm)
    rep = {"kind": "golden_eval", "label": label, "stub": bool(args.fake), **rep}
    if hasattr(llm, "report"):
        rep["endpoint"] = llm.report()
    elif hasattr(llm, "usage"):
        rep["llm_usage"] = llm.usage.as_dict()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.name}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                                           newline="\n")
    md = to_markdown(rep)
    (out / f"{args.name}.md").write_text(md, encoding="utf-8", newline="\n")
    print(md)
    print(f"wrote {out / (args.name + '.json')}")
    # a case whose LLM-only baseline call failed is in `failures` (check llm_baseline); a run where the model
    # answered nothing at all measured nothing
    ep = rep.get("endpoint")
    return 1 if ep and ep["calls"] + ep["cache_hits"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
