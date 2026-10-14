"""Which models the kickoff pipeline uses, chosen by environment and by what the platform can run.

    uv run python -m editions.amd.kickoff.model_plan --mode endpoint --endpoint-json <evid>/endpoint.json \
        --out <evid>/model_plan.json
    python3 -m editions.amd.kickoff.model_plan --mode notebook --vram-gb 48 --out <evid>/model_plan.json \
        --shell <work>/model_plan.env

Two roles, both set by environment variables, nothing hard-wired:

- **drafting**: writes the prose fields of the drafts. Default **Gemma 4** (Google's open model: the Google partner
  technology of this entry, together with Google's open-source ADK). ``WAPSI_LLM_MODEL`` (endpoint id) or
  ``WAPSI_BASE_MODEL`` (Hugging Face id, self-hosted) overrides it.
- **judge**: a second, smaller, different model that screens untrusted complaint text next to the deterministic
  rules (``LLMJudgeGuard``). ``WAPSI_JUDGE_MODEL`` (endpoint id; ``WAPSI_VERIFIER_MODEL`` is read too) or
  ``WAPSI_VERIFIER_MODEL_ID`` (Hugging Face id) overrides it; ``WAPSI_JUDGE_MODEL=none`` switches the judge off.

Fallback lists (first one the platform can run wins; ``WAPSI_LLM_FALLBACK_MODELS`` / ``WAPSI_JUDGE_FALLBACK_MODELS``
replace them with a comma list; ``WAPSI_LLM_MODELS``, the ordered list voice intake and the agent fail over along,
is read for the drafting list when ``WAPSI_LLM_FALLBACK_MODELS`` is not set):

| Platform | Drafting, in order | Judge, in order |
|---|---|---|
| endpoint (AMD Token Factory ids) | ``Gemma-4-31B-IT``, ``Qwen3.8-27B``, ``DeepSeek-V4-Flash``, ``MiMo-V2.6-Flash``, ``GLM-5.3-Flash`` | ``MiniCPM5-2B``, ``Qwen3.8-27B``, ``DeepSeek-V4-Flash`` |
| self-hosted (notebook, droplet; Hugging Face ids) | ``google/gemma-4-31B-it`` (needs ~80 GB to serve, ~150 GB to fine-tune and merge), ``google/gemma-4-E4B-it`` (~24 GB to serve, ~48 GB to fine-tune), ``Qwen/Qwen3.6-35B-A3B`` (only if Gemma 4 will not serve) | ``google/gemma-4-E4B-it`` |

On an endpoint the plan reads ``GET /models`` (the catalogue changes: on 2026-10-14 ``Gemma-4-31B-IT`` was served,
then for a while it was not; a missing model is never assumed). A model the operator named in ``WAPSI_LLM_MODEL`` is
never replaced silently: when it is not served the plan has no drafting model and says so, until the operator waits
for it (``endpoint.py wait-model``) or accepts the fallback list with ``WAPSI_ALLOW_MODEL_FALLBACK=1``. Self-hosted,
it reads the GPU memory (``amd-smi`` / ``rocm-smi`` / torch) and picks the largest Gemma 4 that fits. The memory
thresholds are estimates from the LoRA presets (``editions/amd/lora/configs/*.json``), not measurements; an explicit
environment variable always wins. The plan records whether the drafting model is a Google open model
(``google_open_model``): a fallback outside the Gemma family is flagged, because the Google partner prize needs
Google technology in the workload.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "wapsi-amd-model-plan/1"

# endpoint ids: AMD Token Factory catalogue on 2026-10-14 (GET /models is read at run time; nothing is assumed served)
ENDPOINT_DRAFT = ("Gemma-4-31B-IT", "Qwen3.8-27B", "DeepSeek-V4-Flash", "MiMo-V2.6-Flash", "GLM-5.3-Flash")
ENDPOINT_JUDGE = ("MiniCPM5-2B", "Qwen3.8-27B", "DeepSeek-V4-Flash")

# self-hosted: Hugging Face id, GB of GPU memory to serve / to fine-tune and merge (estimates from the LoRA presets),
# LoRA preset, vLLM profile pair (base, merged), merged checkpoint name
SELF_HOSTED_DRAFT: tuple[dict[str, Any], ...] = (
    {"id": "google/gemma-4-31B-it", "serve_gb": 80, "lora_gb": 150, "lora_config": "gemma4-31b-mi300x",
     "base_profile": "gemma4-base", "merged_profile": "gemma4-merged", "merged_name": "wapsi-gemma-4-31b-str-merged"},
    {"id": "google/gemma-4-E4B-it", "serve_gb": 24, "lora_gb": 48, "lora_config": "gemma4-e4b-mi300x",
     "base_profile": "gemma4-base", "merged_profile": "gemma4-merged", "merged_name": "wapsi-gemma-4-e4b-str-merged"},
    {"id": "Qwen/Qwen3.6-35B-A3B", "serve_gb": 90, "lora_gb": 160, "lora_config": "qwen36-35b-a3b-mi300x",
     "base_profile": "qwen-fallback", "merged_profile": "qwen-fallback", "merged_name": "wapsi-qwen36-str-merged"},
)
SELF_HOSTED_JUDGE = ("google/gemma-4-E4B-it",)
JUDGE_SERVE_GB = 24
OFF = ("none", "off", "0", "false")


def family_of(model_id: str) -> str:
    low = model_id.lower()
    for name in ("gemma", "qwen", "deepseek", "minicpm", "mimo", "glm"):
        if name in low:
            return name
    return "other"


def is_google_open_model(model_id: str) -> bool:
    """Gemma is Google's open-weights model family (the Google partner technology of this entry)."""
    return family_of(model_id) == "gemma"


