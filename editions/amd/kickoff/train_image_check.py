"""Day-1 check of the ROCm training image: does the uv.lock library set work on the image's own torch?

    # run_all.sh, stage env, inside wapsi-amd-train:local on the MI300X
    python3 -m editions.amd.kickoff.train_image_check --require-gpu --out editions/amd/evidence/<date>/train_image_check.json
    # anywhere (CPU, the uv venv): the same check, a few seconds
    uv run python -m editions.amd.kickoff.train_image_check --device cpu

Why: ``Dockerfile.train`` keeps the base image's HIP build of torch (2.9.1 in the pinned ``rocm/pytorch`` image) and
installs the uv.lock pins of transformers, peft, trl, accelerate and datasets with ``--no-deps``. uv.lock resolved
those against a newer CUDA torch, so pip never checked them against 2.9.1. This module checks, in order:

1. every training library imports, and the classes ``lora/train.py`` and ``lora/merge.py`` use exist
   (``SFTTrainer``, ``SFTConfig``, ``LoraConfig``, ``PeftModel``, ``Gemma4ForCausalLM``);
2. each library's declared torch requirement (its package metadata) accepts the torch that is installed;
3. one LoRA training step on a tiny random Gemma 4-layout model on the device (forward, backward, optimizer step,
   finite loss, adapter weights changed), the same code path as ``lora/train.py`` at toy size.

It writes the versions and verdicts as JSON and exits 1 if any check fails, so the env stage stops on day 1 rather
than hours later in lora-train. Nothing is downloaded. A tiny random model measures nothing: this is a plumbing check.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as md
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

LIBRARIES = ("torch", "transformers", "peft", "trl", "accelerate", "datasets", "safetensors", "tokenizers")
REQUIRED_NAMES = {
    "trl": ("SFTTrainer", "SFTConfig"),
    "peft": ("LoraConfig", "PeftModel", "get_peft_model"),
    "transformers": ("TrainerCallback", "Gemma4ForCausalLM", "Gemma4TextConfig", "PreTrainedTokenizerFast"),
    "datasets": ("Dataset",),
}
# torch extras that the training code path pulls in (transformers declares torch only under its "torch" extra)
TORCH_EXTRAS = {"transformers": "torch"}


def torch_requirements(dist: str, *, extra: str | None = None) -> list[str]:
    """``torch`` specifiers a distribution declares (unconditionally, or under ``extra``), e.g. ``['>=2.5']``."""
    from packaging.requirements import Requirement

    out = []
    for raw in md.requires(dist) or []:
        req = Requirement(raw)
        if req.name.lower() != "torch":
            continue
        if req.marker is not None and not req.marker.evaluate({"extra": extra or ""}):
            continue
        out.append(str(req.specifier) or "(any)")
    return out


def check_torch_ranges(torch_version: str, dists: tuple[str, ...] = ("transformers", "peft", "trl", "accelerate",
                                                                      "datasets")) -> dict[str, Any]:
    """Does every library's declared torch range accept ``torch_version``? (local tags such as +rocm7.1 ignored)"""
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    version = Version(Version(torch_version).base_version)
    rows: dict[str, Any] = {}
    for dist in dists:
        try:
            specs = torch_requirements(dist, extra=TORCH_EXTRAS.get(dist))
        except md.PackageNotFoundError:
            rows[dist] = {"ok": False, "error": "not installed"}
            continue
        bad = [s for s in specs if s != "(any)" and version not in SpecifierSet(s)]
        rows[dist] = {"ok": not bad, "declares": specs, "rejects": bad}
    return rows


