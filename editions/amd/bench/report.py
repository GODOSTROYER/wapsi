"""Merge the AMD benchmark outputs into one ``AMD_RESULTS.md`` (standard library only).

    uv run python -m editions.amd.bench.report --results-dir editions/amd/bench/results/<stamp>
    uv run python -m editions.amd.bench.report --llm-bench a.json b.json --e2e report.json \
        --telemetry telemetry_summary.json --gnn gnn_metrics.json --lora lora_eval.json \
        --voice voice_bench.json --out editions/amd/AMD_RESULTS.md

With ``--results-dir`` it picks up ``llm_bench*.json``, ``report.json`` (e2e), ``telemetry_summary.json``,
``gnn*.json``, ``lora*.json`` and ``voice_bench*.json`` in that directory. A voice benchmark carries its own
provenance lines (where the recogniser ran, who served the text model); they are printed with its table, because
those runs may come from the AMD Token Factory or a CPU rehearsal rather than the droplet. GNN / LoRA files come from the other AMD work streams; their
schema is not fixed here, so their scalar values are rendered generically (a top-level ``metrics`` object is
preferred when present). Every number in the output is read from those files: the report never invents one, and a
sensor the telemetry could not read is shown as ``n/a`` (``n/a (SR-IOV VF)`` on a virtual function), never as 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from editions.amd.bench.common import fmt, md_table, utc_now  # noqa: E402
from editions.amd.bench.gpu_telemetry import NA, sensor_text  # noqa: E402

MAX_GENERIC_ROWS = 40


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    data.setdefault("_path", str(path))
    return data


def discover(results_dir: Path) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {"llm_bench": [], "e2e": [], "telemetry": [], "gnn": [], "lora": [], "voice": []}
    for p in sorted(results_dir.glob("*.json")):
        name = p.name.lower()
        if name.startswith("llm_bench"):
            found["llm_bench"].append(p)
        elif name.startswith("voice_bench"):
            found["voice"].append(p)
        elif name in ("report.json", "e2e_report.json") or name.startswith("e2e"):
            found["e2e"].append(p)
        elif name.startswith("telemetry") and name.endswith("summary.json"):
            found["telemetry"].append(p)
        elif name.startswith("gnn"):
            found["gnn"].append(p)
        elif name.startswith("lora"):
            found["lora"].append(p)
    return found


def flatten(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).startswith("_"):
                continue
            out += flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        if all(not isinstance(v, dict | list) for v in obj) and len(obj) <= 8:
            out.append((prefix, ", ".join(fmt(v) for v in obj)))
        else:
            for i, v in enumerate(obj[:10]):
                out += flatten(v, f"{prefix}[{i}]")
    else:
        out.append((prefix, obj))
    return out


def _gpu_name(telemetry: list[dict]) -> str | None:
    for t in telemetry:
        for g in t.get("gpus") or []:
            if g.get("name"):
                return str(g["name"])
    return None


def _gb(mb: Any) -> float | str:
    return mb / 1024 if isinstance(mb, int | float) and not isinstance(mb, bool) else NA


def _sensor(g: dict[str, Any], key: str) -> Any:
    """Telemetry value, or ``n/a`` / ``n/a (SR-IOV VF)`` when the sensor was unavailable (older summaries: a VF's 0)."""
    value = g.get(key)
    vf = bool(g.get("sriov_vf"))
    return value if value is not None and not (vf and value == 0) else sensor_text(None, vf)


def _voice_fake(v: dict) -> bool:
    return bool((v.get("settings") or {}).get("fake"))


def voice_headline(voice: list[dict]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for v in voice:
        if _voice_fake(v):
            continue  # a harness check measures no model
        m, label = v.get("metrics") or {}, v.get("label") or "voice intake"
        rtf = (m.get("asr_rtf") or {}).get("p50")
        if rtf is not None:
            rows.append([f"Voice intake ({label}) — speech-to-text real-time factor p50", rtf])
        rows.append([f"Voice intake ({label}) — transcript to checked fields p50 (s)",
                     (m.get("extract_s") or {}).get("p50")])
        if m.get("samples_checked"):
            rows.append([f"Voice intake ({label}) — samples reconciled as documented",
                         f"{m.get('samples_as_documented')}/{m.get('samples_checked')}"])
    return rows


def voice_section(v: dict) -> list[str]:
    m, hw = v.get("metrics") or {}, v.get("hardware") or {}
    asr, llm = hw.get("asr") or {}, hw.get("llm") or {}
    rtf, asr_s, ext, total = (m.get(k) or {} for k in ("asr_rtf", "asr_s", "extract_s", "total_s"))
    lines = [f"## Voice intake: {v.get('label')}", ""]
    if _voice_fake(v):
        lines += ["> **Offline harness check (fake recogniser and fake text model).** Not a measurement.", ""]
    lines += [f"- {line}" for line in v.get("provenance") or []]
    lines += [f"- Speech-to-text `{asr.get('model')}` ({asr.get('backend')}"
              + (f", {asr.get('device')}" if asr.get("device") else "") + f") · text model chain head "
              f"`{llm.get('model')}` · JSON mode {llm.get('json_mode')} · measured {str(v.get('generated_at'))[:19]}",
              "",
              md_table(["Voice notes", "RTF p50", "RTF p95", "ASR p50 (s)", "Extract p50 (s)", "Extract p95 (s)",
                        "Note to fields p50 (s)", "Output tok (mean)", "As documented", "Values agree",
                        "Repaired", "Fallback model"],
                       [[f"{m.get('ok')}/{m.get('runs')}", rtf.get("p50"), rtf.get("p95"), asr_s.get("p50"),
                         ext.get("p50"), ext.get("p95"), total.get("p50"),
                         (m.get("completion_tokens") or {}).get("mean"),
                         f"{m.get('samples_as_documented')}/{m.get('samples_checked')}", m.get("value_agreement"),
                         m.get("repaired_runs"), m.get("failover_runs")]]), ""]
    by_model = m.get("answers_by_model") or {}
    if by_model:
        per = m.get("extract_s_by_model") or {}
        lines += ["Answers per text model (a number is credited only to the model that produced it):", "",
                  md_table(["Model", "Answers", "Extract p50 (s)", "Extract p95 (s)"],
                           [[k, n, (per.get(k) or {}).get("p50"), (per.get(k) or {}).get("p95")]
                            for k, n in by_model.items()]), ""]
    return lines


def headline(llm: list[dict], e2e: list[dict], telemetry: list[dict]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for b in llm:
        levels = b.get("levels") or []
        if not levels:
            continue
        low = min(levels, key=lambda lv: lv["concurrency"])
        best = max(levels, key=lambda lv: lv.get("output_tokens_per_s") or 0)
        rows.append([f"{b.get('model')} — TTFT p50 at concurrency {low['concurrency']} (s)", low["ttft_s"]["p50"]])
        rows.append([f"{b.get('model')} — peak output tokens/s (concurrency {best['concurrency']})",
                     best.get("output_tokens_per_s")])
    for e in e2e:
        m, c = e.get("metrics", {}), e.get("cost", {})
        conc = e.get("settings", {}).get("concurrency")
        rows.append([f"Cases/hour ({e.get('world')}, concurrency {conc})", m.get("cases_per_hour")])
        rows.append(["Time to hold plan p50 (s)", (m.get("time_to_hold_plan_s") or {}).get("p50")])
        rows.append([f"Cost per case (USD at ${c.get('gpu_hour_usd')}/GPU-hour)", c.get("usd_per_case")])
        if c.get("inr_per_case") is not None:
            rows.append([f"Cost per case (INR at ₹{c.get('inr_per_usd')}/$)", c.get("inr_per_case")])
        rows.append(["Draft verification pass rate", m.get("draft_verification_pass_rate")])
        rows.append(["LLM prose fallback rate", m.get("prose_fallback_rate")])
    for t in telemetry:
        for g in t.get("gpus") or []:
            peak = g.get("vram_used_mb_peak")
            total = g.get("vram_total_mb")
            if peak is not None:
                rows.append([f"GPU{g.get('gpu')} peak VRAM (GB of {fmt(_gb(total), 1)})", peak / 1024])
    return rows


def render(llm: list[dict], e2e: list[dict], telemetry: list[dict], gnn: list[dict], lora: list[dict],
           voice: list[dict] | None = None) -> str:
    voice = voice or []
    sources = [d["_path"] for d in [*llm, *e2e, *telemetry, *gnn, *lora, *voice]]
    stamps = sorted({str(d.get("generated_at") or d.get("started_at") or "")[:10]
                     for d in [*llm, *e2e, *telemetry, *gnn, *lora, *voice]} - {""})
    hosts = sorted({str((d.get("env") or {}).get("host")) for d in [*llm, *e2e, *voice] if d.get("env")})
    commits = sorted({str((d.get("env") or {}).get("git_commit")) for d in [*llm, *e2e, *voice] if d.get("env")})
    gpu = _gpu_name(telemetry) or "GPU (name not captured)"
    if llm or e2e or telemetry or gnn or lora:
        lines = [
            "# Wapsi on AMD Instinct MI300X — measured results",
            "",
            f"- Hardware: {gpu} · hosts: {', '.join(hosts) or '—'} · commits: {', '.join(commits) or '—'}",
            f"- Measured on: {', '.join(stamps) or '—'} · report generated {utc_now().isoformat()}",
            "- Every number below is read from the JSON files listed at the end, produced by `editions/amd/bench/*` "
            "runs on the host named above (an MI300X droplet or an AMD GPU notebook; the run's evidence.md says "
            "which). Nothing is hand-entered.",
            "",
        ]
    else:  # voice benchmarks only: they say themselves where each model ran (no droplet claim in the header)
        lines = [
            "# Wapsi on AMD — voice intake results",
            "",
            f"- Client hosts: {', '.join(hosts) or '—'} · commits: {', '.join(commits) or '—'}",
            f"- Measured on: {', '.join(stamps) or '—'} · report generated {utc_now().isoformat()}",
            "- Every number below is read from the JSON files listed at the end, produced by "
            "`editions/amd/bench/voice_bench.py`. Nothing is hand-entered. Each section states where its models ran.",
            "",
        ]
    if any(str((e.get("model") or {}).get("llm") or "").startswith("fake-llm") for e in e2e):
        lines += ["> **Contains an offline harness check (FakeLLM, CPU).** Those rows are not GPU measurements.", ""]
    head = headline(llm, e2e, telemetry) + voice_headline(voice)
    if head:
        lines += ["## Headline", "", md_table(["Metric", "Value"], head), ""]
    for b in llm:
        s = b.get("settings", {})
        lines += [f"## Serving: `{b.get('model')}`", "",
                  f"Root `{b.get('model_root')}` · max_model_len {b.get('max_model_len')} · prompts {s.get('prompts')}"
                  f" · input ≈ {s.get('input_len')} tok · output {s.get('output_len')} tok", "",
                  md_table(["Concurrency", "OK", "TTFT p50 (s)", "TTFT p95 (s)", "ITL p50 (s)", "TPOT p50 (s)",
                            "Output tok/s", "Tok/s per user", "Req/s"],
                           [[lv["concurrency"], f"{lv['completed']}/{lv['requests']}", lv["ttft_s"]["p50"],
                             lv["ttft_s"]["p95"], lv["itl_s"]["p50"], lv["tpot_s"]["p50"],
                             lv["output_tokens_per_s"], lv.get("output_tokens_per_s_per_user"),
                             lv["requests_per_s"]] for lv in b.get("levels") or []]), ""]
    for e in e2e:
        m, c, model = e.get("metrics", {}), e.get("cost", {}), e.get("model", {})
        lines += [f"## End to end: {e.get('world')} ({m.get('cases_ok')}/{m.get('cases')} cases, "
                  f"concurrency {e.get('settings', {}).get('concurrency')})", "",
                  f"LLM `{model.get('llm')}` · scorer `{model.get('scorer')}` · guard `{model.get('guard')}`", "",
                  md_table(["Cases/hour", "Hold plan p50 (s)", "Hold plan p95 (s)", "Case p50 (s)", "Case p95 (s)",
                            "LLM calls/case", "Verification pass", "Prose fallback", "Hold exact match",
                            "$ per case", "₹ per case"],
                           [[m.get("cases_per_hour"), (m.get("time_to_hold_plan_s") or {}).get("p50"),
                             (m.get("time_to_hold_plan_s") or {}).get("p95"), (m.get("case_total_s") or {}).get("p50"),
                             (m.get("case_total_s") or {}).get("p95"), m.get("llm_calls_per_case"),
                             m.get("draft_verification_pass_rate"), m.get("prose_fallback_rate"),
                             m.get("hold_exact_match_rate"), c.get("usd_per_case"), c.get("inr_per_case")]], digits=4),
                  ""]
        judge = (e.get("settings") or {}).get("judge")
        if judge:
            lines += ["Independent verifier model (LLM judge) vs rules guard:", "",
                      md_table(["Screens", "Judge calls", "Judge errors", "Rules flagged", "Judge flagged",
                                "Agreement"],
                               [[judge.get("screens"), judge.get("judge_calls"), judge.get("judge_errors"),
                                 judge.get("rules_flagged"), judge.get("judge_flagged"), judge.get("agreement_rate")]]),
                      ""]
    for t in telemetry:
        lines += [f"## GPU memory and utilisation (source `{t.get('source')}`, {t.get('samples')} samples every "
                  f"{t.get('interval_s')} s)", "",
                  md_table(["GPU", "Name", "VRAM total (GB)", "Peak used (GB)", "Mean used (GB)", "GFX mean %",
                            "GFX peak %", "Power mean (W)"],
                           [[g.get("gpu"), g.get("name"), _gb(g.get("vram_total_mb")),
                             _gb(g.get("vram_used_mb_peak")), _gb(g.get("vram_used_mb_mean")),
                             _sensor(g, "gfx_activity_pct_mean"), _sensor(g, "gfx_activity_pct_peak"),
                             _sensor(g, "power_w_mean")]
                            for g in t.get("gpus") or []], digits=1), ""]
        if t.get("processes"):
            lines += ["Peak VRAM per container (one MI300X):", "",
                      md_table(["GPU", "Owner", "Peak VRAM (GB)", "Processes"],
                               [[p.get("gpu"), p.get("owner"), (p.get("vram_mb_peak") or 0) / 1024,
                                 ", ".join(p.get("process_names") or [])] for p in t["processes"]], digits=1), ""]
    for v in voice:
        lines += voice_section(v)
    for title, docs in (("GNN (GraphSAGE on ROCm)", gnn), ("LoRA fine-tune (Gemma 4, merged)", lora)):
        for d in docs:
            body = d.get("metrics") if isinstance(d.get("metrics"), dict) else d
            rows = [[k, v] for k, v in flatten(body)][:MAX_GENERIC_ROWS]
            lines += [f"## {title}: `{Path(d['_path']).name}`", "", md_table(["Field", "Value"], rows, digits=4), ""]
    lines += ["## Sources", ""] + [f"- `{s}`" for s in sources] + [
        "", "## Reproduce", "",
        "See `editions/amd/RUNBOOK.md` (bench section): `gpu_telemetry.py` on the host, `llm_bench.py` against the "
        "vLLM endpoint, `e2e_bench.py` over the golden cases, `voice_bench.py` for voice intake, then this report.",
        ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-dir", default=None)
    ap.add_argument("--llm-bench", nargs="*", default=[])
    ap.add_argument("--e2e", nargs="*", default=[])
    ap.add_argument("--telemetry", nargs="*", default=[])
    ap.add_argument("--gnn", nargs="*", default=[])
    ap.add_argument("--lora", nargs="*", default=[])
    ap.add_argument("--voice", nargs="*", default=[], help="voice_bench.py results")
    ap.add_argument("--out", default=None, help="default <results-dir>/AMD_RESULTS.md")
    args = ap.parse_args(argv)
    files: dict[str, list[Path]] = {k: [Path(p) for p in getattr(args, k)] for k in
                                    ("llm_bench", "e2e", "telemetry", "gnn", "lora", "voice")}
    if args.results_dir:
        for k, v in discover(Path(args.results_dir)).items():
            files[k] += [p for p in v if p not in files[k]]
    if not any(files.values()):
        ap.error("no inputs: pass --results-dir or individual JSON files")
    docs = {k: [load(p) for p in v] for k, v in files.items()}
    md = render(docs["llm_bench"], docs["e2e"], docs["telemetry"], docs["gnn"], docs["lora"], docs["voice"])
    out = Path(args.out) if args.out else Path(args.results_dir or ".") / "AMD_RESULTS.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8", newline="\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
