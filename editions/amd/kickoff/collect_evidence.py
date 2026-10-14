"""Assemble one evidence bundle from a run_all.sh evidence folder (standard library only).

    python3 editions/amd/kickoff/collect_evidence.py --evidence-dir editions/amd/evidence/2026-10-12 --mode droplet
    uv run python editions/amd/kickoff/collect_evidence.py --evidence-dir editions/amd/evidence/endpoint/<date> \
        --mode endpoint
    python3 editions/amd/kickoff/collect_evidence.py --evidence-dir editions/amd/evidence/notebook/<date> --mode notebook
    uv run python editions/amd/kickoff/collect_evidence.py --evidence-dir editions/amd/evidence/cpu-rehearsal/<date> \
        --mode rehearsal

Writes ``evidence.json`` (machine-readable, read by ``fill_pitch.py``) and ``evidence.md`` (dated tables for the
README and the deck) into the folder. Every number is read from the result files the stages wrote (benchmarks,
telemetry, GNN, LoRA, golden-set evaluation, A/B) or derived from them by the arithmetic shown in ``derivations``;
nothing is typed in. A missing input leaves its headline value ``null`` and is listed under ``missing``. A
measurement taken under errors (an llm_bench level with failed, rate-limited or paced requests, an e2e run with
failed cases, an A/B with no case both arms completed) stays in the tables but is never a headline value; it is
listed under ``warnings`` with the stages' recorded measurement failures (``measure_failures`` in the stage notes).

The three execution modes write the same bundle; ``compute`` says where the workload ran, and that is the only
thing the pitch may claim:

- ``droplet`` (``gpu`` is its old name): ``amd-smi static`` (or, for a later-week ``--stage bench,ab,evidence``
  folder without it, the bench telemetry's amd-smi GPU name) must name an MI300X.
- ``notebook``: an AMD ROCm JupyterLab without Docker. The GPU is whatever ``amd-smi`` / ``rocm-smi`` names, or the
  device a ROCm (HIP) build of torch reports (``torch_info.json``). It is recorded as named, never as an MI300X
  unless it is one.
- ``endpoint``: no GPU on this machine. ``endpoint.json`` (``endpoint.py probe``) records the endpoint host, the
  model id and the provider; the GPU is provider-side and no GPU name, memory or power figure exists. An endpoint
  counts as AMD-served when it is an AMD host (Token Factory) or the operator declared its hardware.

``results_valid`` is true only for a run of one of those modes whose identity checks pass and whose every stage
started inside the event window (2026-10-12 15:00 UTC .. 2026-10-18 14:00 UTC). ``gpu_results_valid`` is the same
for the two modes that have a GPU of their own (always false for an endpoint run). A ``rehearsal`` bundle is always
labelled "CPU rehearsal" and is never valid. A run whose prose was mostly the fixed paragraph (prose fallback rate
above ``MAX_HEALTHY_FALLBACK_RATE`` in the golden eval or the end-to-end bench) is not valid either: the model was
not answering in language (a degraded shared deployment answers HTTP 200 with garbage, which the prose validator
rejects), so what the run measured is the fallback text, not the model.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from editions.amd.kickoff.stagelog import EVENT_END, EVENT_START, canon_mode, parse_ts  # noqa: E402

SCHEMA = "wapsi-amd-evidence/2"
EVENT = "lablab.ai AMD Developer Hackathon ACT III"
GPU_LABEL = f"MI300X run on AMD Developer Cloud ({EVENT})"
NOTEBOOK_LABEL = f"AMD ROCm notebook run ({EVENT})"
ENDPOINT_LABEL = (f"Endpoint run ({EVENT}): the LLM workload is served by the provider's GPUs through its API; no GPU "
                  "on this machine, so no GPU name, memory or power figure is reported")
REHEARSAL_LABEL = ("CPU REHEARSAL: tiny worlds, a tiny random model and stub LLMs on a laptop. It proves the plumbing "
                   "only. NOT GPU results; never quote these numbers.")
AMD_GPU_RE = re.compile(r"\b(AMD|Radeon|Instinct|MI\d{3}[A-Z]?)\b", re.I)
LATENCY_BENCHES = ("llm", "wapsi_prompts")  # benches whose per-request latency is a headline (not only tokens/s)


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _get(obj: Any, *keys: Any) -> Any:
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list) and isinstance(k, int) and -len(obj) <= k < len(obj):
            obj = obj[k]
        else:
            return None
    return obj


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _r(v: Any, digits: int = 4) -> float | None:
    x = _num(v)
    return None if x is None else round(x, digits)


def _mean(values: Iterable[Any]) -> float | None:
    vals = [x for x in (_num(v) for v in values) if x is not None]
    return sum(vals) / len(vals) if vals else None


def _failed_requests(lv: dict[str, Any]) -> int | None:
    """Requests of one llm_bench level that failed (None: the level completed nothing, so it measured nothing)."""
    done = lv.get("completed")
    if isinstance(done, int) and done == 0:
        return None
    err = lv.get("errors")
    if isinstance(err, int):
        return err
    req = lv.get("requests")
    return req - done if isinstance(req, int) and isinstance(done, int) else 0


# --------------------------------------------------------------------------- sections


def serving(evid: Path, head: dict[str, Any], src: dict[str, str], warn: list[str]) -> dict[str, Any]:
    """llm_bench results, every concurrency level each file has. A level with failed requests, HTTP 429s or
    requests paced by ``--rpm`` (or nothing completed) is shown in the tables but never becomes a headline number:
    its throughput and latency were not measured at that concurrency."""
    out: dict[str, Any] = {}
    for stem, key in (("llm_bench", "llm"), ("llm_bench_verifier", "verifier"), ("llm_bench_base", "base"),
                      ("llm_bench_wapsi_prompts", "wapsi_prompts")):
        bench = _load(evid / "bench" / f"{stem}.json")
        if bench is None:
            continue
        rel = f"bench/{stem}.json"
        levels = [lv for lv in bench.get("levels") or [] if isinstance(lv, dict)]
        out[key] = {"model": bench.get("model"), "model_root": bench.get("model_root"), "label": bench.get("label"),
                    "generated_at": bench.get("generated_at"), "rpm": _get(bench, "settings", "rpm"),
                    "levels": [{"concurrency": lv.get("concurrency"), "ok": f"{lv.get('completed')}/{lv.get('requests')}",
                                "output_tokens_per_s": lv.get("output_tokens_per_s"),
                                "ttft_p50_s": _get(lv, "ttft_s", "p50"), "ttft_p95_s": _get(lv, "ttft_s", "p95"),
                                "e2e_p50_s": _get(lv, "e2e_s", "p50"), "e2e_p95_s": _get(lv, "e2e_s", "p95"),
                                "request_tokens_per_s_p50": _get(lv, "request_tokens_per_s", "p50"),
                                "requests_per_s": lv.get("requests_per_s"), "rate_limited": lv.get("rate_limited"),
                                "stream_buffered": lv.get("stream_buffered"), "cost_usd": lv.get("cost_usd")}
                               for lv in levels]}
        clean = []
        for lv in levels:
            failed, c = _failed_requests(lv), lv.get("concurrency")
            limited, paced = lv.get("rate_limited") or 0, _num(lv.get("paced_s")) or 0.0
            if failed == 0 and not limited and not paced:
                clean.append(lv)
            elif failed != 0:
                what = "nothing completed" if failed is None else f"{failed} of {lv.get('requests')} requests failed"
                warn.append(f"{rel} concurrency {c}: {what}; not used as a headline number (re-run --stage bench)")
            else:
                warn.append(f"{rel} concurrency {c}: {limited} HTTP 429 and {paced:g} s of pacing inside the level "
                            "(the endpoint's request cap); not used as a headline number (re-run --stage bench "
                            "when the key is idle)")
        for lv in levels:
            c, ok = lv.get("concurrency"), lv in clean
            head[f"{key}_tok_s_c{c}"] = _r(lv.get("output_tokens_per_s"), 1) if ok else None
            src[f"{key}_tok_s_c{c}"] = f"{rel}: levels[concurrency={c}].output_tokens_per_s"
            if key in LATENCY_BENCHES:
                for name, path_keys, digits in (("ttft_p50_s", ("ttft_s", "p50"), 3), ("ttft_p95_s", ("ttft_s", "p95"), 3),
                                                ("e2e_p50_s", ("e2e_s", "p50"), 3), ("e2e_p95_s", ("e2e_s", "p95"), 3),
                                                ("req_tok_s_p50", ("request_tokens_per_s", "p50"), 1)):
                    head[f"{key}_{name}_c{c}"] = _r(_get(lv, *path_keys), digits) if ok else None
                    src[f"{key}_{name}_c{c}"] = f"{rel}: levels[concurrency={c}].{'.'.join(path_keys)}"
        if key == "llm":
            if clean:
                best = max(clean, key=lambda lv: _num(lv.get("output_tokens_per_s")) or 0.0)
                head["llm_peak_tok_s"] = _r(best.get("output_tokens_per_s"), 1)
                head["llm_peak_tok_s_concurrency"] = best.get("concurrency")
                src["llm_peak_tok_s"] = f"{rel}: max over levels without errors of output_tokens_per_s"
            head["llm_model"] = bench.get("model")
            head["llm_stream_buffered"] = any(lv.get("stream_buffered") for lv in levels) if levels else None
    return out


def end_to_end(evid: Path, head: dict[str, Any], src: dict[str, str], deriv: dict[str, str],
               warn: list[str], *, own_gpu: bool = True, rehearsal: bool = False) -> list[dict[str, Any]]:
    """e2e_bench results. A run with failed cases is shown in the table but never becomes a headline number (its
    cases/hour and cost per case were measured under errors). ``own_gpu=False`` (endpoint mode): there is no GPU
    hour to divide by, so drafts per GPU-hour and the GPU-hour cost per case stay ``null``; drafts per hour and the
    API-reported cost per case are reported instead."""
    rows = []
    for path in sorted((evid / "bench").glob("e2e*.json")) + sorted((evid / "bench").glob("report.json")):
        rep = _load(path)
        if not rep or rep.get("kind") != "e2e_bench":
            continue
        m, c, s = rep.get("metrics") or {}, rep.get("cost") or {}, rep.get("settings") or {}
        conc, wall, drafts = s.get("concurrency"), _num(m.get("wall_s")), _num(m.get("drafts"))
        n, n_ok = m.get("cases"), m.get("cases_ok")
        failed = m.get("cases_failed")
        if not isinstance(failed, int):
            failed = n - n_ok if isinstance(n, int) and isinstance(n_ok, int) else 0
        clean = failed == 0 and n_ok != 0
        if not clean:
            warn.append(f"bench/{path.name} (concurrency {conc}): {failed} of {n} cases failed; not used as a "
                        "headline number (re-run --stage bench)")
        if str(_get(rep, "model", "llm") or "").startswith("fake-llm") and not rehearsal:
            clean = False  # the offline FakeLLM in a real run's folder: a harness check, never a number
            warn.append(f"bench/{path.name} was written with the offline FakeLLM (--fake-llm); not used as a "
                        "headline number (re-run --stage bench against the model)")
        per_hour = round(drafts / wall * 3600, 1) if drafts and wall else None
        rows.append({
            "clean": clean,
            "file": f"bench/{path.name}",
            "world": rep.get("world"),
            "concurrency": conc,
            "llm": _get(rep, "model", "llm"),
            "cases": f"{m.get('cases_ok')}/{m.get('cases')}",
            "cases_per_hour": m.get("cases_per_hour"),
            "rate_capped": bool(m.get("cases_per_hour_rate_capped")),
            "drafts_per_hour": per_hour,
            "drafts_per_gpu_hour": per_hour if own_gpu else None,
            "time_to_hold_plan_p50_s": _get(m, "time_to_hold_plan_s", "p50"),
            "case_p95_s": _get(m, "case_total_s", "p95"),
            "llm_calls_per_case": m.get("llm_calls_per_case"),
            "llm_latency_p50_s": _get(m, "llm_latency_s", "p50"),
            "llm_latency_p95_s": _get(m, "llm_latency_s", "p95"),
            "llm_seconds_per_case_p50": _get(m, "llm_seconds_per_case", "p50"),
            "draft_verification_pass_rate": m.get("draft_verification_pass_rate"),
            "prose_fallback_rate": m.get("prose_fallback_rate"),
            "hold_exact_match_rate": m.get("hold_exact_match_rate"),
            "usd_per_case": c.get("usd_per_case") if own_gpu else None,
            "inr_per_case": c.get("inr_per_case") if own_gpu else None,
            "api_usd_per_case": c.get("api_usd_per_case"),
            "api_inr_per_case": c.get("api_inr_per_case"),
            "inr_per_usd": c.get("inr_per_usd"),
            "gpu_hour_usd": c.get("gpu_hour_usd") if own_gpu else None,
        })
    deriv["drafts_per_hour"] = "drafts / wall_s x 3600, from the e2e report"
    deriv["drafts_per_gpu_hour"] = ("drafts / wall_s x 3600 (one GPU for the whole run, from the e2e report); null in "
                                    "endpoint mode, where no GPU hour exists to divide by")
    deriv["usd_per_case"] = "gpu_hour_usd / cases_per_hour (e2e_bench cost block); null without a GPU-hour price"
    deriv["inr_per_case"] = "usd_per_case x inr_per_usd, the day's rate passed to e2e_bench"
    deriv["api_usd_per_case"] = ("sum of usage.cost the API reported over the run / completed cases (endpoint mode; "
                                 "a relative usage cost, the trial is not billed)")
    per_case = ("usd_per_case", "inr_per_case", "api_usd_per_case", "api_inr_per_case")
    for row in rows:
        c = row["concurrency"]
        rel = row["file"]
        for key, field in (("e2e_cases_per_hour", "cases_per_hour"), ("usd_per_case", "usd_per_case"),
                           ("inr_per_case", "inr_per_case"), ("drafts_per_gpu_hour", "drafts_per_gpu_hour"),
                           ("drafts_per_hour", "drafts_per_hour"), ("api_usd_per_case", "api_usd_per_case")):
            head[f"{key}_c{c}"] = _r(row[field], 8 if key in per_case else 1) if row["clean"] else None
            src[f"{key}_c{c}"] = f"{rel}: {field}"
    measured = [r for r in rows if r["clean"] and _num(r["cases_per_hour"])]
    if measured:
        best = max(measured, key=lambda r: r["cases_per_hour"])
        head["e2e_best_concurrency"] = best["concurrency"]
        for key in ("cases_per_hour", "usd_per_case", "inr_per_case", "drafts_per_gpu_hour", "drafts_per_hour",
                    "api_usd_per_case", "api_inr_per_case", "llm_calls_per_case", "llm_latency_p50_s",
                    "llm_latency_p95_s", "llm_seconds_per_case_p50", "draft_verification_pass_rate",
                    "prose_fallback_rate", "time_to_hold_plan_p50_s"):
            head[f"e2e_best_{key}"] = _r(best[key], 8 if key in per_case else 4)
            src[f"e2e_best_{key}"] = f"{best['file']}: {key} (highest cases/hour)"
        head["e2e_rate_capped"] = best["rate_capped"]
        head["inr_per_usd"] = best["inr_per_usd"]
        head["gpu_hour_usd"] = best["gpu_hour_usd"]
    return rows


def _owner_role(owner: str, names: Iterable[str]) -> str:
    text = " ".join([owner or "", *names]).lower()
    if "verifier" in text:
        return "verifier"
    if "gnn" in text:
        return "gnn"
    if "app" in text and "llm" not in text:
        return "app"
    if "llm" in text or "vllm" in text:
        return "llm"
    return "other"


def memory(evid: Path, head: dict[str, Any], src: dict[str, str]) -> dict[str, Any] | None:
    tel = _load(evid / "bench" / "telemetry_summary.json")
    if not tel:
        return None
    rel = "bench/telemetry_summary.json"
    gpu = (tel.get("gpus") or [{}])[0]
    per_role: dict[str, float] = {}
    for p in tel.get("processes") or []:
        role = _owner_role(str(p.get("owner") or ""), p.get("process_names") or [])
        per_role[role] = per_role.get(role, 0.0) + (_num(p.get("vram_mb_peak")) or 0.0)
    for role, mb in per_role.items():
        head[f"vram_{role}_gb"] = round(mb / 1024, 1)
        src[f"vram_{role}_gb"] = f"{rel}: processes[owner~{role}].vram_mb_peak / 1024"
    for key, field, digits in (("vram_peak_gb", "vram_used_mb_peak", 1), ("vram_total_gb", "vram_total_mb", 1)):
        v = _num(gpu.get(field))
        head[key] = None if v is None else round(v / 1024, digits)
        src[key] = f"{rel}: gpus[0].{field} / 1024"
    head["gfx_activity_pct_mean"] = _r(gpu.get("gfx_activity_pct_mean"), 1)
    head["power_w_mean"] = _r(gpu.get("power_w_mean"), 1)
    return {"source": tel.get("source"), "samples": tel.get("samples"), "interval_s": tel.get("interval_s"),
            "gpu": gpu.get("name"), "per_role_peak_gb": {k: round(v / 1024, 1) for k, v in per_role.items()},
            "unavailable_sensors": gpu.get("unavailable_sensors")}


def gnn(evid: Path, head: dict[str, Any], src: dict[str, str]) -> dict[str, Any] | None:
    met = _load(evid / "gnn_metrics.json")
    ev = _load(evid / "gnn_eval.json")
    hard = _load(evid / "gnn_hard_eval.json")
    if not (met or ev or hard):
        return None
    out: dict[str, Any] = {}
    if met:
        hist = met.get("history") or []
        epochs = [h.get("epoch_s") for h in hist]
        later = epochs[1:] if len(epochs) > 1 else epochs  # epoch 1 includes kernel compilation / warm-up
        best = next((h for h in hist if h.get("epoch") == met.get("best_epoch")), None)
        out["train"] = {"device": _get(met, "device", "name") or _get(met, "device", "device"),
                        "label": met.get("label"), "parameters": met.get("parameters"), "epochs_run": len(hist),
                        "best_epoch": met.get("best_epoch"), "train_s": met.get("train_s"),
                        "graph_build_s": _get(met, "data", "graph_build_s"),
                        "train_cases": _get(met, "data", "train_cases"), "train_edges": _get(met, "data", "train_edges"),
                        "amlworld_pretrain": bool(met.get("amlworld_pretrain"))}
        head["gnn_epoch_s"] = _r(_mean(later), 3)
        src["gnn_epoch_s"] = "gnn_metrics.json: mean history[].epoch_s (epochs after the first)"
        head["gnn_edges_per_s"] = _r(_mean(h.get("edges_per_s") for h in hist[1:] or hist), 0)
        src["gnn_edges_per_s"] = "gnn_metrics.json: mean history[].edges_per_s (epochs after the first)"
        peaks = [x for x in (_num(h.get("peak_mem_mb")) for h in hist) if x is not None]
        head["gnn_peak_mem_gb"] = round(max(peaks) / 1024, 2) if peaks else None
        src["gnn_peak_mem_gb"] = "gnn_metrics.json: max history[].peak_mem_mb / 1024"
        head["gnn_val_auc"] = _r((best or {}).get("val_auc") or _get(met, "val", "seed", "auc"))
        src["gnn_val_auc"] = "gnn_metrics.json: history[best_epoch].val_auc"
        head["gnn_test_auc"] = _r(_get(met, "test", "pooled", "seed", "auc"))
        src["gnn_test_auc"] = "gnn_metrics.json: test.pooled.seed.auc"
        head["gnn_train_s"] = _r(met.get("train_s"), 1)
        src["gnn_train_s"] = "gnn_metrics.json: train_s"
    if ev:
        pooled = _get(ev, "primary", "pooled") or {}
        out["heldout"] = {"label": ev.get("label"), "pooled": pooled}
        g = pooled.get("gnn") or {}
        head["gnn_heldout_disposition"] = _r(g.get("disposition_accuracy"))
        src["gnn_heldout_disposition"] = "gnn_eval.json: primary.pooled.gnn.disposition_accuracy"
        head["gnn_heldout_ih_recall"] = _r(_get(g, "innocent_holder", "recall"))
        src["gnn_heldout_ih_recall"] = "gnn_eval.json: primary.pooled.gnn.innocent_holder.recall"
    if hard:
        key = next((k for k in (hard.get("by_scorer") or {}) if k.startswith("gnn:")), None)
        g = (hard.get("by_scorer") or {}).get(key) or {}
        h = (hard.get("by_scorer") or {}).get("heuristic-v2") or {}
        out["hard_world"] = {"world": hard.get("world"), "cases": hard.get("cases"), "gnn_scorer": key,
                             "gnn": {k: g.get(k) for k in ("disposition_accuracy", "role_recall", "role_precision")},
                             "gnn_classification": g.get("classification"),
                             "heuristic": {k: h.get(k) for k in ("disposition_accuracy", "role_recall")}}
        for hk, path in (("gnn_hard_disposition", ("disposition_accuracy",)),
                         ("gnn_hard_mule_auc", ("classification", "mule_auc")),
                         ("gnn_hard_ih_recall", ("role_recall", "INNOCENT_HOLDER"))):
            head[hk] = _r(_get(g, *path))
            src[hk] = f"gnn_hard_eval.json: by_scorer[{key}].{'.'.join(path)}"
        head["gnn_hard_world"] = hard.get("world")
    return out


def lora(evid: Path, head: dict[str, Any], src: dict[str, str]) -> dict[str, Any] | None:
    ev, tr = _load(evid / "lora_eval.json"), _load(evid / "lora_train_metrics.json")
    ds, mg = _load(evid / "lora_dataset.json"), _load(evid / "lora_merge_report.json")
    if not (ev or tr or ds or mg):
        return None
    out: dict[str, Any] = {}
    if ev:
        models = ev.get("models") or {}
        out["eval"] = {label: {k: _get(m, "overall", k) for k in (
            "acceptance_rate", "fallback_rate", "prose_verifier_pass_rate", "all_drafts_verifier_pass_rate",
            "placeholder_coverage", "invented_number_rate", "injection_obeyed_rate")} for label, m in models.items()}
        out["eval_cases"] = ev.get("cases")
        for label in ("base", "tuned"):
            for key in ("prose_verifier_pass_rate", "fallback_rate", "acceptance_rate", "invented_number_rate"):
                head[f"lora_{label}_{key}"] = _r(_get(models, label, "overall", key))
                src[f"lora_{label}_{key}"] = f"lora_eval.json: models.{label}.overall.{key}"
    if tr:
        out["train"] = {k: tr.get(k) for k in ("base_model", "device", "dtype", "train_examples", "global_steps",
                                               "train_runtime_s", "eval_loss_before", "eval_loss_after",
                                               "peak_mem_gb", "trainable_params", "total_params")}
        out["train"]["tokens_per_s"] = tr.get("tokens_per_s")
        head["lora_tokens_per_s"] = _r(_get(tr, "tokens_per_s", "mean"), 1)
        src["lora_tokens_per_s"] = "lora_train_metrics.json: tokens_per_s.mean"
        head["lora_peak_mem_gb"] = _r(tr.get("peak_mem_gb"), 1)
        src["lora_peak_mem_gb"] = "lora_train_metrics.json: peak_mem_gb"
        head["lora_train_runtime_s"] = _r(tr.get("train_runtime_s"), 1)
        src["lora_train_runtime_s"] = "lora_train_metrics.json: train_runtime_s"
    if ds:
        out["dataset"] = {k: ds.get(k) for k in ("teacher", "requests", "accepted", "rejected", "splits")
                          if k in ds} or {"keys": sorted(ds)[:20]}
    if mg:
        out["merge"] = {"passed": mg.get("passed"), "adapter_vs_merged": mg.get("adapter_vs_merged")}
    return out


GOLDEN_KEYS = ("hold_exact_match_rate", "false_freeze_count", "disposition_accuracy", "draft_verification_pass_rate",
               "draft_numeric_accuracy", "citation_validity", "llm_only_hallucination_rate", "llm_prose_fallback_rate",
               "injection_flag_recall", "injection_leaks")


def golden(evid: Path, head: dict[str, Any], src: dict[str, str], warn: list[str],
           *, rehearsal: bool = False) -> dict[str, Any] | None:
    """The golden-set evaluation with the model under test (``golden_eval.py``). A stub-LLM report is a plumbing
    check: outside the rehearsal it is flagged and gives no headline number."""
    rep = _load(evid / "golden_eval.json")
    if not rep:
        return None
    m = rep.get("metrics") or {}
    usable = rehearsal or not rep.get("stub")
    if not usable:
        warn.append("golden_eval.json was written with a stub LLM (--fake): no golden-set number is reported "
                    "(re-run --stage eval against the model)")
    for key in GOLDEN_KEYS:
        head[f"golden_{key}"] = _num(m.get(key)) if usable else None
        src[f"golden_{key}"] = f"golden_eval.json: metrics.{key}"
    head["golden_cases"] = rep.get("cases") if usable else None
    src["golden_cases"] = "golden_eval.json: cases"
    return {"label": rep.get("label"), "world": rep.get("world"), "cases": rep.get("cases"),
            "adversarial_cases": rep.get("adversarial_cases"), "llm": _get(rep, "config", "llm"),
            "scorer": _get(rep, "config", "scorer"), "stub": rep.get("stub"),
            "metrics": {k: m.get(k) for k in GOLDEN_KEYS}, "failures": len(rep.get("failures") or [])}


AB_KEYS = ("docs", "docs_with_wrong_amount", "invented_accounts", "unknown_citations", "verifier_pass_rate",
           "clean_rate")


def ab(evid: Path, head: dict[str, Any], src: dict[str, str], warn: list[str],
       *, rehearsal: bool = False) -> dict[str, Any] | None:
    rep = _load(evid / "ab_hallucination.json")
    if not rep:
        return None
    if rep.get("stub") and not rehearsal:  # stub LLMs (--fake) in a real run's folder: a plumbing check, no number
        warn.append("ab_hallucination.json was written with stub LLMs (--fake): no A/B number is reported (re-run "
                    "--stage ab against the model)")
        return {"label": rep.get("label"), "llm": rep.get("llm"), "world": rep.get("world"), "cases": rep.get("cases"),
                "cases_compared": None, "stub": True, "arms": None}
    # like with like: ab_bound_str_* are the bound arm's STRs alone (one per case, as in the raw arm)
    for prefix, path in (("ab_raw", ("raw",)), ("ab_bound", ("bound",)), ("ab_bound_str", ("bound", "str_only"))):
        a = _get(rep, "arms", *path) or {}
        for key in AB_KEYS:
            head[f"{prefix}_{key}"] = _num(a.get(key)) if key.endswith("rate") else a.get(key)
            src[f"{prefix}_{key}"] = f"ab_hallucination.json: arms.{'.'.join(path)}.{key}"
    head["ab_bound_prose_fallbacks"] = _get(rep, "arms", "bound", "prose_fallbacks")
    src["ab_bound_prose_fallbacks"] = "ab_hallucination.json: arms.bound.prose_fallbacks"
    head["ab_cases"] = rep.get("cases")
    head["ab_cases_compared"] = rep.get("cases_compared", rep.get("cases"))
    errors = {arm: _get(rep, "arms", arm, "case_errors") or 0 for arm in ("raw", "bound")}
    if not head["ab_cases_compared"]:  # nothing compared (e.g. every request failed): no A/B number at all
        for k in [k for k in head if k.startswith(("ab_raw_", "ab_bound_"))]:
            head[k] = None
        warn.append(f"ab_hallucination.json: no case was completed by both arms (case errors raw {errors['raw']}, "
                    f"bound {errors['bound']}); no A/B number is reported (re-run --stage ab)")
    elif any(errors.values()):
        warn.append(f"ab_hallucination.json: case errors raw {errors['raw']}, bound {errors['bound']}; both arms are "
                    f"compared on the {head['ab_cases_compared']} of {rep.get('cases')} cases both completed")
    return {"label": rep.get("label"), "llm": rep.get("llm"), "world": rep.get("world"), "cases": rep.get("cases"),
            "cases_compared": head["ab_cases_compared"], "stub": rep.get("stub"), "arms": rep.get("arms")}


def stages(evid: Path, warn: list[str] | None = None) -> list[dict[str, Any]]:
    out = []
    for path in sorted((evid / "stages").glob("*.json")):
        rec = _load(path)
        if rec:
            out.append({k: rec.get(k) for k in ("index", "stage", "status", "started_at", "ended_at", "duration_s",
                                                "window", "notes")})
            notes = rec.get("notes") or {}
            if warn is not None and str(notes.get("measure_failures") or "0") != "0":
                warn.append(f"stage {rec.get('stage')}: {notes['measure_failures']} measurement(s) exited non-zero "
                            f"({notes.get('measure_failed') or '?'}); the stage went on, see {rec.get('log')}")
    return out


def hardware(evid: Path) -> dict[str, Any]:
    """This machine's GPU identity, best source first: ``amd-smi static`` (env or evidence stage); the bench
    telemetry, whose GPU names come from the same tools (a later-week ``--stage bench,ab,evidence`` run); a ROCm
    (HIP) build of torch (``torch_info.json``, the notebook without ``amd-smi``). Empty for an endpoint run."""
    st = _load(evid / "amd_smi_static.json") or {}
    gpus = st.get("gpus") or []
    name = gpus[0].get("name") if gpus else None
    source = "amd_smi_static.json" if name else None
    if not name:
        tel = _load(evid / "bench" / "telemetry_summary.json") or {}
        tel_name = _get(tel, "gpus", 0, "name")
        if tel_name and tel.get("source") in ("amd-smi", "rocm-smi"):
            name, source = str(tel_name), f"bench/telemetry_summary.json ({tel.get('source')})"
    torch = _load(evid / "torch_info.json") or {}
    if not name and torch.get("available") and torch.get("hip") and torch.get("device_name"):
        name, source = str(torch["device_name"]), "torch_info.json (torch.cuda.get_device_name, ROCm build)"
    vram = _num(torch.get("vram_total_mb"))
    return {"available": bool(st.get("available")), "gpus": gpus, "gpu_name": name, "gpu_name_source": source,
            "sriov_vf": any(g.get("sriov_vf") for g in gpus), "rocm_version": st.get("rocm_version"),
            "torch": {"version": torch.get("torch"), "hip": torch.get("hip"),
                      "vram_total_gb": round(vram / 1024, 1) if vram else None} if torch else None,
            "captured_at": st.get("captured_at"), "reason": st.get("reason")}


def endpoint_info(evid: Path, head: dict[str, Any], src: dict[str, str], results: dict[str, Any]) -> dict[str, Any] | None:
    """Where an endpoint run's calls went (``endpoint.json`` from ``endpoint.py probe``), the model plan, and what
    the API said the run cost: the sum of ``usage.cost`` over the result files' ``endpoint`` blocks."""
    info = _load(evid / "endpoint.json")
    plan = _load(evid / "model_plan.json")
    blocks = {name: _get(_load(evid / rel) or {}, "endpoint")
              for name, rel in (("golden_eval", "golden_eval.json"), ("ab", "ab_hallucination.json"))}
    for path in sorted((evid / "bench").glob("e2e*.json")):
        blocks[path.stem] = _get(_load(path) or {}, "endpoint")
    blocks = {k: v for k, v in blocks.items() if isinstance(v, dict)}
    bench_cost = [_num(lv.get("cost_usd")) for sv in (results.get("serving") or {}).values()
                  for lv in sv.get("levels") or []]
    costs = [_num(b.get("cost_usd")) for b in blocks.values()] + bench_cost
    costs = [c for c in costs if c is not None]
    if not (info or plan or blocks):
        return None
    if plan:
        head["drafting_model_planned"] = _get(plan, "drafting", "id")
        head["judge_model_id"] = _get(plan, "judge", "id")
    if info or blocks:
        head["endpoint_host"] = (info or {}).get("endpoint_host") or next(
            (b.get("endpoint_host") for b in blocks.values() if b.get("endpoint_host")), None)
        head["model_id"] = (info or {}).get("model") or next(
            (b.get("model") for b in blocks.values() if b.get("model")), None)
        head["api_cost_usd_total"] = round(sum(costs), 6) if costs else None
        src["api_cost_usd_total"] = ("sum of usage.cost in the endpoint blocks of golden_eval, ab_hallucination, "
                                     "e2e* and in the llm_bench* levels")
        head["api_calls_total"] = sum(int(b.get("calls") or 0) for b in blocks.values()) or None
        head["api_rate_limited_429_total"] = sum(int(b.get("rate_limited_429") or 0) for b in blocks.values())
        hosts = {b.get("endpoint_host") for b in blocks.values() if b.get("endpoint_host")}
        if info and hosts - {info.get("endpoint_host")}:
            head["endpoint_host_mismatch"] = sorted(str(h) for h in hosts - {info.get("endpoint_host")})
    return {"endpoint": {k: (info or {}).get(k) for k in ("endpoint_host", "base_url", "provider", "model",
                                                          "model_requested", "model_fallback",
                                                          "model_served", "model_card", "limits", "probe", "gpu",
                                                          "json_mode", "probed_at", "ok", "error")} if info else None,
            "model_plan": {k: plan.get(k) for k in ("platform", "drafting", "judge", "lora", "lora_config",
                                                    "requested", "fallback_in_use", "warnings")} if plan else None,
            "calls": {name: {k: b.get(k) for k in ("calls", "cache_hits", "errors", "rate_limited_429",
                                                   "prompt_tokens", "completion_tokens", "cost_usd")}
                      for name, b in blocks.items()}}


