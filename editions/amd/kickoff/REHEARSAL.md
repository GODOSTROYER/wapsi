# CPU rehearsal record (before the event; not GPU results)

`editions/amd/kickoff/run_all.sh --rehearsal` ran every stage end to end on a Windows laptop (CPU only, Git Bash,
uv venv) on **2026-10-07**, at commit `13ee458` plus this kickoff work. Inputs were four tiny synthetic worlds, a tiny
random 2-layer model and stub LLMs behind the OpenAI-compatible mock endpoint. It proves the plumbing (commands,
stage records, markers, resume, background servers, evidence bundle, the pitch guard); it measures nothing about a
GPU, so no number from it is reported anywhere.

| # | Stage | Result | Wall time on the laptop |
|---|---|---|---|
| 1 | env | ok | 199 s |
| 2 | models | ok (nothing to download) | 1 s |
| 3 | gnn | ok (2 CPU epochs + 1 AMLworld-format epoch, held-out and harness evaluation) | 165 s |
| 4 | lora-data | ok (the first attempt stopped on a Windows console encoding error; fixed with `PYTHONUTF8=1`, then the run resumed from this stage, skipping 1 to 3) | 33 s |
| 5 | lora-train | ok (4 steps) | 347 s |
| 6 | merge | ok (logit check passed) | 201 s |
| 7 | lora-eval | ok | 209 s |
| 8 | serve | ok (mock endpoints + GNN scoring service, both smoke tests) | 67 s |
| 9 | bench | ok (llm_bench x 3, e2e_bench at concurrency 1 and 4, report) | 46 s |
| 10 | ab | ok | 13 s |
| 11 | evidence | ok: `evidence.json` labelled `cpu-rehearsal`, `gpu_results_valid: false`; `fill_pitch.py` refused it (exit 3) | 4 s |

The bundle itself stays local (`editions/amd/evidence/cpu-rehearsal/`, gitignored). On the MI300X the same stages run
with the real models; their timings are measured on the day.
