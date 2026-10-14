# Evidence bundles (written during 12–18 Oct 2026)

`editions/amd/kickoff/run_all.sh` writes one folder per run mode and UTC date here:

```
endpoint/<date>/          a run against an OpenAI-compatible endpoint (AMD Token Factory): no GPU on this machine
notebook/<date>/          a run in an AMD ROCm JupyterLab without Docker
<date>/                   a run on the AMD Developer Cloud MI300X droplet
cpu-rehearsal/<date>/     laptop plumbing checks: gitignored, labelled, never results
```

Inside each:

```
evidence.json      the bundle: where it ran (compute), headline numbers, their source files, stage timeline, validity
evidence.md        the same as dated tables for the README and the deck
stages/            NN-<stage>.json (UTC start/end, exit code, notes, budget) and NN-<stage>.ok markers
logs/              one log per stage, with timestamps
bench/             llm_bench*.json, e2e_c*.json (and telemetry_summary.json where there is a GPU)
golden_eval.json, ab_hallucination.json, model_plan.json, preflight.json
endpoint.json      endpoint runs: host, provider, the model's card, the limits, the probe (no key, no account ids)
amd_smi/, amd_smi_static.json, torch_info.json, gnn_*.json, lora_*.json, train_image_check.json   GPU runs
```

What a bundle may claim follows where it ran. An endpoint bundle names the endpoint host and the model id; the GPU is
the provider's, so it carries no GPU name, memory or power figure and `gpu_results_valid` is false. A notebook bundle
names the GPU as `amd-smi`, `rocm-smi` or a ROCm build of torch names it, and says MI300X only when that name is an
MI300X. A droplet bundle must have an MI300X in `amd-smi static`.

## Bundles so far

| Folder | Where it ran | What it holds |
|---|---|---|
| `notebook/2026-10-14/` | AMD AI Notebook, one AMD Instinct MI300X (SR-IOV VF), ROCm 7.14, Gemma 4 31B served by vLLM on the notebook | serving and end-to-end benchmarks, the raw-vs-bound A/B, the GNN trained and evaluated, the golden-set eval; two sessions of the same day merged, see its `SESSIONS.md` |

Nothing here before kickoff (12 Oct 15:00 UTC) is a result, and nothing from before kickoff may be added.
`collect_evidence.py` sets `results_valid` only for a run whose identity checks pass and whose every stage started
inside the event window; `fill_pitch.py` takes nothing else.
