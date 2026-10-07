# MI300X evidence (written during 12–18 Oct 2026)

`editions/amd/kickoff/run_all.sh` writes one folder per UTC date here:

```
<date>/evidence.json      the bundle (headline numbers, their source files, stage timeline, budget, validity)
<date>/evidence.md        the same as dated tables for the README and the deck
<date>/stages/            NN-<stage>.json (UTC start/end, exit code, notes, budget) and NN-<stage>.ok markers
<date>/logs/              one log per stage, with timestamps
<date>/amd_smi/           amd-smi snapshots before and after each stage; amd_smi_static.json at the top
<date>/bench/             llm_bench*.json, e2e_c*.json, telemetry_summary.json, AMD_RESULTS.md inputs
<date>/gnn_*.json, lora_*.json, ab_hallucination.json, preflight.json
```

Nothing here before kickoff (12 Oct 15:00 UTC) is a GPU result, and nothing from before kickoff may be added.
`collect_evidence.py` sets `gpu_results_valid` only for an MI300X run whose every stage started inside the event
window. CPU rehearsals write to `cpu-rehearsal/` (gitignored) and are labelled as such.
