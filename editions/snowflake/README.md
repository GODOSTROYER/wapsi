# Wapsi — Snowflake edition (Snowflake CoCo CLI Hackathon, GCC edition)

The Snowflake-native layer of Wapsi: data in `WAPSI`, the portable Python core running unchanged inside Snowpark
procedures, Cortex Search / Analyst / Agent on top, a Streamlit in Snowflake app, and governance. **Everything
Snowflake-side is built by the team inside CoCo (Cortex Code) sessions** so the hackathon evidence is genuine; this
folder holds the playbook for those sessions, the reference templates they adapt, tested hook scripts, and the
evidence they produce.

Start here: [`COCO_PLAYBOOK.md`](COCO_PLAYBOOK.md) (steps S01–S22 by gate: G0 Sep 16–18, G1 Sep 27, G2 Oct 3).

## What lives here
```
editions/snowflake/
  README.md               this file
  COCO_PLAYBOOK.md        step-by-step CoCo build script: prompts, objects, acceptance SQL, evidence, demo script
  hooks/                  CoCo hook scripts (Python, tested locally, no Snowflake needed)
    session_start.py        SessionStart: latest KPIs + next gate + stale-skill warning (one-line budget)
    post_edit_tests.py      PostToolUse edit|write: pytest for the edited area; syncs skills on skill edits
    pre_sql_guard.py        PreToolUse snowflakesqlexecute|bash: blocks destructive / costly / ungoverned SQL
  templates/              reference files to adapt in CoCo (see templates/README.md)
    settings.json           hooks config -> .cortex/settings.json
    agents/                 sql-reviewer, sop-auditor, deck-writer subagents -> .cortex/agents/
    mcp.json.example        Jira, Slack and WAPSI_MCP client entries -> ~/.snowflake/cortex/mcp.json
    automations/            prompt files for cortex automation create + the local 1-minute drain runner
    semantic_view_metrics.md  WAPSI_FRAUD_SV metrics, dimensions, 6 verified queries
    agent_instructions.md   WAPSI_OFFICER instructions and reference CREATE AGENT spec
  coco-sessions/          evidence: transcripts/, screenshots/, notes/, INDEX.md, save_session.py
  --- created in CoCo sessions (see the playbook) ---
  sql/                    00_account.sql … 90_governance.sql (numbered, idempotent)
  snowpark/               build_zip.py, parity_check.py, train_mule_xgb.py, wapsi_sf/ (Snowflake adapters + procs)
  sis/                    streamlit_app.py + environment.yml (wraps ../../ui)
  tests/                  unit tests for wapsi_sf and the zip build (no network)
  deck/                   deck.md, narration.md (deck-writer subagent)
```
Related, outside this folder: the six skills in `.claude/skills/` (mirrored to `.agents/skills/` by
`scripts/sync_skills.py`) — `synth-fraud-ledger`, `money-trail-tracer`, `lien-calculator`, `str-drafter`,
`sop-citer`, `wapsi-eval`. Invoke them in CoCo with `$skill-name`.

## Snowflake objects (target)
`WAPSI` with schemas `RAW` (synthetic world), `CORE` (dynamic tables `ACCOUNT_FEATURES_24H`, `DEVICE_SHARING`,
`PASS_THROUGH`), `CASEMGMT` (case tables, procedures `TRACE_CASE`, `BUILD_HOLD_PLAN`, `CASE_SUMMARY`, `DRAFT`,
`VERIFY`, `APPROVE`, `RUN_CLOCKS`, `OPEN_CASE`; stream + tasks), `DOCS` (corpus, `CITATIONS`, Cortex Search
`SOP_SEARCH`), `APP` (semantic view `WAPSI_FRAUD_SV`, agent `WAPSI_OFFICER`, managed MCP server `WAPSI_MCP`,
Streamlit `WAPSI_APP`, `EVAL_RUNS`), `ML` (ground truth, golden cases, mule model — never granted to app roles).
Roles: `WAPSI_ADMIN`, `WAPSI_APP_OWNER`, `WAPSI_ANALYST`, `WAPSI_SUPERVISOR`, `WAPSI_AUDITOR`,
`WAPSI_LEA_READONLY`, `WAPSI_ACTIONS_RUNNER`, `WAPSI_ML`.

Legal rules follow `AGENTS.md` convention 6 as corrected on 2026-09-16: nodal / pool / escrow accounts are held for
the disputed amount (`HOLD_AMOUNT_INTERMEDIARY`, SOP 9.1(iii)) with no forward hold (SOP 9.1(i)); the STR
seven-working-day limit is from the FIU-IND FAQ (PML Rule 8 says "promptly").