def check_imports() -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for name in LIBRARIES:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - every failure is a verdict
            rows[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            continue
        missing = [n for n in REQUIRED_NAMES.get(name, ()) if not hasattr(mod, n)]
        rows[name] = {"ok": not missing, "version": getattr(mod, "__version__", None), "missing": missing}
    return rows


def lora_step(device: str) -> dict[str, Any]:
    """One LoRA optimizer step on a tiny random Gemma 4-layout model (the lora/train.py code path, toy size)."""
    import torch
    from peft import LoraConfig, get_peft_model

    from editions.amd.lora import modeling

    t0 = time.perf_counter()
    texts = ["Hold the tainted amount only.", "Grounds of suspicion: layered transfers through mule accounts."] * 8
    tok = modeling.build_tiny_tokenizer(texts, vocab_size=320)
    model = modeling.build_tiny_model(tok, arch="gemma4", hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                                      num_attention_heads=2, num_key_value_heads=1).to(device)
    targets = modeling.detect_lora_targets(model, "attn_mlp")
    peft_model = get_peft_model(model, LoraConfig(r=4, lora_alpha=8, lora_dropout=0.0, bias="none",
                                                  task_type="CAUSAL_LM", target_modules=targets.regex))
    peft_model.train()
    batch = tok(texts[:4], return_tensors="pt", padding=True).to(device)
    labels = batch["input_ids"].clone()
    labels[batch["attention_mask"] == 0] = -100
    lora_params = [p for n, p in peft_model.named_parameters() if p.requires_grad]
    before = [p.detach().clone() for p in lora_params]
    opt = torch.optim.AdamW(lora_params, lr=1e-2)
    loss = peft_model(**batch, labels=labels).loss
    loss.backward()
    opt.step()
    changed = sum(1 for p, b in zip(lora_params, before, strict=True) if not torch.equal(p.detach(), b))
    finite = bool(torch.isfinite(loss.detach()).item())
    return {"ok": finite and changed > 0, "device": device, "loss": round(float(loss.detach()), 4),
            "loss_finite": finite, "lora_modules": len(targets.modules), "trainable_tensors": len(lora_params),
            "tensors_changed": changed, "seconds": round(time.perf_counter() - t0, 2)}


def run(device: str = "auto", require_gpu: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {"kind": "train_image_check", "schema": 1,
                              "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "note": "plumbing check on a tiny random model; measures nothing"}
    report["imports"] = imports = check_imports()
    if not imports.get("torch", {}).get("ok"):
        report["ok"] = False
        return report
    import torch

    report["torch"] = {"version": torch.__version__, "hip": getattr(torch.version, "hip", None),
                       "cuda": torch.version.cuda, "gpu_visible": torch.cuda.is_available(),
                       "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}
    report["torch_ranges"] = check_torch_ranges(torch.__version__)
    gpu_ok = torch.cuda.is_available() or not require_gpu
    if not gpu_ok:
        report["gpu_error"] = "no GPU visible to torch in this container (--device=/dev/kfd --device=/dev/dri?)"
    dev = ("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else device
    if all(r.get("ok") for r in imports.values()):
        try:
            report["lora_step"] = lora_step(dev)
        except Exception as exc:  # noqa: BLE001
            report["lora_step"] = {"ok": False, "device": dev, "error": f"{type(exc).__name__}: {exc}"[:500]}
    else:
        report["lora_step"] = {"ok": False, "error": "skipped: an import failed"}
    report["ok"] = bool(gpu_ok and all(r.get("ok") for r in imports.values())
                        and all(r.get("ok") for r in report["torch_ranges"].values()) and report["lora_step"]["ok"])
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="auto", help="auto (GPU if visible) | cpu | cuda")
    ap.add_argument("--require-gpu", action="store_true", help="fail when torch sees no GPU (the MI300X image)")
    ap.add_argument("--out", default=None, help="write the report here (JSON)")
    args = ap.parse_args(argv)
    rep = run(args.device, args.require_gpu)
    for name, row in rep["imports"].items():
        print(f"  {name:<13} {'ok' if row.get('ok') else 'FAIL'}  {row.get('version') or row.get('error') or ''}"
              + (f"  missing {row['missing']}" if row.get("missing") else ""))
    for name, row in (rep.get("torch_ranges") or {}).items():
        print(f"  torch range of {name:<12} {'ok' if row.get('ok') else 'FAIL'}  {row.get('declares') or row.get('error')}")
    step = rep.get("lora_step") or {}
    print(f"  LoRA step     {'ok' if step.get('ok') else 'FAIL'}  {json.dumps({k: v for k, v in step.items() if k != 'ok'})}")
    if rep.get("gpu_error"):
        print(f"  GPU           FAIL  {rep['gpu_error']}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {out}")
    print("train image check: " + ("ok" if rep["ok"] else "FAILED (see RUNBOOK, Troubleshooting: training image)"))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
