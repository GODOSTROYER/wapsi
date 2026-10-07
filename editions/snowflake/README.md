# Wapsi — Snowflake edition (Snowflake CoCo CLI Hackathon, GCC edition)

Wapsi runs where the bank's data lives. The synthetic ledger sits in `WAPSI.RAW`; the portable Python core runs
unchanged inside Snowpark procedures (one reproducible zip); Cortex `AI_COMPLETE` writes prose only, never a number;
Cortex Search serves the SOP corpus with citation ids; a semantic view and a Cortex Agent answer the nodal officer; a
Streamlit in Snowflake app runs the shared case room; every audit event is sealed into a per-case hash chain. The
edition is complete in the repo and tested offline; CoCo sessions deploy it, prove it live and build the semantic
view and agent with CoCo's own skills ([`COCO_SPRINT.md`](COCO_SPRINT.md)).

## Quick start
```powershell
uv sync --all-extras
uv run pytest -q tests/snowflake                                   # offline: a SQLite-backed Snowpark fake
uv run python -m editions.snowflake.deploy --step all --dry-run   # every statement, deterministic, no connection
$env:WAPSI_SF_CONNECTION = 'VJ22713'                               # the default connection name
uv run python -m editions.snowflake.deploy --step bootstrap,load,package,procs
```
`deploy.py` reads the named connection from `~/.snowflake/connections.toml` (`--connection` overrides
`WAPSI_SF_CONNECTION`) and never reads or prints credentials. Every step is safe to re-run; case history in
`CASEMGMT` is never dropped (new columns arrive with `ADD COLUMN IF NOT EXISTS`).

| Step | Does |
|---|---|
| `bootstrap` | `sql/00_account` (ACCOUNTADMIN), `01_database`, `10_raw`, `20_casemgmt` |
| `load` | generated worlds → `RAW` / `RAW_SHOWCASE` (labels → `ML`), golden cases → `ML.GOLDEN_CASES`, row counts |
| `package` | build `wapsi.zip` + `wapsi_app.zip`, PUT to `@WAPSI.APP.CODE` |
| `procs` | `sql/30_procs`, `WAPSI_VERSION()` smoke, showcase parity (`parity` alone re-runs it) |
| `search` | docs corpus + citation registry, Cortex Search `SOP_SEARCH`, smoke query |
| `semantic` | semantic view `WAPSI_FRAUD_SV` + over-hold smoke (must be 0) |
| `agent` | Cortex Agent `WAPSI_OFFICER` |
| `app` | Streamlit files → `@WAPSI.APP.SIS_STAGE/wapsi`, `sql/85_app` |
| `governance` | `sql/90_governance`; Enterprise also `91_governance_enterprise` (`--edition auto` detects) |
| `eval` | golden cases through the procedures (real `AI_COMPLETE`) → `APP.EVAL_RUNS`; `--no-draft` traces and plans only |
| `cross-region` | optional `sql/05_cross_region` (ACCOUNTADMIN) |

## Layout
```
editions/snowflake/
  deploy.py          plan + run the steps (--dry-run prints every statement)
  sqlscript.py       SQL splitter / loader (strings, comments, $$ bodies, -- @optional)
  sql/               00_account … 91_governance_enterprise (numbered, idempotent); 99_teardown (human only)
  snowpark/          build_zip.py; wapsi_sf/ procs.py (handlers), casestore.py (SnowflakeCaseStore), docsearch.py
  app/               streamlit_app.py, sf_data_source.py, environment.yml (warehouse runtime)
  hooks/             CoCo hooks: session_start.py, post_edit_tests.py, pre_sql_guard.py
  templates/         CoCo settings, subagents, automations, semantic-view and agent specs (templates/README.md)
  coco-sessions/     transcripts, notes (C1–C5), screenshots, save_session.py
  COCO_SPRINT.md     the lean sprint (run this)
  COCO_PLAYBOOK.md   long-form reference: objects, acceptance SQL, demo script
src/wapsi/adapters/snowflake_ledger.py   SnowflakeLedger (one bound query per protocol call; snapshot mode)
src/wapsi/adapters/cortex_llm.py         CortexLLM (AI_COMPLETE / COMPLETE, prompt always a bind)
tests/snowflake/                         SQL files, deploy plan, ledger, procedures, audit chain, app, zip
```
The six repo skills (`.claude/skills/`, mirrored to `.agents/skills/`) load in CoCo as `$skill-name`.

## Objects
Schemas: `RAW` / `RAW_SHOWCASE` (synthetic worlds), `ML` (ground truth, never granted to app roles), `CORE`
(deferred), `CASEMGMT` (case tables, procedures), `DOCS` (corpus, `CITATIONS`, `SOP_SEARCH`), `APP` (semantic view,
agent, app, settings, `EVAL_RUNS`). Roles: `WAPSI_ADMIN`, `WAPSI_APP_OWNER`, `WAPSI_ANALYST`, `WAPSI_SUPERVISOR`,
`WAPSI_AUDITOR`, `WAPSI_LEA_READONLY`, `WAPSI_ACTIONS_RUNNER`, `WAPSI_ML`.

