# IAM: least privilege for the Wapsi GCP edition

Every workload runs as its own service account. No workload has `roles/editor` or `roles/owner`, and no
service account keys are created. `deploy/cloudrun.sh bootstrap`, `deploy` and `wire` apply exactly the
bindings below. Role names were checked against the IAM role reference; the ones marked **[verify]** should be
confirmed in the console before the first deploy.

The **Profile** column shows when a binding is applied:

- **all**: always.
- **free**: the default `PROFILE=free` on a Google Cloud free trial, with Gemini through the Gemini API and an AI
  Studio key.
- **vertex**: `GEMINI_BACKEND=vertex`, the default in `PROFILE=paid`.
- **armor**: `MODEL_ARMOR_TEMPLATE` is set.
- **trace**: `TRACE=1`.
- **wire**: the optional event path.
- **gemma**: the paid GPU add-on.

On the free profile, the Gemini API key is the only credential that is not a Google-managed identity. You create
it in AI Studio and store it in the Secret Manager secret `wapsi-gemini-api-key` yourself. The scripts only grant
read access to it and mount it by name.

## Workload service accounts

| Service account | Used by | Roles | Scope | Profile | Why |
|---|---|---|---|---|---|
| `wapsi-api@` | Cloud Run `wapsi-api` (ADK agents, Pub/Sub trigger) | `roles/bigquery.jobUser` | project | all | run ledger queries (each capped by `maximum_bytes_billed`) |
| | | `roles/bigquery.dataViewer` | dataset `wapsi` only (DCL `GRANT ... ON SCHEMA`) | all (with `LEDGER=bigquery`) | read the synthetic ledger; no write access |
| | | `roles/datastore.user` | project | all | Firestore case store (cases, chunks, audit) and, with `ADK_STORE=firestore`, ADK sessions and memory |
| | | `roles/logging.logWriter` | project | all | structured audit logs |
| | | `roles/secretmanager.secretAccessor` | secret `wapsi-role-signing-key` only | all | HMAC key for signed role grants |
| | | `roles/secretmanager.secretAccessor` | secret `wapsi-gemini-api-key` only | free (`aistudio`) | the Gemini API key, mounted as `GEMINI_API_KEY` |
| | | `roles/aiplatform.user` | project | vertex | Gemini on Agent Platform, Memory Bank and Agent Runtime sessions |
| | | `roles/modelarmor.user` **[verify]** | project | armor | `SanitizeUserPrompt` / `SanitizeModelResponse` |
| | | `roles/cloudtrace.agent`, `roles/monitoring.metricWriter`, `roles/telemetry.tracesWriter` **[verify]** | project | trace | ADK OpenTelemetry export (`WAPSI_OTEL_TO_CLOUD=1`) |
| | | `roles/storage.objectViewer` | bucket `<project>-wapsi-intake` only | wire, or paid | read uploaded complaints (on the Gemini API, the app downloads them itself) |
| | | `roles/storage.objectViewer` | bucket `WORLD_BUCKET` only | `LEDGER=parquet` with a bucket | the read-only Parquet world mount |
| | | `roles/run.invoker` | service `wapsi-gemma` only | gemma | call the private Gemma tier (ID token) |
| `wapsi-ui@` | Cloud Run `wapsi-ui` (Streamlit) | same as `wapsi-api@` minus tracing, the intake bucket and Gemma | project / dataset / secrets | as above | the UI re-runs the pipeline and records approvals |
| `wapsi-pubsub-push@` | Pub/Sub push subscriptions (created by `wire`) | `roles/run.invoker` | service `wapsi-api` only | wire | authenticated push to `/apps/case_pipeline/trigger/pubsub` |
| `wapsi-gemma@` | Cloud Run `wapsi-gemma` (vLLM) | none | — | gemma | serves a public model; needs no Google API access |
| `wapsi-build@` | Cloud Build | `roles/cloudbuild.builds.builder`, `roles/artifactregistry.writer`, `roles/logging.logWriter` | project | all | build and push the image |

The ADK trigger route verifies the Pub/Sub OIDC token: `WAPSI_TRIGGER_AUDIENCE` is the API URL and
`WAPSI_TRIGGER_SERVICE_ACCOUNTS` is `wapsi-pubsub-push@`.

## Google-managed service agents

| Service agent | Grant | Why |
|---|---|---|
| Pub/Sub (`service-<n>@gcp-sa-pubsub`) | `roles/iam.serviceAccountTokenCreator` on `wapsi-pubsub-push@` | mint OIDC tokens for push |
| Pub/Sub | `roles/pubsub.publisher` on each `*-dlq` topic, `roles/pubsub.subscriber` on each `*-push` subscription | dead-lettering after 5 attempts |
| Vertex AI (`service-<n>@gcp-sa-aiplatform`) | `roles/storage.objectViewer` on the intake bucket (vertex only) | Gemini on Agent Platform reads `gs://` files for multimodal intake; the Gemini API path downloads the bytes with `wapsi-api@` instead |
| Cloud Storage (`service-<n>@gs-project-accounts`) | `roles/pubsub.publisher` on `wapsi-uploads` (gcloud grants it when the notification is created) | upload notifications |
| Cloud Scheduler | its default service agent role | publish `{"task": "clocks"}` to `wapsi-clocks` |

## People

| Who | Roles | Notes |
|---|---|---|
| Deployer | On a free trial, the project owner (you) already has everything. Otherwise: `roles/run.admin`, `roles/iam.serviceAccountUser` on the runtime service accounts, `roles/cloudbuild.builds.editor`, `roles/artifactregistry.admin`, `roles/secretmanager.admin`, `roles/bigquery.admin` on the dataset, `roles/datastore.owner`, and for the optional parts `roles/pubsub.editor`, `roles/cloudscheduler.admin`, `roles/modelarmor.admin` **[verify]**. The budget alert needs Billing Account Administrator or Costs Manager on the billing account | only for setup; remove after the event |
| Officers and supervisors (production) | `roles/iap.httpsResourceAccessor` on `wapsi-api` / `wapsi-ui` | IAP adds `X-Goog-Authenticated-User-Email`. The server requires the ADK `user_id` to match it and grants the supervisor role only to `WAPSI_SUPERVISORS` |
| Uploaders | `roles/storage.objectCreator` on the intake bucket | cannot read or list other uploads |
| Judges (public demo) | none; `wapsi-ui` is public and shows synthetic data only | `wapsi-api` stays private unless `PUBLIC_API=1` for the judging window |

## Role checks inside the app

- The approval tool reads the role from ADK session state, never from tool arguments. A role counts only if it
  carries an HMAC grant for the session's `user_id`, signed with the key in Secret Manager.
- `RoleStateMiddleware` strips client-supplied `user:wapsi_*` keys from every create-session, PATCH and `/run`
  body. It then writes a fresh grant based on the authenticated identity.
- `WAPSI_TRUST_ROLE_HEADER=1` accepts the `X-Wapsi-Role` header without IAP. It exists only for a public
  judging deployment on synthetic data. Never enable it with real data.
- Maker-checker is enforced twice: the guard plugin refuses `approve_draft` for non-supervisors, and so does
  the tool. `wapsi.pipeline.run.approve` then re-verifies the exact draft content before recording anything.