# Fallback families that think before they answer on the shared endpoint (seen live on 2026-10-14: Qwen3.8-27B spent a
# 32-token budget on reasoning and returned no content). Thinking cannot be switched off there, only turned down, and
# thinking tokens count toward max_tokens: so these models get reasoning_effort "low" and token headroom. Gemma 4 does
# not think by default and gets nothing extra.
THINKING_FAMILIES = ("qwen", "deepseek", "glm", "mimo")
THINKING_HEADROOM_TOKENS = 1024


def request_defaults(model_id: str | None) -> tuple[dict[str, Any], int]:
    """(extra request fields, extra max_tokens) a served model needs on a provider's endpoint. The environment
    (``WAPSI_LLM_EXTRA_BODY``, ``WAPSI_LLM_MAX_TOKENS_HEADROOM``) overrides both."""
    if model_id and family_of(model_id) in THINKING_FAMILIES:
        return {"reasoning_effort": "low"}, THINKING_HEADROOM_TOKENS
    return {}, 0


def _split(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _match(wanted: str, served: Sequence[str]) -> str | None:
    """The served id equal to ``wanted`` (exactly, else ignoring case)."""
    if wanted in served:
        return wanted
    return next((s for s in served if s.lower() == wanted.lower()), None)


def _role(model_id: str | None, source: str, reason: str, candidates: Sequence[str]) -> dict[str, Any]:
    return {"id": model_id, "family": family_of(model_id) if model_id else None,
            "google_open_model": bool(model_id) and is_google_open_model(model_id), "source": source,
            "reason": reason, "candidates": list(candidates)}


def plan_endpoint(served: Sequence[str] | None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Models for endpoint mode. ``served``: ids from ``GET /models`` (``None`` = not probed: nothing is checked)."""
    env = os.environ if env is None else env
    warnings: list[str] = []
    # WAPSI_LLM_MODELS is the same ordered list as voice intake and the agent read (one list for every surface)
    drafts = (_split(env.get("WAPSI_LLM_FALLBACK_MODELS")) or _split(env.get("WAPSI_LLM_MODELS"))
              or list(ENDPOINT_DRAFT))
    explicit = (env.get("WAPSI_LLM_MODEL") or "").strip()
    allow = (env.get("WAPSI_ALLOW_MODEL_FALLBACK") or "").strip() == "1"
    first_served = None if served is None else next((m for c in drafts if (m := _match(c, served))), None)
    if explicit and (served is None or _match(explicit, served)):
        chosen = _match(explicit, served) if served else explicit
        drafting = _role(chosen, "WAPSI_LLM_MODEL", "set by the operator", drafts)
    elif explicit and not allow:  # the operator named a model: never swap it silently
        drafting = _role(None, "WAPSI_LLM_MODEL", f"{explicit} is not served by this endpoint right now", drafts)
        warnings.append(f"WAPSI_LLM_MODEL={explicit} is not served right now (the catalogue changes). Wait for it "
                        "(endpoint.py wait-model) and re-run, or accept the fallback list with "
                        f"WAPSI_ALLOW_MODEL_FALLBACK=1 (first served: {first_served or 'none'})")
    else:
        if explicit:
            warnings.append(f"WAPSI_LLM_MODEL={explicit} is not served by this endpoint right now: using the "
                            "fallback list (WAPSI_ALLOW_MODEL_FALLBACK=1)")
        pick = drafts[0] if served is None else first_served
        why = "first of the fallback list" if served is None else "first of the fallback list that the endpoint serves"
        drafting = _role(pick, "default" if pick == drafts[0] else "fallback", why, drafts)
        if pick is None:
            warnings.append("none of the drafting candidates is served: set WAPSI_LLM_MODEL to a served id")
        elif served is not None and not explicit and pick != drafts[0]:
            warnings.append(f"{drafts[0]} is not served by this endpoint right now: using {pick} from the fallback list")
    if drafting["id"] and not drafting["google_open_model"]:
        warnings.append(f"the drafting model {drafting['id']} is not a Gemma model: the Google partner prize needs "
                        "Google technology in the workload (Gemma, or ADK)")
    judges = _split(env.get("WAPSI_JUDGE_FALLBACK_MODELS")) or list(ENDPOINT_JUDGE)
    wanted = (env.get("WAPSI_JUDGE_MODEL") or env.get("WAPSI_VERIFIER_MODEL") or "").strip()
    if wanted.lower() in OFF and wanted:
        judge = _role(None, "WAPSI_JUDGE_MODEL", "switched off by the operator: rules-only screening", judges)
    elif wanted and (served is None or _match(wanted, served)):
        judge = _role(_match(wanted, served) if served else wanted, "WAPSI_JUDGE_MODEL", "set by the operator", judges)
    else:
        if wanted:
            warnings.append(f"judge model {wanted} is not served by this endpoint: using the fallback list")
        options = [c for c in judges if c.lower() != str(drafting["id"]).lower()]  # a different model, or no judge
        pick = (options[0] if options else None) if served is None else \
            next((m for c in options if (m := _match(c, served))), None)
        judge = _role(pick, "default" if pick and pick == judges[0] else "fallback",
                      "smallest different model the endpoint serves" if pick else "no different model is served: "
                      "rules-only screening", judges)
    return {"platform": "endpoint", "drafting": drafting, "judge": judge, "served": list(served or []),
            "served_checked": served is not None, "requested": explicit or None,
            "fallback_in_use": bool(drafting["id"]) and drafting["source"] != "WAPSI_LLM_MODEL"
            and str(drafting["id"]).lower() != (explicit or drafts[0]).lower(), "warnings": warnings}


def plan_self_hosted(vram_gb: float | None, env: Mapping[str, str] | None = None, *,
                     platform: str = "notebook") -> dict[str, Any]:
    """Models for a GPU box (notebook, droplet). ``vram_gb``: the GPU's memory (``None`` = unknown: the first
    candidate is planned and flagged). Fine-tuning needs more memory than serving, so a GPU that serves the 31B
    model but cannot fine-tune it serves the base model and skips the LoRA stages (``lora: false``)."""
    env = os.environ if env is None else env
    warnings: list[str] = []
    by_id = {c["id"].lower(): c for c in SELF_HOSTED_DRAFT}
    order = [by_id.get(m.lower(), {"id": m}) for m in _split(env.get("WAPSI_LLM_FALLBACK_MODELS"))] \
        or [c for c in SELF_HOSTED_DRAFT if family_of(c["id"]) == "gemma"]  # Qwen only when asked for
    ids = [c["id"] for c in order]
    explicit = (env.get("WAPSI_BASE_MODEL") or "").strip()
    if explicit:
        spec = dict(by_id.get(explicit.lower(), {"id": explicit}))
        drafting = _role(explicit, "WAPSI_BASE_MODEL", "set by the operator", ids)
    elif vram_gb is None:
        spec = dict(order[0])
        drafting = _role(spec["id"], "default", "GPU memory unknown: the first candidate is planned", ids)
        warnings.append("GPU memory unknown: confirm the model fits (amd-smi), or set WAPSI_BASE_MODEL")
    else:
        fit = next((c for c in order if c.get("serve_gb") is None or vram_gb >= c["serve_gb"]), None)
        spec = dict(fit or order[-1])
        why = f"largest candidate that serves in {vram_gb:.0f} GB" if fit else \
            f"no candidate is known to serve in {vram_gb:.0f} GB: the smallest is planned"
        drafting = _role(spec["id"], "default" if spec["id"] == ids[0] else "fallback", why, ids)
        if fit is None:
            warnings.append(f"{spec['id']} may not fit in {vram_gb:.0f} GB: use endpoint mode for the LLM side")
    lora_gb, serve_gb = spec.get("lora_gb"), spec.get("serve_gb")
    lora = vram_gb is None or lora_gb is None or vram_gb >= lora_gb
    if not lora:
        warnings.append(f"{vram_gb:.0f} GB is below the ~{lora_gb} GB estimated to fine-tune and merge {spec['id']}: "
                        "the LoRA stages are skipped and the base model is served (WAPSI_LORA=1 forces them)")
    if (env.get("WAPSI_LORA") or "").strip() in ("0", "1"):
        lora = env["WAPSI_LORA"].strip() == "1"
    if drafting["id"] and not drafting["google_open_model"]:
        warnings.append(f"the drafting model {drafting['id']} is not a Gemma model: the Google partner prize needs "
                        "Google technology in the workload (Gemma, or ADK)")
    wanted = (env.get("WAPSI_VERIFIER_MODEL_ID") or "").strip()
    judges = list(SELF_HOSTED_JUDGE)
    if wanted.lower() in OFF and wanted:
        judge = _role(None, "WAPSI_VERIFIER_MODEL_ID", "switched off by the operator: rules-only screening", judges)
    elif wanted:
        judge = _role(wanted, "WAPSI_VERIFIER_MODEL_ID", "set by the operator", judges)
    else:
        room = vram_gb is None or serve_gb is None or vram_gb >= serve_gb + JUDGE_SERVE_GB
        same = judges[0].lower() == str(drafting["id"]).lower()
        if room and not same:
            judge = _role(judges[0], "default", "small second model next to the drafting model", judges)
        else:
            judge = _role(None, "default", "no memory for a second model, or it would be the drafting model itself: "
                                           "rules-only screening", judges)
    return {"platform": platform, "vram_gb": vram_gb, "drafting": drafting, "judge": judge, "lora": lora,
            "lora_config": (env.get("WAPSI_LORA_CONFIG") or "").strip() or spec.get("lora_config"),
            "base_profile": (env.get("WAPSI_BASE_PROFILE") or "").strip() or spec.get("base_profile", "gemma4-base"),
            "merged_profile": spec.get("merged_profile", "gemma4-merged"),
            "merged_name": (env.get("WAPSI_MERGED_NAME") or "").strip() or spec.get("merged_name"),
            "memory_estimates_gb": {"serve": serve_gb, "lora": lora_gb, "judge": JUDGE_SERVE_GB,
                                    "status": "estimates from the LoRA presets, not measurements"},
            "warnings": warnings}


def detect_vram_gb() -> float | None:
    """The first GPU's memory in GB from ``amd-smi`` / ``rocm-smi`` (``None`` when neither answers)."""
    root = Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from editions.amd.bench import gpu_telemetry as tel  # noqa: PLC0415 - standard library only

        snap = tel.sample_once(map_docker=False)
    except Exception:  # noqa: BLE001 - no tool, or an unexpected output shape: the memory is simply unknown
        return None
    total = next((g.vram_total_mb for g in snap.gpus if g.vram_total_mb), None)
    return round(total / 1024, 1) if total else None


def shell_lines(plan: Mapping[str, Any]) -> list[str]:
    """``KEY=value`` lines ``run_all.sh`` reads (never evaluated as code)."""
    drafting, judge = plan["drafting"], plan["judge"]
    out = {"PLAN_PLATFORM": plan["platform"], "PLAN_DRAFT_MODEL": drafting["id"] or "",
           "PLAN_JUDGE_MODEL": judge["id"] or "", "PLAN_GOOGLE_OPEN_MODEL": "1" if drafting["google_open_model"] else "0"}
    if plan["platform"] != "endpoint":
        out.update({"PLAN_LORA": "1" if plan.get("lora") else "0", "PLAN_LORA_CONFIG": plan.get("lora_config") or "",
                    "PLAN_BASE_PROFILE": plan.get("base_profile") or "",
                    "PLAN_MERGED_PROFILE": plan.get("merged_profile") or "",
                    "PLAN_MERGED_NAME": plan.get("merged_name") or ""})
    return [f"{k}={v}" for k, v in out.items()]


def to_text(plan: Mapping[str, Any]) -> str:
    d, j = plan["drafting"], plan["judge"]
    lines = [f"model plan ({plan['platform']})",
             f"  drafting: {d['id'] or 'NONE'} [{d['source']}: {d['reason']}]"
             + (" (Google open model: Gemma)" if d["google_open_model"] else ""),
             f"  judge:    {j['id'] or 'none (rules-only screening)'} [{j['source']}: {j['reason']}]"]
    if plan["platform"] != "endpoint":
        lines.append(f"  LoRA:     {'yes, preset ' + str(plan.get('lora_config')) if plan.get('lora') else 'skipped'}"
                     f" | merged checkpoint {plan.get('merged_name')}")
    lines += [f"  WARNING: {w}" for w in plan.get("warnings") or []]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="model_plan.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", required=True, choices=["endpoint", "notebook", "droplet"])
    ap.add_argument("--endpoint-json", default=None, help="endpoint.json from `endpoint.py probe` (served model ids)")
    ap.add_argument("--vram-gb", type=float, default=None, help="GPU memory in GB (self-hosted modes)")
    ap.add_argument("--torch-info", default=None, help="torch_info.json from `stagelog.py torch-info` (GPU memory)")
    ap.add_argument("--detect-vram", action="store_true", help="read the GPU memory with amd-smi / rocm-smi")
    ap.add_argument("--out", default=None, help="write the plan as JSON")
    ap.add_argument("--shell", default=None, help="write KEY=value lines for run_all.sh")
    args = ap.parse_args(argv)
    if args.mode == "endpoint":
        served = None
        if args.endpoint_json and Path(args.endpoint_json).exists():
            info = json.loads(Path(args.endpoint_json).read_text(encoding="utf-8"))
            served = info.get("models") if info.get("models_status") == 200 else None
            if served is not None:  # listed, but the probe's call was refused (the catalogue was changing)
                served = [m for m in served if m not in (info.get("models_refused") or [])]
        plan = plan_endpoint(served)
    else:
        vram = args.vram_gb
        if vram is None and args.torch_info and Path(args.torch_info).exists():
            mb = json.loads(Path(args.torch_info).read_text(encoding="utf-8")).get("vram_total_mb")
            vram = round(mb / 1024, 1) if isinstance(mb, int | float) and mb > 0 else None
        if vram is None and args.detect_vram:
            vram = detect_vram_gb()
        plan = plan_self_hosted(vram, platform=args.mode)
    plan = {"schema": SCHEMA, "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), **plan}
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8", newline="\n")
    if args.shell:
        sh = Path(args.shell)
        sh.parent.mkdir(parents=True, exist_ok=True)
        sh.write_text("\n".join(shell_lines(plan)) + "\n", encoding="utf-8", newline="\n")
    print(to_text(plan))
    return 0 if plan["drafting"]["id"] else 1


if __name__ == "__main__":
    sys.exit(main())
