# Wapsi on Snowflake — lean CoCo sprint (Sep 29 – Oct 3, 2026)

The Snowflake edition is complete in the repo and tested offline (`uv run pytest tests/snowflake`):
`sql/00–91`, Snowpark handlers (`snowpark/wapsi_sf`), `deploy.py`, the SiS app (`app/`). CoCo therefore runs
**4 short sessions (+1 optional)** that deploy, prove, build the semantic view and agent with its own skills,
debug live and leave judge-visible evidence. Deploy steps run through `deploy.py`, whose warehouse and Cortex use is
billed to the trial's account credits, not to the CoCo budget. [`COCO_PLAYBOOK.md`](COCO_PLAYBOOK.md) stays the
long-form reference (objects, acceptance SQL, demo script). Feature freeze **Oct 2 22:00 IST**; submit **Oct 3**.

## Budget — target ≤ $10 of CoCo credit, hard stop $15
| # | Day | Session (evidence id) | Lifecycle | effort / max-turns | Est. $ | Cum. $ |
|---|---|---|---|---|---|---|
| C1 | Sep 29 | review deploy, bootstrap + load + package + procs, guard test, parity | Plan, Build, Test, Govern | low / 25 | 2.00 | 2.00 |
| C2 | Sep 30 | semantic view + Cortex Agent with CoCo's semantic-view and agent skills | Build, Test | low / 30 | 3.00 | 5.00 |
| C3 | Oct 1 | Cortex Search, DRAFT / VERIFY with AI_COMPLETE, real-LLM golden-set eval | Build, Test | low / 20 | 1.75 | 6.75 |
| C4 | Oct 2 | SiS app, governance (edition-aware), final smoke | Run, Govern | low / 20 | 1.75 | 8.50 |
| C5 | Oct 2–3 | optional fix-ups from the notes' open issues | Test | low / 12 | 1.00 | 9.50 |
| | | reserve | | | 0.50 | 10.00 |

Estimates assume ~$0.07 per low-effort turn; replace them with C1's measured cost. `--effort medium` only where a
session says so, and only after a low-effort attempt failed.

**Cost check after every session** (outside CoCo, never through it; CoCo usage lags by up to a few hours):
```powershell
function coco-cost($from, $to) { snow sql -c VJ22713 -q "ALTER SESSION SET TIMEZONE = 'UTC'; SELECT ROUND(SUM(token_credits),3) AS credits, SUM(tokens) AS tokens FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY WHERE LOWER(interface) = 'cli' AND usage_time BETWEEN '$from' AND '$to'" }
Import-Csv editions/snowflake/coco-sessions/notes/cost_log.csv | ForEach-Object { $_.session; coco-cost $_.start_utc $_.end_utc }
```
USD = credits × the price per credit on the billing page (or the dollar figure the cost page shows for CoCo).

**Stop rule.** (1) A session that overran its estimate by more than 50 %: cut the next session's `--max-turns` by a
third and drop its optional items. (2) Cumulative ≥ $10: cancel C5; finish anything left with `deploy.py` and
Snowsight worksheets. (3) Cumulative ≥ **$15: hard stop**, no further `cortex exec`. (4) Never re-run a whole session:
a failed item becomes a C5 line. (5) One `-2` continuation is allowed when a session stops at `--max-turns` with an
item half done: same prompt plus "resume: skip finished items", `--max-turns 8`.

## Day 0 — Sep 28 evening (no CoCo spend)
1. `uv sync --all-extras`; `uv run pytest -q`; `uv run python -m editions.snowflake.deploy --step all --dry-run`
   (read it once: it is every statement the sessions will run).
2. Connection `VJ22713` (OAuth, active in CoCo). Set `$env:WAPSI_SF_CONNECTION = 'VJ22713'` in the profile.
   Bootstrap needs ACCOUNTADMIN once (C1); later steps run as `WAPSI_ADMIN`.
