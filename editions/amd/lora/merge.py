"""Merge a LoRA adapter into its base model and export a plain Hugging Face checkpoint for vLLM.

    uv run python -m editions.amd.lora.merge --run models/lora/tiny --out models/lora/tiny-merged
    python -m editions.amd.lora.merge --run models/lora/gemma4-31b --out /models/wapsi-gemma-4-31b-str-merged \
        --device cuda --merge-dtype fp32 --save-dtype bf16

vLLM has no LoRA runtime for Gemma 4, so the adapter is folded into the weights (``merge_and_unload``) and saved
as safetensors shards + tokenizer (+ chat template / processor files) + generation config, with the base model's
own class (a multimodal Gemma 4 checkpoint stays ``Gemma4ForConditionalGeneration``, vision tower untouched).

Checks (``merge_report.json``; exit code 1 on failure):
  A. adapter model vs merged model logits, same dtype, on a few real prompts (``--atol``);
  B. the checkpoint reloaded from disk vs the adapter model (tolerance when the dtypes match; otherwise top-1
     agreement ``--min-top1`` and relative max diff ``--rtol``, since bf16 rounding of merged weights is expected).
Merging in fp32 (default) keeps A exact; on an MI300X a 31B model in fp32 (~124 GB) fits in 192 GB.
"""

from __future__ import annotations

import argparse
import gc
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from editions.amd.lora.common import read_jsonl, write_json

AUX_PATTERNS = (
    "*.jinja",
    "chat_template.json",
    "preprocessor_config.json",
    "processor_config.json",
    "video_preprocessor_config.json",
    "tokenizer.model",
)
DEFAULT_PROMPTS = [
    [
        {
            "role": "user",
            "content": '{"task": "case_summary", "instructions": "Summarise the case.", "placeholders": '
            '[{"name": "total_stolen", "value": "x", "meaning": "total reported stolen"}]}',
        }
    ],
    [{"role": "user", "content": "Write one formal paragraph for a bank record using only placeholders."}],
]


def _prompts(path: str | None, n: int) -> list[list[dict[str, str]]]:
    if path and Path(path).exists():
        rows = read_jsonl(Path(path))[:n]
        return [r["messages"][:-1] for r in rows if r.get("messages")] or DEFAULT_PROMPTS[:n]
    return DEFAULT_PROMPTS[:n]


def _checkpoint_dtype(base: str, device: str) -> Any:
    """The dtype the base checkpoint was published in (read before loading, which overwrites it)."""
    import torch
    import transformers

    from editions.amd.lora import modeling

    cfg = transformers.AutoConfig.from_pretrained(str(base))
    for c in (cfg, getattr(cfg, "text_config", None)):
        value = getattr(c, "dtype", None) or getattr(c, "torch_dtype", None) if c is not None else None
        if isinstance(value, torch.dtype):
            return value
        if isinstance(value, str) and value.replace("torch.", "") in ("bfloat16", "float16", "float32"):
            return getattr(torch, value.replace("torch.", ""))
    return modeling.resolve_dtype("auto", device)


