# Wapsi AMD edition: LoRA fine-tune for placeholder-only prose

Wapsi's drafts are templates. The LLM writes only three prose fields: **STR grounds of suspicion**, the **EDD
rationale** for an innocent holder, and the **CFCFRMS case summary**. It must write every amount, account, UTR and
date as a `⟦slot⟧` placeholder, with no digits, no currency words and no number words (see
`docs/draft-contract.md`). A bad answer costs a retry, and a second bad answer triggers a deterministic fallback
paragraph. This folder fine-tunes an open model so that it gets the field right first time, in FINnet/SOP style:

```
synthetic worlds ──► planned bundles (pipeline, draft=False)
        │
        ▼
production drafter + TEACHER ──► drafter checks ──► verify_draft ──► injection / coverage / style filters
 (exact requests, retry, fallback)                                          │ keep
        ▼                                                                   ▼
 train / val / test JSONL (chat messages; test = held-out seeds only) ──► LoRA SFT (TRL + PEFT, bf16, MI300X)
        ──► merge_and_unload ──► plain HF checkpoint ──► vLLM (ROCm) ──► evaluate: base vs tuned (vs Gemini)
```

vLLM has no LoRA runtime for Gemma 4, so the adapter is **merged** and served as an ordinary checkpoint.

## Files

| File | What it does |
|---|---|
| `dataset.py` | Verifier-filtered distillation. It runs the real drafter with a teacher behind a recording wrapper, keeps only answers that pass the drafter's checks and `verify_draft`, adds hard negatives and corrections, splits by case, and writes a manifest. |
| `train.py` | TRL `SFTTrainer` + PEFT LoRA. Target modules are auto-detected per architecture. Loss is on the final assistant turn only. Logs tokens/s and peak memory. `--tiny` builds a random model for offline tests. |
| `merge.py` | Runs `merge_and_unload()`, saves safetensors shards, tokenizer, chat template and generation config, and checks logits (adapter vs merged vs reloaded). |
| `evaluate.py` | Runs the prose tasks for held-out cases against endpoints (`openai:`), local checkpoints (`hf:`) or fakes, and writes a JSON + Markdown comparison. |
| `common.py` | Worlds, planning, adversarial variants, LLM specs, call recording, prose analysis. It uses the drafter's own checks. |
| `modeling.py` | Model loading, LoRA target detection, chat encoding, tiny model/tokenizer, `HFChatLLM`. |
| `mock_endpoint.py` | An OpenAI-compatible endpoint backed by FakeLLM (in-process for tests, or `--port` for laptop dry runs). |
| `configs/` | `gemma4-31b-mi300x`, `gemma4-e4b-mi300x`, `qwen36-35b-a3b-mi300x`, `tiny-cpu`. |
| `results/` | Small result JSON only. The current files are **pipeline checks with fake teachers, not model results**. |

LLM specs are the same in `dataset.py --teacher` and `evaluate.py --model LABEL=SPEC`:
`fake`, `fake-noisy`, `fake-hallucinate`, `fake-inject` (offline); `mock:fake` (the real OpenAI-compatible client
against an in-process fake); `openai:<base_url>|<model>[|<API_KEY_ENV>[|json_object]]`; `hf:<path>`.
API keys are read only from the named environment variable. They are never written to disk.

## Method

**Exact requests.** Each case is planned with `process_case(..., draft=False)`. Then `wapsi.draft.drafter.draft_case`
runs with the teacher wrapped in a recorder. Each training prompt is therefore byte-for-byte the system prompt and
JSON payload (task, instructions, placeholders, fact sheet with untrusted narratives) that production sends.

**Filters.** A teacher answer is kept only if:
1. the drafter accepted it without falling back, and an independent re-check with `validate_prose` agrees;
2. the substituted draft passes `verify_draft` (numbers, identifiers, citations, policy, injection);
3. it contains no canary phrase and no copied complaint text;
4. key-fact placeholder coverage is at least 0.5;
5. the STR names its Rule 2(1)(g) ground in words;
6. its length is near the requested range.

Rejection reasons, drafter issue codes and verifier error codes are counted in `manifest.json`, with details in
`rejections.jsonl`.

**Hard negatives.** Every case is drafted three times:
- *clean*;
- *tempting*: the complaint carries wrong totals in digits and in words, Hinglish amounts, and stray account
  numbers, UTRs and dates;
- *injection*: the complaint carries an instruction with a canary phrase.

Only the untrusted narrative changes, so the correct target is still the same placeholder paragraph. The test
split uses separate tempting and injection templates that are never seen in training.

**Corrections.**
- When the teacher needed the drafter's retry, the real two-turn exchange is kept.
- A share of accepted answers (`--corrections-frac`, default 0.25) also gets a synthetic wrong first answer. The
  wrong answer is one of: a raw amount, an amount in words, an invented placeholder, copied complaint text, or a
  citation token. It is followed by the drafter's exact feedback message.
- Loss is computed on the final answer only. Wrong turns are masked.