3. Hooks (CoCo snapshots them at session start):
```powershell
New-Item -ItemType Directory -Force .cortex/agents | Out-Null
Copy-Item editions/snowflake/templates/settings.json .cortex/settings.json
Copy-Item editions/snowflake/templates/agents/*.md .cortex/agents/
Set-Content editions/snowflake/coco-sessions/notes/cost_log.csv 'session,start_utc,end_utc,credits,usd,driver'
```
4. In `cortex -c VJ22713`: `/model` → **Auto Efficient** → `/quit` (so `cortex exec` inherits a cheap default).

## Running a session and capturing evidence
Every session is one PowerShell block from the repo root: it pipes a single-quoted here-string (`'@` at column 0)
to `cortex exec`, which auto-rejects permission prompts, hence `--bypass`; the PreToolUse guard hook still runs
(C1 proves it). `-o` saves CoCo's final message as the session note. Then, for **every** session:
```powershell
$t1 = (Get-Date).ToUniversalTime().ToString('s'); Add-Content editions/snowflake/coco-sessions/notes/cost_log.csv "$ID,$t0,$t1,,,coco"
uv run python editions/snowflake/coco-sessions/save_session.py --session latest --step $ID --slug <slug> --gate G2 --note "<one line>"
```
Screenshots go to `coco-sessions/screenshots/<ID>_<slug>_<n>.png` (crop account URLs, user names, tokens); each
session lists its shots. Commit the note, the saved transcript and the screenshots together.

**CoCo rules (every prompt points here).** Read only the files the prompt names. SQL goes through
`snowflake_sql_execute` so the guard sees it; `deploy.py` runs through bash. Fix only what fails, minimally, and say
which file and why. Never `CREATE OR REPLACE` a CASEMGMT table. The final message is the session note: what ran,
result tables, fixes (file + one line), open issues.

### C1 · Sep 29 · review, deploy core, guard, parity — slug `deploy-core`
```powershell
$ID='C1'; $t0=(Get-Date).ToUniversalTime().ToString('s'); @'
Wapsi session C1. Read AGENTS.md and the "CoCo rules" and "### C1" parts of editions/snowflake/COCO_SPRINT.md.
1. Review editions/snowflake/deploy.py and sql/00_account.sql, 01_database.sql, 10_raw.sql, 20_casemgmt.sql,
   30_procs.sql for Snowflake syntax or privilege problems. List findings; fix only real errors.
2. Guard test: run DROP SCHEMA WAPSI.GUARD_TEST with snowflake_sql_execute; it must be blocked [drop-db-schema].
3. Run: uv run python -m editions.snowflake.deploy --connection VJ22713 --step bootstrap,load,package,procs
   On a failure: diagnose, fix minimally, re-run only that step.
4. Run SELECT AI_COMPLETE('claude-sonnet-5', 'Reply with the single word: ready'). If it fails for region or
   availability, run the deploy with --step cross-region and retry once.
Note: findings, fixes, the load row-count table, the parity table, the AI_COMPLETE result, edition hints.
'@ | cortex exec -c VJ22713 --bypass --max-turns 25 --effort low -o editions/snowflake/coco-sessions/notes/C1.md
```
**Accept** parity `N/N OK` (holds A ₹43,200 · B ₹27,648 · D ₹48,000 · L ₹4,800 · P ₹19,200 = ₹1,42,848; forward
hold ₹96,000; recoverable ₹3,34,848; showcase 2 hold ₹1,20,000, P2; `audit chain intact` for both cases: the
hash chain survives the Snowflake round trip), `WAPSI_VERSION` `matches_fixture: true`,
every row count `OK`, the DROP blocked. **Shots** `_1` blocked DROP, `_2` row counts, `_3` parity table.
**Then (no CoCo):** `uv run python -m editions.snowflake.deploy --step eval --eval-world demo --limit 15 --no-draft`
fills the case tables with 15 demo cases for Cortex Analyst (trace + plan only, no AI_COMPLETE).

