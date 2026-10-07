# Editions

Wapsi is entered in three hackathons from one MIT-licensed repository. Each edition is a thin, platform-specific
layer under `editions/<platform>/` on top of the same portable core in `src/wapsi`. The core decides every number,
disposition, draft and citation. An edition decides where the data lives, which model writes the prose, how
officers reach the system and how it runs on that platform.

Why one core and not three products: [ADR 0005](adr/0005-one-core-three-editions.md). How the adapters plug in:
[architecture.md](architecture.md#how-editions-plug-in). What was built when: [originality.md](originality.md).

## Summary

Status as of 2026-09-29, checked against the repository.

| | Snowflake | Google Cloud | AMD |
|---|---|---|---|
| Hackathon | Snowflake CoCo CLI Hackathon (GCC edition) | AI Builder Cup | lablab.ai AMD Developer Hackathon ACT III |
| Key dates | CoCo build sprint 29 Sep–3 Oct; submission 3 Oct (hard deadline 4 Oct, 23:59 IST); finale 27–30 Oct if shortlisted | Planned: completed and deployed between 4 and 18 Oct ([originality.md](originality.md)) | Event window 12–18 Oct 2026 |
| Folder | [`editions/snowflake`](../editions/snowflake/README.md) | [`editions/gcp`](../editions/gcp/README.md) | [`editions/amd`](../editions/amd/README.md) |
| Platform layer | Snowpark procedures running the core unchanged (`TRACE_CASE`, `BUILD_HOLD_PLAN`, `DRAFT`, `VERIFY`, `APPROVE`), Cortex Search `SOP_SEARCH`, Cortex Analyst semantic view `WAPSI_FRAUD_SV`, Cortex Agent `WAPSI_OFFICER`, Streamlit in Snowflake `WAPSI_APP`, RBAC and edition-aware governance; deployed and proven in CoCo (Cortex Code) sessions | ADK agents and a workflow on Cloud Run, Gemini (Gemini API free tier by default), BigQuery, Firestore, Secret Manager; optional Vertex AI, Model Armor, Pub/Sub, Cloud Scheduler and a private Gemma tier on Cloud Run GPU | vLLM on ROCm serving open models on one AMD Instinct MI300X, a GraphSAGE mule scorer in pure PyTorch, a LoRA fine-tune for the prose fields, benchmarks |
| Ledger | `SnowflakeLedger` (bind-parameter queries on `WAPSI.RAW`), or a Snowpark snapshot into `ParquetLedger` | `BigQueryLedger`, or `ParquetLedger` | `ParquetLedger` |
| Prose LLM | `CortexLLM` on Cortex `AI_COMPLETE` | `GeminiLLM`; Gemma via `OpenAICompatLLM` (paid add-on) | `OpenAICompatLLM` against vLLM |
| Case store | `SnowflakeCaseStore` on `WAPSI.CASEMGMT` (hash-chained `AUDIT_LOG` plus `AUDIT_ANCHOR`) | `FirestoreCaseStore` | `FileCaseStore` |
| Guard | `RulesGuard`; Cortex guardrails as a setting (`APP.SETTINGS`, off by default) | `RulesGuard`; `ModelArmorGuard` when a template is configured | `RulesGuard`; plus an LLM judge when the verifier model is served |
| Status | Complete in the repository and tested offline (80 tests in `tests/snowflake`): SQL scripts `00`–`99`, Snowpark handlers, the SiS app and `deploy.py`. Four short CoCo sessions plus one optional (C1–C5, 29 Sep–3 Oct) deploy it, prove parity on Snowflake and save the evidence. | Code built and tested offline with fakes. Deploy scripts written and dry-run in tests with a fake `gcloud`, not run against Google Cloud. Nothing deployed. | Code prepared before the event and tested offline. Training, fine-tuning, serving and benchmarks run on the MI300X only during 12–18 Oct 2026. No AMD GPU results exist yet. |

## What is shared and what is not

```mermaid
flowchart TB
    subgraph SHARED["Shared by all three editions"]
        CORE["src/wapsi<br/>trace, taint, hold plan, classify,<br/>drafts, verifier, clocks, search"]
        UI["ui/app.py<br/>Streamlit officer app"]
        GOLD["data/golden + evals/<br/>golden set, harness, guard corpus"]
        DOCS["data/docs<br/>citations.json, facts.json"]
        SK[".claude/skills<br/>6 skills, mirrored to .agents/skills"]
    end
    SF["editions/snowflake<br/>Snowpark, Cortex, SiS, CoCo hooks"] --> CORE
    GC["editions/gcp<br/>ADK, Cloud Run, Gemini, BigQuery, Firestore"] --> CORE
    AM["editions/amd<br/>vLLM-ROCm, GNN, LoRA, benchmarks"] --> CORE
```

| Shared (one copy, same behaviour everywhere) | Platform-specific (per edition) |
|---|---|
| Money-trail tracing and exact per-victim taint (`wapsi.graph`) | Where the ledger lives and how a case's slice of it is loaded |
| Amount-specific hold plan and every SOP rule (`wapsi.graph.holdplan`) | Which LLM writes the three prose fields |
| Account classification and reasons (`wapsi.classify`) | Where case bundles, approvals and audit events are stored |
| Eight draft kinds, the LEA clarification letter and the placeholder-only prose contract (`wapsi.draft`) | How officers and supervisors sign in and reach the app |
| The verifier, the LEA request checklist, the account-level exception and the approval fingerprint (`wapsi.verify`, `wapsi.pipeline.run`) | Event intake (Snowflake streams and tasks, Pub/Sub, a REST API) |
| SOP clocks, the bank working-day calendar and the regulatory radar (`wapsi.domain`); the hash-chained audit log (`wapsi.audit`) | Extra guards on top of `RulesGuard` (Cortex AI guardrails, Model Armor, an LLM judge) |
| Citation ids and verbatim quotes (`wapsi.citations`, `data/docs/`) | Optional extra scorers (Snowflake ML XGBoost, a GNN) |
| The Streamlit officer app (`ui/`) | Deployment, cost controls and platform governance |
| Golden set, evaluation harness, showcase fixtures | Platform benchmarks and evidence for the judges |

The showcase numbers are the parity test for every edition: ₹1,42,848.00 held across 5 accounts and
₹3,34,848.00 recoverable for the victim in showcase case 1, against ₹5,03,50,000.00 that a whole-account freeze
would immobilise (`src/wapsi/fixtures/showcase.py`).

## Snowflake edition (CoCo)

**Folder:** [`editions/snowflake`](../editions/snowflake/README.md). Session plan:
[`COCO_SPRINT.md`](../editions/snowflake/COCO_SPRINT.md); long-form reference (objects, acceptance SQL, demo
script): [`COCO_PLAYBOOK.md`](../editions/snowflake/COCO_PLAYBOOK.md).

The portable core runs **unchanged inside Snowpark stored procedures**: the procedures do the Snowflake I/O, and
every number, disposition, draft and check still comes from `src/wapsi`. The whole edition is in the repository
and tested offline; CoCo (Cortex Code) sessions deploy it, prove it on Snowflake and leave judge-visible evidence.

**Deploy it**

```bash
uv run python -m editions.snowflake.deploy --step all --dry-run      # print the plan and every SQL statement
uv run python -m editions.snowflake.deploy --step all                # connection from WAPSI_SF_CONNECTION
uv run python -m editions.snowflake.deploy --step governance --edition auto
uv run python -m editions.snowflake.deploy --step eval               # real Cortex AI_COMPLETE golden-set run
```

`--step all` runs `bootstrap`, `load`, `package`, `procs`, `search`, `semantic`, `agent`, `app` and `governance`
in order; `parity`, `eval` and the optional `cross-region` step run on request. Every step is safe to re-run
(`IF NOT EXISTS`, `CREATE OR REPLACE` for code objects, truncate-and-reload for `RAW`), and case history in
`CASEMGMT` is never dropped. The Snowpark session comes from a named connection in `~/.snowflake/connections.toml`;
`deploy.py` never reads or prints credentials.

**Snowflake objects**

| Object | What it is | Script |
|---|---|---|
| Warehouse `WAPSI_WH` | `XSMALL`, auto-suspend 60 s, statement timeout 300 s, resource monitor `WAPSI_RM` | `sql/00_account.sql` |
| Roles | `WAPSI_ADMIN` (owner), `WAPSI_APP_OWNER` (Streamlit, owner's rights), `WAPSI_ANALYST` (maker), `WAPSI_SUPERVISOR` (checker, inherits analyst), `WAPSI_AUDITOR` (read-only, masked), `WAPSI_LEA_READONLY` (read-only, masked), `WAPSI_ACTIONS_RUNNER`, `WAPSI_ML` | `sql/00_account.sql` |
| Database `WAPSI` | schemas `RAW` (demo world), `RAW_SHOWCASE` (hand-checked showcase), `ML` (ground truth and golden set, never granted to app roles), `CORE`, `CASEMGMT` (cases, trails, hold plans, drafts, approvals, actions, clocks, `AUDIT_LOG`, `AUDIT_ANCHOR`), `DOCS` (corpus chunks, citation registry), `APP` (semantic view, agent, app, settings, `EVAL_RUNS`) | `sql/01_database.sql`, `10_raw.sql`, `20_casemgmt.sql` |
| Procedures | `CASEMGMT.TRACE_CASE`, `BUILD_HOLD_PLAN`, `CASE_SUMMARY`, `DRAFT`, `VERIFY`, `APPROVE` and `APP.WAPSI_VERSION`, all Python handlers in `snowpark/wapsi_sf/procs.py` over the packaged core (`wapsi.zip`) | `sql/30_procs.sql` |
| Cortex Search `DOCS.SOP_SEARCH` | the legal corpus, with `citation_id`, `doc_id` and `section` attributes; target lag 1 day | `sql/60_docs.sql`, `61_search.sql` |
| Semantic view `APP.WAPSI_FRAUD_SV` | cases, holds, trail edges and clocks for Cortex Analyst; no personal data and no ground truth | `sql/70_semantic_view.sql` |
| Cortex Agent `APP.WAPSI_OFFICER` | tools: Cortex Analyst on the semantic view, `SOP_SEARCH` and the case procedures. `APPROVE` is deliberately not a tool: approval stays a human maker-checker step | `sql/80_agent.sql` |
| Streamlit in Snowflake `APP.WAPSI_APP` | the shared officer app (`ui/app.py`, unchanged) on `SnowflakeDataSource`, warehouse runtime | `sql/85_app.sql`, `app/` |
| Governance | Standard edition: RBAC plus secure views (`V_CUSTOMERS`, `V_COMPLAINTS`, `V_CASES`, `V_HOLD_PLAN`, `V_TRAIL_EDGES`); Enterprise edition adds masking policies `MASK_ACCOUNT_ID` and `MASK_PII` on the base tables. `--edition auto` detects which applies | `sql/90_governance.sql`, `91_governance_enterprise.sql` |

**Adapters.** `SnowflakeLedger` (`src/wapsi/adapters/snowflake_ledger.py`) answers every `Ledger` call with one
bind-parameter query on `WAPSI.RAW`, or snapshots a schema into a local `ParquetLedger`. `CortexLLM`
(`src/wapsi/adapters/cortex_llm.py`) calls `AI_COMPLETE` for the prose fields only; the prompt is always a bind.
`CortexDocSearch` (`snowpark/wapsi_sf/docsearch.py`) queries `SOP_SEARCH` and reads the citation registry, falling
back to the packaged quotes. `SnowflakeCaseStore` (`snowpark/wapsi_sf/casestore.py`) keeps the case bundle and
flattened copies for SQL and Cortex Analyst; `AUDIT_LOG` is insert-only and hash-chained, and every append writes
a checkpoint to `AUDIT_ANCHOR`. Trial accounts have no external network access, so approved actions are staged
in `CASEMGMT.ACTIONS` for a local runner.

**CoCo sessions.** Four short sessions plus one optional (C1–C5, 29 Sep–3 Oct 2026) review and run the deploy, prove showcase
parity and the SQL guard hook, build the semantic view and the agent with CoCo's own skills, add the real-LLM
golden-set run, the SiS app and governance, and save the transcripts in `coco-sessions/`. Target: at most $10 of
CoCo credit, hard stop at $15. The hook configuration in `templates/settings.json` runs `hooks/pre_sql_guard.py`
before CoCo's SQL and shell tools (matcher `snowflake_sql_execute|sql_execute|bash`) and
`hooks/post_edit_tests.py` after file edits (`edit|write|apply_patch`); the guard blocks destructive, costly or
ungoverned SQL, such as a `DROP SCHEMA`, edits to the append-only audit log or grants on the ground-truth schema.

## Google Cloud edition (ADK)

**Folder:** [`editions/gcp`](../editions/gcp/README.md). Diagram and service-by-service table:
[`editions/gcp/architecture.md`](../editions/gcp/architecture.md).

**What is platform-specific**

- Two ADK apps: `wapsi_officer` (an `LlmAgent` with ten pipeline tools, such as `trace_money`, `hold_plan`,
  `draft_documents` and `approve_draft`, and a multimodal intake sub-agent) and `case_pipeline` (a deterministic
  ADK workflow for Pub/Sub hold notices, uploads and clock sweeps).
- Two Cloud Run services from one image: `wapsi-api` (ADK on FastAPI, the Pub/Sub trigger route, `/healthz`;
  private) and `wapsi-ui` (the shared Streamlit app on the same case store; public, for judges; all data is
  synthetic).
- A free-trial default profile: Gemini through the Gemini API free tier with an AI Studio key kept in Secret
  Manager, scale to zero and a 5 USD budget alert. Vertex AI, Model Armor, Memory Bank and the Gemma GPU tier are
  opt-in flags.
- Adapters that live in `src/wapsi/adapters` because they are general-purpose: `BigQueryLedger` (parameterised
  SQL, partitioned and clustered tables, a bytes-billed cap), `FirestoreCaseStore`, `ModelArmorGuard` and
  `GeminiLLM`. A private Gemma tier on Cloud Run GPU is reached through `OpenAICompatLLM`.
- Role grants signed with an HMAC key from Secret Manager; a supervisor role is needed to approve.
- A demo mode with zero cloud access: showcase ledger, in-memory case store, `FakeLLM`, `RulesGuard` and a
  scripted ADK model.

**Status, honestly.** The edition README says that nothing has been deployed. The code is built and unit-tested
locally with fakes (`tests/gcp`: a SQLite-backed BigQuery fake, an in-memory Firestore, fake Model Armor and
Gemini). The deploy scripts are written and dry-run in tests with a fake `gcloud`, but have not been run against
Google Cloud. The README also lists what was verified against the installed SDKs, what was taken from
documentation without running it (model ids, GPU availability, IAP flags), and open items such as Memory Bank
needing an extra package. The edition README has the latest state.

## AMD edition (ROCm)

**Folder:** [`editions/amd`](../editions/amd/README.md). Event-week steps: [`RUNBOOK.md`](../editions/amd/RUNBOOK.md).
The GNN and LoRA work streams have their own READMEs in `editions/amd/gnn/` and `editions/amd/lora/`.

**What is platform-specific**

- **Serving:** `serving/start_llm.sh` starts vLLM (OpenAI-compatible) on the MI300X. The compose file serves the
  drafting model and, with the `verifier` profile, a second, smaller model on the same GPU that acts as an
  independent LLM judge. The edition README plans Gemma 4 31B with a merged STR-drafting LoRA at about 55% of GPU
  memory, and Gemma 4 E4B as the judge at about 15%.
- **App:** a FastAPI service (`app/api.py`) and the shared Streamlit app on a JSON-file case store, calling
  `process_case` with `ParquetLedger`, `OpenAICompatLLM` (or `FakeLLM` in demo mode), `LLMJudgeGuard` (the rules
  guard plus the judge, which can add flags but never clear one) and `GnnHttpScorer`.
- **GNN:** an edge-aware GraphSAGE mule scorer in pure PyTorch (`gnn/`), trained on synthetic worlds with separate
  train, validation and test seeds. It implements the core's `Scorer` protocol, and the app falls back to the
  heuristic scorer when the GNN service is unavailable.
- **LoRA:** verifier-filtered distillation and a LoRA fine-tune of an open model for the placeholder-only prose
  fields (`lora/`).
- **Benchmarks:** LLM serving, GPU telemetry and end-to-end case benchmarks (`bench/`). The package itself says
  that no numbers live in it: every result is produced on the MI300X during the event.
- **Demo profile:** `docker compose --profile demo` runs the app with `WAPSI_DEMO_MODE=1`, no GPU and no model
  downloads.

**Status, honestly.** The code in `docker-compose.yml`, `serving/`, `app/` and `bench/` is prepared before the
event and tested offline with fakes (`tests/amd`); the edition README states that none of it has touched an AMD
GPU yet. Per [originality.md](originality.md), the AMD-specific AI work (GNN training on ROCm, the fine-tune,
vLLM-ROCm serving and benchmarks) is done during 12–18 Oct 2026. Everything that exists before then is disclosed
and will sit under the git tag `amd-baseline`, created at kickoff; the tag does not exist yet. There are no MI300X
measurements in the repository; results will be published in `editions/amd/AMD_RESULTS.md` after the event-week
runs. The only results committed now are labelled CPU-development checks (`gnn/results/cpu-dev-2026-09-23.json`,
and a fake-model pipeline check in `lora/results/`). Pre-training on IBM AMLworld is marked event-time only in
`gnn/train.py`. The app's extra dependencies are the `amd-app` extra (FastAPI, uvicorn); training uses the `amd`
extra.

## How the editions stay consistent

| Mechanism | Where |
|---|---|
| One implementation of every rule, imported by all editions | `src/wapsi` |
| Hand-computed showcase numbers every adapter must reproduce | `src/wapsi/fixtures/showcase.py`, `tests/test_contracts.py` |
| Same golden set and harness for every edition | `data/golden/`, `evals/harness.py` |
| Adapter parity tests | `tests/gcp` (BigQuery SQL against `InMemoryLedger` on SQLite); `tests/snowflake` (Snowpark fakes) and, on the account, `uv run python -m editions.snowflake.deploy --step parity` (`TRACE_CASE` + `BUILD_HOLD_PLAN` on both showcase cases against `wapsi.fixtures.showcase`) |
| Same skills in every coding agent (Claude Code, CoCo, Gemini CLI) | `.claude/skills/`, mirrored to `.agents/skills/` by `scripts/sync_skills.py` |
| Same conventions for every contributor and agent | `AGENTS.md` |

## Related documents

- [architecture.md](architecture.md): ports, adapters and the pipeline
- [ADR 0005](adr/0005-one-core-three-editions.md): why one core and three thin editions
- [originality.md](originality.md): build timeline and event boundaries