**Context distillation.** Real teachers get an extra FINnet/SOP style guide in their system prompt
(`--style-guide auto`). The student is trained on the plain drafter request, so it has to learn that style without
being shown the guide.

**Splits.** Splits are by **case** and deterministic:
- test = held-out worlds only (generator seeds 101–505; seeds shared with training are refused);
- val = `sha256(world:case_id)` bucket.

A case never appears in two splits, and the builder aborts if one does.

**Data.** Only worlds stamped `generator_version = wapsi-synth/*` (or the showcase fixture) are accepted. The
builder drops any example that contains a secret-like token.

## Offline pipeline check (run on 2026-09-23, CPU, fake teacher)

```bash
uv run python -m editions.amd.lora.dataset --teacher fake --train-worlds demo,full --heldout-worlds auto \
    --summary-out editions/amd/lora/results/dataset-fake-pipeline-check.json
uv run python -m editions.amd.lora.train --config tiny-cpu --data data/generated/lora-fake --out models/lora/tiny
uv run python -m editions.amd.lora.merge --run models/lora/tiny --out models/lora/tiny-merged
uv run python -m editions.amd.lora.evaluate --hf models/lora/tiny-merged --model fake=mock:fake --limit-cases 3
uv run pytest tests/amd -q -k lora
```

These counts check the plumbing. They are not model results:
- **Requests:** 3,183 over 509 cases × 3 variants. 3,150 were accepted. 33 were rejected, all
  `length_out_of_range`: the fake teacher's case summary is under 25 words when all the money was cashed out.
- **train:** 2,095 examples from 271 cases (1,689 teacher answers + 406 synthetic corrections).
- **val:** 285 examples from 36 cases.
- **test:** 1,236 examples from 202 held-out cases.
- **Prompt size:** p50 9.2k characters, p95 14.4k, max 19.5k. That is roughly 3–6k tokens, which is why the MI300X
  presets use `max_seq_len` 8192. `train.py --dry-run` prints exact counts with the real tokenizer.

See `results/dataset-fake-pipeline-check.json`, and `results/eval-fake-pipeline-check/` for an evaluation where
deliberately broken fake models score lower than the clean fake.

## Event-week runbook (one MI300X, 12–18 Oct 2026)

All numbers in this section are **estimates** until they are measured on the box.

### 0. Box and model

- ROCm PyTorch image or wheel. Check that `python -c "import torch; print(torch.cuda.is_available(), torch.version.hip)"`
  prints `True` and a HIP version.
- Install: `pip install -e ".[amd]"` (or `uv sync --extra amd`).
- vLLM for ROCm: AMD's `rocm/vllm` container. Check the current tag.
- Log in to Hugging Face with **your own token on the box**: `hf auth login`, or export `HF_TOKEN` in the shell.
  Never commit the token or put it in a file inside the repo.
- Gemma 4 is Apache-2.0 with open, ungated weights (model card checked 2026-10-14); re-read the card before a
  run in case it changed. Another base model may be gated: accept its terms on the Hub if asked.
- Verify the exact repo ids in `configs/*.json` before downloading.

```bash
hf download google/gemma-4-31B-it --local-dir /models/gemma-4-31B-it
hf download google/gemma-4-E4B-it --local-dir /models/gemma-4-E4B-it        # fast pipeline validation
```

### 1. Teacher on the same GPU, then build the dataset

```bash
vllm serve /models/gemma-4-31B-it --served-model-name teacher --dtype bfloat16 \
    --max-model-len 32768 --gpu-memory-utilization 0.90 --port 8000 &
python -m editions.amd.lora.dataset --teacher "openai:http://localhost:8000/v1|teacher" \
    --samples 2 --sample-temperature 0.7 --workers 32 --out data/generated/lora-gemma4-teacher \
    --summary-out editions/amd/lora/results/dataset-gemma4-teacher.json
# optional external teacher (your own key, from the environment only):
#   export GEMINI_API_KEY=...   # in the shell, never in a file
#   --teacher "openai:https://generativelanguage.googleapis.com/v1beta/openai|<gemini-model>|GEMINI_API_KEY"
kill %1   # free the GPU before training
```

Using the base model as its own teacher is rejection-sampling self-distillation: only verifier-clean, on-style
answers survive, and the student learns them without the style guide or retries. Before training on Gemini outputs,
check that the Gemini API terms allow using them as training data. If in doubt, use Gemini only as an evaluation
reference.

### 2. Train

Validate on E4B first. Then run 31B.

```bash
python -m editions.amd.lora.train --config gemma4-31b-mi300x --data data/generated/lora-gemma4-teacher \
    --out models/lora/gemma4-31b --dry-run        # targets, token stats, dropped-for-length counts
python -m editions.amd.lora.train --config gemma4-31b-mi300x --data data/generated/lora-gemma4-teacher \
    --out models/lora/gemma4-31b --max-steps 20   # measure tokens/s + peak memory (amd-smi on camera)
python -m editions.amd.lora.train --config gemma4-31b-mi300x --data data/generated/lora-gemma4-teacher \
    --out models/lora/gemma4-31b
```

