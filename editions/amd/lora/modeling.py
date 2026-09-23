"""Model and tokenizer helpers shared by ``train``, ``merge`` and ``evaluate``.

- loading a checkpoint with the class its ``config.architectures`` names (so a multimodal Gemma 4 checkpoint is
  trained, merged and saved as ``Gemma4ForConditionalGeneration`` and vLLM serves the merged weights like the base);
- LoRA target auto-detection per architecture (Gemma / Gemma 3 / Gemma 4, Llama, Qwen 2/3, Qwen 3.5 MoE / hybrid);
- chat encoding with loss on the final assistant turn only;
- a tiny random model + byte-level BPE tokenizer for offline CPU tests;
- ``HFChatLLM``: a local model behind Wapsi's ``LLM`` protocol.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import torch
import transformers
from torch import nn

TINY_SPECIALS = ["<pad>", "<unk>", "<bos>", "<eos>", "<start_of_turn>", "<end_of_turn>"]
TINY_CHAT_TEMPLATE = (
    "{{ bos_token }}{% for m in messages %}<start_of_turn>{{ m['role'] }}\n{{ m['content'] }}<end_of_turn>\n"
    "{% endfor %}{% if add_generation_prompt %}<start_of_turn>assistant\n{% endif %}"
)
TINY_DEFAULTS: dict[str, Any] = {
    "arch": "llama",
    "hidden_size": 64,
    "intermediate_size": 128,
    "num_hidden_layers": 2,
    "num_attention_heads": 4,
    "num_key_value_heads": 2,
    "max_position_embeddings": 16384,
    "vocab_size": 2048,
}

ATTN_LEAVES = ("q_proj", "k_proj", "v_proj", "o_proj")
MLP_LEAVES = ("gate_proj", "up_proj", "down_proj")
# Qwen 3.5 / 3.6 hybrid layers (Gated DeltaNet linear attention)
LINEAR_ATTN_LEAVES = ("in_proj_qkv", "in_proj_z", "out_proj")
# never adapt towers, projectors, routers, heads or Gemma 4 per-layer-embedding projections
EXCLUDE_RE = re.compile(
    r"vision|visual|audio|multi_modal|mm_projector|embed_|lm_head|router|per_layer|\.gate$"
)


# --------------------------------------------------------------------------- device / dtype / loading


def resolve_device(device: str = "auto") -> str:
    if device == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise SystemExit(
            "--device cuda requested but torch.cuda.is_available() is False (ROCm PyTorch installed?)"
        )
    return device


def resolve_dtype(dtype: str, device: str) -> torch.dtype:
    if dtype == "auto":
        if device.startswith("cuda") and torch.cuda.is_bf16_supported():
            return torch.bfloat16
        return torch.float32
    return {
        "bf16": torch.bfloat16,
        "bfloat16": torch.bfloat16,
        "fp16": torch.float16,
        "float16": torch.float16,
        "fp32": torch.float32,
        "float32": torch.float32,
    }[dtype]


def model_class(path_or_config: Any, override: str | None = None) -> type:
    """The class named by ``config.architectures`` (or ``override``), falling back to AutoModelForCausalLM."""
    if override:
        cls = getattr(transformers, override, None)
        if cls is None:
            raise SystemExit(f"transformers has no class {override!r}")
        return cls
    cfg = path_or_config
    if isinstance(path_or_config, (str, Path)):
        cfg = transformers.AutoConfig.from_pretrained(str(path_or_config))
    for arch in getattr(cfg, "architectures", None) or []:
        cls = getattr(transformers, arch, None)
        if cls is not None:
            return cls
    return transformers.AutoModelForCausalLM


def load_model(
    path: str | Path,
    *,
    dtype: torch.dtype,
    device: str,
    attn_implementation: str | None = None,
    model_class_name: str | None = None,
) -> Any:
    cls = model_class(path, model_class_name)
    kwargs: dict[str, Any] = {"dtype": dtype}
    if attn_implementation:
        kwargs["attn_implementation"] = attn_implementation
    if device.startswith("cuda"):
        kwargs["device_map"] = {"": torch.device(device).index or 0}
    model = cls.from_pretrained(str(path), **kwargs)
    if not device.startswith("cuda"):
        model.to(device)
    return model


def load_tokenizer(path: str | Path) -> Any:
    try:
        tok = transformers.AutoTokenizer.from_pretrained(str(path))
    except (OSError, ValueError, KeyError):
        tok = transformers.AutoProcessor.from_pretrained(str(path)).tokenizer
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    return tok


def family_of(config: Any) -> str:
    mt = str(getattr(config, "model_type", "") or "").lower()
    for fam in ("gemma4", "gemma3", "gemma2", "gemma", "qwen3_5", "qwen3", "qwen2", "llama", "mistral"):
        if mt.startswith(fam):
            return fam
    return mt or "unknown"


# --------------------------------------------------------------------------- LoRA targets


@dataclass
class TargetReport:
    family: str
    mode: str
    regex: str
    modules: list[str]
    by_leaf: dict[str, int]
    excluded: dict[str, int] = field(default_factory=dict)
    fused_expert_params: int = 0

    def summary(self) -> str:
        lines = [
            f"LoRA targets for family={self.family} mode={self.mode}: {len(self.modules)} Linear modules",
            "  matched: " + ", ".join(f"{k} x{v}" for k, v in sorted(self.by_leaf.items())),
        ]
        if self.excluded:
            lines.append(
                "  excluded (towers/heads/routers/PLE): "
                + ", ".join(f"{k} x{v}" for k, v in sorted(self.excluded.items()))
            )
        if self.fused_expert_params:
            lines.append(
                f"  skipped {self.fused_expert_params} fused MoE expert tensors (not nn.Linear; not adapted)"
            )
        lines.append(f"  target_modules regex: {self.regex}")
        return "\n".join(lines)


def detect_lora_targets(model: nn.Module, mode: str = "attn_mlp") -> TargetReport:
    """Find the language-model projections to adapt.

    ``mode``: ``attn`` (q/k/v/o [+ linear-attention projections]), ``attn_mlp`` (+ gate/up/down, including MoE
    shared experts), or an explicit comma list of leaf names. Towers (vision/audio), projectors, routers, the LM
    head and Gemma 4 per-layer-embedding projections are always excluded. Returns a regex PEFT full-matches.
    """
    if mode == "attn":
        wanted = set(ATTN_LEAVES) | set(LINEAR_ATTN_LEAVES)
    elif mode in ("attn_mlp", "auto", "all"):
        wanted = set(ATTN_LEAVES) | set(MLP_LEAVES) | set(LINEAR_ATTN_LEAVES)
    else:
        wanted = {m.strip() for m in mode.split(",") if m.strip()}
    matched: list[str] = []
    excluded: Counter = Counter()
    for name, mod in model.named_modules():
        leaf = name.rsplit(".", 1)[-1]
        if leaf not in wanted:
            continue
        if not isinstance(mod, nn.Linear):
            # e.g. Gemma 4 vision-tower ``q_proj`` is a Gemma4ClippableLinear wrapper; a plain suffix match in PEFT
            # would hit it and fail, which is why targets are passed as an explicit regex
            excluded[f"{leaf}:{type(mod).__name__}"] += 1
            continue
        if EXCLUDE_RE.search(name):
            excluded[leaf] += 1
            continue
        matched.append(name)
    if not matched:
        raise SystemExit(f"no LoRA target modules found for mode={mode!r}; pass --target-modules explicitly")
    fused = sum(1 for n, p in model.named_parameters() if ".experts." in n and p.dim() == 3)
    patterns = sorted({re.sub(r"\.\d+\.", ".N.", n) for n in matched})
    regex = "(?:" + "|".join(re.escape(p).replace(r"\.N\.", r"\.\d+\.") for p in patterns) + ")"
    return TargetReport(
        family=family_of(getattr(model, "config", None)),
        mode=mode,
        regex=regex,
        modules=matched,
        by_leaf=dict(Counter(n.rsplit(".", 1)[-1] for n in matched)),
        excluded=dict(excluded),
        fused_expert_params=fused,
    )


# --------------------------------------------------------------------------- chat encoding


def encode_chat(
    tokenizer: Any,
    messages: Sequence[dict[str, str]],
    *,
    max_len: int | None = None,
    chat_template_kwargs: dict | None = None,
) -> tuple[dict[str, list[int]] | None, str]:
    """``input_ids`` + ``labels`` with loss on the final assistant turn only (earlier turns, including the wrong
    first answers of correction examples, are masked). Returns (None, reason) when the example cannot be used."""
    if not messages or messages[-1].get("role") != "assistant":
        return None, "no_final_assistant"
    kw = chat_template_kwargs or {}
    prompt_text = tokenizer.apply_chat_template(
        list(messages[:-1]), tokenize=False, add_generation_prompt=True, **kw
    )
    full_text = tokenizer.apply_chat_template(list(messages), tokenize=False, **kw)
    if not full_text.startswith(prompt_text):
        return None, "template_prefix_mismatch"
    prompt_ids = tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
    completion_ids = tokenizer(full_text[len(prompt_text) :], add_special_tokens=False)["input_ids"]
    if not completion_ids:
        return None, "empty_completion"
    ids = list(prompt_ids) + list(completion_ids)
    if max_len is not None and len(ids) > max_len:
        return None, "too_long"
    return {"input_ids": ids, "labels": [-100] * len(prompt_ids) + list(completion_ids)}, "ok"


# --------------------------------------------------------------------------- tiny offline model


def build_tiny_tokenizer(texts: Iterable[str], vocab_size: int = 2048) -> Any:
    """Byte-level BPE trained on the dataset itself (any text encodes; no downloads)."""
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers

    tok = Tokenizer(models.BPE())
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(
        vocab_size=vocab_size,
        special_tokens=TINY_SPECIALS,
        initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
        show_progress=False,
    )
    tok.train_from_iterator(texts, trainer=trainer)
    fast = transformers.PreTrainedTokenizerFast(
        tokenizer_object=tok,
        bos_token="<bos>",
        eos_token="<end_of_turn>",
        pad_token="<pad>",
        unk_token="<unk>",
        additional_special_tokens=["<start_of_turn>", "<eos>"],
    )
    fast.chat_template = TINY_CHAT_TEMPLATE
    return fast


def build_tiny_model(tokenizer: Any, *, seed: int = 0, **dims: Any) -> Any:
    """A randomly initialised 2-layer model (Llama, Gemma 3, Gemma 4 text or Qwen 3 layout)."""
    spec = {**TINY_DEFAULTS, **{k: v for k, v in dims.items() if v is not None}}
    arch = spec.pop("arch")
    spec.pop("vocab_size", None)
    torch.manual_seed(seed)
    h, heads = int(spec["hidden_size"]), int(spec["num_attention_heads"])
    common = {
        "vocab_size": len(tokenizer),
        "hidden_size": h,
        "intermediate_size": int(spec["intermediate_size"]),
        "num_hidden_layers": int(spec["num_hidden_layers"]),
        "num_attention_heads": heads,
        "num_key_value_heads": int(spec["num_key_value_heads"]),
        "max_position_embeddings": int(spec["max_position_embeddings"]),
        "pad_token_id": tokenizer.pad_token_id,
        "bos_token_id": tokenizer.bos_token_id,
        "eos_token_id": tokenizer.eos_token_id,
    }
    if arch == "llama":
        cfg, cls = (
            transformers.LlamaConfig(**common, tie_word_embeddings=False),
            transformers.LlamaForCausalLM,
        )
    elif arch == "qwen3":
        cfg, cls = transformers.Qwen3Config(**common, head_dim=h // heads), transformers.Qwen3ForCausalLM
    elif arch == "gemma3":
        cfg = transformers.Gemma3TextConfig(**common, head_dim=h // heads, sliding_window=256)
        cls = transformers.Gemma3ForCausalLM
    elif arch == "gemma4":
        n = int(spec["num_hidden_layers"])
        cfg = transformers.Gemma4TextConfig(
            **common,
            head_dim=h // heads,
            global_head_dim=h // heads,
            vocab_size_per_layer_input=len(tokenizer),
            hidden_size_per_layer_input=8,
            layer_types=["sliding_attention"] * (n - 1) + ["full_attention"],
            sliding_window=256,
        )
        cls = transformers.Gemma4ForCausalLM
    else:
        raise SystemExit(f"unknown tiny arch {arch!r} (llama | qwen3 | gemma3 | gemma4)")
    model = cls(cfg)
    model.generation_config = transformers.GenerationConfig(
        bos_token_id=tokenizer.bos_token_id,
        eos_token_id=tokenizer.eos_token_id,
        pad_token_id=tokenizer.pad_token_id,
        do_sample=False,
    )
    return model


# --------------------------------------------------------------------------- telemetry


def peak_memory_gb() -> float | None:
    """``torch.cuda.max_memory_allocated`` in GiB (works on ROCm builds, where torch.cuda maps to HIP)."""
    if torch.cuda.is_available():
        return round(torch.cuda.max_memory_allocated() / 2**30, 3)
    return None


def env_info() -> dict[str, Any]:
    import peft
    import trl

    info: dict[str, Any] = {
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "peft": peft.__version__,
        "trl": trl.__version__,
        "cuda_available": torch.cuda.is_available(),
        "hip": getattr(torch.version, "hip", None),
        "cuda": torch.version.cuda,
    }
    if torch.cuda.is_available():
        info["device_name"] = torch.cuda.get_device_name(0)
        info["device_total_gb"] = round(torch.cuda.get_device_properties(0).total_memory / 2**30, 1)
    return info


# --------------------------------------------------------------------------- local model as Wapsi LLM


class HFChatLLM:
    """A local HF model behind ``wapsi.ports.LLM`` (greedy decoding at temperature 0).

    No constrained decoding: unlike vLLM with ``response_format``, the JSON wrapper is up to the model; the
    drafter's ``parse_response`` also accepts plain text.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        device: str = "auto",
        dtype: str = "auto",
        max_new_tokens: int = 700,
        chat_template_kwargs: dict | None = None,
        name: str | None = None,
    ) -> None:
        self.device = resolve_device(device)
        self.tokenizer = load_tokenizer(path)
        self.model = load_model(path, dtype=resolve_dtype(dtype, self.device), device=self.device)
        self.model.eval()
        self.max_new_tokens = max_new_tokens
        self.kw = chat_template_kwargs or {}
        self.name = name or f"hf:{Path(str(path)).name}"
        self._lock = threading.Lock()

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
    ) -> str:
        text = self.tokenizer.apply_chat_template(
            [{"role": m["role"], "content": m["content"]} for m in messages],
            tokenize=False,
            add_generation_prompt=True,
            **self.kw,
        )
        enc = self.tokenizer(text, return_tensors="pt", add_special_tokens=False)
        enc = {k: v.to(self.model.device) for k, v in enc.items()}
        gen: dict[str, Any] = {
            "max_new_tokens": max(1, min(max_tokens, self.max_new_tokens)),
            "do_sample": temperature > 0,
            "pad_token_id": self.tokenizer.pad_token_id,
        }
        if temperature > 0:
            gen["temperature"] = temperature
        with self._lock, torch.inference_mode():
            out = self.model.generate(**enc, **gen)
        return self.tokenizer.decode(out[0, enc["input_ids"].shape[1] :], skip_special_tokens=True)
