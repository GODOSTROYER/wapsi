"""Assemble one evidence bundle from a run_all.sh evidence folder (standard library only).

    python3 editions/amd/kickoff/collect_evidence.py --evidence-dir editions/amd/evidence/2026-10-12 --mode gpu
    uv run python editions/amd/kickoff/collect_evidence.py --evidence-dir editions/amd/evidence/cpu-rehearsal/<date> \
        --mode rehearsal

Writes ``evidence.json`` (machine-readable, read by ``fill_pitch.py``) and ``evidence.md`` (dated tables for the
README and the deck) into the folder. Every number is read from the result files the stages wrote (benchmarks,
telemetry, GNN, LoRA, A/B) or derived from them by the arithmetic shown in ``derivations``; nothing is typed in.
A missing input leaves its headline value ``null`` and is listed under ``missing``.

GPU results are flagged valid (``gpu_results_valid``) only when the run is labelled ``gpu``, ``amd-smi static``
names an MI300X, and every stage started inside the event window (2026-10-12 15:00 UTC .. 2026-10-18 14:00 UTC).
A ``rehearsal`` bundle is always labelled "CPU rehearsal" and is never valid as GPU results.
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

from editions.amd.kickoff.stagelog import EVENT_END, EVENT_START, parse_ts  # noqa: E402

SCHEMA = "wapsi-amd-evidence/1"
GPU_LABEL = "MI300X run on AMD Developer Cloud (lablab.ai AMD Developer Hackathon ACT III)"
REHEARSAL_LABEL = ("CPU REHEARSAL: tiny worlds, a tiny random model and stub LLMs on a laptop. It proves the plumbing "
                   "only. NOT GPU results; never quote these numbers.")
LLM_LEVELS = (1, 4, 16, 64)
VERIFIER_LEVELS = (1, 4, 16)
E2E_LEVELS = (1, 8, 16)


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


def _level(bench: dict[str, Any] | None, concurrency: int) -> dict[str, Any] | None:
    for lv in (bench or {}).get("levels") or []:
        if lv.get("concurrency") == concurrency:
            return lv
    return None


def _mean(values: Iterable[Any]) -> float | None:
    vals = [x for x in (_num(v) for v in values) if x is not None]
    return sum(vals) / len(vals) if vals else None


# --------------------------------------------------------------------------- sections


def serving(evid: Path, head: dict[str, Any], src: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for stem, key, levels in (("llm_bench", "llm", LLM_LEVELS), ("llm_bench_verifier", "verifier", VERIFIER_LEVELS),
                              ("llm_bench_base", "base", LLM_LEVELS),
                              ("llm_bench_wapsi_prompts", "wapsi_prompts", (1, 8, 32))):
        path = evid / "bench" / f"{stem}.json"
        bench = _load(path)
        if bench is None:
            continue
        rel = f"bench/{stem}.json"
        out[key] = {"model": bench.get("model"), "model_root": bench.get("model_root"),
                    "generated_at": bench.get("generated_at"),
                    "levels": [{"concurrency": lv.get("concurrency"), "ok": f"{lv.get('completed')}/{lv.get('requests')}",
                                "output_tokens_per_s": lv.get("output_tokens_per_s"),
                                "ttft_p50_s": _get(lv, "ttft_s", "p50"), "ttft_p95_s": _get(lv, "ttft_s", "p95"),
                                "e2e_p95_s": _get(lv, "e2e_s", "p95"), "requests_per_s": lv.get("requests_per_s")}
                               for lv in bench.get("levels") or []]}
        for c in levels:
            lv = _level(bench, c)
            if lv is None:
                continue
            head[f"{key}_tok_s_c{c}"] = _r(lv.get("output_tokens_per_s"), 1)
            src[f"{key}_tok_s_c{c}"] = f"{rel}: levels[concurrency={c}].output_tokens_per_s"
            if key == "llm":
                for name, path_keys in (("ttft_p50_s", ("ttft_s", "p50")), ("ttft_p95_s", ("ttft_s", "p95")),
                                        ("e2e_p95_s", ("e2e_s", "p95"))):
                    head[f"llm_{name}_c{c}"] = _r(_get(lv, *path_keys), 3)
                    src[f"llm_{name}_c{c}"] = f"{rel}: levels[concurrency={c}].{'.'.join(path_keys)}"
        if key == "llm" and bench.get("levels"):
            best = max(bench["levels"], key=lambda lv: _num(lv.get("output_tokens_per_s")) or 0.0)
            head["llm_peak_tok_s"] = _r(best.get("output_tokens_per_s"), 1)
            head["llm_peak_tok_s_concurrency"] = best.get("concurrency")
            src["llm_peak_tok_s"] = f"{rel}: max over levels of output_tokens_per_s"
            head["llm_model"] = bench.get("model")
    return out


def end_to_end(evid: Path, head: dict[str, Any], src: dict[str, str], deriv: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((evid / "bench").glob("e2e*.json")) + sorted((evid / "bench").glob("report.json")):
        rep = _load(path)
        if not rep or rep.get("kind") != "e2e_bench":
            continue
        m, c, s = rep.get("metrics") or {}, rep.get("cost") or {}, rep.get("settings") or {}
        conc, wall, drafts = s.get("concurrency"), _num(m.get("wall_s")), _num(m.get("drafts"))
        rows.append({
            "file": f"bench/{path.name}",
            "world": rep.get("world"),
            "concurrency": conc,
            "llm": _get(rep, "model", "llm"),
            "cases": f"{m.get('cases_ok')}/{m.get('cases')}",
            "cases_per_hour": m.get("cases_per_hour"),
            "drafts_per_gpu_hour": round(drafts / wall * 3600, 1) if drafts and wall else None,
            "time_to_hold_plan_p50_s": _get(m, "time_to_hold_plan_s", "p50"),
            "case_p95_s": _get(m, "case_total_s", "p95"),
            "draft_verification_pass_rate": m.get("draft_verification_pass_rate"),
            "prose_fallback_rate": m.get("prose_fallback_rate"),
            "hold_exact_match_rate": m.get("hold_exact_match_rate"),
            "usd_per_case": c.get("usd_per_case"),
            "inr_per_case": c.get("inr_per_case"),
            "inr_per_usd": c.get("inr_per_usd"),
            "gpu_hour_usd": c.get("gpu_hour_usd"),
        })
    deriv["drafts_per_gpu_hour"] = "drafts / wall_s x 3600 (one GPU for the whole run, from the e2e report)"
    deriv["usd_per_case"] = "gpu_hour_usd / cases_per_hour (e2e_bench cost block)"
    deriv["inr_per_case"] = "usd_per_case x inr_per_usd, the day's rate passed to e2e_bench"
    for row in rows:
        c = row["concurrency"]
        rel = row["file"]
        for key, field in (("e2e_cases_per_hour", "cases_per_hour"), ("usd_per_case", "usd_per_case"),
                           ("inr_per_case", "inr_per_case"), ("drafts_per_gpu_hour", "drafts_per_gpu_hour")):
            head[f"{key}_c{c}"] = _r(row[field], 6 if "per_case" in key else 1)
            src[f"{key}_c{c}"] = f"{rel}: {field}"
    measured = [r for r in rows if _num(r["cases_per_hour"])]
    if measured:
        best = max(measured, key=lambda r: r["cases_per_hour"])
        head["e2e_best_concurrency"] = best["concurrency"]
        for key in ("cases_per_hour", "usd_per_case", "inr_per_case", "drafts_per_gpu_hour",
                    "draft_verification_pass_rate", "prose_fallback_rate", "time_to_hold_plan_p50_s"):
            head[f"e2e_best_{key}"] = _r(best[key], 6 if "per_case" in key else 4)
            src[f"e2e_best_{key}"] = f"{best['file']}: {key} (highest cases/hour)"
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


def ab(evid: Path, head: dict[str, Any], src: dict[str, str]) -> dict[str, Any] | None:
    rep = _load(evid / "ab_hallucination.json")
    if not rep:
        return None
    for arm in ("raw", "bound"):
        a = _get(rep, "arms", arm) or {}
        for key in ("docs", "docs_with_wrong_amount", "invented_accounts", "unknown_citations", "verifier_pass_rate",
                    "clean_rate"):
            head[f"ab_{arm}_{key}"] = _num(a.get(key)) if key.endswith("rate") else a.get(key)
            src[f"ab_{arm}_{key}"] = f"ab_hallucination.json: arms.{arm}.{key}"
    head["ab_bound_prose_fallbacks"] = _get(rep, "arms", "bound", "prose_fallbacks")
    src["ab_bound_prose_fallbacks"] = "ab_hallucination.json: arms.bound.prose_fallbacks"
    head["ab_cases"] = rep.get("cases")
    return {"label": rep.get("label"), "llm": rep.get("llm"), "world": rep.get("world"), "cases": rep.get("cases"),
            "stub": rep.get("stub"), "arms": rep.get("arms")}


def stages(evid: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted((evid / "stages").glob("*.json")):
        rec = _load(path)
        if rec:
            out.append({k: rec.get(k) for k in ("index", "stage", "status", "started_at", "ended_at", "duration_s",
                                                "window", "notes")})
    return out


def hardware(evid: Path) -> dict[str, Any]:
    st = _load(evid / "amd_smi_static.json") or {}
    gpus = st.get("gpus") or []
    return {"available": bool(st.get("available")), "gpus": gpus,
            "gpu_name": gpus[0]["name"] if gpus else None,
            "sriov_vf": any(g.get("sriov_vf") for g in gpus), "rocm_version": st.get("rocm_version"),
            "captured_at": st.get("captured_at"), "reason": st.get("reason")}


def validity(mode: str, hw: dict[str, Any], stage_rows: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    reasons = []
    if mode != "gpu":
        reasons.append("CPU rehearsal: not a GPU run")
    if not (hw.get("gpu_name") and "MI300" in str(hw["gpu_name"])):
        reasons.append("amd-smi static does not name an AMD Instinct MI300X")
    if not stage_rows:
        reasons.append("no stage records")
    start, end = parse_ts(EVENT_START), parse_ts(EVENT_END)
    for s in stage_rows:
        t = s.get("started_at")
        if not t or not (start <= parse_ts(t) <= end):
            reasons.append(f"stage {s.get('stage')} started at {t}, outside the event window")
    return not reasons, reasons


def build(evid: Path, mode: str) -> dict[str, Any]:
    head: dict[str, Any] = {}
    src: dict[str, str] = {}
    deriv: dict[str, str] = {}
    stage_rows = stages(evid)
    hw = hardware(evid)
    results = {
        "serving": serving(evid, head, src),
        "e2e": end_to_end(evid, head, src, deriv),
        "memory": memory(evid, head, src),
        "gnn": gnn(evid, head, src),
        "lora": lora(evid, head, src),
        "ab": ab(evid, head, src),
    }
    valid, reasons = validity(mode, hw, stage_rows)
    budget = next((s.get("budget") for s in reversed([_load(p) or {} for p in sorted((evid / "stages").glob("*.json"))])
                   if s.get("budget")), None)
    started = [s["started_at"] for s in stage_rows if s.get("started_at")]
    ended = [s["ended_at"] for s in stage_rows if s.get("ended_at")]
    head["measured_date"] = evid.name if re.fullmatch(r"\d{4}-\d{2}-\d{2}", evid.name) else None
    head["gpu_name"] = hw.get("gpu_name")
    return {
        "schema": SCHEMA,
        "mode": "gpu" if mode == "gpu" else "cpu-rehearsal",
        "label": GPU_LABEL if mode == "gpu" else REHEARSAL_LABEL,
        "gpu_results_valid": valid,
        "invalid_reasons": reasons,
        "date": evid.name,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event_window": {"start": EVENT_START, "end": EVENT_END},
        "run": {"first_stage_started_at": min(started) if started else None,
                "last_stage_ended_at": max(ended) if ended else None,
                "git_commit": next((s["notes"].get("git_commit") for s in stage_rows
                                    if (s.get("notes") or {}).get("git_commit")), None),
                "amd_baseline": next((s["notes"].get("amd_baseline") for s in stage_rows
                                      if (s.get("notes") or {}).get("amd_baseline")), None),
                "served_profile": next((s["notes"].get("served_profile") for s in stage_rows
                                        if (s.get("notes") or {}).get("served_profile")), None)},
        "hardware": hw,
        "stages": stage_rows,
        "budget": budget,
        "headline": head,
        "sources": src,
        "derivations": deriv,
        "missing": sorted(k for k, v in head.items() if v is None),
        "results": results,
    }


# --------------------------------------------------------------------------- markdown


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.6f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:,.2f}".rstrip("0").rstrip(".")
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
    ("A/B documents with a wrong amount: raw / bound", ("ab_raw_docs_with_wrong_amount", "ab_bound_docs_with_wrong_amount")),
    ("A/B invented account numbers: raw / bound", ("ab_raw_invented_accounts", "ab_bound_invented_accounts")),
    ("A/B unknown or repealed citations: raw / bound", ("ab_raw_unknown_citations", "ab_bound_unknown_citations")),
    ("A/B clean documents rate: raw / bound", ("ab_raw_clean_rate", "ab_bound_clean_rate")),
]


def to_markdown(ev: dict[str, Any]) -> str:
    h, hw = ev["headline"], ev["hardware"]
    rehearsal = ev["mode"] != "gpu"
    title = (f"# Wapsi AMD evidence: CPU rehearsal, {ev['date']}" if rehearsal
             else f"# Wapsi on AMD Instinct MI300X: evidence, {ev['date']}")
    lines = [title, ""]
    if rehearsal:
        lines += [f"> **{REHEARSAL_LABEL}**", ""]
    lines += [f"- Label: {ev['label']}",
              f"- GPU results valid: **{_fmt(ev['gpu_results_valid'])}**"
              + (f" ({'; '.join(ev['invalid_reasons'][:3])})" if ev["invalid_reasons"] else ""),
              f"- Run: {_fmt(ev['run']['first_stage_started_at'])} .. {_fmt(ev['run']['last_stage_ended_at'])} UTC · "
              f"commit `{_fmt(ev['run']['git_commit'])}` · amd-baseline `{_fmt(ev['run']['amd_baseline'])}` · "
              f"served profile {_fmt(ev['run']['served_profile'])}",
              f"- Event window: {ev['event_window']['start']} .. {ev['event_window']['end']}",
              f"- Generated {ev['generated_at']} by `editions/amd/kickoff/collect_evidence.py` from the files in this "
              "folder. Every value is read from a result file (see `sources` in evidence.json).", ""]
    lines += ["## Hardware (`amd-smi static`)", ""]
    if hw.get("available"):
        lines += _table(["GPU", "Name", "SR-IOV VF", "ROCm"],
                        [[g.get("gpu"), g.get("name"), g.get("sriov_vf"), hw.get("rocm_version")] for g in hw["gpus"]])
    else:
        lines += [f"Not captured: {hw.get('reason') or 'amd-smi static missing'}."]
    lines += ["", "## Headline", ""]
    lines += _table(["Metric", "Value"], [[label, " / ".join(_fmt(h.get(k)) for k in keys)]
                                          for label, keys in HEADLINE_ROWS])
    e2e = ev["results"]["e2e"]
    if e2e:
        lines += ["", "## End to end (golden cases through the pipeline)", ""]
        lines += _table(["Concurrency", "Cases", "Cases/hour", "Drafts/GPU-hour", "Hold plan p50 (s)", "Case p95 (s)",
                         "Verifier pass", "Prose fallback", "$ per case", "₹ per case", "File"],
                        [[r["concurrency"], r["cases"], r["cases_per_hour"], r["drafts_per_gpu_hour"],
                          r["time_to_hold_plan_p50_s"], r["case_p95_s"], r["draft_verification_pass_rate"],
                          r["prose_fallback_rate"], r["usd_per_case"], r["inr_per_case"], r["file"]] for r in e2e])
    for key, name in (("llm", "Drafting model"), ("verifier", "Verifier model"), ("base", "Base model (before LoRA)")):
        sv = ev["results"]["serving"].get(key)
        if not sv:
            continue
        lines += ["", f"## Serving: {name} `{sv.get('model')}`", ""]
        lines += _table(["Concurrency", "OK", "Output tok/s", "TTFT p50 (s)", "TTFT p95 (s)", "Latency p95 (s)", "Req/s"],
                        [[lv["concurrency"], lv["ok"], lv["output_tokens_per_s"], lv["ttft_p50_s"], lv["ttft_p95_s"],
                          lv["e2e_p95_s"], lv["requests_per_s"]] for lv in sv["levels"]])
    lines += ["", "## Stages", ""]
    lines += _table(["#", "Stage", "Status", "Started (UTC)", "Ended (UTC)", "Seconds", "Window"],
                    [[s.get("index"), s.get("stage"), s.get("status"), s.get("started_at"), s.get("ended_at"),
                      s.get("duration_s"), s.get("window")] for s in ev["stages"]])
    b = ev.get("budget")
    if b:
        lines += ["", f"Budget at the last stage ({b.get('label')}): {b.get('hours')} h x ${b.get('gpu_hour_usd')} = "
                  f"${b.get('droplet_usd')} (+ ${b.get('spent_before_usd')} earlier) = ${b.get('total_usd')} of "
                  f"${b.get('budget_usd')} ({b.get('pct')}%)."]
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
    ap.add_argument("--mode", choices=["gpu", "rehearsal"], required=True)
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
    print(f"wrote {evid / 'evidence.json'} and evidence.md ({ev['mode']}, gpu_results_valid={ev['gpu_results_valid']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