def compute(mode: str, hw: dict[str, Any], ep: dict[str, Any] | None) -> dict[str, Any]:
    """Where the workload ran, in the words the pitch may use (``short``) and in full (``label``)."""
    name = hw.get("gpu_name")
    info = (ep or {}).get("endpoint") or {}
    prov = info.get("provider") or {}
    model = info.get("model")
    if mode == "droplet":
        short = "MI300X" if name and "MI300" in str(name) else str(name or "unidentified GPU")
        return {"kind": "amd-developer-cloud-droplet", "gpu_side": "local", "gpu_name": name, "short": short,
                "label": f"AMD Developer Cloud droplet, {name or 'GPU not identified'}"}
    if mode == "notebook":
        short = f"{'MI300X' if name and 'MI300' in str(name) else name or 'unidentified GPU'} (AMD ROCm notebook)"
        return {"kind": "amd-rocm-notebook", "gpu_side": "local", "gpu_name": name, "short": short,
                "label": f"AMD ROCm notebook (no Docker), {name or 'GPU not identified'}"}
    if mode == "endpoint" and not info:
        return {"kind": "provider-endpoint", "gpu_side": "provider", "gpu_name": None, "endpoint_host": None,
                "model_id": None, "provider": None, "amd_served": None, "short": "an endpoint that was not identified",
                "label": "endpoint not identified (endpoint.json is missing)"}
    if mode == "endpoint":
        host = info.get("endpoint_host")
        if prov.get("kind") == "amd-token-factory":
            short = f"AMD Token Factory ({model} on AMD GPUs)"
            label = (f"AMD Token Factory endpoint {host}, model {model}: served on the provider's AMD GPUs through "
                     "the shared trial API; the GPU model is not visible to a client (not an MI300X droplet benchmark)")
        else:
            declared = prov.get("hardware_declared")
            short = f"{host} ({model}{'; ' + declared + ', as declared' if declared else ''})"
            label = (f"OpenAI-compatible endpoint {host}, model {model}; hardware "
                     + (f"declared by the operator, not measured: {declared}" if declared else "not verified"))
        return {"kind": "provider-endpoint", "gpu_side": prov.get("gpu_side") or "provider", "gpu_name": None,
                "endpoint_host": host, "model_id": model, "provider": prov.get("name"),
                "amd_served": prov.get("amd_served"), "short": short, "label": label}
    return {"kind": "cpu-rehearsal", "gpu_side": "none", "gpu_name": None, "short": "CPU rehearsal",
            "label": "laptop CPU rehearsal (no GPU, stub models)"}