def _copy_aux(base: str, out: Path) -> list[str]:
    """Copy chat-template / processor files the tokenizer save does not write (vLLM needs them for VLM classes)."""
    src = Path(base)
    if not src.is_dir():
        try:
            from huggingface_hub import snapshot_download

            src = Path(snapshot_download(base, allow_patterns=list(AUX_PATTERNS), local_files_only=True))
        except Exception:  # noqa: BLE001 - offline / not cached: nothing to copy
            return []
    copied = []
    for pattern in AUX_PATTERNS:
        for f in src.glob(pattern):
            dest = out / f.name
            if f.is_file() and not dest.exists():
                shutil.copyfile(f, dest)
                copied.append(f.name)
    return copied


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=None, help="train.py output dir (reads train_config.json and adapter/)")
    ap.add_argument("--adapter", default=None, help="adapter dir (default <run>/adapter)")
    ap.add_argument(
        "--base", default=None, help="base model (default: from train_config.json / adapter_config.json)"
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--merge-dtype", default="fp32", choices=["fp32", "bf16", "fp16"])
    ap.add_argument(
        "--save-dtype",
        default="auto",
        choices=["auto", "fp32", "bf16", "fp16"],
        help="auto = the base checkpoint's dtype (bf16 for Gemma 4 / Qwen)",
    )
    ap.add_argument("--max-shard-size", default="5GB")
    ap.add_argument(
        "--check-data", default=None, help="JSONL with messages for the logit check (default: val/test)"
    )
    ap.add_argument("--check-prompts", type=int, default=3)
    ap.add_argument(
        "--check-max-tokens", type=int, default=1024, help="truncate check prompts (left) to this"
    )
    ap.add_argument("--check-positions", type=int, default=16, help="compare logits at the last N positions")
    ap.add_argument("--atol", type=float, default=None, help="max |diff| (default 1e-3 fp32, 5e-2 bf16/fp16)")
    ap.add_argument(
        "--rtol", type=float, default=0.05, help="reload check across dtypes: max|diff| / max|logit|"
    )
    ap.add_argument(
        "--min-top1", type=float, default=0.95, help="reload check across dtypes: top-1 agreement"
    )
    ap.add_argument("--no-reload-check", action="store_true")
    args = ap.parse_args(argv)

    import torch
    from peft import PeftModel

    from editions.amd.lora import modeling

    t0 = time.perf_counter()
    run = Path(args.run) if args.run else None
    adapter = Path(args.adapter) if args.adapter else (run / "adapter" if run else None)
    if adapter is None or not (adapter / "adapter_config.json").exists():
        raise SystemExit(f"no adapter found at {adapter}")
    train_cfg: dict[str, Any] = {}
    if run and (run / "train_config.json").exists():
        train_cfg = json.loads((run / "train_config.json").read_text(encoding="utf-8"))
    adapter_cfg = json.loads((adapter / "adapter_config.json").read_text(encoding="utf-8"))
    base = args.base or train_cfg.get("base_model") or adapter_cfg.get("base_model_name_or_path")
    if not base:
        raise SystemExit("cannot tell the base model; pass --base")
    model_class_name = (train_cfg.get("args") or {}).get("model_class")
    chat_kw = (train_cfg.get("args") or {}).get("chat_template_kwargs") or {}
    device = modeling.resolve_device(args.device)
    merge_dtype = modeling.resolve_dtype(args.merge_dtype, device)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tok_src = adapter if (adapter / "tokenizer_config.json").exists() else Path(base)
    tok = modeling.load_tokenizer(tok_src)
    save_dtype = (
        _checkpoint_dtype(base, device)
        if args.save_dtype == "auto"
        else modeling.resolve_dtype(args.save_dtype, device)
    )
    model = modeling.load_model(base, dtype=merge_dtype, device=device, model_class_name=model_class_name)
    peft_model = PeftModel.from_pretrained(model, str(adapter))
    peft_model.eval()

    check_data = args.check_data
    if check_data is None and run:
        for cand in (
            Path((train_cfg.get("data") or {}).get("val_file") or ""),
            Path((train_cfg.get("data") or {}).get("train_file") or ""),
        ):
            if cand.name and cand.exists():
                check_data = str(cand)
                break
    prompts = _prompts(check_data, args.check_prompts)
    encoded = []
    for msgs in prompts:
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, **chat_kw)
        ids = tok(text, add_special_tokens=False, return_tensors="pt")["input_ids"][
            :, -args.check_max_tokens :
        ]
        encoded.append(ids)

    def logits_of(m: Any) -> list[torch.Tensor]:
        res = []
        with torch.inference_mode():
            for ids in encoded:
                lg = m(input_ids=ids.to(m.device)).logits[0, -args.check_positions :].float().cpu()
                res.append(lg)
        return res

    def compare(a: list[torch.Tensor], b: list[torch.Tensor]) -> dict[str, float]:
        diffs = [(x - y).abs() for x, y in zip(a, b, strict=True)]
        max_abs = max(float(d.max()) for d in diffs)
        scale = max(float(x.abs().max()) for x in a) or 1.0
        top1 = torch.cat([(x.argmax(-1) == y.argmax(-1)).float() for x, y in zip(a, b, strict=True)])
        return {
            "max_abs_diff": max_abs,
            "mean_abs_diff": float(torch.cat([d.flatten() for d in diffs]).mean()),
            "rel_max_diff": max_abs / scale,
            "top1_agreement": float(top1.mean()),
        }

    ref = logits_of(peft_model)
    with peft_model.disable_adapter():
        base_logits = logits_of(peft_model)
    effect = compare(ref, base_logits)
    print(
        f"[merge] adapter effect vs base (should be > 0 after training): max_abs_diff={effect['max_abs_diff']:.3g}"
    )
    merged = peft_model.merge_and_unload()
    merged.eval()
    check_a = compare(ref, logits_of(merged))
    atol = args.atol if args.atol is not None else (1e-3 if merge_dtype == torch.float32 else 5e-2)
    check_a["passed"] = check_a["max_abs_diff"] <= atol
    print(f"[merge] adapter vs merged ({merge_dtype}): {check_a}")

    if save_dtype != merge_dtype:
        merged.to(save_dtype)
    merged.save_pretrained(str(out), max_shard_size=args.max_shard_size)
    tok.save_pretrained(str(out))
    copied = _copy_aux(base, out)
    del merged, peft_model, model
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    check_b: dict[str, Any] = {"skipped": True}
    if not args.no_reload_check:
        reloaded = modeling.load_model(
            out, dtype=save_dtype, device=device, model_class_name=model_class_name
        )
        reloaded.eval()
        check_b = compare(ref, logits_of(reloaded))
        if save_dtype == merge_dtype:
            check_b["passed"] = check_b["max_abs_diff"] <= atol
        else:
            check_b["passed"] = (
                check_b["top1_agreement"] >= args.min_top1 and check_b["rel_max_diff"] <= args.rtol
            )
        print(f"[merge] adapter vs reloaded checkpoint ({save_dtype}): {check_b}")
        del reloaded
        gc.collect()

    files = sorted(p.name for p in out.iterdir() if p.is_file())
    report = {
        "base_model": str(base),
        "adapter": str(adapter),
        "out": str(out),
        "model_class": model_class_name,
        "device": device,
        "merge_dtype": str(merge_dtype),
        "save_dtype": str(save_dtype),
        "check_prompts": len(encoded),
        "check_positions": args.check_positions,
        "atol": atol,
        "adapter_effect_vs_base": effect,
        "adapter_vs_merged": check_a,
        "adapter_vs_reloaded": check_b,
        "passed": bool(check_a["passed"] and check_b.get("passed", True)),
        "files": files,
        "aux_files_copied": copied,
        "size_gb": round(sum(p.stat().st_size for p in out.iterdir() if p.is_file()) / 2**30, 3),
        "seconds": round(time.perf_counter() - t0, 1),
        "serve_hint": f"vllm serve {out} --served-model-name <name> --dtype bfloat16 --max-model-len 16384",
    }
    write_json(out / "merge_report.json", report)
    print(
        f"[merge] {'PASSED' if report['passed'] else 'FAILED'} -> {out} ({report['size_gb']} GiB, {len(files)} files)"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
