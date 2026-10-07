# Paused state: adversarial review of feat/amd-kickoff-automation

Paused on 2026-10-07 by an orchestrator request. The builder's last commit was `b10721e`. The review fixes so far are
in `c1aa65d` ("WIP: paused"). **Main has not been merged yet** (main is at `ec2cb2e`).

## Verified so far
- `run_all.sh --list`, `preflight.sh --rehearsal` (0 FAIL) and `preflight.sh --offline` in GPU mode all run on this
  laptop. On a laptop the GPU-mode preflight correctly reports FAIL for the tag, the window and the GPU, and exits 1.
- `run_all.sh --dry-run --date 2026-10-12`: all 11 stages print. A planted `HF_TOKEN` value is never printed.
- The CPU rehearsal bundle (`editions/amd/evidence/cpu-rehearsal/2026-10-07/`, gitignored): `evidence.json` is
  labelled `cpu-rehearsal` with `gpu_results_valid=false`. The headline keys match the real output schemas of
  gnn train/evaluate, evals.harness, lora eval/train, llm_bench, e2e_bench and the A/B; `gpu_telemetry` summary keys
  match `collect_evidence.memory()`.
- `uv run pytest tests/amd/test_amd_kickoff.py -m "not rehearsal"`: 28 passed (after the WIP fixes).
  `uv run ruff check editions/amd/kickoff tests/amd`: clean.
- The full suite (`--ignore=tests/snowflake`) was started once. With `-x` it stopped at
  `test_shell_scripts_parse[run_all.sh]`, but that test passes on its own, so this looks like load-related
  flakiness that is not yet diagnosed. A second full run was stopped by the pause.

## Fixed in c1aa65d
1. ROCm training image: the requirements filter missed `cuda-toolkit`, `cuda-bindings` and `cuda-pathfinder`, which
   come from `torch==2.14` in uv.lock. They are multi-GB NVIDIA wheels in an AMD image. The filter is now
   `^(torch|triton|pytorch-triton|nvidia-|cuda-)`.
2. `--no-baseline-check` and `--allow-after-window` were vetoed by the env stage's preflight, which still FAILed on
   those rows. run_all now passes `--allow-no-baseline` / `--allow-after-window` to preflight, and preflight turns
   those rows into WARN.
3. `env_set` failed (awk on a missing file) when a later stage (`--stage serve`) ran on a fresh clone without
   `editions/amd/.env`. It now creates the file from `.env.example` with mode 600.

## Open review findings (not yet fixed), in priority order
1. **ab_hallucination ignores `VLLM_API_KEY`.** `OpenAICompatLLM(api_key=None)` reads only `WAPSI_LLM_API_KEY`, so
   with the RUNBOOK's `VLLM_API_KEY` set, every A/B request gets a 401. Fix: pass
   `api_key=os.environ.get("WAPSI_LLM_API_KEY") or os.environ.get("VLLM_API_KEY") or None`, and add a test.
2. **One bench error kills an unattended overnight run.** `llm_bench` exits 1 on any request error, `e2e_bench` on
   any failed case, and `ab_hallucination` on any case error. Under `set -e` the lora-data stage (base bench before
   the dataset!), the bench stage and the ab stage fail, telemetry is left running, and ab/evidence never run. A
   `measure` helper was drafted but not applied (the python patch failed on a quoting mismatch, so nothing was
   written). What it should do:
   - GPU mode: run, and on failure record `measure_failures=N` in the stage notes and continue. The rehearsal stays
     strict.
   - Use it for `llm_bench_base` in gpu_lora_data, the three llm_bench runs and three e2e_bench runs in gpu_bench,
     and the A/B in gpu_ab.
   - Make the amd-smi raw dumps in gpu_bench `|| true`.
   - Optionally, ab returns 1 only when an arm has no successful case.