MAX_HEALTHY_FALLBACK_RATE = 0.5  # Gemma 4 on the Token Factory, healthy: ~0.24 on the showcase; garbage: 1.0
FALLBACK_KEYS = ("golden_llm_prose_fallback_rate", "e2e_best_prose_fallback_rate")


def model_health(head: dict[str, Any], mode: str = "endpoint") -> list[str]:
    """Reasons a run's real-model figures cannot be used: the prose was mostly the fixed paragraph. The golden eval
    counts in every mode; the end-to-end bench only in endpoint mode (on an own GPU its throughput stands apart
    from the quality of a base model's prose, which the LoRA comparison reports)."""
    reasons = []
    for key in FALLBACK_KEYS if mode == "endpoint" else FALLBACK_KEYS[:1]:
        rate = head.get(key)
        if isinstance(rate, int | float) and not isinstance(rate, bool) and rate > MAX_HEALTHY_FALLBACK_RATE:
            reasons.append(f"{key} = {rate}: the model's prose was rejected for most drafts (a degraded endpoint "
                           "answering in garbage?); the run measured the fixed paragraph, not the model")
    return reasons


def validity(mode: str, hw: dict[str, Any], stage_rows: list[dict[str, Any]],
             ep: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    reasons = []
    name = hw.get("gpu_name")
    if mode == "rehearsal":
        reasons.append("CPU rehearsal: not a GPU run")
    if mode in ("droplet", "rehearsal") and not (name and "MI300" in str(name)):
        reasons.append("neither amd-smi static nor the bench telemetry names an AMD Instinct MI300X")
    if mode == "notebook":
        from_torch = str(hw.get("gpu_name_source") or "").startswith("torch_info")
        if not name:
            reasons.append("no GPU identity: amd-smi / rocm-smi did not answer and no ROCm build of torch saw a device")
        elif not (from_torch or AMD_GPU_RE.search(str(name))
                  or str(hw.get("gpu_name_source") or "").startswith(("amd_smi", "bench/telemetry"))):
            reasons.append(f"the GPU {name!r} is not identified as an AMD GPU")
    if mode == "endpoint":
        info = (ep or {}).get("endpoint") or {}
        prov = info.get("provider") or {}
        if not info:
            reasons.append("endpoint.json is missing: the env stage (endpoint.py probe) records the endpoint host and model")
        else:
            if not info.get("ok"):
                reasons.append(f"the endpoint probe failed: {info.get('error') or 'unknown'}")
            if not (info.get("endpoint_host") and info.get("model")):
                reasons.append("the endpoint host or the model id is not recorded")
            if prov.get("kind") == "self-hosted":
                reasons.append("the endpoint is on this machine: use notebook or droplet mode so its GPU is identified")
            elif not prov.get("amd_served"):
                reasons.append("the endpoint is not an AMD host and WAPSI_ENDPOINT_HARDWARE declares no AMD hardware")
    if not stage_rows:
        reasons.append("no stage records")
    start, end = parse_ts(EVENT_START), parse_ts(EVENT_END)
    for s in stage_rows:
        t = s.get("started_at")
        if not t or not (start <= parse_ts(t) <= end):
            reasons.append(f"stage {s.get('stage')} started at {t}, outside the event window")
    return not reasons, reasons


def build(evid: Path, mode: str) -> dict[str, Any]:
    mode = canon_mode(mode)
    head: dict[str, Any] = {}
    src: dict[str, str] = {}
    deriv: dict[str, str] = {}
    warn: list[str] = []
    stage_rows = stages(evid, warn)
    hw = hardware(evid)
    results: dict[str, Any] = {
        "serving": serving(evid, head, src, warn),
        "e2e": end_to_end(evid, head, src, deriv, warn, own_gpu=mode != "endpoint", rehearsal=mode == "rehearsal"),
        "memory": memory(evid, head, src),
        "gnn": gnn(evid, head, src),
        "lora": lora(evid, head, src),
        "golden": golden(evid, head, src, warn, rehearsal=mode == "rehearsal"),
        "ab": ab(evid, head, src, warn, rehearsal=mode == "rehearsal"),
    }
    ep = endpoint_info(evid, head, src, results)
    results["endpoint"] = ep
    for w in _get(ep, "model_plan", "warnings") or []:
        warn.append(f"model plan: {w}")
    if head.get("endpoint_host_mismatch"):
        warn.append(f"result files name another endpoint host than endpoint.json: {head['endpoint_host_mismatch']} "
                    "(re-run the stages against one endpoint)")
    valid, reasons = validity(mode, hw, stage_rows, ep)
    if mode != "rehearsal":
        unhealthy = model_health(head, mode)
        reasons += unhealthy
        valid = valid and not unhealthy
    where = compute(mode, hw, ep)
    budget = next((s.get("budget") for s in reversed([_load(p) or {} for p in sorted((evid / "stages").glob("*.json"))])
                   if s.get("budget")), None)
    started = [s["started_at"] for s in stage_rows if s.get("started_at")]
    ended = [s["ended_at"] for s in stage_rows if s.get("ended_at")]
    head["measured_date"] = evid.name if re.fullmatch(r"\d{4}-\d{2}-\d{2}", evid.name) else None
    head["gpu_name"] = hw.get("gpu_name")
    labels = {"droplet": GPU_LABEL, "notebook": NOTEBOOK_LABEL, "endpoint": ENDPOINT_LABEL,
              "rehearsal": REHEARSAL_LABEL}

    def note(key: str) -> Any:
        return next((s["notes"].get(key) for s in stage_rows if (s.get("notes") or {}).get(key)), None)

    return {
        "schema": SCHEMA,
        "mode": "cpu-rehearsal" if mode == "rehearsal" else mode,
        "label": labels[mode],
        "compute": where,
        "results_valid": valid,
        "gpu_results_valid": valid and mode in ("droplet", "notebook"),
        "invalid_reasons": reasons,
        "date": evid.name,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event_window": {"start": EVENT_START, "end": EVENT_END},
        "run": {"first_stage_started_at": min(started) if started else None,
                "last_stage_ended_at": max(ended) if ended else None,
                "git_commit": note("git_commit"), "amd_baseline": note("amd_baseline"),
                "served_profile": note("served_profile"), "serving_stack": note("serving_stack")},
        "hardware": hw,
        "stages": stage_rows,
        "budget": budget,
        "headline": head,
        "sources": src,
        "derivations": deriv,
        "missing": sorted(k for k, v in head.items() if v is None),
        "warnings": warn,
        "results": results,
    }


# --------------------------------------------------------------------------- markdown


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.8f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(_fmt(c).replace("|", "\\|") for c in r) + " |" for r in rows]
    return out


