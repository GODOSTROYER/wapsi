"""LoRA SFT of an open model on the Wapsi prose dataset (TRL ``SFTTrainer`` + PEFT).

    # offline CPU smoke test (random 2-layer model + BPE tokenizer trained on the data; no downloads)
    uv run python -m editions.amd.lora.train --config tiny-cpu --data data/generated/lora-fake --out models/lora/tiny

    # MI300X (ROCm PyTorch), event week
    python -m editions.amd.lora.train --config gemma4-31b-mi300x --data data/generated/lora-<teacher> \
        --out models/lora/gemma4-31b

- LoRA targets are auto-detected per architecture (``modeling.detect_lora_targets``) and printed;
- loss on the final assistant turn only (prompts and the wrong first answers of correction examples are masked);
- bf16 + gradient checkpointing on GPU, packing off by default, eval on the val split every ``--eval-steps``;
- logs tokens/s and peak memory (``torch.cuda.max_memory_allocated``, which is HIP-backed on ROCm);
- writes ``adapter/`` (PEFT adapter + tokenizer), ``trainer_state.json``, ``train_config.json``,
  ``train_metrics.json`` and, in ``--tiny`` mode, the random base model under ``tiny-base/``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from editions.amd.lora.common import CONFIGS, describe, read_jsonl, sha256_file, write_json

META_KEYS = {"name", "description", "notes", "estimates", "serve", "model_notes", "dataset_notes"}


def load_preset(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    p = Path(value)
    if not p.exists():
        p = CONFIGS / (value if value.endswith(".json") else f"{value}.json")
    if not p.exists():
        raise SystemExit(f"config preset not found: {value}")
    return json.loads(p.read_text(encoding="utf-8"))


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--config", default=None, help="preset name under configs/ or a JSON path (CLI flags override)"
    )
    ap.add_argument("--model", default=None, help="HF hub id or local path of the base model")
    ap.add_argument(
        "--tiny", action="store_true", help="random tiny model + tokenizer trained on the data (tests)"
    )
    ap.add_argument("--tiny-arch", default="llama", choices=["llama", "qwen3", "gemma3", "gemma4"])
    ap.add_argument("--tiny-hidden-size", type=int, default=64)
    ap.add_argument("--tiny-layers", type=int, default=2)
    ap.add_argument("--tiny-vocab-size", type=int, default=2048)
    ap.add_argument("--data", default=None, help="dataset dir with train.jsonl / val.jsonl")
    ap.add_argument("--train-file", default=None)
    ap.add_argument("--val-file", default=None)
    ap.add_argument("--out", default=None, help="output dir (default models/lora/<preset name>)")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--dtype", default="auto", choices=["auto", "bf16", "fp16", "fp32"])
    ap.add_argument(
        "--model-class", default=None, help="override the transformers class (e.g. Gemma4ForCausalLM)"
    )
    ap.add_argument("--attn-implementation", default=None, help="sdpa | eager | flash_attention_2 ...")
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--lora-alpha", type=int, default=32)
    ap.add_argument("--lora-dropout", type=float, default=0.05)
    ap.add_argument("--target-modules", default="attn_mlp", help="attn | attn_mlp | comma list of leaf names")
    ap.add_argument("--max-seq-len", type=int, default=8192, help="examples longer than this are dropped")
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--eval-batch-size", type=int, default=None)
    ap.add_argument("--grad-accum", type=int, default=16)
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--max-steps", type=int, default=0, help=">0 overrides --epochs")
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--lr-scheduler", default="cosine")
    ap.add_argument("--warmup", type=float, default=0.03, help="warmup steps (int) or ratio in [0, 1)")
    ap.add_argument("--weight-decay", type=float, default=0.0)
    ap.add_argument("--eval-steps", type=int, default=50)
    ap.add_argument("--logging-steps", type=int, default=5)
    ap.add_argument("--save-steps", type=int, default=0, help="0 = no intermediate checkpoints")
    ap.add_argument("--gradient-checkpointing", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--packing", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--loss-type", default="chunked_nll", help="chunked_nll | nll | dft (TRL)")
    ap.add_argument(
        "--chat-template-kwargs", default=None, help='JSON, e.g. {"enable_thinking": false} for Qwen'
    )
    ap.add_argument("--max-train-examples", type=int, default=0)
    ap.add_argument("--max-eval-examples", type=int, default=200)
    ap.add_argument("--eval-before", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--report-to", default="none")
    ap.add_argument(
        "--dry-run", action="store_true", help="load, detect targets, tokenize, print stats; no training"
    )
    return ap


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = parser()
    pre, _ = ap.parse_known_args(argv)
    preset = load_preset(pre.config)
    dests = {a.dest for a in ap._actions}
    unknown = sorted(k for k in preset if k not in dests and k not in META_KEYS)
    if unknown:
        raise SystemExit(f"unknown keys in preset {pre.config}: {unknown}")
    ap.set_defaults(**{k: v for k, v in preset.items() if k in dests})
    args = ap.parse_args(argv)
    args.preset_name = preset.get("name") or (Path(pre.config).stem if pre.config else None)
    if isinstance(args.chat_template_kwargs, str):
        args.chat_template_kwargs = json.loads(args.chat_template_kwargs)
    if not args.tiny and not args.model:
        raise SystemExit("--model is required (or --tiny)")
    if args.data:
        args.train_file = args.train_file or str(Path(args.data) / "train.jsonl")
        vf = Path(args.data) / "val.jsonl"
        args.val_file = args.val_file or (str(vf) if vf.exists() else None)
    if not args.train_file:
        raise SystemExit("--data or --train-file is required")
    if args.out is None:
        args.out = str(Path("models") / "lora" / (args.preset_name or "run"))
    return args


def _messages(path: str | None, limit: int = 0) -> list[list[dict[str, str]]]:
    if not path or not Path(path).exists():
        return []
    rows = read_jsonl(Path(path))
    msgs = [r["messages"] for r in rows if r.get("messages")]
    return msgs[:limit] if limit else msgs


def _encode_all(tok: Any, examples: list[list[dict[str, str]]], args: argparse.Namespace):
    from editions.amd.lora.modeling import encode_chat

    ids, labels, reasons = [], [], Counter()
    lengths, completion = [], []
    for m in examples:
        enc, why = encode_chat(tok, m, max_len=None, chat_template_kwargs=args.chat_template_kwargs)
        if enc is None:
            reasons[why] += 1
            continue
        n = len(enc["input_ids"])
        lengths.append(n)
        completion.append(sum(1 for x in enc["labels"] if x != -100))
        if n > args.max_seq_len:
            reasons["too_long"] += 1
            continue
        reasons["kept"] += 1
        ids.append(enc["input_ids"])
        labels.append(enc["labels"])
    stats = {"tokens": describe(lengths), "completion_tokens": describe(completion), "outcome": dict(reasons)}
    return ids, labels, stats


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import TrainerCallback, set_seed
    from trl import SFTConfig, SFTTrainer

    from editions.amd.lora import modeling

    set_seed(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    device = modeling.resolve_device(args.device)
    dtype = modeling.resolve_dtype(args.dtype, device)
    train_msgs = _messages(args.train_file, args.max_train_examples)
    val_msgs = _messages(args.val_file, args.max_eval_examples)
    if not train_msgs:
        raise SystemExit(f"no training examples in {args.train_file}")
    print(f"[train] device={device} dtype={dtype} train={len(train_msgs)} val={len(val_msgs)}")

    t_load = time.perf_counter()
    if args.tiny:
        texts = [m["content"] for ex in train_msgs + val_msgs for m in ex]
        tok = modeling.build_tiny_tokenizer(texts, vocab_size=args.tiny_vocab_size)
        tiny = modeling.build_tiny_model(
            tok,
            seed=args.seed,
            arch=args.tiny_arch,
            hidden_size=args.tiny_hidden_size,
            intermediate_size=2 * args.tiny_hidden_size,
            num_hidden_layers=args.tiny_layers,
        )
        base_path = out / "tiny-base"
        tiny.save_pretrained(base_path)
        tok.save_pretrained(base_path)
        del tiny
        base = str(base_path)
    else:
        base = args.model
    tok = modeling.load_tokenizer(base)
    model = modeling.load_model(
        base,
        dtype=dtype,
        device=device,
        attn_implementation=args.attn_implementation,
        model_class_name=args.model_class,
    )
    load_s = time.perf_counter() - t_load
    n_params = sum(p.numel() for p in model.parameters())
    print(
        f"[train] base={base} class={type(model).__name__} params={n_params / 1e9:.3f}B loaded in {load_s:.1f}s"
    )
    if modeling.peak_memory_gb() is not None:
        print(f"[train] memory after load: {modeling.peak_memory_gb()} GiB")

    targets = modeling.detect_lora_targets(model, args.target_modules)
    print(targets.summary())

    tr_ids, tr_labels, tr_stats = _encode_all(tok, train_msgs, args)
    va_ids, va_labels, va_stats = _encode_all(tok, val_msgs, args)
    print(f"[train] train tokens {json.dumps(tr_stats)}")
    print(f"[train] val tokens {json.dumps(va_stats)}")
    if not tr_ids:
        raise SystemExit("every training example was dropped (see outcome counts; raise --max-seq-len?)")
    dropped = tr_stats["outcome"].get("too_long", 0)
    if dropped / max(1, len(train_msgs)) > 0.05:
        print(f"[train] WARNING: {dropped} training examples exceed --max-seq-len={args.max_seq_len}")

    lora = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=targets.regex,
    )
    config_record = {
        "args": {k: v for k, v in vars(args).items()},
        "base_model": base,
        "model_class": type(model).__name__,
        "family": targets.family,
        "params_total": n_params,
        "lora": {
            "r": args.lora_r,
            "alpha": args.lora_alpha,
            "dropout": args.lora_dropout,
            "target_regex": targets.regex,
            "matched_by_leaf": targets.by_leaf,
            "matched_modules": len(targets.modules),
            "excluded": targets.excluded,
            "fused_expert_tensors_skipped": targets.fused_expert_params,
        },
        "loss_masking": "final assistant turn only (prompt and earlier turns labelled -100)",
        "data": {
            "train_file": args.train_file,
            "val_file": args.val_file,
            "train_sha256": sha256_file(Path(args.train_file)),
            "val_sha256": sha256_file(Path(args.val_file)) if args.val_file else None,
            "train_tokens": tr_stats,
            "val_tokens": va_stats,
        },
        "env": modeling.env_info(),
    }
    write_json(out / "train_config.json", config_record)
    if args.dry_run:
        print("[train] dry run: stopping before training")
        return 0

    train_ds = Dataset.from_dict({"input_ids": tr_ids, "labels": tr_labels})
    eval_ds = Dataset.from_dict({"input_ids": va_ids, "labels": va_labels}) if va_ids else None
    steps_per_epoch = math.ceil(len(tr_ids) / (args.batch_size * args.grad_accum))

    class Telemetry(TrainerCallback):
        def __init__(self) -> None:
            self.history: list[dict[str, Any]] = []
            self._t = time.perf_counter()
            self._tokens = 0.0

        def on_train_begin(self, a, state, control, **kw):  # noqa: ANN001
            self._t = time.perf_counter()
            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()

        def on_log(self, a, state, control, logs=None, **kw):  # noqa: ANN001
            logs = dict(logs or {})
            entry: dict[str, Any] = {"step": state.global_step, "epoch": round(state.epoch or 0.0, 4)}
            entry.update({k: v for k, v in logs.items() if isinstance(v, (int, float))})
            now = time.perf_counter()
            if "num_tokens" in logs:
                dt, dtok = now - self._t, float(logs["num_tokens"]) - self._tokens
                if dt > 0 and dtok > 0:
                    entry["tokens_per_s"] = round(dtok / dt, 1)
                self._t, self._tokens = now, float(logs["num_tokens"])
            mem = modeling.peak_memory_gb()
            if mem is not None:
                entry["peak_mem_gb"] = mem
            self.history.append(entry)
            keys = (
                "loss",
                "eval_loss",
                "tokens_per_s",
                "peak_mem_gb",
                "learning_rate",
                "mean_token_accuracy",
            )
            print("[train] " + " ".join(f"{k}={entry[k]}" for k in ("step", *keys) if k in entry))

    telemetry = Telemetry()
    sft = SFTConfig(
        output_dir=str(out / "checkpoints"),
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.eval_batch_size or args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        num_train_epochs=args.epochs,
        max_steps=args.max_steps if args.max_steps > 0 else -1,
        learning_rate=args.lr,
        lr_scheduler_type=args.lr_scheduler,
        warmup_steps=args.warmup,
        weight_decay=args.weight_decay,
        logging_steps=args.logging_steps,
        logging_first_step=True,
        eval_strategy="steps" if eval_ds is not None else "no",
        eval_steps=args.eval_steps,
        save_strategy="steps" if args.save_steps > 0 else "no",
        save_steps=args.save_steps if args.save_steps > 0 else 500,
        save_total_limit=2,
        bf16=dtype == torch.bfloat16,
        fp16=dtype == torch.float16,
        gradient_checkpointing=args.gradient_checkpointing,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        max_length=args.max_seq_len,
        packing=args.packing,
        loss_type=args.loss_type,
        seed=args.seed,
        data_seed=args.seed,
        report_to=args.report_to,
        use_cpu=device == "cpu",
        dataloader_num_workers=0,
        dataloader_pin_memory=device.startswith("cuda"),
        disable_tqdm=True,
    )
    trainer = SFTTrainer(
        model=model,
        args=sft,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        processing_class=tok,
        peft_config=lora,
        callbacks=[telemetry],
    )
    trainable = sum(p.numel() for p in trainer.model.parameters() if p.requires_grad)
    print(
        f"[train] trainable params {trainable:,} ({100 * trainable / n_params:.3f}% of {n_params:,}); "
        f"{steps_per_epoch} optimizer steps/epoch"
    )

    eval_before = trainer.evaluate() if (eval_ds is not None and args.eval_before) else {}
    t0 = time.perf_counter()
    result = trainer.train()
    wall = time.perf_counter() - t0
    eval_after = trainer.evaluate() if eval_ds is not None else {}

    adapter_dir = out / "adapter"
    trainer.save_model(str(adapter_dir))
    tok.save_pretrained(adapter_dir)
    trainer.state.save_to_json(str(out / "trainer_state.json"))
    train_losses = [h["loss"] for h in telemetry.history if "loss" in h]
    tps = [h["tokens_per_s"] for h in telemetry.history if "tokens_per_s" in h]
    metrics = {
        "note": "Measured on this machine; tiny-model runs are pipeline checks, not model results.",
        "device": device,
        "dtype": str(dtype),
        "base_model": base,
        "train_examples": len(tr_ids),
        "val_examples": len(va_ids),
        "dropped_train_examples": {k: v for k, v in tr_stats["outcome"].items() if k != "kept"},
        "global_steps": trainer.state.global_step,
        "train_runtime_s": round(wall, 2),
        "train_loss_mean": round(result.training_loss, 6) if result.training_loss is not None else None,
        "train_loss_first": train_losses[0] if train_losses else None,
        "train_loss_last": train_losses[-1] if train_losses else None,
        "eval_loss_before": eval_before.get("eval_loss"),
        "eval_loss_after": eval_after.get("eval_loss"),
        "tokens_per_s": describe(tps),
        "peak_mem_gb": modeling.peak_memory_gb(),
        "trainable_params": trainable,
        "total_params": n_params,
        "history": telemetry.history,
    }
    write_json(out / "train_metrics.json", metrics)
    print(
        f"[train] done in {wall:.1f}s: loss {metrics['train_loss_first']} -> {metrics['train_loss_last']}, "
        f"eval {metrics['eval_loss_before']} -> {metrics['eval_loss_after']}, peak {metrics['peak_mem_gb']} GiB; "
        f"adapter -> {adapter_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
