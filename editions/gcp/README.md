# Wapsi, Google Cloud edition (AI Builder Cup)

ADK 2.9 agents on Cloud Run, Gemini on Agent Platform, Gemma 4 on Cloud Run GPU, BigQuery, Firestore,
Model Armor, Pub/Sub, Cloud Scheduler, Cloud Storage, Memory Bank and Secret Manager. See
[architecture.md](architecture.md) for the diagram and what each service does. All data is synthetic.

> Nothing in this folder has been deployed yet. The code is built and unit-tested locally with fakes; the
> deploy scripts are written but have not been run.

## Layout

```
editions/gcp/
  app/
    config.py      env configuration (WAPSI_*), demo vs cloud
    services.py    adapters per mode: BigQuery/Firestore/Gemini/Model Armor or showcase/in-memory/FakeLLM
    tools.py       ADK tools: open_case, list_cases, trace_money, explain_account, hold_plan,
                   verification_report, draft_documents, approve_draft, search_sop, case_clocks
    intake.py      extract_complaint (Gemini multimodal, untrusted output, ledger cross-check)
    agent.py       App "wapsi_officer": LlmAgent + wapsi_intake sub-agent + plugins
    workflow.py    App "case_pipeline": ADK 2.x Workflow for Pub/Sub notices, uploads and clock sweeps
    plugins.py     guard (Model Armor -> RulesGuard), structured audit log, memory saving
    identity.py    HMAC-signed role grants in session state
    memory.py      Memory Bank (WAPSI_MEMORY_BANK_ID) or in-memory
    demo_model.py  offline scripted BaseLlm so the agents run with zero cloud access
    server.py      FastAPI: ADK API (+ dev UI), Pub/Sub trigger, /healthz, role middleware
    ui_source.py   Streamlit data source on the CaseStore
  agents/          ADK agents_dir: wapsi_officer/, case_pipeline/
  ui_app.py        Streamlit entry point (shared ui/app.py on the case store)
  Dockerfile       python:3.11-slim + uv, non-root; one image for the API and UI services
  deploy/          cloudrun.sh, gemma_vllm_service.sh, cloudbuild.yaml, bigquery_setup.sql,
                   model_armor_template.json, iam.md
src/wapsi/adapters/
  bigquery_ledger.py  firestore_store.py  model_armor_guard.py  gemini_llm.py
tests/gcp/         offline tests (SQLite-backed BigQuery fake, in-memory Firestore, fake Model Armor/Gemini)
```

## Run locally in demo mode (no Google Cloud access)

Demo mode is on whenever `WAPSI_PROJECT` is unset, or when `WAPSI_DEMO_MODE=1`. It uses the showcase ledger
(or `WAPSI_WORLD=data/generated/demo`), an in-memory case store, `FakeLLM` for draft prose, `RulesGuard`, and
a scripted ADK model in place of Gemini.

```bash
uv run pytest tests/gcp -q                                    # offline test suite
uv run uvicorn editions.gcp.app.server:app --port 8080        # ADK API + dev UI + Pub/Sub trigger
#   open http://localhost:8080/dev-ui/  (app: wapsi_officer)
uv run adk web editions/gcp/agents --port 8000 --session_service_uri memory:// --artifact_service_uri memory://
uv run streamlit run editions/gcp/ui_app.py                   # case room UI on the same services
```

Things to type in the dev UI: `Open case 31009260000101`, `why is HB01-2000000001 held? explain the account`,
`show the clocks`, `extract demo://complaint/31109260000102`, and
`approve DRF-CASE-31009260000101-HOLD_INSTRUCTION-HB01-1000000001`.

The dev UI's default user is an analyst, so that approval is refused. To act as a supervisor, set
`WAPSI_DEMO_DEFAULT_ROLE=supervisor`, use a user id that starts with `supervisor`, or send the demo header:

```bash
curl -s localhost:8080/apps/wapsi_officer/users/ravi/sessions -H 'X-Wapsi-Role: supervisor' \
  -H 'Content-Type: application/json' -d '{}'
curl -s localhost:8080/run -H 'Content-Type: application/json' -d '{"app_name":"wapsi_officer",
  "user_id":"ravi","session_id":"<id>","new_message":{"role":"user","parts":[{"text":
  "approve DRF-CASE-31009260000101-HOLD_INSTRUCTION-HB01-1000000001"}]}}'
# a CFCFRMS notice through the Pub/Sub trigger route
curl -s localhost:8080/apps/case_pipeline/trigger/pubsub -H 'Content-Type: application/json' -d \
  "{\"message\":{\"data\":\"$(printf '{"ncrp_ids":["31109260000102","31109260000103"]}' | base64 -w0)\"}}"
```

