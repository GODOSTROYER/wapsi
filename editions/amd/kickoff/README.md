# AMD ACT III kickoff automation

One command turns whatever AMD compute you have into dated, verifiable evidence. The day-by-day plan for 14 to 18 Oct
is in [`../RUNBOOK.md`](../RUNBOOK.md); the fastest path (endpoint mode, about 15 minutes) is its first section.

```bash
editions/amd/kickoff/preflight.sh                  # detects the mode; green/amber/red table; exit 1 if anything is red
editions/amd/kickoff/run_all.sh --dry-run          # every command of the run, nothing executed
editions/amd/kickoff/run_all.sh                    # the run (resumable: done stages are skipped)
editions/amd/kickoff/run_all.sh --mode endpoint    # or notebook | droplet: choose the mode yourself
editions/amd/kickoff/run_all.sh --stage bench,ab   # repeat stages; --from serve repeats serve and everything after
editions/amd/kickoff/run_all.sh --rehearsal        # the same pipeline on a laptop CPU: tiny inputs, stub models
```

On Windows, run these from **Git Bash** (`bash editions/amd/kickoff/run_all.sh ...`). In PowerShell, `bash` can be
WSL's, which sees neither the uv venv nor the Windows paths of this checkout.

## Three modes, one evidence bundle

| | `endpoint` | `notebook` | `droplet` |
|---|---|---|---|
| Detected when | no AMD GPU, `WAPSI_LLM_BASE_URL` set | an AMD GPU (amd-smi / rocm-smi, or a ROCm torch), no Docker daemon | an AMD GPU and Docker |
| LLM | an OpenAI-compatible endpoint: AMD Token Factory, or anyone's vLLM | served here: `python -m vllm.entrypoints.openai.api_server`, or transformers if vLLM is absent | served here: vLLM-ROCm containers (compose) |
| GNN, LoRA | not run (recorded as skipped) | plain python on the ROCm torch | the ROCm training image |
| Evidence folder | `evidence/endpoint/<UTC date>/` | `evidence/notebook/<UTC date>/` | `evidence/<UTC date>/` |
| Hardware identity | endpoint host + model id (`endpoint.json`); the GPU is the provider's | the GPU as amd-smi / rocm-smi / torch names it | `amd-smi static`: MI300X |
| Cost | `usage.cost` as the API reports it; the GPU-hour cost stays empty | GPU-hour cost only if `WAPSI_GPU_HOUR_USD` is set | $ per case at `WAPSI_GPU_HOUR_USD` |

| Stage | Endpoint | Notebook | Droplet | CPU rehearsal (plumbing only) |
|---|---|---|---|---|
| `env` | preflight, demo world, docs index, `endpoint.py probe` (host, provider, model card, limits, one completion) | preflight, GPU identity (`amd_smi_static.json`, `torch_info.json`), `uv.lock` pins with `pip --no-deps` on the ROCm torch, `train_image_check.py`, worlds, docs index | preflight, `amd-smi static`, `uv sync`, ~120 worlds, docs index, pinned image digests, ROCm training image + `train_image_check.py` | preflight, four tiny worlds, a tiny AMLworld-format CSV |
| `models` | `model_plan.py` from `GET /models` | `model_plan.py` by GPU memory, weights via `huggingface_hub` | the plan (recorded), Gemma 4 31B + E4B into the HF cache | nothing (random tiny model) |
| `gnn` | skipped | GraphSAGE on ROCm, no-external-signals variant, held-out + hard-world eval | the same, in the training image (+ AMLworld pre-training if the CSV is there) | 2 CPU epochs |
| `lora-data` … `lora-eval` | skipped | base model as teacher, verifier-filtered dataset, LoRA, merge, base vs tuned in-process (skipped by the plan when the GPU is too small) | the same with vLLM-ROCm containers | stub teacher, tiny-cpu preset |
| `serve` | skipped (the provider serves) | drafting model (+ judge with vLLM) + GNN service as background processes | compose: merged model + verifier + GNN + app | mock endpoints + the real GNN service |
| `eval` | `golden_eval.py --endpoint`: the golden set with the real model, plus the LLM-only baseline | `golden_eval.py` against the local server | the same | FakeLLM |
| `bench` | `llm_bench` at 1 / 4 / 8 under `--rpm`, real drafting prompts, `e2e_bench --endpoint` | telemetry, `llm_bench`, `e2e_bench` | telemetry, `llm_bench` at 1 / 4 / 16 / 64, verifier bench, `e2e_bench` at 1 / 8 / 16, `AMD_RESULTS.md` | the same against stubs |
| `ab` | raw free-form STR vs Wapsi-bound drafts, same model, same cases; like with like: raw STRs vs the bound arm's STRs | the same | the same | stub LLMs that always hallucinate |
| `evidence` | `evidence.json` + `evidence.md`, `fill_pitch.py` diff | the same | the same | the same, labelled CPU rehearsal; `fill_pitch.py` must refuse it |