### C2 · Sep 30 · semantic view + agent with CoCo's skills — slug `semantic-agent`
This is where CoCo adds the most: its semantic-view skill turns the deployed base view into an Analyst-ready model
and its agent skill tests the agent. Judges look for this transcript.
```powershell
$ID='C2'; $t0=(Get-Date).ToUniversalTime().ToString('s'); @'
Wapsi session C2. Read the "CoCo rules" and "### C2" parts of editions/snowflake/COCO_SPRINT.md, then
editions/snowflake/templates/semantic_view_metrics.md and templates/agent_instructions.md.
1. Run: uv run python -m editions.snowflake.deploy --step semantic   (deploys the base WAPSI.APP.WAPSI_FRAUD_SV)
2. With your semantic-view skill, improve WAPSI.APP.WAPSI_FRAUD_SV: add the template's 6 verified queries (in
   SEMANTIC_VIEW() form), its SQL-generation instructions and the derived metric hold_vs_freeze_ratio. Validate
   every verified question with Cortex Analyst against the template SQL; deploy. If no hold lapses within 15 days,
   set WAPSI.APP.DEMO_CLOCK to a date that makes question 1 non-empty and say which date.
3. Save the final DDL (SELECT GET_DDL('SEMANTIC_VIEW', 'WAPSI.APP.WAPSI_FRAUD_SV')) into sql/70_semantic_view.sql.
4. Run the deploy with --step agent. With your agent skill, test WAPSI.APP.WAPSI_OFFICER with the template's 6
   acceptance conversations. Change instructions only for a failed test and mirror the change in sql/80_agent.sql.
Note: each question, Analyst SQL and result; each conversation with tool calls and verdict; changes made.
'@ | cortex exec -c VJ22713 --bypass --max-turns 30 --effort low -o editions/snowflake/coco-sessions/notes/C2.md
```
**Accept** `over_hold_amount` 0 in every row of the template's acceptance query; held = `SUM(hold_amount)` of
HOLD_PLAN; 6/6 verified questions answered as the template SQL; agent: trace totals = C1, verification shown before
any draft text, injection quoted and refused, "Approve the STR" and "Freeze the account" refused, the LEA
request answered with its missing fields and the identifiable amount. If Analyst validation fails twice, a
`-2` continuation may use `--effort medium`. **Shots** `_1` Analyst answer + SQL (branches lapsing in 15 days),
`_2` agent trace answer, `_3` injection refused, `_4` approval refused.

### C3 · Oct 1 · search, Cortex drafting, real-LLM eval — slug `search-draft-eval`
```powershell
$ID='C3'; $t0=(Get-Date).ToUniversalTime().ToString('s'); @'
Wapsi session C3. Read the "CoCo rules" and "### C3" parts of editions/snowflake/COCO_SPRINT.md.
1. Run: uv run python -m editions.snowflake.deploy --step search. Then use $sop-citer on Cortex Search for
   "can we hold a payment aggregator's pool account?" and "merchant goods delivered".
2. Check WAPSI.APP.SETTINGS llm_model answers AI_COMPLETE; if not, set it to a Claude model that does.
3. CALL WAPSI.CASEMGMT.DRAFT('CASE-31009260000101', 'ALL') and show the verification table per draft (use the
   $str-drafter Snowflake queries). Then show the verifier failing a tampered amount with:
   uv run python .claude/skills/str-drafter/scripts/draft_and_verify.py 31009260000101 --world showcase-memory --kind HOLD_INSTRUCTION --tamper "₹43,200.00" "₹45,000.00"
4. Run: uv run python -m editions.snowflake.deploy --step eval --eval-world showcase
   and: uv run python -m editions.snowflake.deploy --step eval --eval-world demo --limit 12
Note: search hits with citation ids, verification table, tampered-draft failure, both eval metric tables.
'@ | cortex exec -c VJ22713 --bypass --max-turns 20 --effort low -o editions/snowflake/coco-sessions/notes/C3.md
```
**Accept** SOP-9-1-III and SOP-9-2 among the hits; every draft of case 1 `passed`; tampered draft FAILED on
`numeric`; eval `over_hold_inr` 0, `hold_exact_match` 1.0 on showcase, two `cortex-ai-complete` rows in
`APP.EVAL_RUNS`. **Shots** `_1` search hits, `_2` verification table, `_3` tampered failure, `_4` eval metrics.

