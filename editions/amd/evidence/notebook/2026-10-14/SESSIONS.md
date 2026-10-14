# Two GPU sessions behind this folder (14 October 2026)

**Where it ran.** AMD AI Notebooks (notebooks.amd.com): an AMD ROCm JupyterLab without Docker. Each session had one
whole AMD Instinct MI300X, exposed to the notebook as a single SR-IOV virtual function (`amd-smi` names it "AMD
Instinct MI300X VF", 191.7 GB). The two sessions got **different MI300X GPUs** on different hosts:

| Session | `asic_serial` | VBIOS part number / version | Host boot time (stage records) | Hardware records |
|---|---|---|---|---|
| 4 (golden set) | `0xD14074DBA91C454A` | `113-M3000108-103` / `00159017` | 2026-10-02T14:42:31Z | `session4/amd_smi_static.json`, `session4/torch_info.json` |
| 5 (everything else) | `0x8816642CF4E8921A` | `113-M3000100-103` / `00123529` | 2026-10-02T14:47:52Z | `amd_smi_static.json`, `torch_info.json` |

Both report the same model name, ROCm 7.14.1 and torch 2.11.0+rocm7.14.1 (HIP 7.14.60850). Gemma 4 31B
(`google/gemma-4-31B-it`, served as `wapsi-llm`) was served on the notebook itself by the image's vLLM build
(0.23.1.dev, `python -m vllm.entrypoints.openai.api_server`, port 8000). This is not the AMD Developer Cloud droplet:
nothing here ran in Docker Compose, and no droplet figure comes from this folder. Pitch wording: "measured on an AMD
Instinct MI300X (AMD AI Notebook, ROCm 7.14, vLLM)".

## Which session produced which file

| Stage | Session | Ran (UTC) | Commit | Files |
|---|---|---|---|---|
| 1 env, 2 models, 8 serve | 5 | 14:29:54 to 14:35:50 | `fc20109` | `stages/01-*`, `02-*`, `08-*`, `amd_smi_static.json`, `torch_info.json`, `train_image_check.json`, `model_plan.json` |
| 10 bench | 5 | 14:35:52 to 14:51:10 | `fc20109` | `bench/` (llm_bench, llm_bench_wapsi_prompts, e2e_c1 / c8 / c16, telemetry), `AMD_RESULTS.md` |
| 11 ab | 5 | 14:51:12 to 14:54:42 | `fc20109` | `ab_hallucination.json` / `.md` |
| 3 gnn | 5 | 14:57:18 to 15:23:03 | `fc20109` | `gnn_metrics.json`, `gnn_noext_metrics.json`, `gnn_eval.json`, `gnn_hard_eval.json`, `hard-20261014T152303Z.*` |
| 12 evidence | 5 | 15:23:16 | `fc20109` | `evidence.json` / `.md` (rebuilt after the merge, see below) |
| 9 eval (golden set) | **4** | 13:34:59 to 13:57:40 | `483b111` | `golden_eval.json` / `.md`, `stages/09-eval.json`, `amd_smi/09-eval-*.json` |
| 1 env, 2 models, 8 serve (the eval's GPU and setup) | **4** | 13:23:33 to 13:34:58 | `483b111` | `session4/`: `amd_smi_static.json`, `torch_info.json`, `train_image_check.json`, `model_plan.json`, `stages/01-env.json`, `02-models.json`, `08-serve.json`, `amd_smi/01-*`, `02-*`, `08-*` |
| Whisper, by hand | 5 | 15:25 | `fc20109` | `editions/amd/bench/results/voice-mi300x-notebook-20261014/` |

Session 4 ran 13:22 to 14:25 UTC, session 5 14:29 to 15:23 UTC, on the same AMD AI Notebook service with the same
model and the same served profile (`gemma4-base`), but on two different MI300X GPUs (table above). `483b111` and
`fc20109` differ only in `editions/amd/kickoff/session_sprint.sh` (the judge is dropped before serving); the
application, the harness and the benchmarks are the same code. Session 5
did not run the golden-set eval, so its eval files were copied from session 4's pack into this folder, unchanged, and
`collect_evidence.py --mode notebook` rebuilt `evidence.json` over the merged folder (results_valid true: every
stage started inside the event window on an AMD GPU). Session 5's records of stages 1, 2 and 8 overwrote session
4's in the top-level folder, so session 4's are kept unchanged in `session4/` (`collect_evidence.py` reads only the
top-level `stages/` and `amd_smi_static.json`, so they do not enter the bundle). The bundle's hardware section is
session 5's GPU; the golden set ran on session 4's. The bundle's "commit" line names the first stage's commit
(`fc20109`); `stages/09-eval.json` carries a `session` note that names session 4, `483b111` and both GPUs'
`asic_serial`. That note is the only text added to a stage record.

## Why two sessions

AMD AI Notebook sessions on the MI300X end after about an hour. `session_sprint.sh` therefore runs the kickoff stages
in blocks, highest value first, and packs the small result files after every block, so a session that ends keeps
what it finished. Session 4 ran env, serve and eval and lost bench and ab (next section); session 5 ran env, serve,
bench, ab and gnn, then rebuilt the bundle. The later packs of each session (`s4_*`, `s5_*`) are superseded by the
files here.

## The judge-port incident (session 4)

The model plan included an optional second-opinion judge (`google/gemma-4-E4B-it`) served by a second vLLM on port
8001, next to the drafting model on 8000. On one GPU the main engine takes port 8001 for its own distributed init when
8000 is its API port, so the judge never came up. The serve stage, which waits for every planned server, was stopped
(status failed, exit 143) with the drafting model up and answering. The eval stage then ran against the drafting
model and completed (`golden_eval.json`: every prose answer and every raw-model answer came from `vllm-rocm:wapsi-llm`).
The bench and ab stages waited for the judge again and were stopped before they measured anything; their partial
files are not used. Commit `fc20109` blanks the judge in the model plan before serving (`no_judge` in
`session_sprint.sh`), so session 5 served the drafting model alone and its bench and ab completed. `model_plan.json`
still names the judge because the plan is written before it is dropped; no judge was served, and screening was
rules-only (`bench/e2e_*.json`: `guard: rules`).

## The transformers 5.5 overlay for vLLM

The notebook image's vLLM (0.23.1.dev) was built against the transformers release that introduced Gemma 4 (5.5.x).
The env stage installs the project's pinned stack (transformers 5.17.0, `train_image_check.json`), and with it vLLM
refused Gemma 4's per-layer config ("head_dim is a per-layer attribute"); relaxing that check made it build the
full-attention layers with the wrong head size (512 instead of 256) and fail while loading the weights. Commit
`483b111` gives vLLM its own transformers 5.5 and tokenizers 0.22 in an overlay directory on `PYTHONPATH`
(`vllm_overlay` in `session_sprint.sh`); everything else keeps the pinned versions. Both sessions served Gemma 4 this
way.

## Other notes

- **Budget line.** The stage records carry a budget counted from the notebook host's boot time (2026-10-02), which
  is the shared host's, not the session's, so their dollar total is not what the run cost. `evidence.md` no longer
  prints it, and `stagelog.py` now starts a notebook budget at the first `run_all.sh` run of the folder. Each
  session lasted about an hour (times above).
- **`AMD_RESULTS.md`** is `editions/amd/bench/report.py` over `bench/`, written on the notebook. Its header said the
  runs were "on the droplet"; that one sentence was reworded to the generator's corrected text ("the host named
  above"). No number in it was changed.
- **The GNN evaluations score roles only.** `gnn_hard_eval.json` and `hard-20261014T152303Z.*` were run with the
  offline stub for prose (`llm: fake-llm`), so their prose fields measure nothing about a model; the GNN was trained
  and scored on the MI300X (`gnn_metrics.json`: `device`). The serve stage's "gnn_service: not served" is because
  the GNN stage ran after serving; the end-to-end bench used the default heuristic scorer.
- **Two prose-fallback rates, two denominators.** The golden set's `llm_prose_fallback_rate` 0.0332
  (`golden_eval.json`) is over all 301 drafts, most of which have no model-written prose: 10 replaced and 73 kept
  from the model (`prose_by_model`), so 10 of the 83 drafts that ask the model for prose. The end-to-end bench's
  `prose_fallback_rate` is over those drafts only: 9 of 83 at concurrency 1 and 16 (0.1084), 10 of 83
  at concurrency 8 (0.1205) (`bench/e2e_c*.json`, `metrics.llm_drafts` / `prose_fallbacks`). Every replaced field
  became the fixed paragraph and every draft still passed the verifier. `evidence.md` now names each denominator.
- **The GNN's validation AUC of 1.** `gnn_metrics.json` reports validation and held-out (`heldout-404`, `heldout-505`)
  ROC-AUC 1.0: those are ordinary synthetic worlds like the training worlds, easy enough to saturate. The hard
  world (64 cases, confounders built to fool the scorer, never in training) is the real test:
  mule ROC-AUC 0.9202 against the default heuristic's 0.9411, mule recall 0.7415 against 0.966, innocent-holder recall
  0.875 against 0.6875 (`gnn_hard_eval.json`), so the heuristic stays the default.
- **Cost per case** in `bench/e2e_*.json` is the MI300X list price of $1.99 per GPU-hour divided by the measured
  cases per hour (`cost.note`); the notebook's own credits are not billed per case.
