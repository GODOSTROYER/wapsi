"""End-to-end benchmark: golden cases through ``process_case`` with the real LLM on the MI300X.

    # on the droplet (host), against vLLM on 127.0.0.1:8000
    uv run python -m editions.amd.bench.e2e_bench --world demo --llm-url http://127.0.0.1:8000/v1 \
        --concurrency 8 --telemetry editions/amd/bench/results/<stamp>/telemetry_summary.json \
        --llm-bench editions/amd/bench/results/<stamp>/llm_bench.json --out editions/amd/bench/results/<stamp>
    # anywhere, offline (FakeLLM; checks the harness, measures nothing about the GPU)
    uv run python -m editions.amd.bench.e2e_bench --world showcase-memory --fake-llm

Measures, over N golden cases (``data/golden/<world>/cases.jsonl`` on ``data/generated/<world>``; the special world
``showcase-memory`` uses the in-memory showcase ledger):

- cases/hour at the chosen concurrency, p50/p95 time-to-hold-plan and total time per case
- LLM calls per case and their latency, prose fallback rate (drafts whose LLM prose failed the checks and were
  replaced by the deterministic paragraph), draft verification pass rate, hold exact-match vs the golden set
- cost per case at ``--gpu-hour-usd`` (default 1.99, AMD Developer Cloud MI300X) and optionally in rupees
  (``--inr-per-usd``: pass the day's rate; nothing is assumed)
- GPU memory split per container from ``gpu_telemetry.py``'s summary, and serving numbers from ``llm_bench.py``

Writes ``report.json`` + ``report.md`` (or ``<--name>.json/.md``) into ``--out`` (default ``editions/amd/bench/results/<utc stamp>/``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from editions.amd.bench.common import (  # noqa: E402
    SCHEMA_VERSION,
    dist,
    env_info,
    md_table,
    results_dir,
    utc_now,
    write_json,
)

DEFAULT_GPU_HOUR_USD = 1.99


class TimedLLM:
    """Wraps an ``LLM``: counts calls, errors and latency (thread-safe). Implements ``wapsi.ports.LLM``."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", type(inner).__name__)
        self.latencies: list[float] = []
        self.errors = 0
        self._lock = threading.Lock()

    def complete(self, messages, *, temperature: float = 0.0, max_tokens: int = 1024, json_schema=None) -> str:
        t = time.perf_counter()
        try:
            return self.inner.complete(messages, temperature=temperature, max_tokens=max_tokens,
                                       json_schema=json_schema)
        except Exception:
            with self._lock:
                self.errors += 1
            raise
        finally:
            with self._lock:
                self.latencies.append(time.perf_counter() - t)


def load_world(world: str):
    if world == "showcase-memory":
        from wapsi.fixtures.showcase import showcase_ledger

        return showcase_ledger()
    from wapsi.adapters.parquet_ledger import ParquetLedger

    path = ROOT / "data" / "generated" / world
    if not (path / "transactions.parquet").exists():
        raise SystemExit(f"world not found: {path} (uv run python -m wapsi.synth --size {world} --out {path})")
    return ParquetLedger(path)


def load_golden(world: str, limit: int | None = None) -> list:
    from wapsi.models import GoldenCase

    name = "showcase" if world == "showcase-memory" else world
    path = ROOT / "data" / "golden" / name / "cases.jsonl"
    if not path.exists():
        raise SystemExit(f"golden set not found: {path}")
    cases = [GoldenCase.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return cases[:limit] if limit else cases


def model_card(base_url: str, api_key: str | None) -> dict[str, Any]:
    """Best-effort ``GET /models`` (root path, max_model_len) for the report."""
    try:
        import httpx

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        resp = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=5)
        resp.raise_for_status()
        cards = resp.json().get("data") or []
        return cards[0] if cards else {}
    except Exception:  # noqa: BLE001 - informational only
        return {}