## Configuration

`app/config.py` lists every variable. The main ones are:

- `WAPSI_PROJECT`, `WAPSI_REGION` (`asia-south1`) and `WAPSI_LOCATION` (the Gemini location, `global`).
- `WAPSI_BQ_DATASET`, `WAPSI_MODEL` (`gemini-3.5-flash`) and `WAPSI_MODEL_ARMOR_TEMPLATE`.
- `WAPSI_MEMORY_BANK_ID`, `WAPSI_PRIVATE_LLM_URL` and `WAPSI_SUPERVISORS`.
- `WAPSI_ROLE_SIGNING_KEY`, which comes from Secret Manager.
- `WAPSI_DEMO_MODE`.

## Deploy (step by step)

Prerequisites:
- A project with billing upgraded. The free trial blocks GPUs.
- `gcloud`, `bq` and `uv` installed.
- The owner or deployer roles listed in [deploy/iam.md](deploy/iam.md).
- Cloud Run GPU quota, requested early: L4 in us-central1, or RTX PRO 6000 in asia-south2.

```bash
export PROJECT_ID=wapsi-aibc REGION=asia-south1 BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX
gcloud auth login && gcloud auth application-default login
```

1. **Bootstrap** (`bash editions/gcp/deploy/cloudrun.sh bootstrap`). This sets up:
   - the APIs and Artifact Registry;
   - four service accounts with least-privilege roles;
   - the BigQuery dataset, with dataset-scoped read access;
   - Firestore in Native mode;
   - the role-grant secret in Secret Manager;
   - the Model Armor template from `deploy/model_armor_template.json`;
   - the private intake bucket, with a 30-day lifecycle;
   - a 50 USD budget with alerts.
2. **Memory Bank** (optional). Create an Agent Runtime instance once and export its id:
   ```python
   import vertexai  # needs google-cloud-aiplatform (see "Open items")
   client = vertexai.Client(project="wapsi-aibc", location="asia-south1")
   print(client.agent_engines.create().api_resource.name.split("/")[-1])
   ```
   `export MEMORY_BANK_ID=<id>`. The app uses the full resource name `agentengine://projects/.../reasoningEngines/<id>`,
   so Memory Bank stays in `asia-south1` even though Gemini uses `global`.
3. **Data** (`bash editions/gcp/deploy/cloudrun.sh data`). Creates the partitioned and clustered tables and
   loads a generated synthetic world (`python -m wapsi.adapters.bigquery_ledger load ...`).
4. **Build** (`bash editions/gcp/deploy/cloudrun.sh build`). Cloud Build builds the image with
   `editions/gcp/Dockerfile` and pushes it to Artifact Registry.
5. **Gemma private tier** (optional). Run `TIER=l4 bash editions/gcp/deploy/gemma_vllm_service.sh`, or use
   `TIER=rtx` for asia-south2. Then `export GEMMA_URL=<service url> GEMMA_REGION=<region>`.
6. **Deploy** (`SUPERVISORS=ravi@bank.example bash editions/gcp/deploy/cloudrun.sh deploy`). This deploys:
   - `wapsi-api` (private by default), with the startup and liveness probes on `/healthz`;
   - `wapsi-ui` (public; synthetic data), with the startup probe on `/_stcore/health`.
7. **Wire** (`bash editions/gcp/deploy/cloudrun.sh wire`). Creates the Pub/Sub topics and OIDC push
   subscriptions (with dead-letter topics) to `/apps/case_pipeline/trigger/pubsub`, the Cloud Storage finalize
   notifications, and the Cloud Scheduler clock job.
8. **Identity-Aware Proxy** (production). Enable IAP on both services and grant
   `roles/iap.httpsResourceAccessor` to officers and supervisors (`gcloud beta run services update ... --iap`
   **[verify]**). For a public judging demo without IAP, deploy with `PUBLIC_API=1 TRUST_ROLE_HEADER=1`
   (synthetic data only).
9. **Judging window**: run `bash editions/gcp/deploy/cloudrun.sh judging-on` (min-instances=1, no cold
   starts). For Gemma, also run `gcloud run services update wapsi-gemma --min-instances 1`.
