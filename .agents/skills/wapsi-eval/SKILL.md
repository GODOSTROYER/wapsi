---
name: wapsi-eval
description: Run the Wapsi golden-set evaluation (traced-amount recall, over-hold, false freezes, innocent-holder and merchant precision/recall, STR numeric accuracy, citation validity, injection resistance, p95 time to hold plan), compare it with the previous report and the build-plan targets, and post a KPI summary (session, WAPSI.APP.EVAL_RUNS, optional Slack notice via the ACTIONS table). Use for "eval", "KPIs", "golden set", "did we regress", "scorecard", nightly runs and before any demo or submission.
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
- Optional: one row in `WAPSI.APP.EVAL_RUNS` (KPI page in the Streamlit app) and a `SLACK_NOTICE` action row.

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
3. Post the summary:
   - Always: paste the KPI table into the session.
   - Snowflake (KPI page): `uv run python .claude/skills/wapsi-eval/scripts/kpi_summary.py --sql` prints an
     `INSERT INTO WAPSI.APP.EVAL_RUNS ...` statement; run it with your SQL tool.
   - Slack: do **not** call Slack directly from Snowflake. Stage a notice for the drain automation:
     ```sql
     INSERT INTO WAPSI.CASEMGMT.ACTIONS (action_id, case_id, kind, payload, status, created_at)
     SELECT 'ACT-EVAL-' || TO_VARCHAR(CURRENT_TIMESTAMP(), 'YYYYMMDDHH24MISS'), 'SYSTEM', 'SLACK_NOTICE',
            OBJECT_CONSTRUCT('channel', '#wapsi-ops', 'text', '<paste the --brief line>'), 'PENDING', CURRENT_TIMESTAMP();
     ```
4. Compare: the script picks the second-newest `<world>-*.json` automatically; use `--previous <file>` to pin a
   baseline (e.g. the G1 report). `--fail-on-regression` turns any worse metric into exit 1 (use in CI/automations).

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
- Ground truth lives in `data/golden` locally and `WAPSI.ML` in Snowflake; only this skill and training read it,
  under the build role, never under app roles.
- The `--brief` mode never fails, so it is safe in a SessionStart hook.

## How to verify
- `kpi_summary.py` exits 0 when all targets are met, 1 when any is missed, 2 when no report exists.
- `evals/reports/latest.json` has a fresh `generated_at` after `--run`.
- In Snowflake: `SELECT world, generated_at, report:metrics:traced_amount_recall FROM WAPSI.APP.EVAL_RUNS ORDER BY generated_at DESC LIMIT 2;`