3. **Later-week runs produce invalid bundles.** Running `--stage bench,ab,evidence` into a new date folder skips the
   env stage, so no `amd_smi_static.json` is written. `collect_evidence` then sets `gpu_results_valid=false` and
   `fill_pitch` refuses. Fix: in gpu_evidence, run `slog static` if the file is missing (always in a dry run). Also
   note `git_commit` in a stage other than env.
4. **fill_pitch drops rows from earlier bundles.** It regenerates the whole NUMBERS.md section from one bundle. A
   second bundle that fills only some slots therefore drops the rows of slots filled earlier, and
   `pitch/check_numbers.py` then fails on unregistered ₹/$/GB tokens still in the deck. Fix: merge with the existing
   generated rows (each row has its own Source) and keep the rows whose keys the new bundle does not refill. Add a
   two-bundle test.
5. A failed lora-eval leaves `wapsi-eval-base` holding port 8000, so `--from serve` then fails. Fix: start gpu_serve
   with `run_soft docker rm -f wapsi-eval-base wapsi-eval-tuned`.
6. preflight reports free space for `SCRATCH=/mnt/scratch` even when that path does not exist: it walks up to `/`.
   That gives a false PASS, and the models would then land on the root disk. It should FAIL when SCRATCH does not
   exist ("export SCRATCH=<mount> or mkdir it").
7. `fill_pitch` USD format shows `$0.0000` for costs under $0.00005. It should keep two significant figures (the
   decimals depend on the value; the Value column uses the same decimals).
8. The gpu_evidence hint says `py .../fill_pitch.py`. It should say `uv run python`.
9. Docs: on Windows, the rehearsal must run from Git Bash (`bash editions/amd/kickoff/run_all.sh --rehearsal`). In
   PowerShell, `bash` may resolve to WSL. The RUNBOOK and kickoff/README should say so.
10. Nice to have: compare like with like in the A/B. The raw arm writes one STR per case, while the bound arm counts
    all of its drafts (about 10 per case). Add STR-only bound counts (`DraftKind.STR`) to the summary and the md.
11. Risk, not verifiable here: `Dockerfile.train` installs uv.lock pins (transformers, peft, trl resolved against
    torch 2.14) with `--no-deps` on top of the image's torch 2.9.1. Smoke-test `import trl` in the image on day 1.
12. `wapsi.search.build` (run_soft in env) needs `data/docs/raw`, which is not in git. On a fresh droplet the index
    is not built, and e2e_bench runs without docs search. Either fetch first or note it in the RUNBOOK.

## Next commands (in order)
```bash
cd Z:/Projects/Hakathon/wapsi-wt/amd-kickoff-automation
# 1. apply open findings 1-9 (+ tests), then:
uv run pytest tests/amd/test_amd_kickoff.py -q --basetemp=.pytest_tmp_review-amd -p no:cacheprovider
uv run ruff check
git commit -am "AMD kickoff review fixes" (message ends with the Co-Authored-By line)
# 2. merge main (orchestrator instruction)
git merge main
#    conflicts: accept every deletion of editions/snowflake/**, tests/snowflake/**, tests/test_coco_hooks.py,
#    src/wapsi/adapters/{snowflake_ledger,cortex_llm}.py, pitch/coco/**; add no Snowflake/Cortex/CoCo text.
#    editions/amd/README.md: keep main's originality bullet text, re-add only this branch's kickoff/ + evidence/
#    table rows and the "kickoff/ ... --rehearsal" sentence in the "Prepared before the event" bullet.
#    pitch/NUMBERS.md: keep main's; fill_pitch.py must still find the "\n## Command outputs" anchor (check).
uv sync --extra amd --extra amd-app --extra gcp --extra ui --extra ml --extra docs
uv run pytest -q --basetemp=.pytest_tmp_review-amd -p no:cacheprovider   # full suite; re-check test_shell_scripts_parse flake
uv run ruff check
git commit   # the merge
# 3. delete this file in the final commit
```
