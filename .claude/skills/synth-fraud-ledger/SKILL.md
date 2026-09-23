---
name: synth-fraud-ledger
description: Generate the synthetic Wapsi fraud world (banks, accounts, UPI/IMPS transactions, NCRP complaints, mule rings, golden cases) with `wapsi.synth` and load it into Snowflake WAPSI.RAW (ground truth into WAPSI.ML) using Snowpark write_pandas or PUT + COPY INTO, then prove the load with row-count and timestamp checks. Use when asked to create, refresh, reseed or load demo data, or when Snowflake RAW tables are empty or stale.
---

# synth-fraud-ledger

Builds a deterministic synthetic world and loads it into Snowflake. All data is synthetic; never load real
customer, victim or employer data.

## Inputs
- `size`: `showcase` (hand-computed fixture world, 2 cases), `demo` (default), or a larger size the generator supports.
- `seed`: integer, default `7`. Same seed → byte-identical Parquet.
- Snowflake connection name (from `~/.snowflake/connections.toml`), default `wapsi`.
- Target: database `WAPSI`, schema `RAW` (product data) and `ML` (ground truth `labels_*`, never granted to app roles).

## Outputs
- `data/generated/<size>/*.parquet` (one file per table, see `docs/data-contract.md`) and `data/golden/<size>/cases.jsonl`.
- Tables `WAPSI.RAW.{BANKS, CUSTOMERS, ACCOUNTS, TRANSACTIONS, INVOICES, GST_REGISTRY, EXTERNAL_SIGNALS,
  DEVICE_LINKS, COMPLAINTS, COMPLAINT_TXNS, META}` and `WAPSI.ML.{LABELS_ACCOUNTS, LABELS_FLOWS, LABELS_COMPLAINTS}`.
- A row-count table (Parquet rows vs Snowflake rows) printed at the end. Any mismatch = failure.

## Steps

1. **Generate** (from the repo root):
   ```
   uv run python -m wapsi.synth --size demo --seed 7 --out data/generated/demo
   ```
   For the hand-checked world use `--size showcase --out data/generated/showcase`.

2. **Dry-run the load plan** (no Snowflake needed; checks Parquet columns against the reference DDL):
   ```
   uv run python .claude/skills/synth-fraud-ledger/scripts/load_world.py --world data/generated/demo --dry-run
   ```
   Stop and report if it prints `COLUMN MISMATCH`. Fix the DDL or the generator; do not load partially.

3. **Create objects** (once). Run `references/raw_ddl.sql` with your SQL tool. It creates `WAPSI`, `RAW`, `ML`, the
   stage `RAW.WORLD_STAGE` and all tables. Timestamps are `TIMESTAMP_NTZ` holding **UTC** (convention: store UTC,
   display IST with `CONVERT_TIMEZONE('UTC','Asia/Kolkata', ts)`). Money is `NUMBER(18,2)` rupees.

4. **Load**, option A (preferred, Snowpark `write_pandas`):
   ```
   uv run --with "snowflake-snowpark-python[pandas]" python .claude/skills/synth-fraud-ledger/scripts/load_world.py --world data/generated/demo --connection wapsi
   ```
   The script truncates each table, converts tz-aware timestamps to naive UTC, upper-cases column names and calls
   `session.write_pandas(df, TABLE, database="WAPSI", schema=..., quote_identifiers=True, use_logical_type=True)`.
   Add `--create` to run the reference DDL first.

   Option B (pure SQL, PUT + COPY). Generate the statements, review, then run them:
   ```
   uv run python .claude/skills/synth-fraud-ledger/scripts/load_world.py --world data/generated/demo --emit-sql > %TEMP%\wapsi_load.sql
   ```
   Shape of each statement ([COPY INTO docs](https://docs.snowflake.com/en/sql-reference/sql/copy-into-table)):
   ```sql
   PUT 'file://Z:/Projects/Hakathon/wapsi/data/generated/demo/transactions.parquet' @WAPSI.RAW.WORLD_STAGE/demo/
       AUTO_COMPRESS = FALSE OVERWRITE = TRUE;
   TRUNCATE TABLE WAPSI.RAW.TRANSACTIONS;
   COPY INTO WAPSI.RAW.TRANSACTIONS
     FROM @WAPSI.RAW.WORLD_STAGE/demo/transactions.parquet
     FILE_FORMAT = (TYPE = PARQUET USE_LOGICAL_TYPE = TRUE)
     MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE
     ON_ERROR = ABORT_STATEMENT;
   ```
   `PUT` runs from SnowSQL / `snow sql`, not from a Snowsight worksheet. `snow stage copy data/generated/demo @WAPSI.RAW.WORLD_STAGE/demo/ --overwrite`
   is an alternative upload. Known doc caveat: with `MATCH_BY_COLUMN_NAME`, an empty Parquet string value can
   raise an error; if that happens use the commented transformation `COPY ... FROM (SELECT $1:col::TYPE ...)` the
   script prints under each table.

5. **Verify** (always; paste the output into the session):
   ```sql
   SELECT 'TRANSACTIONS' t, COUNT(*) n FROM WAPSI.RAW.TRANSACTIONS
   UNION ALL SELECT 'COMPLAINTS', COUNT(*) FROM WAPSI.RAW.COMPLAINTS
   UNION ALL SELECT 'ACCOUNTS', COUNT(*) FROM WAPSI.RAW.ACCOUNTS
   UNION ALL SELECT 'COMPLAINT_TXNS', COUNT(*) FROM WAPSI.RAW.COMPLAINT_TXNS;
   -- exactly one home bank, and it matches META
   SELECT COUNT_IF(is_home) AS home_banks,
          ANY_VALUE(IFF(is_home, bank_id, NULL)) AS home_bank,
          (SELECT value FROM WAPSI.RAW.META WHERE key = 'home_bank_id') AS meta_home
   FROM WAPSI.RAW.BANKS;
   -- every complaint transaction exists; orphan count must be 0
   SELECT COUNT(*) AS orphans FROM WAPSI.RAW.COMPLAINT_TXNS c
   LEFT JOIN WAPSI.RAW.TRANSACTIONS t ON t.txn_id = c.txn_id WHERE t.txn_id IS NULL;
   -- timestamps landed as UTC: range must sit inside META world_start..world_end
   SELECT MIN(ts), MAX(ts) FROM WAPSI.RAW.TRANSACTIONS;
   SELECT key, value FROM WAPSI.RAW.META WHERE key IN ('world','seed','world_start','world_end');
   ```
   For `showcase`, `TX-001` must read `2026-09-10 05:32:00` (11:02 IST). If it reads 11:02, the load stored IST
   wall-clock time: drop and reload with option A.

## Guardrails
- Ground truth (`labels_*`) goes to `WAPSI.ML` only. Never grant `ML` to `WAPSI_ANALYST`, `WAPSI_LEA_READONLY`,
  the Cortex Agent or the semantic view. The product must not read it.
- Never edit generated Parquet by hand; regenerate with a seed.
- Reloading is destructive for RAW only (TRUNCATE + load). Never truncate `CASEMGMT` tables from this skill.
- Keep warehouse `WAPSI_WH` at XSMALL; a demo load does not need more.

## How to verify this skill worked
- Script exit code 0 and the row-count table shows `OK` for every table.
- The SQL checks above return: home_banks = 1 and equal to meta_home, orphans = 0, timestamps within the world window.
- `$money-trail-tracer` on a complaint from `RAW.COMPLAINTS` returns a non-empty trail.
