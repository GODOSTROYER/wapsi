"""LoRA target detection per architecture and final-turn loss masking (offline, tiny random models)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")
peft = pytest.importorskip("peft")

from editions.amd.lora import modeling  # noqa: E402

TEXTS = ['{"text": "The complaint ⟦ncrp_ids⟧ reports ⟦total_stolen⟧ stolen."}', "system user assistant"] * 20


@pytest.fixture(scope="module")
def tok():
    return modeling.build_tiny_tokenizer(TEXTS, vocab_size=400)


def _leaves(report):
    return set(report.by_leaf)


@pytest.mark.parametrize("arch", ["llama", "qwen3", "gemma3", "gemma4"])
def test_detects_attention_and_mlp_projections(tok, arch):
    model = modeling.build_tiny_model(
        tok, arch=arch, hidden_size=32, intermediate_size=64, num_hidden_layers=2
    )
    report = modeling.detect_lora_targets(model, "attn_mlp")
    assert _leaves(report) == {"q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"}
    assert len(report.modules) == 14
    assert report.family == arch
    assert not any("lm_head" in m or "per_layer" in m for m in report.modules)
    attn_only = modeling.detect_lora_targets(model, "attn")
    assert _leaves(attn_only) == {"q_proj", "k_proj", "v_proj", "o_proj"}
    for name in report.modules:
        assert re.fullmatch(report.regex, name)
    assert not re.fullmatch(report.regex, "lm_head")


def test_gemma4_multimodal_excludes_towers_and_trains(tok):
    tc = {
        "vocab_size": len(tok),
        "hidden_size": 32,
        "intermediate_size": 64,
        "num_hidden_layers": 2,
        "num_attention_heads": 2,
        "num_key_value_heads": 1,
        "head_dim": 16,
        "global_head_dim": 16,
        "vocab_size_per_layer_input": len(tok),
        "hidden_size_per_layer_input": 4,
        "layer_types": ["sliding_attention", "full_attention"],
        "pad_token_id": tok.pad_token_id,
    }
    vc = {
        "hidden_size": 16,
        "intermediate_size": 32,
        "num_hidden_layers": 1,
        "num_attention_heads": 2,
        "num_key_value_heads": 2,
        "head_dim": 8,
    }
    model = transformers.Gemma4ForConditionalGeneration(
        transformers.Gemma4Config(text_config=tc, vision_config=vc, audio_config=None)
    )
    report = modeling.detect_lora_targets(model, "attn_mlp")
    assert report.modules and all("language_model" in m for m in report.modules)
    assert report.excluded, "vision-tower projections must be reported as excluded"
    lora = peft.LoraConfig(r=4, lora_alpha=8, target_modules=report.regex, task_type="CAUSAL_LM")
    pm = peft.get_peft_model(model, lora)
    ids = torch.tensor([tok("⟦total_stolen⟧ stolen", add_special_tokens=False)["input_ids"]])
    out = pm(input_ids=ids, labels=ids)
    assert torch.isfinite(out.loss)
    out.loss.backward()
    grads = [p.grad for n, p in pm.named_parameters() if "lora_" in n and p.requires_grad]
    assert grads and all(g is not None for g in grads)


def test_qwen35_moe_hybrid_targets_attention_linear_attention_and_shared_expert():
    cfg = transformers.Qwen3_5MoeTextConfig(
        vocab_size=128,
        hidden_size=32,
        intermediate_size=64,
        moe_intermediate_size=16,
        shared_expert_intermediate_size=16,
        num_hidden_layers=2,
        num_attention_heads=2,
        num_key_value_heads=1,
        head_dim=16,
        num_experts=4,
        num_experts_per_tok=2,
        layer_types=["linear_attention", "full_attention"],
        linear_num_key_heads=2,
        linear_num_value_heads=2,
        linear_key_head_dim=8,
        linear_value_head_dim=8,
    )
    model = transformers.Qwen3_5MoeForCausalLM(cfg)
    report = modeling.detect_lora_targets(model, "attn_mlp")
    assert {"q_proj", "o_proj", "in_proj_qkv", "out_proj", "gate_proj", "down_proj"} <= _leaves(report)
    assert any("shared_expert" in m for m in report.modules)
    assert report.fused_expert_params > 0  # fused experts are reported, not adapted
    assert not any(m.endswith(".gate") for m in report.modules)


def test_loss_only_on_final_assistant_turn(tok):
    good = '{"text": "The complaint ⟦ncrp_ids⟧ reports ⟦total_stolen⟧ stolen."}'
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
        {"role": "assistant", "content": '{"text": "Rs 4,80,000 stolen."}'},
        {"role": "user", "content": "Your answer broke these rules"},
        {"role": "assistant", "content": good},
    ]
    enc, why = modeling.encode_chat(tok, messages)
    assert why == "ok"
    trained = [t for t, lab in zip(enc["input_ids"], enc["labels"], strict=True) if lab != -100]
    text = tok.decode(trained, skip_special_tokens=True)
    assert text.strip() == good
    assert "4,80,000" not in text  # the wrong first answer is masked
    first_label = next(i for i, lab in enumerate(enc["labels"]) if lab != -100)
    assert all(lab == -100 for lab in enc["labels"][:first_label])
    too_long, why = modeling.encode_chat(tok, messages, max_len=5)
    assert too_long is None and why == "too_long"