### C4 · Oct 2 (before the 22:00 freeze) · app, governance, final smoke — slug `app-governance`
```powershell
$ID='C4'; $t0=(Get-Date).ToUniversalTime().ToString('s'); @'
Wapsi session C4. Read the "CoCo rules" and "### C4" parts of editions/snowflake/COCO_SPRINT.md.
1. Run: uv run python -m editions.snowflake.deploy --step app. Keep RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'
   even if your Streamlit skill prefers the container runtime (trials have no compute pool or PyPI egress).
2. Run: uv run python -m editions.snowflake.deploy --step governance --edition auto. Report the edition.
3. Role tests with snowflake_sql_execute: as WAPSI_AUDITOR SELECT * FROM WAPSI.APP.V_HOLD_PLAN LIMIT 3 (masked)
   and SELECT * FROM WAPSI.RAW.CUSTOMERS LIMIT 1 (denied); as WAPSI_ANALYST SELECT COUNT(*) FROM
   WAPSI.ML.LABELS_ACCOUNTS (denied); SHOW GRANTS ON SCHEMA WAPSI.ML. Then show the guard blocking
   UPDATE WAPSI.CASEMGMT.HOLD_PLAN SET hold_amount = 1 and DELETE FROM WAPSI.CASEMGMT.AUDIT_LOG.
4. Final smoke: CALL WAPSI.CASEMGMT.CASE_SUMMARY('CASE-31009260000101'); the semantic-view over-hold query;
   DESCRIBE AGENT WAPSI.APP.WAPSI_OFFICER; SHOW STREAMLITS IN SCHEMA WAPSI.APP.
Note: edition, role-test table (expected vs actual), blocked commands, smoke results, what I must click in the app.
'@ | cortex exec -c VJ22713 --bypass --max-turns 20 --effort low -o editions/snowflake/coco-sessions/notes/C4.md
```
**Accept** WAPSI_APP exists; auditor sees `HB01-XXXXXX0001`-style account numbers; ML and RAW denied to app
roles; both commands blocked (`[hand-edited-hold]`, `[audit-append-only]`); CASE_SUMMARY reports
`audit_chain.status = intact` and `anchored = true`. **Human, in the browser:** open
WAPSI_APP as yourself (persona switcher: analyst → Generate drafts; supervisor → approve the HB01-1000000001 hold
instruction) → `SELECT * FROM WAPSI.CASEMGMT.LIENS` shows ₹43,200. **Shots** `_1` Inbox, `_2` Case Room, `_3` Hold
Plan (baseline strip), `_4` Drafts & Verification, `_5` Approvals, `_6` masked auditor rows, `_7` blocked UPDATE.

### C5 · optional · fix-ups — slug `fixups`
```powershell
$ID='C5'; $t0=(Get-Date).ToUniversalTime().ToString('s'); @'
Wapsi session C5. Read the "CoCo rules" part of editions/snowflake/COCO_SPRINT.md and the "open issues" of
editions/snowflake/coco-sessions/notes/C1.md to C4.md. Fix the three most important, re-run only the affected
deploy step and its acceptance check. No new features. Note: each issue, fix, re-check result.
'@ | cortex exec -c VJ22713 --bypass --max-turns 12 --effort low -o editions/snowflake/coco-sessions/notes/C5.md
```

## Oct 3 · submission (no CoCo spend)
Walkthrough video (3–4 min): SiS golden path, the agent answering with SQL, the verifier failing a tampered draft,
the blocked DROP. Check `coco-sessions/INDEX.md` lists C1–C4 (C5); `git grep -i snowflakecomputing` is empty;
deck metrics come from `APP.EVAL_RUNS` and the C3 note. After submitting: `ALTER WAREHOUSE WAPSI_WH SUSPEND;`.
`sql/99_teardown.sql` is for after the finale, run by a human in a worksheet (the guard blocks it in CoCo).

## Deferred (finale Oct 27–30, only if shortlisted)
Managed MCP server, Jira/Slack drain and CoCo automations, intake stream/tasks, dynamic tables, RUN_CLOCKS task,
row access policies, Snowflake ML mule model, CI with headless CoCo (playbook S02, S07, S08, S15–S18, S20).
