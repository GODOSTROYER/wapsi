---
name: wapsi-eval
description: Run the Wapsi golden-set evaluation (traced-amount recall, over-hold, false freezes, innocent-holder and merchant precision/recall, STR numeric accuracy, citation validity, injection resistance, p95 time to hold plan), compare it with the previous report and the build-plan targets, and report a KPI summary in the session. Use for "eval", "KPIs", "golden set", "did we regress", "scorecard", nightly runs and before any demo or submission.
---

# wapsi-eval

Every claim in the demo and the deck is scored against the golden set. This skill runs the harness, reads
`evals/reports/latest.json` (schema: `docs/eval-report.md`) and reports targets and regressions. It never edits
code, fixtures or golden files to make a number pass.

## Inputs
- World: `demo` (default), `showcase`, or another generated world with `data/golden/<world>/cases.jsonl`.
- Mode: full (default) or `--quick` (first 10 cases, for hooks).
- LLM: `fake` (default, deterministic) or `fake-hallucinate` / `openai:<base_url>|<model>` for baselines.

## Outputs
- `evals/reports/<world>-<timestamp>.json/.md` and `evals/reports/latest.json` (written by the harness).
- A KPI table: value, target, met / MISSED, delta vs the previous report for the same world.

## Steps
1. Make sure the world and golden set exist (else use `$synth-fraud-ledger`):
   ```
   uv run python -m wapsi.synth --size demo --seed 7 --out data/generated/demo
   ```
2. Run the harness and summarise (one command):
   ```
   uv run python .claude/skills/wapsi-eval/scripts/kpi_summary.py --run --world demo
   ```
   or separately:
   ```
   uv run python -m evals.harness --world demo
   uv run python .claude/skills/wapsi-eval/scripts/kpi_summary.py
   ```
   Hallucination baseline for the deck: `uv run python -m evals.harness --world demo --llm fake-hallucinate`
   (write it to a separate folder with `--out evals/reports/baseline` so it is not compared as a regression).
3. Post the summary: paste the KPI table into the session. `--brief` prints one line (for hooks and status
   messages).
4. Compare: the script picks the second-newest `<world>-*.json` automatically; use `--previous <file>` to pin a
   baseline (e.g. a committed report such as `evals/reports/demo.json`). `--fail-on-regression` turns any worse
   metric into exit 1 (use in CI/automations).

## Targets (WAPSI_BUILD_PLAN §5)
| KPI | Target |
|---|---|
| Traced-amount recall (located / stolen) | ≥ 95% |
| Over-hold ₹ | ₹0.00 |
| False-freeze count | 0 |
| Innocent-holder precision / recall | ≥ 0.90 / ≥ 0.80 |
| Merchant (delivered, no-hold) precision | ≥ 0.95 |
| STR / draft numeric accuracy after verifier | 100% |
| Citation validity | 100% |
| Injection leaks (adversarial complaints causing a violation) | 0 |
| p95 time to hold plan | < 90 s |
Mule F1, disposition accuracy, hold exact match, LLM-only hallucination rate and cost per case are reported
without a hard target.

## How to report
- Lead with the `--brief` line, then the table, then MISSED targets and regressions with the first failures.
- For every MISSED target name the typologies involved (`per_typology`) and the failing case ids. Do not
  speculate on causes you did not check; propose the next diagnostic command instead.
- Numbers are copied from the script output only.

## Guardrails
- Never edit `src/wapsi/fixtures/showcase.py`, `data/golden/**` or the harness to change a result. If you think
  the golden set is wrong, say so and stop.
- Ground truth lives in `data/golden/` and in each generated world's `labels_*.parquet` and `golden/`; only this
  skill, the harness and training code read it, never product code (ledgers, pipeline, UI).
- The `--brief` mode never fails, so it is safe in a SessionStart hook.

## How to verify
- `kpi_summary.py` exits 0 when all targets are met, 1 when any is missed, 2 when no report exists.
- `evals/reports/latest.json` has a fresh `generated_at` after `--run`.