HEADLINE_ROWS = [
    ("Drafting model output tokens/s at concurrency 1 / 4 / 16 / 64",
     ("llm_tok_s_c1", "llm_tok_s_c4", "llm_tok_s_c16", "llm_tok_s_c64")),
    ("TTFT p50 / p95 at concurrency 16 (s)", ("llm_ttft_p50_s_c16", "llm_ttft_p95_s_c16")),
    ("Request latency p95 at concurrency 16 (s)", ("llm_e2e_p95_s_c16",)),
    ("Verifier output tokens/s at concurrency 1 / 4 / 16", ("verifier_tok_s_c1", "verifier_tok_s_c4", "verifier_tok_s_c16")),
    ("Cases per hour at concurrency 1 / 8 / 16", ("e2e_cases_per_hour_c1", "e2e_cases_per_hour_c8", "e2e_cases_per_hour_c16")),
    ("Drafts per GPU-hour (best concurrency)", ("e2e_best_drafts_per_gpu_hour",)),
    ("$ per case (best concurrency)", ("e2e_best_usd_per_case",)),
    ("₹ per case (best concurrency, the day's rate)", ("e2e_best_inr_per_case",)),
    ("Draft verification pass rate / prose fallback rate", ("e2e_best_draft_verification_pass_rate",
                                                            "e2e_best_prose_fallback_rate")),
    ("Peak VRAM: llm / verifier / gnn / device (GB)", ("vram_llm_gb", "vram_verifier_gb", "vram_gnn_gb", "vram_peak_gb")),
    ("GNN: s per epoch / validation AUC / peak memory (GB)", ("gnn_epoch_s", "gnn_val_auc", "gnn_peak_mem_gb")),
    ("GNN on the hard world: dispositions / mule AUC / innocent-holder recall",
     ("gnn_hard_disposition", "gnn_hard_mule_auc", "gnn_hard_ih_recall")),
    ("LoRA: verifier pass rate base -> tuned", ("lora_base_prose_verifier_pass_rate", "lora_tuned_prose_verifier_pass_rate")),
    ("LoRA: prose fallback rate base -> tuned", ("lora_base_fallback_rate", "lora_tuned_fallback_rate")),
    ("LoRA training: tokens/s / peak memory (GB)", ("lora_tokens_per_s", "lora_peak_mem_gb")),
]
QUALITY_ROWS = [
    ("Golden set with this model: cases / hold exact match / false freezes",
     ("golden_cases", "golden_hold_exact_match_rate", "golden_false_freeze_count")),
    ("Golden set: draft numeric accuracy / citation validity / verifier pass rate",
     ("golden_draft_numeric_accuracy", "golden_citation_validity", "golden_draft_verification_pass_rate")),
    ("Golden set: prose fallback rate / injection leaks", ("golden_llm_prose_fallback_rate", "golden_injection_leaks")),
    ("Same model without slots (LLM-only baseline): cases with an amount not in the case",
     ("golden_llm_only_hallucination_rate",)),
    ("A/B like with like, STRs with a wrong amount: raw / bound", ("ab_raw_docs_with_wrong_amount",
                                                                   "ab_bound_str_docs_with_wrong_amount")),
    ("A/B STRs compared: raw / bound", ("ab_raw_docs", "ab_bound_str_docs")),
    ("A/B all bound drafts with a wrong amount / all bound drafts", ("ab_bound_docs_with_wrong_amount",
                                                                     "ab_bound_docs")),
    ("A/B invented account numbers: raw / bound", ("ab_raw_invented_accounts", "ab_bound_invented_accounts")),
    ("A/B unknown or repealed citations: raw / bound", ("ab_raw_unknown_citations", "ab_bound_unknown_citations")),
    ("A/B clean documents rate: raw / bound", ("ab_raw_clean_rate", "ab_bound_clean_rate")),
]