10. **Smoke test**:
    - `curl $(gcloud run services describe wapsi-api --region $REGION --format='value(status.url)')/healthz`
      with an identity token.
    - `gcloud pubsub topics publish wapsi-notices --message '{"ncrp_ids":["<id>"]}'`, then check that the
      case appears in the UI.
11. **After judging**: run `bash editions/gcp/deploy/cloudrun.sh judging-off` and set Gemma back to
    `--min-instances 0`.

## Cost guardrails

- **Scale to zero** outside judging: every service defaults to `--min-instances 0`, with
  `--max-instances 3` (API/UI) and `1` (Gemma). The GPU is the only expensive part (roughly $1/h on L4,
  $3/h on RTX PRO 6000 while an instance is up). Keep Gemma at zero instances unless you are recording or
  judging.
- **Budget**: 50 USD, with alerts at 50%, 90% and 100% (`bootstrap`). Check Billing reports daily during the
  event.
- **BigQuery**: every query sets `maximum_bytes_billed` (20 GiB), partition pruning on `ts`, clustering on
  the account columns, and job labels (`app=wapsi`) for cost attribution.
- **Gemini**: `gemini-3.5-flash` by default, low temperature, `max_output_tokens` caps, context caching per
  agent, and structured JSON output with a bounded retry (3 retries, exponential backoff).
- **Model Armor**: the standalone tier is free up to 2M tokens a month (then $0.10 per 1M tokens). The guard
  plugin caches results per text, and each narrative is screened once per tool call.
- **Storage and logs**: uploads are deleted after 30 days. Audit logs are structured JSON; set a log bucket
  retention if you keep the project running.
- **Tear down**:
  ```bash
  gcloud run services delete wapsi-api wapsi-ui wapsi-gemma --region ...
  gcloud scheduler jobs delete wapsi-clocks --location ...
  ```
  Then delete the Pub/Sub topics, the bucket and the dataset, or delete the project.

## What was verified and what was assumed

Verified locally against the installed packages: google-adk 2.9.1, google-genai 2.23, google-cloud-bigquery
3.45, google-cloud-firestore 2.30 and google-cloud-modelarmor.

- The ADK pieces work offline: `LlmAgent`, `App(plugins=...)`, plugin callback signatures, `ToolContext.state`
  and `user_id`, sub-agent transfer, the graph `Workflow` with routes, `InMemoryRunner`,
  `get_fast_api_app(..., trigger_sources=["pubsub"])`, the `/apps/{app}/trigger/pubsub` route, and
  `AgentLoader` on `editions/gcp/agents`.
- The Model Armor request and response types (`SanitizeUserPromptRequest`, `filter_results` map keys,
  `invocation_result`) are the SDK's real types. The template JSON parses as `modelarmor_v1.Template`.
- BigQuery parameter and load-job objects (`ArrayQueryParameter`, `TimePartitioning`, clustering) and the
  google-genai config fields (`response_json_schema`, `labels`, `thinking_config`) are real.
- The adapter SQL runs on SQLite through a small dialect translation, and matches `InMemoryLedger` exactly on
  the showcase world and on a sample of the demo world.

Taken from current docs but not run against Google Cloud:
- The model ids `gemini-3.5-flash` and `google/gemma-4-E4B-it`.
- The Gemma vLLM image and flags, the RTX PRO 6000 and asia-south2 availability, and the Memory Bank URI format.
- The Model Armor regional endpoint and the `modelarmor.user` role name.
- The BigQuery `WITH RECURSIVE` 500-iteration limit.
- The gcloud flag spellings, and IAP on Cloud Run.

## Open items

- **Memory Bank needs `google-cloud-aiplatform`.** Neither `VertexAiMemoryBankService` nor
  `agentengine://` sessions work without it, and the `gcp` extra in `pyproject.toml` does not pull it in: use
  `google-adk[gcp]` or add `google-cloud-aiplatform[agent_engines]`. Until then the app logs a warning and
  falls back to in-memory memory and sessions.
- **`ui/app.py` has no data-source hook.** `ui_app.py` works around this by seeding
  `st.session_state["_wapsi_ds"]`; a `WAPSI_DATA_SOURCE` switch in `ui/app.py` would be cleaner.
- **Data residency of the agent conversation.** Tool results shown to the agent include victim names, and
  Gemini uses `global` by default. For a bank deployment, set `WAPSI_LOCATION=asia-south1`, or run the agents
  on the private Gemma tier (`WAPSI_AGENT_ON_PRIVATE=1`), and confirm with the bank.