## Evidence convention
Every CoCo session that builds, tests or runs something is saved with
`uv run python editions/snowflake/coco-sessions/save_session.py --session latest --step <Sxx> --slug <kebab> --gate <G0|G1|G2>`
plus screenshots named `<Sxx>_<slug>_<n>.png`. Details: [`coco-sessions/README.md`](coco-sessions/README.md).
Judges look for CoCo use at every lifecycle stage (plan, build, test, run, govern); the playbook tags each step.

## Cost guardrails
| Guardrail | Setting | Where |
|---|---|---|
| One small warehouse | `WAPSI_WH` XSMALL, `AUTO_SUSPEND = 60`, `AUTO_RESUME = TRUE`, `INITIALLY_SUSPENDED = TRUE` | S01 |
| Runaway queries (incl. recursive CTEs, which have no engine depth cap) | `STATEMENT_TIMEOUT_IN_SECONDS = 300` on the warehouse; depth + visited-path guards in SQL | S01, S05 |
| Credit cap | resource monitor `WAPSI_RM` (notify 50 % / 80 %, suspend 100 %, suspend immediately 110 %) on `WAPSI_WH` | S01 |
| Blocked in CoCo | warehouse sizes above SMALL, `AUTO_SUSPEND` > 300 s, `ALTER ACCOUNT` except Cortex settings, egress integrations | `hooks/pre_sql_guard.py` |
| Refresh cadence | dynamic tables `TARGET_LAG = '1 hour'` (1 minute only while demoing); Cortex Search `TARGET_LAG = '1 day'`; tasks suspended outside demos; tasks run on `WAPSI_WH` | S08, S09, S07 |
| LLM spend | prose-only generation with slots; `temperature 0`; agent budget 60 s / 16k tokens; small model for citation audits; drain runner calls CoCo only when actions are pending; hourly (not per-minute) automations | S10, S12, S16 |
| Watch non-warehouse spend | resource monitors cover warehouses only; check `SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY` by `SERVICE_TYPE` weekly (Cortex, serverless tasks, CoCo) **[verify view columns]** | weekly |
| After the event | `cortex automation suspend <name>`; `ALTER TASK … SUSPEND`; `ALTER DYNAMIC TABLE … SUSPEND` | Oct 4 / Oct 31 |

## Trial-account constraints and fallbacks
| Constraint (source) | Consequence | Fallback in this design |
|---|---|---|
| CoCo CLI is not on standard trials ([CoCo CLI docs](https://docs.snowflake.com/en/user-guide/cortex-code/cortex-code-cli)) | need the dedicated CoCo trial | sign up at signup.snowflake.com/cortex-code |
| No external network access on trials ([trial accounts](https://docs.snowflake.com/en/user-guide/admin-trial-account)) | no external access integrations; procedures, SiS and tasks cannot call Jira/Slack; SiS container runtime cannot pip-install | outbound work is staged in `CASEMGMT.ACTIONS`; a local CoCo runner drains Jira/Slack through MCP; SiS uses the **warehouse runtime** with Snowflake-channel packages |
| Edition may be Standard (CoCo trial default per strategy doc) | masking policies, row access policies and Cortex AI Guardrails need Enterprise | secure views with `IS_ROLE_IN_SESSION()`, app-level role mapping, RulesGuard + verifier + `AI_COMPLETE` guardrails option |
| Streamlit runs with owner's rights | `CURRENT_ROLE()` is the owner inside the app | `st.user.user_name` + `APP.USER_ROLES`; show role masking in a worksheet; restricted app-owner role |
| New Streamlit apps default to the container runtime (2026_06 bundle) | needs a compute pool and PyPI egress | `RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'` |
| CoCo automations: hourly minimum, sandboxed, no local MCP, run as default role ([docs](https://docs.snowflake.com/en/user-guide/cortex-code/cortex-code-automations)) | no 1-minute automation; no Jira/Slack from the sandbox | Snowflake tasks for 1-minute SQL work; local runner for Jira/Slack; automations for hourly/daily digests and parity evals |
| `gemini-3.1-pro` is Public Preview and cross-region only | may be unavailable | default `claude-sonnet-5`; Gemini optional in this edition |
| 30-day trial vs finale Oct 27–30 | account may expire | convert to paid with `WAPSI_RM`, or cached demo mode + recorded walkthrough |
| Python: local venv is 3.14 / pandas 3; Snowflake procedures run 3.12 with Anaconda pandas 2.x; SiS warehouse runtime is ≤ 3.11 | behaviour drift | CI pins 3.11; S04 smoke procedure and `parity_check.py` compare numbers inside Snowflake with local ones |
| `wapsi.citations` reads `data/docs/citations.json` from the source tree | quotes are empty when imported from a zip | quotes served from `DOCS.CITATIONS` via the Snowflake `DocSearch` adapter |
