# AMD ACT III kickoff automation

One command turns a fresh MI300X droplet into dated, verifiable evidence, so the GPU hours go into results instead
of setup. The hour-by-hour plan for 12 Oct is in [`../RUNBOOK.md`](../RUNBOOK.md) ("Kickoff day").

```bash
editions/amd/kickoff/preflight.sh                 # green/amber/red table; exit 1 if anything is red
editions/amd/kickoff/run_all.sh --dry-run         # every command of the run, nothing executed
editions/amd/kickoff/run_all.sh                   # the run (resumable: done stages are skipped)
editions/amd/kickoff/run_all.sh --stage bench,ab  # repeat stages; --from serve repeats serve and everything after
editions/amd/kickoff/run_all.sh --rehearsal       # the same pipeline on a laptop CPU: tiny inputs, stub models
```

| Stage | MI300X run | CPU rehearsal (plumbing check only) |
|---|---|---|
| `env` | preflight, `amd-smi static`, `uv sync`, ~120 synthetic worlds, pinned image digests, ROCm training image | preflight, four tiny worlds, a tiny AMLworld-format CSV |
| `models` | Gemma 4 31B (base) and Gemma 4 E4B (verifier) into the HF cache | nothing (random tiny model) |
| `gnn` | GraphSAGE on ROCm (+ AMLworld pre-training if the CSV is there), no-external-signals variant, held-out + hard-world eval | 2 CPU epochs, 1 AMLworld epoch, same evaluators |
| `lora-data` | base model on vLLM-ROCm as teacher; smoke test; base serving bench; verifier-filtered dataset | stub teacher behind the OpenAI-compatible mock endpoint |
| `lora-train`, `merge`, `lora-eval` | TRL + PEFT bf16 LoRA, fp32 merge, base vs tuned on held-out cases | tiny-cpu preset, 4 steps |
| `serve` | compose: merged model + verifier + GNN + app, smoke tests | mock endpoints + the real GNN service |
| `bench` | amd-smi telemetry, `llm_bench` (1/4/16/64), verifier bench, `e2e_bench` at 1/8/16, `AMD_RESULTS.md` | the same benches against stubs, no telemetry |
| `ab` | raw free-form STR vs Wapsi-bound drafts, same model | stub LLMs that always hallucinate |
| `evidence` | `evidence.json` + `evidence.md`, `fill_pitch.py` diff | the same, labelled CPU rehearsal; `fill_pitch.py` must refuse it |

Every stage writes `editions/amd/evidence/<UTC date>/stages/NN-<stage>.json` (UTC start and end, exit code, notes, the
budget at that moment), a log under `logs/`, and amd-smi snapshots under `amd_smi/`. `collect_evidence.py` reads only
those files. `fill_pitch.py` writes only numbers that `collect_evidence.py` read from them, and only from a bundle
whose `gpu_results_valid` is true (an MI300X in `amd-smi static`, every stage inside 12 Oct 15:00 UTC to 18 Oct
14:00 UTC).

Environment the run reads: `SCRATCH`, `HF_TOKEN` (never printed), `WAPSI_INR_PER_USD` (the day's rate, for ₹ per
case), `WAPSI_DROPLET_STARTED_AT` and `WAPSI_SPENT_USD` (budget), `WAPSI_BUDGET_USD` (100), `WAPSI_GPU_HOUR_USD`
(1.99), `AMLWORLD_CSV`, `ROCM_PYTORCH_IMAGE`, and the model choice (`WAPSI_BASE_MODEL`, `WAPSI_BASE_PROFILE`,
`WAPSI_LORA_CONFIG`, `WAPSI_MERGED_NAME`; the Qwen fallback is in the RUNBOOK).