def _hold_exact(bundle, golden) -> tuple[int, int]:
    from wapsi.money import eq

    got = {d.account_id: d.hold_amount for d in bundle.plan.decisions} if bundle.plan else {}
    n = ok = 0
    for acct, exp in golden.expected_hold.items():
        n += 1
        ok += int(eq(got.get(acct, 0.0), exp))
    return ok, n


def run_cases(services, golden: Sequence, concurrency: int, llm: TimedLLM) -> tuple[list[dict], float]:
    from editions.amd.app.services import process
    from editions.amd.app.summary import case_summary

    def one(g) -> dict[str, Any]:
        calls_before = len(llm.latencies)
        t = time.perf_counter()
        try:
            bundle = process(services, list(g.ncrp_ids), case_id=g.case_id, persist=services.store is not None)
        except Exception as exc:  # noqa: BLE001 - a failed case is a data point
            return {"case_id": g.case_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                    "wall_s": time.perf_counter() - t}
        wall = time.perf_counter() - t
        s = case_summary(bundle)
        hold_ok, hold_n = _hold_exact(bundle, g)
        return {**s, "ok": True, "wall_s": wall, "typology": g.typology, "is_adversarial": g.is_adversarial,
                "hold_exact": hold_ok, "hold_n": hold_n, "llm_calls_approx": len(llm.latencies) - calls_before}

    t0 = time.perf_counter()
    if concurrency <= 1:
        rows = [one(g) for g in golden]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            rows = list(pool.map(one, golden))
    return rows, time.perf_counter() - t0


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def memory_split(telemetry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not telemetry:
        return None
    gpu = (telemetry.get("gpus") or [{}])[0]
    return {
        "source": telemetry.get("source"),
        "gpu_name": gpu.get("name"),
        "vram_total_mb": gpu.get("vram_total_mb"),
        "vram_used_mb_peak": gpu.get("vram_used_mb_peak"),
        "gfx_activity_pct_mean": gpu.get("gfx_activity_pct_mean"),
        "power_w_mean": gpu.get("power_w_mean"),
        "per_owner": [{"owner": p.get("owner"), "vram_mb_peak": p.get("vram_mb_peak"),
                       "process_names": p.get("process_names")} for p in telemetry.get("processes") or []],
    }


def serving_summary(llm_bench: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if not llm_bench:
        return None
    return [{"model": llm_bench.get("model"), "concurrency": lv["concurrency"],
             "ttft_p50_s": lv["ttft_s"]["p50"], "ttft_p95_s": lv["ttft_s"]["p95"],
             "output_tokens_per_s": lv["output_tokens_per_s"], "requests_per_s": lv["requests_per_s"]}
            for lv in llm_bench.get("levels") or []]


def build_report(rows: list[dict], wall_s: float, *, world: str, concurrency: int, llm: TimedLLM,
                 model_info: dict[str, Any], gpu_hour_usd: float, inr_per_usd: float | None,
                 telemetry: dict[str, Any] | None = None, llm_bench: dict[str, Any] | None = None,
                 extras: dict[str, Any] | None = None) -> dict[str, Any]:
    ok = [r for r in rows if r.get("ok")]
    drafts = sum(r["drafts"] for r in ok)
    llm_drafts = sum(r["llm_drafts"] for r in ok)
    cases_per_hour = round(len(ok) / wall_s * 3600, 2) if wall_s > 0 and ok else None
    usd_per_case = round(gpu_hour_usd / cases_per_hour, 8) if cases_per_hour else None
    t_plan = [r["timings_s"].get("time_to_hold_plan_s") for r in ok]
    t_total = [r["wall_s"] for r in ok]
    return {
        "kind": "e2e_bench",
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now().isoformat(),
        "world": world,
        "env": env_info(),
        "model": model_info,
        "settings": {"concurrency": concurrency, "cases_requested": len(rows), "gpu_hour_usd": gpu_hour_usd,
                     "inr_per_usd": inr_per_usd, **(extras or {})},
        "metrics": {
            "cases": len(rows),
            "cases_ok": len(ok),
            "cases_failed": len(rows) - len(ok),
            "wall_s": round(wall_s, 3),
            "cases_per_hour": cases_per_hour,
            "time_to_hold_plan_s": dist(t_plan),
            "case_total_s": dist(t_total),
            "llm_calls": len(llm.latencies),
            "llm_errors": llm.errors,
            "llm_calls_per_case": _ratio(len(llm.latencies), len(ok)),
            "llm_latency_s": dist(llm.latencies),
            "drafts": drafts,
            "drafts_passed": sum(r["drafts_passed"] for r in ok),
            "draft_verification_pass_rate": _ratio(sum(r["drafts_passed"] for r in ok), drafts),
            "cases_all_verified_rate": _ratio(sum(1 for r in ok if r["all_verified"]), len(ok)),
            "llm_drafts": llm_drafts,
            "prose_fallbacks": sum(r["prose_fallbacks"] for r in ok),
            "prose_fallback_rate": _ratio(sum(r["prose_fallbacks"] for r in ok), llm_drafts),
            "hold_exact_match_rate": _ratio(sum(r["hold_exact"] for r in ok), sum(r["hold_n"] for r in ok)),
            "injection_flags": sum(r["injection_flags"] for r in ok),
        },
        "cost": {
            "gpu_hour_usd": gpu_hour_usd,
            "usd_per_case": usd_per_case,
            "inr_per_usd": inr_per_usd,
            "inr_per_case": round(usd_per_case * inr_per_usd, 6) if usd_per_case and inr_per_usd else None,
            "usd_per_1000_cases": round(usd_per_case * 1000, 3) if usd_per_case else None,
            "note": "whole-GPU price divided by measured throughput at this concurrency",
        },
        "memory": memory_split(telemetry),
        "serving": serving_summary(llm_bench),
        "cases": [{k: r.get(k) for k in ("case_id", "ok", "typology", "is_adversarial", "wall_s", "drafts",
                                          "drafts_passed", "prose_fallbacks", "hold_exact", "hold_n", "error")}
                  for r in rows],
        "failures": [{"case_id": r["case_id"], "failed_checks": r.get("failed_checks", []), "error": r.get("error")}
                     for r in rows if not r.get("ok") or r.get("failed_checks")][:50],
    }


def to_markdown(rep: dict[str, Any]) -> str:
    m, c, s = rep["metrics"], rep["cost"], rep["settings"]
    model = rep.get("model") or {}
    lines = [
        f"# Wapsi end-to-end benchmark — {rep['world']}",
        "",
        f"Generated {rep['generated_at']} · host `{rep['env'].get('host')}` · commit `{rep['env'].get('git_commit')}`",
        "",
        *(["> **Offline harness check (FakeLLM, CPU).** Not a GPU measurement; do not report these numbers.", ""]
          if str(model.get("llm") or "").startswith("fake-llm") else []),
        "## Model",
        "",
        md_table(["LLM", "Served from", "Max model len", "Scorer", "Guard"],
                 [[model.get("llm"), model.get("root") or model.get("base_url"), model.get("max_model_len"),
                   model.get("scorer"), model.get("guard")]]),
        "",
        "## Throughput and cost",
        "",
        md_table(["Cases", "Concurrency", "Cases/hour", "Hold plan p50 (s)", "Hold plan p95 (s)",
                  "Case p50 (s)", "Case p95 (s)", "$/GPU-hour", "$ per case", "₹ per case"],
                 [[f"{m['cases_ok']}/{m['cases']}", s["concurrency"], m["cases_per_hour"],
                   m["time_to_hold_plan_s"]["p50"], m["time_to_hold_plan_s"]["p95"], m["case_total_s"]["p50"],
                   m["case_total_s"]["p95"], c["gpu_hour_usd"], c["usd_per_case"], c["inr_per_case"]]], digits=4),
        "",
        "## Quality (verifier checks on every draft)",
        "",
        md_table(["Drafts", "Verification pass rate", "Cases fully verified", "LLM prose drafts",
                  "Prose fallback rate", "Hold exact match", "LLM calls/case", "LLM p50 latency (s)"],
                 [[m["drafts"], m["draft_verification_pass_rate"], m["cases_all_verified_rate"], m["llm_drafts"],
                   m["prose_fallback_rate"], m["hold_exact_match_rate"], m["llm_calls_per_case"],
                   m["llm_latency_s"]["p50"]]]),
    ]
    mem = rep.get("memory")
    if mem:
        lines += ["", f"## GPU memory split ({mem.get('gpu_name') or 'GPU 0'}, source {mem.get('source')})", "",
                  md_table(["Owner", "Peak VRAM (GB)", "Processes"],
                           [[p["owner"], (p["vram_mb_peak"] or 0) / 1024, ", ".join(p.get("process_names") or [])]
                            for p in mem["per_owner"]] +
                           [["total (device)", (mem.get("vram_used_mb_peak") or 0) / 1024,
                             f"of {(mem.get('vram_total_mb') or 0) / 1024:.1f} GB"]], digits=1)]
    serving = rep.get("serving")
    if serving:
        lines += ["", "## Serving (llm_bench)", "",
                  md_table(["Model", "Concurrency", "TTFT p50 (s)", "TTFT p95 (s)", "Output tok/s", "Req/s"],
                           [[r["model"], r["concurrency"], r["ttft_p50_s"], r["ttft_p95_s"],
                             r["output_tokens_per_s"], r["requests_per_s"]] for r in serving])]
    if rep["failures"]:
        lines += ["", f"## Failures ({len(rep['failures'])})", ""]
        for f in rep["failures"][:20]:
            detail = f.get("error") or "; ".join(f"{x['check']}: {x['message']}" for x in f["failed_checks"][:3])
            lines.append(f"- `{f['case_id']}` {detail}")
    return "\n".join(lines) + "\n"


def _load_json(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--world", default="demo", help="data/generated/<world> (or showcase-memory)")
    ap.add_argument("--limit", type=int, default=None, help="first N golden cases")
    ap.add_argument("--concurrency", type=int, default=4, help="cases processed in parallel")
    ap.add_argument("--llm-url", default=os.environ.get("WAPSI_LLM_BASE_URL", "http://127.0.0.1:8000/v1"))
    ap.add_argument("--llm-model", default=os.environ.get("WAPSI_LLM_SERVED_NAME", "wapsi-llm"),
                    help="served model name (default $WAPSI_LLM_SERVED_NAME or wapsi-llm)")
    ap.add_argument("--llm-timeout", type=float, default=180.0)
    ap.add_argument("--fake-llm", action="store_true", help="offline FakeLLM (harness check; no GPU numbers)")
    ap.add_argument("--verifier-url", default=os.environ.get("WAPSI_VERIFIER_BASE_URL") or None)
    ap.add_argument("--verifier-model", default=os.environ.get("WAPSI_VERIFIER_SERVED_NAME", "wapsi-verifier"))
    ap.add_argument("--gnn-url", default=os.environ.get("WAPSI_GNN_URL") or None)
    ap.add_argument("--gpu-hour-usd", type=float, default=DEFAULT_GPU_HOUR_USD)
    ap.add_argument("--inr-per-usd", type=float, default=None, help="rupees per dollar on the day (optional)")
    ap.add_argument("--telemetry", default=None, help="telemetry_summary.json from gpu_telemetry.py")
    ap.add_argument("--llm-bench", default=None, help="llm_bench.json from llm_bench.py")
    ap.add_argument("--store-dir", default=None, help="also persist bundles into this FileCaseStore (UI)")
    ap.add_argument("--no-search", action="store_true", help="skip the local docs index")
    ap.add_argument("--out", default=None)
    ap.add_argument("--name", default="report",
                    help="output file stem: report (default) or e2e_<label>, e.g. e2e_c16 for a concurrency sweep")
    args = ap.parse_args(argv)
    if args.name != "report" and not args.name.startswith("e2e"):
        ap.error("--name must be 'report' or start with e2e")

    from editions.amd.app.services import Services
    from editions.amd.app.settings import AmdSettings
    from wapsi.adapters.memory import RecordingActionSink
    from wapsi.verify.injection import RulesGuard

    api_key = os.environ.get("WAPSI_LLM_API_KEY") or os.environ.get("VLLM_API_KEY")
    if args.fake_llm:
        from wapsi.adapters.llm_fake import FakeLLM

        inner = FakeLLM(name="fake-llm (offline harness check)")
        info: dict[str, Any] = {"llm": inner.name, "base_url": None}
    else:
        from wapsi.adapters.llm_openai_compat import OpenAICompatLLM

        inner = OpenAICompatLLM(base_url=args.llm_url, model=args.llm_model, timeout=args.llm_timeout,
                                name=f"vllm-rocm:{args.llm_model}")
        card = model_card(args.llm_url, api_key)
        info = {"llm": inner.name, "base_url": args.llm_url, "root": card.get("root"),
                "max_model_len": card.get("max_model_len")}
    llm = TimedLLM(inner)

    guard: Any = RulesGuard()
    if args.verifier_url and not args.fake_llm:
        from editions.amd.app.judge import LLMJudgeGuard
        from wapsi.adapters.llm_openai_compat import OpenAICompatLLM

        guard = LLMJudgeGuard(OpenAICompatLLM(base_url=args.verifier_url, model=args.verifier_model,
                                              timeout=60, max_retries=1, name=f"vllm-rocm:{args.verifier_model}"))
    scorer = None
    if args.gnn_url:
        from editions.amd.app.gnn_client import GnnHttpScorer

        scorer = GnnHttpScorer(args.gnn_url)
    search = None
    if not args.no_search:
        from editions.amd.app.services import _docs_search

        search = _docs_search()
    store = None
    if args.store_dir:
        from editions.amd.app.file_store import FileCaseStore

        store = FileCaseStore(args.store_dir)

    settings = AmdSettings(demo_mode=args.fake_llm)
    services = Services(settings=settings, ledger=load_world(args.world), store=store, llm=llm,  # type: ignore[arg-type]
                        guard=guard, search=search, sink=RecordingActionSink(), scorer=scorer)
    info["scorer"] = getattr(scorer, "name", "heuristic-v2") if scorer else "heuristic-v2"
    info["guard"] = getattr(guard, "name", "rules")

    golden = load_golden(args.world, args.limit)
    print(f"processing {len(golden)} cases from {args.world} at concurrency {args.concurrency} with {llm.name}",
          flush=True)
    rows, wall = run_cases(services, golden, args.concurrency, llm)
    extras: dict[str, Any] = {}
    if scorer is not None:
        extras["gnn"] = scorer.stats.as_dict()
    if hasattr(guard, "stats"):
        extras["judge"] = guard.stats.as_dict()
    rep = build_report(rows, wall, world=args.world, concurrency=args.concurrency, llm=llm, model_info=info,
                       gpu_hour_usd=args.gpu_hour_usd, inr_per_usd=args.inr_per_usd,
                       telemetry=_load_json(args.telemetry), llm_bench=_load_json(args.llm_bench), extras=extras)
    out = results_dir(args.out)
    write_json(out / "report.json", rep)
    (out / "report.md").write_text(to_markdown(rep), encoding="utf-8", newline="\n")
    print(to_markdown(rep))
    print(f"wrote {out / 'report.json'}")
    return 0 if rep["metrics"]["cases_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