## The pieces

| File | What it does |
|---|---|
| `preflight.sh` | Detects the mode (`--detect`) and checks it. Endpoint: base URL, key present (never shown), `GET /models` serves the model, JSON mode, `GET /usage` limits. Notebook: the notebook's python and its ROCm torch, vLLM importable, disk. Droplet: amd-smi, Docker and Compose, the scratch disk, `HF_TOKEN`, model access. Every mode: python, uv, the `amd-baseline` tag, the event window. |
| `run_all.sh` | The twelve stages. Logs, UTC stage records and done-markers under the evidence folder; amd-smi snapshots before and after each stage where there is a GPU; the event-window guard; the droplet budget guard. A GPU box sets a provider endpoint in the shell aside (the model is served locally there). |
| `endpoint.py` | The metered client for a shared endpoint: at most `WAPSI_LLM_RPM` starts a minute and `WAPSI_LLM_MAX_CONCURRENCY` in flight, HTTP 429 waited out and retried, `json_object` instead of a strict schema, one system message first, a disk cache for temperature-0 requests, latency / tokens / `usage.cost` per call. `probe` identifies the endpoint for the bundle; `check` is preflight's rows; `wait-model [--call]` polls until a model is served (and answers). |
| `model_plan.py` | Drafting model and judge by environment: Gemma 4 first, a documented fallback list, a judge that is a different model. On an endpoint it reads `GET /models`; on a GPU box, the GPU memory. A named model is never swapped silently. |
| `golden_eval.py` | The golden-set harness with the model under test (numeric accuracy, citation validity, verifier pass rate, prose fallbacks, injection leaks, the LLM-only baseline). Roles and holds come from the scorer and the rules, never from the LLM. |
| `ab_hallucination.py` | The A/B: the same model writing a free-form STR vs writing inside Wapsi's slots and verifier, scored by one auditor (wrong amounts, invented accounts, unknown or repealed citations) on the cases both arms completed. |
| `collect_evidence.py` | One `evidence.json` + `evidence.md` from the stage outputs. `compute` says where it ran; `results_valid` is true only when the mode's identity checks pass and every stage started inside the event window; `gpu_results_valid` only for a run with a GPU of its own. |
| `fill_pitch.py` | Fills the pitch's open slots from a bundle (dry-run diff by default). The pitch writes an unmeasured figure `TBD(slot_id)` and lists the id in the open-slots table of `pitch/NUMBERS.md`; this script replaces each `TBD(slot_id)` it has a value for, adds the rows that back it to a generated section of `NUMBERS.md`, and deletes the slot's row from the open-slots table. Words follow the run: `{where}` is the bundle's place; a slot the pitch words as an MI300X or a Gemma 4 figure is filled only by a run that is one. `--slots` lists every slot: the ones the pitch uses, the optional ones a bundle also carries (`ep_*`, `gpu_*`), and the ones that are filled by hand from their own results files. Refuses rehearsal or invalid evidence. |
| `stagelog.py` | Stage records, the event window, the budget, amd-smi snapshots, `detect-mode`, `torch-info`. Standard library only. |
| `wapsi_amd_kickoff.ipynb`, `make_notebook.py` | The notebook for a ROCm JupyterLab: each cell calls the scripts above. Generated; a test keeps the committed file equal to the generator's output. |
| `train_image_check.py`, `Dockerfile.train` | The training libraries on the ROCm torch: imports, declared torch ranges, one LoRA step on the GPU. |
| `rehearsal.py`, `REHEARSAL.md` | Tiny inputs for the CPU rehearsal, and the record of the rehearsal before the event. |
| `endpoint.env.example` | The names of the endpoint variables, for a private file outside the repository. |

Measurements (`golden_eval`, `llm_bench`, `e2e_bench`, the A/B) never stop an unattended run: a failed request or
case is recorded in the result file and in the stage notes (`measure_failures`), the stage goes on, and `evidence.md`
lists it under "Warnings"; a level or run with failures, HTTP 429s or pacing is never a headline number. The CPU
rehearsal stays strict.

Every stage writes `stages/NN-<stage>.json` (UTC start and end, exit code, notes, the budget at that moment) and a
log under `logs/`. `collect_evidence.py` reads only the files in the evidence folder; `fill_pitch.py` writes only
numbers `collect_evidence.py` read from them, and only from a valid bundle.

Secrets: `WAPSI_LLM_API_KEY` (or `RADEON_API_KEY`), `HF_TOKEN`, `VLLM_API_KEY`. They are read from the environment,
sent only as an `Authorization` header (or passed to a container by name), redacted in every printed command, and
never written into a report, a cache file, a log or the notebook.