| Procedure (`CASEMGMT`) | Rights | Core call |
|---|---|---|
| `TRACE_CASE(P_NCRP_ID)` | owner | group complaints, `trace_case` (pro-rata, SOP 5(xiii)) |
| `BUILD_HOLD_PLAN(P_CASE_ID)` | owner | `classify_trace`, `build_hold_plan`, `verify_plan` |
| `DRAFT(P_CASE_ID, P_KIND)` | owner | `draft_case` (AI_COMPLETE prose, slots for numbers) + `verify_draft`, ≤ 2 redrafts |
| `VERIFY(P_DRAFT_ID)` | owner | `verify_draft` |
| `CASE_SUMMARY(P_CASE_ID)` | owner | ≤ 2 KB status for the agent, incl. the audit-chain check |
| `CHECK_LEA(P_CASE_ID, P_REQUEST)` | owner | `check_lea`: 8-field checklist, hold ≤ identifiable amount, clarification letter |
| `REQUEST_ACCOUNT_LEVEL_HOLD(...)` | owner | `request_account_level_hold` (maker; ≥ 1 listed reason) |
| `APPROVE(...)` | caller | `approve` (supervisor only, checker ≠ maker); hands an approved exception to ↓ |
| `APPROVE_ACCOUNT_LEVEL(...)` | owner | `approve` with the ledger: applies the exception, redrafts for fresh approval |
| `APPEND_AUDIT(...)` | owner | one sealed audit event (the actions runner's only audit write path) |

The agent's tools are Cortex Analyst, Cortex Search and the read / draft / check procedures; it can never approve,
request an exception or write actions.

## The rules, in Snowflake
- **Amount-specific holds by default.** `EXCEPTIONAL_ACCOUNT_LEVEL_HOLD` exists only after an officer's
  `REQUEST_ACCOUNT_LEVEL_HOLD` is approved through `APPROVE` by a different supervisor (fingerprint-bound). Its
  `hold_amount` stays the amount-specific figure; `LIENS` holds one `AMOUNT` lien per account plus an
  `ACCOUNT_LEVEL_EXCEPTION` row (₹0), so a re-approved hold instruction never doubles the money on hold.
  `baseline_freeze_amount` is the whole-account freeze baseline (comparison only).
- **Innocent holders** (SOP 5(xii)): `INNOCENT_HOLDER` (bona fide recipient) and `UNWITTING_CONDUIT` get
  `HOLD_AMOUNT_NO_ESCALATION`. No automatic STR; the officer decides (`STR_RECOMMENDED_OFFICER_DECISION` on conduits).
- **Police / LEA requests**: `CHECK_LEA` records each check in the audit chain (`V_LEA_CHECKS`); the plan never changes.
- **Simulated CFCFRMS interface**: `CFCFRMS_UPDATE` actions carry `payload:interface = 'SIMULATED_CFCFRMS'`.
- **Clocks** carry `NOTES` (how each was counted); forward holds get a `FORWARD_HOLD_REVIEW` clock (90 days, day-75 warning).
- **Hash-chained audit**: `AUDIT_LOG` rows carry `SEQ`, `PREV_HASH`, `HASH` and are INSERT-only; `AUDIT_ANCHOR`
  checkpoints each case (event count + head hash). The guard hook blocks UPDATE / DELETE / MERGE on both; the offline
  tamper drill (edit, delete, reorder, truncate, re-hash) is caught on the Snowflake store.

## Sprint and cost
[`COCO_SPRINT.md`](COCO_SPRINT.md): sessions C1–C5 (Sep 29 – Oct 3), target **≤ $10** of CoCo credit, **hard stop
$15**; `deploy.py` spend is billed to the trial's account credits, not the CoCo budget.

| Guardrail | Setting |
|---|---|
| One warehouse | `WAPSI_WH` XSMALL, `AUTO_SUSPEND = 60`, `STATEMENT_TIMEOUT_IN_SECONDS = 300`, initially suspended |
| Credit cap | resource monitor `WAPSI_RM` (notify 50 % / 80 %, suspend 100 %, suspend immediately 110 %) |
| Blocked in CoCo | sizes above SMALL, `AUTO_SUSPEND` > 300 s, `ALTER ACCOUNT` except Cortex settings, egress, ML grants to app roles, hand-edited holds, any change to the audit chain (`hooks/pre_sql_guard.py`) |
| LLM spend | prose-only generation with slots, `temperature 0`, agent budget 60 s / 16k tokens, Cortex Search `TARGET_LAG = '1 day'` |
| After the event | `ALTER WAREHOUSE WAPSI_WH SUSPEND`; `sql/99_teardown.sql` by a human in a worksheet |

## Trial-account constraints
| Constraint | Design answer |
|---|---|
| CoCo CLI needs the dedicated CoCo trial | sign up at signup.snowflake.com/cortex-code |
| No external network access on trials | outbound work is staged in `CASEMGMT.ACTIONS`; SiS uses the warehouse runtime with Snowflake-channel packages |
| Standard edition: no masking / row access policies | secure views with `IS_ROLE_IN_SESSION()`; Enterprise adds `91_governance_enterprise.sql` |
| Streamlit runs with owner's rights | viewer personas in `APP.USER_ROLES`; the app owner has no RAW or ML access |
| New Streamlit apps default to the container runtime | `RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'` |
| Procedures run Python 3.12, SiS ≤ 3.11, local 3.14 | `WAPSI_VERSION()` smoke and the showcase parity check compare numbers inside Snowflake with the fixture |
