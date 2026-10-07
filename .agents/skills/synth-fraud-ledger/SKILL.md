---
name: synth-fraud-ledger
description: Generate the synthetic Wapsi fraud world (banks, accounts, UPI/IMPS transactions, NCRP complaints, mule rings, golden cases) as Parquet with `wapsi.synth`, then prove it with the local world check (columns and types against wapsi.synth.export.SCHEMAS, row counts against META, one home bank, no orphan complaint transactions, UTC timestamps inside the world window, ground truth kept in labels_* only, loads through ParquetLedger). Use when asked to create, refresh, reseed or check demo data, or when data/generated/<world> is missing or stale.
---

# synth-fraud-ledger

Builds a deterministic synthetic world on disk and proves it before anything reads it. All data is synthetic;
never add real customer, victim or employer data.

## Inputs
- `size`: `showcase` (hand-computed fixture world, 2 cases), `demo` (default), `full` (large) or `hard`
  (evaluation world; its golden set stays next to the world, not in `data/golden/`).
- `seed`: integer, default `7`. Same seed → byte-identical Parquet. `--name` gives a held-out seed its own world
  name (e.g. `--size hard --seed 101 --name hard-101`).
- `--out`: output directory, default `data/generated/<name>`.

## Outputs
- `data/generated/<name>/*.parquet`, one file per table (`docs/data-contract.md`, schemas in
  `wapsi.synth.export.SCHEMAS`): product tables `banks, customers, accounts, transactions, invoices, gst_registry,
  external_signals, device_links, complaints, complaint_txns, meta` and ground truth `labels_accounts,
  labels_flows, labels_complaints`.
- Golden set `data/generated/<name>/golden/cases.jsonl` (+ `SUMMARY.md`), copied to `data/golden/<name>/` except
  for `hard` worlds and large `full` golden files.
- The world-check table and `N/N checks passed` line, pasted into the session.

## Steps
1. **Generate** (from the repo root):
   ```
   uv run python -m wapsi.synth --size demo --seed 7 --out data/generated/demo
   ```
   For the hand-checked world use `--size showcase --out data/generated/showcase`. Timestamps are written as
   timezone-aware **UTC** (convention: store UTC, display IST via `wapsi.timeutil`). Money is float rupees
   rounded with `wapsi.money.r2`.

2. **Check the world** (no network, reads only the Parquet files):
   ```
   uv run python .claude/skills/synth-fraud-ledger/scripts/check_world.py --world data/generated/demo
   uv run python .claude/skills/synth-fraud-ledger/scripts/check_world.py --world showcase
   ```
   It checks: every table present with the columns and types of `SCHEMAS`; `accounts` / `transactions` /
   `complaints` row counts equal META `n_accounts` / `n_transactions` / `n_complaints` and the golden set has
   META `n_cases` cases; exactly one home bank, equal to META `home_bank_id`; every `complaint_txns.txn_id` exists
   in `transactions` (orphans = 0); `transactions.ts` and `complaints.reported_at` are UTC and inside META
   `world_start .. world_end`; no `*_truth` column in a product table; the world loads through
   `wapsi.adapters.parquet_ledger.ParquetLedger` and a complaint resolves to its transactions. For `showcase` it
   also checks the anchor `TX-001` = `2026-09-10T05:32:00+00:00` (11:02 IST); if it reads 11:02 UTC, the world
   stored IST wall-clock time: regenerate it.
   Exit 1 = a check failed: report the FAIL lines verbatim and stop. Fix the generator; do not hand-edit Parquet.

3. **Smoke-test a trace** on a complaint of the new world:
   ```
   uv run python .claude/skills/money-trail-tracer/scripts/trace.py <ncrp_id> --world demo --group
   ```

## Guardrails
- Ground truth (`labels_*.parquet`, `golden/`, `data/golden/`) is read only by `$wapsi-eval`, the evaluation
  harness and training code. Product code (ledgers, pipeline, UI) never reads it.
- Never edit generated Parquet or golden files by hand; regenerate with a seed.
- Regenerating overwrites `data/generated/<name>` only. Never delete `data/golden/**` or `evals/reports/**` from
  this skill.
- `data/generated/` is gitignored; never commit generated worlds.

## How to verify this skill worked
- `wapsi.synth` prints `world=... accounts=... transactions=... complaints=... cases=...` and exits 0.
- `check_world.py` exits 0 and ends with `N/N checks passed` (showcase: 12/12; generated worlds: 11/11).
- `$money-trail-tracer` on a complaint of the world returns a non-empty trail.
- `uv run pytest tests/synth` passes.
