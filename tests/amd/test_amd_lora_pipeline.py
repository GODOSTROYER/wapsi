"""End to end on CPU with a tiny random model: dataset -> LoRA train -> merge -> evaluate (offline)."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("torch")
pytest.importorskip("trl")
pytest.importorskip("peft")

from editions.amd.lora import common, dataset, evaluate, merge, train  # noqa: E402


@pytest.fixture(scope="module")
def work():
    # private temp dir: the shared --basetemp can be wiped by other pytest sessions running in parallel
    d = Path(tempfile.mkdtemp(prefix="wapsi-lora-e2e-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def heldout(work):
    return common.make_mini_world(work / "heldout-e2e", name="heldout-e2e", seed=5151)


@pytest.fixture(scope="module")
def data_dir(work, heldout):
    out = work / "ds"
    assert (
        dataset.main(
            [
                "--teacher",
                "fake",
                "--train-worlds",
                common.SHOWCASE_MEMORY,
                "--heldout-worlds",
                str(heldout),
                "--limit-cases",
                "2",
                "--variants",
                "clean,injection",
                "--corrections-frac",
                "0.5",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    return out


@pytest.fixture(scope="module")
def run_dir(work, data_dir):
    out = work / "run"
    assert (
        train.main(
            [
                "--config",
                "tiny-cpu",
                "--data",
                str(data_dir),
                "--val-file",
                str(data_dir / "test.jsonl"),
                "--out",
                str(out),
                "--max-steps",
                "6",
                "--batch-size",
                "1",
                "--tiny-vocab-size",
                "8192",
                "--max-eval-examples",
                "3",
                "--eval-steps",
                "1000",
                "--logging-steps",
                "1",
            ]
        )
        == 0
    )
    return out


@pytest.fixture(scope="module")
def merged_dir(work, run_dir):
    out = work / "merged"
    assert (
        merge.main(
            ["--run", str(run_dir), "--out", str(out), "--check-prompts", "2", "--check-max-tokens", "256"]
        )
        == 0
    )
    return out


def test_tiny_lora_training_reduces_loss(run_dir):
    metrics = json.loads((run_dir / "train_metrics.json").read_text(encoding="utf-8"))
    assert metrics["global_steps"] == 6
    assert metrics["eval_loss_after"] < metrics["eval_loss_before"]
    assert metrics["train_loss_last"] < metrics["train_loss_first"]
    assert metrics["tokens_per_s"]["count"] > 0
    assert metrics["trainable_params"] < metrics["total_params"]
    cfg = json.loads((run_dir / "train_config.json").read_text(encoding="utf-8"))
    assert cfg["lora"]["matched_by_leaf"].keys() >= {"q_proj", "v_proj", "down_proj"}
    assert cfg["loss_masking"].startswith("final assistant turn")
    assert (run_dir / "adapter" / "adapter_config.json").exists()
    assert (run_dir / "trainer_state.json").exists()
    assert (run_dir / "tiny-base" / "config.json").exists()


def test_merge_matches_adapter_outputs(merged_dir):
    rep = json.loads((merged_dir / "merge_report.json").read_text(encoding="utf-8"))
    assert rep["passed"] is True
    assert rep["adapter_vs_merged"]["max_abs_diff"] <= 1e-3
    assert rep["adapter_vs_reloaded"]["top1_agreement"] == 1.0
    assert rep["adapter_effect_vs_base"]["max_abs_diff"] > 0  # the adapter actually changed the model
    files = set(rep["files"])
    assert {"config.json", "generation_config.json", "tokenizer_config.json"} <= files
    assert any(f.endswith(".safetensors") for f in files)
    assert not any(f.startswith("adapter_") for f in files)  # plain checkpoint for vLLM, no PEFT files


def test_evaluate_tiny_merged_model_and_mock_endpoint(work, merged_dir, heldout):
    out = work / "eval"
    assert (
        evaluate.main(
            [
                "--hf",
                str(merged_dir),
                "--model",
                "fake=mock:fake",
                "--heldout-worlds",
                str(heldout),
                "--limit-cases",
                "1",
                "--variants",
                "clean,injection",
                "--max-new-tokens",
                "12",
                "--device",
                "cpu",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    rep = json.loads((out / "eval.json").read_text(encoding="utf-8"))
    assert (out / "eval.md").read_text(encoding="utf-8").startswith("# Wapsi prose fine-tune")
    assert set(rep["models"]) == {"fake", f"hf-{merged_dir.name}"}
    for label, m in rep["models"].items():
        ov = m["overall"]
        for key in evaluate.HEADLINE:
            assert key in ov, (label, key)
        assert ov["requests"] > 0 and ov["calls"] >= ov["requests"]
        assert 0.0 <= ov["acceptance_rate"] <= 1.0 and 0.0 <= ov["fallback_rate"] <= 1.0
        assert ov["all_drafts_verifier_pass_rate"] == 1.0  # fallbacks keep every draft verifiable
        assert set(m["by_variant"]) == {"clean", "injection"}
    fake = rep["models"]["fake"]["overall"]
    assert fake["acceptance_rate"] == 1.0 and fake["placeholder_coverage"] == 1.0
    assert fake["invented_number_rate"] == 0.0 and fake["injection_obeyed_rate"] == 0.0
    assert fake["json_valid_rate"] == 1.0
