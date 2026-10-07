# Wapsi, Google Cloud edition (AI Builder Cup)

ADK 2.9 agents on Cloud Run with Gemini, BigQuery, Firestore, Secret Manager and Cloud Logging, plus optional
Pub/Sub, Cloud Scheduler and Cloud Storage. The default path runs on a Google Cloud **free trial** with Gemini
through the Gemini API free tier (an AI Studio key). Agent Platform (Vertex AI), Model Armor, Memory Bank and Gemma
4 on Cloud Run GPU are optional, behind flags. See [architecture.md](architecture.md) for the diagram and what each
service does. All data is synthetic.

> Nothing in this folder has been deployed yet. The code is built and unit-tested locally with fakes; the
> deploy scripts are written and dry-run in tests with a fake `gcloud`, but have not been run against Google Cloud.

## Free-trial setup (what you need)

The default profile (`PROFILE=free` in `deploy/cloudrun.sh`) needs no paid upgrade, no GPU and no quota increase.

### What you must do yourself

The scripts cannot do these steps for you.

1. **Start the free trial and create a project.** In the [console](https://console.cloud.google.com/freetrial),
   start the free trial ($300 credit for 90 days; a free-trial billing account is created for you). Then create a
   project, for example `wapsi-aibc-<suffix>` (project ids are globally unique).
2. **Link the free-trial billing account to the project.** Cloud Run, Cloud Build, Artifact Registry and Secret
   Manager need a linked billing account, even inside their free tiers. You are not charged while the account is
   a free-trial account.
   ```bash
   gcloud billing accounts list                     # note the ACCOUNT_ID of "My Billing Account"
   gcloud billing projects link <PROJECT_ID> --billing-account=<ACCOUNT_ID>
   ```
3. **Install the tools.** Install the [gcloud CLI](https://cloud.google.com/sdk/docs/install), which includes `bq`,
   then run `gcloud components install beta` (the deploy uses `gcloud beta run deploy`). Install
   [uv](https://docs.astral.sh/uv/). On Windows, run the scripts from **Git Bash**. [Cloud Shell](https://shell.cloud.google.com)
   also works: it has `gcloud` and `bq` preinstalled and is already signed in.
4. **Sign in twice.** The first login is for `gcloud`; the second creates Application Default Credentials for the
   BigQuery loader and local tools.
   ```bash
   gcloud auth login
   gcloud auth application-default login
   gcloud config set project <PROJECT_ID>
   ```
5. **Create a Gemini API key and keep it secret.**
   - Open [aistudio.google.com/api-keys](https://aistudio.google.com/api-keys), click **Create API key** and
     pick (import) the same project.
   - Leave it on the **Free** tier. Do not click **Set up billing** in AI Studio: that moves the project to the
     paid tier with prepaid credits. The $300 trial credit cannot pay for the Gemini API in AI Studio.
   - The free tier may use prompts to improve Google products. That is acceptable here only because every Wapsi
     record is synthetic.
   - **Locally:** keep the key in an environment variable, never in a file in the repository. In Git Bash:
     `read -rs GEMINI_API_KEY && export GEMINI_API_KEY`.
   - **Cloud Run:** store it in Secret Manager yourself, after `bootstrap` has enabled the API. The deploy
     script never reads, prints or embeds the key; it only mounts the secret by name:
     ```bash
     printf '%s' "$GEMINI_API_KEY" | gcloud secrets create wapsi-gemini-api-key --project <PROJECT_ID> \
       --replication-policy=user-managed --locations=asia-south1 --labels=app=wapsi --data-file=-
     ```
     `bash editions/gcp/deploy/cloudrun.sh gemini-key` prints this command again, with your project filled in.
6. **Choose a region.** Use `asia-south1` (Mumbai) for Cloud Run, Firestore, BigQuery, Artifact Registry and
   Secret Manager. It has Tier 1 Cloud Run pricing and is covered by the free tiers; Firestore's free tier applies
   to the `(default)` database. The Gemini API has no region setting and no data-residency control. It checks
   availability against where the caller runs (India is supported), and Google chooses where requests are
   processed. For in-country processing, use `GEMINI_BACKEND=vertex` with `GEMINI_LOCATION=asia-south1`, which is
   billed per token.

### Commands

```bash
export PROJECT_ID=<PROJECT_ID> REGION=asia-south1          # PROFILE=free is the default
bash editions/gcp/deploy/cloudrun.sh bootstrap   # 9 APIs, 3 service accounts, BigQuery dataset, Firestore,
                                                 # role-signing secret, 5 USD budget alert
# now store the Gemini API key yourself (step 5 above), then:
bash editions/gcp/deploy/cloudrun.sh data        # BigQuery tables + the ~2 MB synthetic demo world
bash editions/gcp/deploy/cloudrun.sh build       # Cloud Build on the free-tier machine -> Artifact Registry
bash editions/gcp/deploy/cloudrun.sh deploy      # wapsi-api (private) + wapsi-ui (public), min-instances 0
```

`bootstrap` enables only these APIs: `run`, `cloudbuild`, `artifactregistry`, `firestore`, `secretmanager`,
`bigquery`, `logging`, `iam` (for the service accounts) and `billingbudgets` (for the budget alert). It adds
`modelarmor` only when `MODEL_ARMOR_TEMPLATE` is set.

Options:

- **Skip BigQuery.** `LEDGER=parquet` makes the image carry the demo world at `/app/data/world` (`BAKE_WORLD=1`,
  the default), so `data` has nothing to load. With `WORLD_BUCKET=<bucket>`, `data` uploads the world to Cloud
  Storage instead, and `deploy` mounts it read-only.
- **Keep ADK sessions.** `ADK_STORE=firestore` stores ADK sessions and memory in Firestore, which survives scale
  to zero. With the default `memory`, the API is capped at one instance so sessions stay together.
- **Public ADK dev UI for judges.** `PUBLIC_API=1 TRUST_ROLE_HEADER=1` (synthetic data only).
- **Event path.** `wire` adds Pub/Sub, the intake bucket, upload notifications and the Cloud Scheduler clock
  sweep, all inside their free tiers.
- **Model Armor.** `MODEL_ARMOR_TEMPLATE=wapsi-intake` turns it on. The deterministic `RulesGuard` is always on.
- **Judging window.** `judging-on` sets min-instances 1 and prints a cost note. Run `judging-off` afterwards.

Smoke test:

```bash
API=$(gcloud run services describe wapsi-api --region asia-south1 --format='value(status.url)')
curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$API/healthz"
#  -> "gemini_backend": "aistudio", "gemini_api_key_set": true, "model": "gemini-3.8-flash"
```

### What it costs

Expected cost at demo scale is **$0**. While the billing account is a free-trial account, Google does not charge
your card: anything beyond the free tiers is drawn from the $300 credit, and the 5 USD budget alert e-mails you
long before that matters.

| Service | Free tier (per month unless noted) | Wapsi at demo scale | Expected |
|---|---|---|---|
| Gemini API, AI Studio key (`gemini-3.8-flash`) | input and output tokens free; rate limited per project (RPM, TPM, RPD; see [AI Studio rate limits](https://aistudio.google.com/rate-limit)); RPD resets at midnight Pacific | agent turns, intake and draft prose | $0. Over the limit you get HTTP 429, never a bill |
| Cloud Run (request-based billing) | 180,000 vCPU-seconds, 360,000 GiB-seconds, 2M requests | API 2 vCPU/2 GiB, UI 1 vCPU/2 GiB, scale to zero | $0 (about 25 active API hours) |
| Cloud Build | 2,500 build-minutes on `e2-standard-2` | about 10 minutes per build | $0 |
| Artifact Registry | 0.5 GB of storage | the cleanup policy keeps 2 images | a few cents a month if the images exceed 0.5 GB |
| BigQuery | 1 TiB of queries, 10 GiB of storage | about 2 MB of data; each query bills at least 10 MB per table | $0 |
| Firestore (`(default)` database) | 1 GiB; 50,000 reads, 20,000 writes and 20,000 deletes a day | cases, drafts, audit | $0 |
| Secret Manager | 6 active secret versions, 10,000 accesses | 2 secrets | $0 |
| Cloud Logging | 50 GiB of logs | structured audit lines | $0 |
| Budget alert | free | 5 USD, alerts at 50/90/100% | $0 |
| Optional `wire` step: Pub/Sub, Cloud Scheduler, Cloud Storage | 10 GiB of messages; 3 Scheduler jobs; Cloud Storage free only in US regions | a handful of messages and KB | under $0.01 |

What could cost money (all off or opt-in by default):

- **`judging-on`.** One warm instance of each service is billed at the Cloud Run idle rate, about 1.5 USD a day
  beyond the free tier. It is paid from the credit. Run `judging-off` when judging ends.
- **`GEMINI_BACKEND=vertex` or `PROFILE=paid`.** Gemini on Agent Platform is billed per token. The Free Trial
  terms exclude only AI Studio Gemini API and partner-model costs from the $300 credit, so this should draw on the
  credit **[verify]**.
- **Model Armor** (`MODEL_ARMOR_TEMPLATE`). The standalone tier is free up to 2M tokens a month, then $0.10 per 1M
  tokens. The API needs a billing account on the project; the free-trial account qualifies.
- **Memory Bank** (`MEMORY_BANK_ID`). A billed Agent Runtime feature that also needs `google-cloud-aiplatform`.
  Off by default; `PROFILE=free` refuses it. Use `ADK_STORE=firestore` instead.
- **Gemma 4 on Cloud Run GPU** (`gemma_vllm_service.sh`). The free trial blocks GPUs and quota increases, so the
  script refuses to run without `ALLOW_GPU=1`. When upgraded, an L4 costs about $1 an hour while an instance is
  up. The AMD edition covers private Gemma inference.
- **Setting up billing in AI Studio.** This moves the key to the paid Gemini tier. Don't.
- **Egress and storage outside the US free tier.** Cloud Storage in `asia-south1` and response traffic cost cents
  at most at demo volume.

## Layout

```
editions/gcp/
  app/
    config.py      env configuration (WAPSI_*), demo vs cloud, free-trial defaults
    services.py    adapters per mode: BigQuery|Parquet / Firestore / Gemini / Model Armor, or showcase/in-memory/FakeLLM
    tools.py       ADK tools: open_case, list_cases, trace_money, explain_account, hold_plan,
                   verification_report, draft_documents, approve_draft, search_sop, case_clocks
    intake.py      extract_complaint (Gemini multimodal, untrusted output, ledger cross-check)
    agent.py       App "wapsi_officer": LlmAgent + wapsi_intake sub-agent + plugins; Gemini API key or Vertex
    workflow.py    App "case_pipeline": ADK 2.x Workflow for Pub/Sub notices, uploads and clock sweeps
    plugins.py     guard (Model Armor -> RulesGuard), structured audit log, memory saving, 429 notice
    tracing.py     ToolTracePlugin (first plugin): every model turn and tool call as an AgentTrace, sealed into
                   the case's audit chain; /wapsi/traces on the API
    trace_demo.py  offline agent-trace demo (real app + tools + guards, scripted model) and its recording
    identity.py    HMAC-signed role grants in session state
    memory.py      in-memory (default), Firestore (WAPSI_ADK_STORE=firestore) or Memory Bank (paid)
    demo_model.py  offline scripted BaseLlm so the agents run with zero cloud access
    server.py      FastAPI: ADK API (+ dev UI), Pub/Sub trigger, /healthz, role middleware
    ui_source.py   Streamlit data source on the CaseStore
  agents/          ADK agents_dir: wapsi_officer/, case_pipeline/
  ui_app.py        Streamlit entry point (shared ui/app.py on the case store)
  Dockerfile       python:3.11-slim + uv, non-root, optional baked demo world; one image for API and UI
  deploy/          cloudrun.sh (PROFILE=free|paid), gemma_vllm_service.sh (paid add-on), cloudbuild.yaml,
                   bigquery_setup.sql, model_armor_template.json, iam.md
src/wapsi/adapters/
  bigquery_ledger.py  firestore_store.py  model_armor_guard.py  gemini_llm.py
tests/gcp/         offline tests (SQLite-backed BigQuery fake, in-memory Firestore, fake Model Armor/Gemini,
                   dry runs of the deploy scripts with a fake gcloud)
```

## Run locally in demo mode (no Google Cloud access)

Demo mode is on whenever `WAPSI_PROJECT` is unset, or when `WAPSI_DEMO_MODE=1`. It uses:

- the showcase ledger, or `WAPSI_WORLD=data/generated/demo`;
- an in-memory case store;
- `FakeLLM` for draft prose;
- `RulesGuard`;
- a scripted ADK model in place of Gemini.

No API key is needed.

```bash
uv run pytest tests/gcp -q                                    # offline test suite
uv run uvicorn editions.gcp.app.server:app --port 8080        # ADK API + dev UI + Pub/Sub trigger
#   open http://localhost:8080/dev-ui/  (app: wapsi_officer)
uv run adk web editions/gcp/agents --port 8000 --session_service_uri memory:// --artifact_service_uri memory://
uv run streamlit run editions/gcp/ui_app.py                   # case room UI on the same services
```

Things to type in the dev UI:

- `Open case 31009260000101`
- `why is HB01-2000000001 held? explain the account`
- `show the clocks`
- `extract demo://complaint/31109260000102`
- `approve DRF-CASE-31009260000101-HOLD_INSTRUCTION-HB01-1000000001`

The dev UI's default user is an analyst, so that approval is refused. To act as a supervisor, do one of these:

- set `WAPSI_DEMO_DEFAULT_ROLE=supervisor`;
- use a user id that starts with `supervisor`;
- send the demo header:

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

## Agent trace: what the agent did, step by step

The Case Room has an **Agent trace** tab. It shows every tool call the ADK agent made on the case, in order:

- the tool name, its arguments (account numbers, phones and names redacted) and a one-line result summary built
  from the tool's own result (numbers come from data);
- latency (a bar per call) and status (OK, blocked by guard, refused by maker-checker, error);
- the safety verdicts around each call: the input screen (Model Armor or RulesGuard), argument screening,
  the maker-checker role check, untrusted complaint text found in a result, the verifier's verdict (plan
  policy check, draft verification, re-verification before an approval), the output screen, and a reply
  grounding check (every rupee amount and account number in the agent's reply must appear in a tool result);
- each model turn (model, latency, tokens when the model reports them, the tools it asked for);
- a summary row: tool calls, time in tools, tokens, blocked calls, guard checks, verifier verdicts;
- the raw JSON of every step.

How it works: `ToolTracePlugin` is the app's **first** plugin, so it sees each callback before the guard can
answer it (a blocked call still gets its step). The guard plugin reports its verdicts into the same
`wapsi.agenttrace.ToolTraceRecorder` (pure Python, edition-neutral). When a run ends, its trace is appended to the
audit log of every case it touched as an `agent_trace` event, so the case's hash chain covers it (editing a
recorded step breaks the chain), and it is logged as one JSON line for Cloud Logging. `GET /wapsi/traces?case_id=`
on the API service returns the runs that instance recorded.

No API key needed: the showcase case shows a recorded offline run (`src/wapsi/agenttrace/showcase_recording.json`):
the real app, tools and guards, driven by the scripted model through a fixed officer script (open a case, explain
an account, an analyst's approval refused by maker-checker, an injected instruction rewritten by the input guard
and the search that follows blocked). In demo mode the GCP UI also runs that script once at start-up, so its traces
sit in the showcase cases' audit chains. With a Gemini key, live runs replace both.

```bash
uv run python -m editions.gcp.app.trace_demo            # run the script, print each run's tool sequence
uv run python -m editions.gcp.app.trace_demo --write    # refresh the recording (one warm-up run first)
uv run streamlit run ui/app.py                          # Case Room -> Agent trace
curl -s 'localhost:8080/wapsi/traces?case_id=CASE-31009260000101&limit=5'
```

## Configuration

`app/config.py` lists every variable. The main ones are:

- **Mode and project.** `WAPSI_DEMO_MODE`, `WAPSI_PROJECT` and `WAPSI_REGION` (`asia-south1`).
- **Gemini.**
  - `WAPSI_GEMINI_BACKEND`: `aistudio` (default) or `vertex`.
  - `GEMINI_API_KEY` or `GOOGLE_API_KEY`: the AI Studio key, mounted from Secret Manager on Cloud Run and never
    logged.
  - `WAPSI_MODEL` (`gemini-3.8-flash`) and `WAPSI_LOCATION` (the Vertex location, `global`).
- **Data.** `WAPSI_LEDGER` (`bigquery` or `parquet`), `WAPSI_WORLD` and `WAPSI_BQ_DATASET`.
- **Sessions and memory.** `WAPSI_ADK_STORE` (`memory` or `firestore`) and `WAPSI_MEMORY_BANK_ID` (paid, off).
- **Optional features.** `WAPSI_MODEL_ARMOR_TEMPLATE` (off), `WAPSI_PRIVATE_LLM_URL` (Gemma on GPU, paid, off),
  `WAPSI_CONTEXT_CACHE` (on for `vertex` only) and `WAPSI_OTEL_TO_CLOUD`.
- **Roles.** `WAPSI_SUPERVISORS`, and `WAPSI_ROLE_SIGNING_KEY` (from Secret Manager).

## Paid profile (billing upgraded)

`PROFILE=paid` switches the defaults:

- Gemini on Agent Platform (`GEMINI_BACKEND=vertex`, with `roles/aiplatform.user` and no API key);
- Model Armor template `wapsi-intake`;
- the intake bucket;
- Cloud Trace and OpenTelemetry export;
- a 50 USD budget;
- a faster Cloud Build machine.

Every step is the same as in the free profile. The extra, optional pieces are:

1. **Memory Bank.** Create an Agent Runtime instance once (this needs `google-cloud-aiplatform`, see "Open items")
   and deploy with `MEMORY_BANK_ID=<id>`. The app uses the full resource name
   `agentengine://projects/.../reasoningEngines/<id>`, so Memory Bank stays in `asia-south1`.
2. **Gemma private tier.** Run `ALLOW_GPU=1 TIER=l4 bash editions/gcp/deploy/gemma_vllm_service.sh` (or
   `TIER=rtx` for asia-south2). This needs Cloud Run GPU quota: L4 in us-central1, or RTX PRO 6000 in asia-south2.
   Then run `PROFILE=paid GEMMA_URL=<url> GEMMA_REGION=<region> bash editions/gcp/deploy/cloudrun.sh deploy`.
3. **Identity-Aware Proxy** (production). Enable IAP on both services and grant `roles/iap.httpsResourceAccessor`
   to officers and supervisors (`gcloud beta run services update ... --iap` **[verify]**).
4. **Judging.** `judging-on`. For Gemma, also run `gcloud run services update wapsi-gemma --min-instances 1`.
   Afterwards run `judging-off` and set Gemma back to `--min-instances 0`.

The deployer's IAM roles are in [deploy/iam.md](deploy/iam.md).

## Cost guardrails

- **Scale to zero.** Every service defaults to `--min-instances 0`. `--max-instances` is 1 for the API with
  in-memory sessions, and 3 otherwise. Gemma is capped at 1 instance.
- **Budget.** 5 USD on the free profile and 50 USD on the paid profile, with alerts at 50%, 90% and 100% of
  actual spend and 100% of forecast. A budget alerts; it does not stop spending. Check Billing reports daily
  during the event.
- **Gemini free tier.** The adapter honours the server's `RetryInfo` on 429 and does not retry a daily-quota
  429, raising a clear message instead. ADK agents retry inside the client (4 attempts), and
  `ModelQuotaPlugin` turns a persistent 429 into a plain-language reply instead of an error. For more
  requests per day, set `WAPSI_MODEL` to another free-tier model such as `gemini-3.5-flash-lite`.
- **BigQuery.** Every query sets `maximum_bytes_billed` (20 GiB) and job labels (`app=wapsi`). Tables use
  partition pruning on `ts` and clustering on the account columns.
- **Gemini generally.** Low temperature, `max_output_tokens` caps and structured JSON output with a bounded retry.
  Explicit context caching runs only on Agent Platform; the Gemini API caches implicitly.
- **Model Armor** (optional). The standalone tier is free up to 2M tokens a month. The guard plugin caches results
  per text, and each narrative is screened once per tool call.
- **Build and images.** Cloud Build uses the default free-tier machine, and an Artifact Registry cleanup policy
  keeps the 2 newest images.
- **Storage and logs.** Uploads are deleted after 30 days. Audit logs are structured JSON.
- **Tear down.**
  ```bash
  gcloud run services delete wapsi-api wapsi-ui --region asia-south1
  gcloud scheduler jobs delete wapsi-clocks --location asia-south1     # only if you ran `wire`
  ```
  Then delete the Pub/Sub topics, the buckets and the dataset, or delete the project.

## What was verified and what was assumed

Verified locally against the installed packages: google-adk 2.9.1, google-genai 2.23, google-cloud-bigquery
3.45, google-cloud-firestore 2.30 and google-cloud-modelarmor.

- **Gemini API client.** `genai.Client(vertexai=False, api_key=...)` targets the Gemini Developer API, and an
  explicit `vertexai=False` overrides `GOOGLE_GENAI_USE_VERTEXAI`. The SDK raises on request `labels` in that
  mode, so Wapsi sends labels only to Vertex. ADK 2.9.1's `Gemini(client_kwargs=..., retry_options=...)` passes
  the key through to the client, and `client_kwargs` is excluded from `repr` and serialization.
- **ADK services.** ADK 2.9.1 ships `FirestoreSessionService` and `FirestoreMemoryService`
  (`google.adk.integrations.firestore`). Its service registry accepts the `firestore://` scheme that Wapsi
  registers.
- **ADK pieces run offline.** `LlmAgent`, `App(plugins=...)`, the plugin callback signatures (including
  `on_model_error_callback`), `ToolContext.state` and `user_id`, sub-agent transfer, the graph `Workflow` with
  routes, `InMemoryRunner`, `get_fast_api_app(..., trigger_sources=["pubsub"])`, the
  `/apps/{app}/trigger/pubsub` route, and `AgentLoader` on `editions/gcp/agents`.
- **Model Armor types.** The request and response types (`SanitizeUserPromptRequest`, the `filter_results` map
  keys, `invocation_result`) are the SDK's real types. The template JSON parses as `modelarmor_v1.Template`.
- **BigQuery and google-genai types.** The BigQuery parameter and load-job objects (`ArrayQueryParameter`,
  `TimePartitioning`, clustering) and the google-genai config fields (`response_json_schema`, `labels`,
  `thinking_config`) are real.
- **Adapter SQL.** It runs on SQLite through a small dialect translation, and matches `InMemoryLedger` exactly on
  the showcase world and on a sample of the demo world.
- **Deploy script.** `cloudrun.sh` bootstrap and deploy are dry-run in tests with a fake `gcloud`. The free
  profile enables exactly the listed APIs, uses no GPU flags, sets min-instances 0 and never prints the key.

Taken from current docs (checked 2026-09-23) but not run against Google Cloud:

- **Gemini API.** `gemini-3.8-flash` is the newest stable Flash model and is free of charge on the free tier
  ([models](https://ai.google.dev/gemini-api/docs/models), [pricing](https://ai.google.dev/gemini-api/docs/pricing)).
  Free-tier RPM, TPM and RPD are no longer published as a table: they are shown per project in
  [AI Studio](https://aistudio.google.com/rate-limit) ([rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)).
- **Google Cloud free tiers.** The numbers in the cost table come from the
  [free features page](https://cloud.google.com/free/docs/free-cloud-features) and
  [Cloud Run pricing](https://cloud.google.com/run/pricing). The $300 credit cannot pay for the Gemini API in AI
  Studio, and the free trial blocks GPUs and quota increases (same page).
- **Model Armor pricing.** Free up to 2M tokens a month, then $0.10 per 1M tokens
  ([Security Command Center pricing](https://cloud.google.com/security-command-center/pricing)).
- **Other paths not run.** The Gemma vLLM image and flags, RTX PRO 6000 and asia-south2 availability, the Memory
  Bank URI format, the Model Armor regional endpoint and the `modelarmor.user` role name, the BigQuery
  `WITH RECURSIVE` 500-iteration limit, the gcloud flag spellings (Artifact Registry cleanup policies, Cloud
  Storage volume mounts, budget thresholds) and IAP on Cloud Run.

## Open items

- **Memory Bank needs `google-cloud-aiplatform`.** Neither `VertexAiMemoryBankService` nor `agentengine://`
  sessions work without it, and the `gcp` extra in `pyproject.toml` does not pull it in. Until it is added, a
  configured `WAPSI_MEMORY_BANK_ID` logs a warning and falls back to `WAPSI_ADK_STORE`.
- **The Firestore ADK store has not been run against Firestore.** `ADK_STORE=firestore` uses ADK's own
  Firestore services. Memory search filters on app, user and keyword, and Firestore may ask for a composite
  index on first use; the error message links to it.
- **`ui/app.py` has no data-source hook.** `ui_app.py` works around this by seeding
  `st.session_state["_wapsi_ds"]`; a `WAPSI_DATA_SOURCE` switch in `ui/app.py` would be cleaner.
- **Data residency of the agent conversation.** Tool results shown to the agent include (synthetic) victim
  names. The Gemini API free tier has no region control and may use prompts to improve Google products. For a
  bank deployment, use `WAPSI_GEMINI_BACKEND=vertex` with `WAPSI_LOCATION=asia-south1`, or a private Gemma tier,
  and confirm with the bank.