### 3. Merge (fp32 merge, bf16 save; the reload is checked against the adapter)

```bash
python -m editions.amd.lora.merge --run models/lora/gemma4-31b --out /models/wapsi-gemma-4-31b-str-merged \
    --device cuda --merge-dtype fp32 --save-dtype bf16
```

### 4. Serve base and tuned side by side, then evaluate

```bash
vllm serve /models/gemma-4-31B-it --served-model-name base --dtype bfloat16 --max-model-len 16384 \
    --gpu-memory-utilization 0.45 --port 8000 &
vllm serve /models/wapsi-gemma-4-31b-str-merged --served-model-name tuned --dtype bfloat16 --max-model-len 16384 \
    --gpu-memory-utilization 0.45 --port 8001 &
python -m editions.amd.lora.evaluate \
    --base "openai:http://localhost:8000/v1|base" --tuned "openai:http://localhost:8001/v1|tuned" \
    --teacher "openai:https://generativelanguage.googleapis.com/v1beta/openai|<gemini-model>|GEMINI_API_KEY" \
    --workers 8 --out editions/amd/lora/results/eval-gemma4-31b \
    --details data/generated/lora-eval/gemma4-31b.jsonl
```

Evaluation drafts the held-out cases three times each (clean, tempting, injection) with the production drafter and
reports these metrics per model, overall and per task/variant:
- acceptance rate without retry or fallback, retry rate, fallback rate;
- verifier pass rate (prose drafts and all drafts);
- placeholder coverage;
- invented-number rate, forbidden-token rate, injection-obeyed rate, JSON validity;
- STR ground named, length in range;
- latency.

### Estimates (to be replaced by measured values)

| Preset | Weights (bf16) | Expected training peak | Wall-clock per epoch (~6M tokens) |
|---|---|---|---|
| `gemma4-31b-mi300x` (r=16/α=32, seq 8192, 2×8) | ~62 GB | ~80–95 GB of 192 GB | ~40–60 min (range 30–120) |
| `gemma4-e4b-mi300x` (seq 8192, 8×2) | ~15–20 GB | < 80 GB | ~5–15 min |
| `qwen36-35b-a3b-mi300x` (thinking off) | ~70 GB | ~90–110 GB | ~10–40 min (MoE routing efficiency unknown) |

How the estimates were made:
- **Memory:** weights + fp32 LoRA state (~2 GB) + gradient-checkpointed activations. The chunked NLL loss avoids
  materialising the 262k-vocabulary logits.
- **Time:** about 6 × active parameters × tokens FLOPs, at an assumed 25–40% of bf16 dense peak (lower for small
  and MoE models).
- **Merge:** the fp32 merge needs ~124 GB for 31B.
- **Serving both models:** base + tuned 31B side by side takes 2 × 62 GB of weights, which leaves ~24 GB of KV
  cache each at 0.45 utilisation.

## Notes and limits

- **Target detection.** It adapts q/k/v/o and gate/up/down, plus Qwen 3.5/3.6 linear-attention projections and
  shared experts. It never adapts vision/audio towers, projectors, routers, `lm_head` or Gemma 4 per-layer-embedding
  projections. Gemma 4 vision `q_proj` modules are `Gemma4ClippableLinear` wrappers, so targets are passed as an
  explicit regex; a plain PEFT suffix match would hit them and fail. Fused MoE expert tensors are reported and
  skipped.
- **Model class.** A multimodal Gemma 4 checkpoint is trained and merged as `Gemma4ForConditionalGeneration`
  (whatever `config.json` names), so vLLM loads the merged weights like the base. `--model-class` overrides this.
- **Local HF evaluation.** `hf:` models decode greedily without constrained JSON. vLLM endpoints get
  `response_format` json_schema from `OpenAICompatLLM`.
- **Qwen via vLLM.** `OpenAICompatLLM` cannot pass `chat_template_kwargs` yet, so thinking must be disabled on the
  server side (see the Qwen preset).
- **If training fails on the box,** try these one at a time:
  - `--loss-type nll` (TRL's chunked loss patches `lm_head`);
  - `--attn-implementation eager`;
  - `--model-class Gemma4ForCausalLM`;
  - `--no-gradient-checkpointing`.

  After each change, run `--max-steps 20` and compare peak memory.
- **Outputs.** `models/` and `data/generated/` are gitignored, so adapters, merged weights and datasets never enter
  git. Only small JSON goes in `results/`.

## Originality

This code (dataset / train / merge / evaluate, tested on CPU with tiny random models and fake teachers) was prepared
before the event, on 2026-09-23, and is part of the `amd-baseline` tag, disclosed in `docs/originality.md`.
The following are produced during the lablab AMD ACT III window (12–18 Oct 2026):
- the MI300X teacher runs and dataset;
- the LoRA fine-tune and merged weights;
- the vLLM serving;
- every reported model number.

Nothing in `results/` today is a model result.