def _levels(ev: dict[str, Any], bench: str) -> list[int]:
    lv = _get(ev, "results", "serving", bench, "levels") or []
    return [x["concurrency"] for x in lv if isinstance(x.get("concurrency"), int)]


def headline_rows(ev: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    """The headline table. Droplet and rehearsal: the fixed MI300X sweep. Notebook and endpoint: the levels the run
    measured; an endpoint run has no GPU-hour, memory, GNN or LoRA rows (nothing of that ran on this side)."""
    if ev["mode"] in ("droplet", "gpu", "cpu-rehearsal"):
        return HEADLINE_ROWS + QUALITY_ROWS
    llm, wp = _levels(ev, "llm"), _levels(ev, "wapsi_prompts")
    e2e = sorted({r["concurrency"] for r in ev["results"]["e2e"] if isinstance(r.get("concurrency"), int)})

    def join(xs: list[int]) -> str:
        return " / ".join(str(x) for x in xs)

    rows: list[tuple[str, tuple[str, ...]]] = []
    if llm:
        rows += [(f"Drafting model output tokens/s at concurrency {join(llm)}", tuple(f"llm_tok_s_c{c}" for c in llm)),
                 (f"Request latency p50 / p95 at concurrency {llm[0]} (s)",
                  (f"llm_e2e_p50_s_c{llm[0]}", f"llm_e2e_p95_s_c{llm[0]}")),
                 (f"TTFT p50 / p95 at concurrency {llm[0]} (s, as the client sees it)",
                  (f"llm_ttft_p50_s_c{llm[0]}", f"llm_ttft_p95_s_c{llm[0]}")),
                 (f"Output tokens/s per request, p50, at concurrency {join(llm)}",
                  tuple(f"llm_req_tok_s_p50_c{c}" for c in llm))]
    if wp:
        rows.append((f"Real drafting prompts: request latency p50 / p95 at concurrency {wp[0]} (s)",
                     (f"wapsi_prompts_e2e_p50_s_c{wp[0]}", f"wapsi_prompts_e2e_p95_s_c{wp[0]}")))
    if e2e:
        rows.append((f"Cases per hour at concurrency {join(e2e)}"
                     + (" (bounded by the request cap)" if ev["headline"].get("e2e_rate_capped") else ""),
                     tuple(f"e2e_cases_per_hour_c{c}" for c in e2e)))
    rows += [("Drafts per hour (best run)", ("e2e_best_drafts_per_hour",)),
             ("LLM calls per case / call latency p50 / p95 (s) / LLM seconds per case p50",
              ("e2e_best_llm_calls_per_case", "e2e_best_llm_latency_p50_s", "e2e_best_llm_latency_p95_s",
               "e2e_best_llm_seconds_per_case_p50")),
             ("Draft verification pass rate / prose fallback rate", ("e2e_best_draft_verification_pass_rate",
                                                                     "e2e_best_prose_fallback_rate"))]
    if ev["mode"] == "endpoint":
        rows += [("API-reported cost per case ($, relative usage cost; the trial is not billed)",
                  ("e2e_best_api_usd_per_case",)),
                 ("API-reported cost of the whole run ($) / calls / HTTP 429s",
                  ("api_cost_usd_total", "api_calls_total", "api_rate_limited_429_total")),
                 ("GPU-hour cost per case", ("e2e_best_usd_per_case",))]
    else:
        rows += [r for r in HEADLINE_ROWS if r[1][0].startswith(("e2e_best_drafts_per_gpu", "e2e_best_usd", "e2e_best_inr",
                                                                "vram_", "gnn_", "lora_"))]
    return rows + QUALITY_ROWS


def to_markdown(ev: dict[str, Any]) -> str:
    h, hw, where = ev["headline"], ev["hardware"], ev.get("compute") or {}
    mode = ev["mode"]
    rehearsal = mode == "cpu-rehearsal"
    title = {"cpu-rehearsal": f"# Wapsi AMD evidence: CPU rehearsal, {ev['date']}",
             "notebook": f"# Wapsi on {where.get('short')}: evidence, {ev['date']}",
             "endpoint": f"# Wapsi on {where.get('short')}: evidence, {ev['date']}"}.get(
        mode, f"# Wapsi on AMD Instinct MI300X: evidence, {ev['date']}")
    lines = [title, ""]
    if rehearsal:
        lines += [f"> **{REHEARSAL_LABEL}**", ""]
    if mode == "endpoint":
        lines += ["> **Endpoint run.** The LLM calls were served by the provider's GPUs through its API. The GPU is not "
                  "visible to a client, so this bundle names the endpoint and the model and makes no GPU name, memory "
                  "or power claim. These are not MI300X droplet benchmarks. Throughput per hour through a shared "
                  "trial key is bounded by its request cap and is labelled so.", ""]
    valid = ev.get("results_valid", ev.get("gpu_results_valid"))
    lines += [f"- Label: {ev['label']}",
              f"- Where it ran: {where.get('label') or 'n/a'}",
              f"- Results valid: **{_fmt(valid)}**"
              + (f" ({'; '.join(ev['invalid_reasons'][:3])})" if ev["invalid_reasons"] else ""),
              f"- Run: {_fmt(ev['run']['first_stage_started_at'])} .. {_fmt(ev['run']['last_stage_ended_at'])} UTC · "
              f"commit `{_fmt(ev['run']['git_commit'])}` · amd-baseline `{_fmt(ev['run']['amd_baseline'])}` · "
              f"served profile {_fmt(ev['run']['served_profile'])}",
              f"- Event window: {ev['event_window']['start']} .. {ev['event_window']['end']}",
              f"- Generated {ev['generated_at']} by `editions/amd/kickoff/collect_evidence.py` from the files in this "
              "folder. Every value is read from a result file (see `sources` in evidence.json).", ""]
    if ev.get("warnings"):
        lines += ["## Warnings", ""] + [f"- {w}" for w in ev["warnings"]] + [""]
    ep = _get(ev, "results", "endpoint") or {}
    info, plan = ep.get("endpoint") or {}, ep.get("model_plan") or {}
    if mode == "endpoint":
        card, limits, probe = info.get("model_card") or {}, info.get("limits") or {}, info.get("probe") or {}
        lines += ["## Endpoint (`endpoint.py probe`)", ""]
        lines += _table(["Field", "Value"], [
            ["Endpoint host", info.get("endpoint_host")],
            ["Provider", _get(info, "provider", "name")],
            ["GPU", f"{_get(info, 'provider', 'gpu_side') or 'provider'}-side: {_get(info, 'provider', 'basis')}"],
            ["Drafting model", f"{info.get('model')} ({_get(plan, 'drafting', 'source')}: {_get(plan, 'drafting', 'reason')})"
             + ("; Google open model (Gemma)" if _get(plan, "drafting", "google_open_model") else "")],
            ["Judge model", _get(plan, "judge", "id") or "none (rules-only screening)"],
            ["JSON mode", info.get("json_mode")],
            ["Context length", card.get("context_length")],
            ["List price per token: prompt / completion ($)",
             f"{_fmt(card.get('price_prompt_usd_per_token'))} / {_fmt(card.get('price_completion_usd_per_token'))}"],
            ["Limits: requests per minute / daily cost cap ($)",
             f"{_fmt(limits.get('rpm_limit'))} / {_fmt(limits.get('daily_cost_limit_usd'))}"],
            ["Probe: latency (s) / tokens in + out / cost ($)",
             f"{_fmt(probe.get('latency_s'))} / {_fmt(probe.get('prompt_tokens'))} + "
             f"{_fmt(probe.get('completion_tokens'))} / {_fmt(probe.get('cost_usd'))}"]])
    else:
        lines += ["## Hardware (`amd-smi static`)", ""]
        if hw.get("available"):
            lines += _table(["GPU", "Name", "SR-IOV VF", "ROCm"],
                            [[g.get("gpu"), g.get("name"), g.get("sriov_vf"), hw.get("rocm_version")] for g in hw["gpus"]])
        elif hw.get("gpu_name"):
            lines += [f"`amd-smi static` not captured in this folder; GPU name from {hw.get('gpu_name_source')}: "
                      f"{hw['gpu_name']}."]
        else:
            lines += [f"Not captured: {hw.get('reason') or 'amd-smi static missing'}."]
        if plan:
            lines += ["", f"Models: drafting `{_get(plan, 'drafting', 'id')}` "
                      f"({_get(plan, 'drafting', 'reason')}), judge `{_get(plan, 'judge', 'id') or 'none'}`, LoRA "
                      f"{'preset ' + str(plan.get('lora_config')) if plan.get('lora') else 'skipped'}."]
    lines += ["", "## Headline", ""]
    lines += _table(["Metric", "Value"], [[label, " / ".join(_fmt(h.get(k)) for k in keys)]
                                          for label, keys in headline_rows(ev)])
    e2e = ev["results"]["e2e"]
    if e2e and mode == "endpoint":
        lines += ["", "## End to end (golden cases through the pipeline, LLM on the endpoint)", ""]
        lines += _table(["Concurrency", "Cases", "Cases/hour", "Drafts/hour", "LLM calls/case", "Call p50 (s)",
                         "Call p95 (s)", "LLM s/case p50", "Verifier pass", "Prose fallback", "API $ per case", "File"],
                        [[r["concurrency"], r["cases"], r["cases_per_hour"], r["drafts_per_hour"],
                          r["llm_calls_per_case"], r["llm_latency_p50_s"], r["llm_latency_p95_s"],
                          r["llm_seconds_per_case_p50"], r["draft_verification_pass_rate"], r["prose_fallback_rate"],
                          r["api_usd_per_case"], r["file"]] for r in e2e])
    elif e2e:
        lines += ["", "## End to end (golden cases through the pipeline)", ""]
        lines += _table(["Concurrency", "Cases", "Cases/hour", "Drafts/GPU-hour", "Hold plan p50 (s)", "Case p95 (s)",
                         "Verifier pass", "Prose fallback", "$ per case", "₹ per case", "File"],
                        [[r["concurrency"], r["cases"], r["cases_per_hour"], r["drafts_per_gpu_hour"],
                          r["time_to_hold_plan_p50_s"], r["case_p95_s"], r["draft_verification_pass_rate"],
                          r["prose_fallback_rate"], r["usd_per_case"], r["inr_per_case"], r["file"]] for r in e2e])
    for key, name in (("llm", "Drafting model"), ("wapsi_prompts", "Drafting model, real drafting prompts"),
                      ("verifier", "Verifier model"), ("base", "Base model (before LoRA)")):
        sv = ev["results"]["serving"].get(key)
        if not sv or (key == "wapsi_prompts" and mode in ("droplet", "gpu", "cpu-rehearsal")):
            continue
        lines += ["", f"## Serving: {name} `{sv.get('model')}`", ""]
        if sv.get("label"):
            lines += [f"Label: {sv['label']}", ""]
        lines += _table(["Concurrency", "OK", "Output tok/s", "TTFT p50 (s)", "TTFT p95 (s)", "Latency p95 (s)", "Req/s"],
                        [[lv["concurrency"], lv["ok"], lv["output_tokens_per_s"], lv["ttft_p50_s"], lv["ttft_p95_s"],
                          lv["e2e_p95_s"], lv["requests_per_s"]] for lv in sv["levels"]])
        if any(lv.get("stream_buffered") for lv in sv["levels"]):
            lines += ["", "The gateway delivered streams in one burst: TTFT here is the wait for that burst."]
    lines += ["", "## Stages", ""]
    lines += _table(["#", "Stage", "Status", "Started (UTC)", "Ended (UTC)", "Seconds", "Window"],
                    [[s.get("index"), s.get("stage"), s.get("status"), s.get("started_at"), s.get("ended_at"),
                      s.get("duration_s"), s.get("window")] for s in ev["stages"]])
    b = ev.get("budget")
    if b and b.get("metered", True) and mode == "notebook" and str(b.get("started_source") or "").startswith("boot"):
        # an older notebook bundle: its stage records counted from the shared host's boot time, not the session's start
        lines += ["", f"Budget: not shown. The stage records counted GPU hours from the notebook host's boot time "
                  f"({b.get('droplet_started_at')}), not from the start of the session, so their total is not what "
                  "this run cost; the session times are in the stage table."]
    elif b and b.get("metered", True):
        lines += ["", f"Budget at the last stage ({b.get('label')}): {b.get('hours')} h x ${b.get('gpu_hour_usd')} = "
                  f"${b.get('droplet_usd')} (+ ${b.get('spent_before_usd')} earlier) = ${b.get('total_usd')} of "
                  f"${b.get('budget_usd')} ({b.get('pct')}%)."]
    elif b:
        lines += ["", f"Budget: {b.get('label')}."]
    if ev["missing"]:
        lines += ["", f"Not measured in this run: {', '.join(ev['missing'])}."]
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--evidence-dir", required=True)
    ap.add_argument("--mode", choices=["droplet", "notebook", "endpoint", "rehearsal", "gpu"], required=True,
                    help="gpu = droplet (its old name)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    evid = Path(args.evidence_dir)
    if not evid.is_dir():
        print(f"evidence folder not found: {evid}", file=sys.stderr)
        return 2
    ev = build(evid, args.mode)
    (evid / "evidence.json").write_text(json.dumps(ev, indent=2, ensure_ascii=False, default=str) + "\n",
                                        encoding="utf-8", newline="\n")
    md = to_markdown(ev)
    (evid / "evidence.md").write_text(md, encoding="utf-8", newline="\n")
    if not args.quiet:
        print(md)
    print(f"wrote {evid / 'evidence.json'} and evidence.md ({ev['mode']}, results_valid={ev['results_valid']}, "
          f"gpu_results_valid={ev['gpu_results_valid']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
