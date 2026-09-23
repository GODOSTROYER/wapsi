#!/usr/bin/env bash
# Wapsi, Google Cloud edition: provision and deploy to Cloud Run.
#
# NOT run by CI and never run automatically. Read it, set the variables, then run one step at a time:
#
#   export PROJECT_ID=wapsi-aibc REGION=asia-south1 BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX
#   bash editions/gcp/deploy/cloudrun.sh bootstrap   # APIs, service accounts, IAM, secrets, data stores
#   bash editions/gcp/deploy/cloudrun.sh data        # BigQuery tables + load a generated synthetic world
#   bash editions/gcp/deploy/cloudrun.sh build       # Cloud Build -> Artifact Registry
#   bash editions/gcp/deploy/cloudrun.sh deploy      # API + UI Cloud Run services
#   bash editions/gcp/deploy/cloudrun.sh wire        # Pub/Sub push subscriptions, GCS notifications, Scheduler
#   bash editions/gcp/deploy/cloudrun.sh judging-on  # min-instances=1 (no cold starts) for the judging window
#   bash editions/gcp/deploy/cloudrun.sh judging-off # back to scale-to-zero
#
# Gemma 4 on Cloud Run GPU is deployed separately: editions/gcp/deploy/gemma_vllm_service.sh
# Least-privilege roles are explained in editions/gcp/deploy/iam.md.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-south1}"                 # Cloud Run, BigQuery, Firestore, Model Armor, GCS, Pub/Sub
GEMINI_LOCATION="${GEMINI_LOCATION:-global}"    # Gemini on Agent Platform (asia-south1 for in-region processing)
MODEL="${MODEL:-gemini-3.5-flash}"
DATASET="${DATASET:-wapsi}"
REPO="${REPO:-wapsi}"
TAG="${TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/wapsi-gcp:${TAG}}"
API_SERVICE="${API_SERVICE:-wapsi-api}"
UI_SERVICE="${UI_SERVICE:-wapsi-ui}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"             # 0 = scale to zero; judging-on sets 1
MAX_INSTANCES="${MAX_INSTANCES:-3}"             # hard cap on concurrent cost
MODEL_ARMOR_TEMPLATE="${MODEL_ARMOR_TEMPLATE:-wapsi-intake}"
MEMORY_BANK_ID="${MEMORY_BANK_ID:-}"            # Agent Runtime (reasoning engine) id, created once in the console/SDK
GEMMA_URL="${GEMMA_URL:-}"                      # https://wapsi-gemma-....run.app (optional private tier)
INTAKE_BUCKET="${INTAKE_BUCKET:-${PROJECT_ID}-wapsi-intake}"
SUPERVISORS="${SUPERVISORS:-}"                  # comma-separated emails (IAP) that may approve
PUBLIC_UI="${PUBLIC_UI:-1}"                     # the judged live URL; synthetic data only
PUBLIC_API="${PUBLIC_API:-0}"                   # keep the agent API private unless a judge needs the ADK dev UI
TRUST_ROLE_HEADER="${TRUST_ROLE_HEADER:-0}"     # 1 only for a public judging demo without IAP (synthetic data)

API_SA="wapsi-api@${PROJECT_ID}.iam.gserviceaccount.com"
UI_SA="wapsi-ui@${PROJECT_ID}.iam.gserviceaccount.com"
PUSH_SA="wapsi-pubsub-push@${PROJECT_ID}.iam.gserviceaccount.com"
BUILD_SA="wapsi-build@${PROJECT_ID}.iam.gserviceaccount.com"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

log() { printf '\n== %s\n' "$*"; }
gc() { gcloud --project "${PROJECT_ID}" --quiet "$@"; }
svc_url() { gc run services describe "$1" --region "${REGION}" --format='value(status.url)'; }
bind_project() { gc projects add-iam-policy-binding "${PROJECT_ID}" --member="serviceAccount:$1" --role="$2" \
  --condition=None >/dev/null; }

bootstrap() {
  log "APIs"
  gc services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
    aiplatform.googleapis.com bigquery.googleapis.com firestore.googleapis.com pubsub.googleapis.com \
    cloudscheduler.googleapis.com modelarmor.googleapis.com secretmanager.googleapis.com \
    storage.googleapis.com logging.googleapis.com cloudtrace.googleapis.com iap.googleapis.com \
    billingbudgets.googleapis.com

  log "Artifact Registry"
  gc artifacts repositories describe "${REPO}" --location "${REGION}" >/dev/null 2>&1 || \
    gc artifacts repositories create "${REPO}" --location "${REGION}" --repository-format docker \
      --description "Wapsi images"

  log "Service accounts (one per workload; see iam.md)"
  for sa in wapsi-api wapsi-ui wapsi-pubsub-push wapsi-build; do
    gc iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 || \
      gc iam service-accounts create "${sa}" --display-name "Wapsi ${sa#wapsi-}"
  done
  for role in roles/aiplatform.user roles/bigquery.jobUser roles/datastore.user roles/modelarmor.user \
              roles/logging.logWriter roles/cloudtrace.agent roles/monitoring.metricWriter; do
    bind_project "${API_SA}" "${role}"
  done
  for role in roles/aiplatform.user roles/bigquery.jobUser roles/datastore.user roles/modelarmor.user \
              roles/logging.logWriter; do
    bind_project "${UI_SA}" "${role}"
  done
  for role in roles/cloudbuild.builds.builder roles/artifactregistry.writer roles/logging.logWriter; do
    bind_project "${BUILD_SA}" "${role}"
  done

  log "BigQuery dataset (read-only for the app)"
  bq --project_id "${PROJECT_ID}" --location "${REGION}" show "${DATASET}" >/dev/null 2>&1 || \
    bq --project_id "${PROJECT_ID}" --location "${REGION}" mk --dataset \
      --description "Wapsi synthetic ledger" --label app:wapsi "${PROJECT_ID}:${DATASET}"
  # dataset-scoped read access (BigQuery DCL), jobs are billed through roles/bigquery.jobUser above
  bq --project_id "${PROJECT_ID}" query --use_legacy_sql=false --location "${REGION}" \
    "GRANT \`roles/bigquery.dataViewer\` ON SCHEMA \`${PROJECT_ID}.${DATASET}\`
     TO \"serviceAccount:${API_SA}\", \"serviceAccount:${UI_SA}\""

  log "Firestore (Native mode) for cases, drafts, approvals and audit"
  gc firestore databases describe --database="(default)" >/dev/null 2>&1 || \
    gc firestore databases create --database="(default)" --location="${REGION}" --type=firestore-native

  log "Secret Manager: HMAC key for signed role grants"
  if ! gc secrets describe wapsi-role-signing-key >/dev/null 2>&1; then
    head -c 48 /dev/urandom | base64 | tr -d '\n' | \
      gc secrets create wapsi-role-signing-key --replication-policy=user-managed --locations="${REGION}" \
        --data-file=- --labels=app=wapsi
  fi
  for sa in "${API_SA}" "${UI_SA}"; do
    gc secrets add-iam-policy-binding wapsi-role-signing-key --member="serviceAccount:${sa}" \
      --role=roles/secretmanager.secretAccessor >/dev/null
  done

  log "Model Armor template ${MODEL_ARMOR_TEMPLATE} (${REGION})"
  # gcloud model-armor needs the regional endpoint; set it for this call only
  if ! CLOUDSDK_API_ENDPOINT_OVERRIDES_MODELARMOR="https://modelarmor.${REGION}.rep.googleapis.com/" \
       gc model-armor templates describe "${MODEL_ARMOR_TEMPLATE}" --location="${REGION}" >/dev/null 2>&1; then
    curl -fsS -X POST \
      -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "Content-Type: application/json" \
      "https://modelarmor.${REGION}.rep.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/templates?templateId=${MODEL_ARMOR_TEMPLATE}" \
      --data-binary "@${ROOT}/editions/gcp/deploy/model_armor_template.json"
  fi

  log "Cloud Storage intake bucket (uploads of complaint PDFs / screenshots)"
  gcloud storage buckets describe "gs://${INTAKE_BUCKET}" >/dev/null 2>&1 || \
    gcloud storage buckets create "gs://${INTAKE_BUCKET}" --project "${PROJECT_ID}" --location "${REGION}" \
      --uniform-bucket-level-access --public-access-prevention --soft-delete-duration=7d
  gcloud storage buckets update "gs://${INTAKE_BUCKET}" --lifecycle-file=<(printf '%s' \
    '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}')
  gcloud storage buckets add-iam-policy-binding "gs://${INTAKE_BUCKET}" \
    --member="serviceAccount:${API_SA}" --role=roles/storage.objectViewer >/dev/null
  # Gemini reads gs:// inputs with the Vertex AI service agent
  PROJECT_NUMBER="$(gc projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
  gcloud storage buckets add-iam-policy-binding "gs://${INTAKE_BUCKET}" \
    --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com" \
    --role=roles/storage.objectViewer >/dev/null

  log "Budget guardrail (alerts at 50/90/100% of 50 USD)"
  if [[ -n "${BILLING_ACCOUNT:-}" ]]; then
    gcloud billing budgets create --billing-account="${BILLING_ACCOUNT}" --display-name="wapsi-aibc" \
      --budget-amount=50USD --filter-projects="projects/${PROJECT_ID}" \
      --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 || true
  else
    echo "BILLING_ACCOUNT not set: create the budget in the console (Billing > Budgets & alerts)."
  fi
}

data() {
  log "BigQuery tables (partitioned + clustered) and a synthetic world"
  sed "s/PROJECT_ID/${PROJECT_ID}/g" "${ROOT}/editions/gcp/deploy/bigquery_setup.sql" | \
    awk '/^-- Optional pre-filter/{exit} {print}' | \
    bq --project_id "${PROJECT_ID}" query --use_legacy_sql=false --location "${REGION}"
  WORLD="${WORLD:-${ROOT}/data/generated/demo}"
  [[ -f "${WORLD}/transactions.parquet" ]] || \
    (cd "${ROOT}" && uv run python -m wapsi.synth --size demo --seed 7 --out "${WORLD}")
  (cd "${ROOT}" && uv run python -m wapsi.adapters.bigquery_ledger load --world "${WORLD}" \
    --project "${PROJECT_ID}" --dataset "${DATASET}" --location "${REGION}")
}

build() {
  log "Cloud Build -> ${IMAGE}"
  gc builds submit "${ROOT}" --region "${REGION}" --config "${ROOT}/editions/gcp/deploy/cloudbuild.yaml" \
    --substitutions "_IMAGE=${IMAGE}" \
    --service-account "projects/${PROJECT_ID}/serviceAccounts/${BUILD_SA}"
  echo "${IMAGE}" > "${ROOT}/editions/gcp/deploy/.last_image"
}

env_file() {  # YAML env file: values may contain commas (WAPSI_SUPERVISORS)
  local f
  f="$(mktemp)"
  cat > "${f}" <<EOF
WAPSI_DEMO_MODE: "0"
WAPSI_PROJECT: "${PROJECT_ID}"
WAPSI_REGION: "${REGION}"
WAPSI_LOCATION: "${GEMINI_LOCATION}"
WAPSI_MODEL: "${MODEL}"
WAPSI_BQ_DATASET: "${DATASET}"
WAPSI_MODEL_ARMOR_TEMPLATE: "${MODEL_ARMOR_TEMPLATE}"
WAPSI_MEMORY_BANK_ID: "${MEMORY_BANK_ID}"
WAPSI_PRIVATE_LLM_URL: "${GEMMA_URL}"
WAPSI_PRIVATE_LLM_MODEL: "${GEMMA_MODEL:-google/gemma-4-E4B-it}"
WAPSI_INTAKE_BUCKETS: "${INTAKE_BUCKET}"
WAPSI_SUPERVISORS: "${SUPERVISORS}"
WAPSI_TRUST_ROLE_HEADER: "${TRUST_ROLE_HEADER}"
WAPSI_ADK_WEB: "${PUBLIC_API}"
GOOGLE_CLOUD_PROJECT: "${PROJECT_ID}"
GOOGLE_CLOUD_LOCATION: "${GEMINI_LOCATION}"
GOOGLE_GENAI_USE_VERTEXAI: "TRUE"
EOF
  echo "${f}"
}

deploy() {
  local image envs auth_api auth_ui
  image="$(cat "${ROOT}/editions/gcp/deploy/.last_image" 2>/dev/null || echo "${IMAGE}")"
  envs="$(env_file)"
  auth_api=$([[ "${PUBLIC_API}" == "1" ]] && echo --allow-unauthenticated || echo --no-allow-unauthenticated)
  auth_ui=$([[ "${PUBLIC_UI}" == "1" ]] && echo --allow-unauthenticated || echo --no-allow-unauthenticated)

  log "API service ${API_SERVICE} (ADK agents + Pub/Sub trigger)"
  gc beta run deploy "${API_SERVICE}" --image "${image}" --region "${REGION}" \
    --service-account "${API_SA}" --env-vars-file "${envs}" \
    --set-secrets "WAPSI_ROLE_SIGNING_KEY=wapsi-role-signing-key:latest" \
    --min-instances "${MIN_INSTANCES}" --max-instances "${MAX_INSTANCES}" \
    --cpu 2 --memory 2Gi --concurrency 20 --timeout 300 --cpu-boost --execution-environment gen2 \
    --startup-probe "httpGet.path=/healthz,httpGet.port=8080,periodSeconds=5,failureThreshold=24,timeoutSeconds=3" \
    --liveness-probe "httpGet.path=/healthz,httpGet.port=8080,periodSeconds=30,failureThreshold=3,timeoutSeconds=5" \
    --labels app=wapsi,component=api,event=aibc ${auth_api}

  log "UI service ${UI_SERVICE} (Streamlit on the same case store)"
  gc beta run deploy "${UI_SERVICE}" --image "${image}" --region "${REGION}" \
    --service-account "${UI_SA}" --env-vars-file "${envs}" \
    --set-secrets "WAPSI_ROLE_SIGNING_KEY=wapsi-role-signing-key:latest" \
    --command streamlit \
    --args "run,editions/gcp/ui_app.py,--server.port=8080,--server.address=0.0.0.0,--server.headless=true,--server.enableXsrfProtection=true" \
    --min-instances "${MIN_INSTANCES}" --max-instances "${MAX_INSTANCES}" \
    --cpu 1 --memory 2Gi --concurrency 40 --timeout 3600 --session-affinity --cpu-boost \
    --startup-probe "httpGet.path=/_stcore/health,httpGet.port=8080,periodSeconds=5,failureThreshold=24,timeoutSeconds=3" \
    --labels app=wapsi,component=ui,event=aibc ${auth_ui}
  rm -f "${envs}"

  if [[ -n "${GEMMA_URL}" ]]; then
    log "Allow the API to call the private Gemma service"
    gc run services add-iam-policy-binding "${GEMMA_SERVICE:-wapsi-gemma}" --region "${GEMMA_REGION:-${REGION}}" \
      --member="serviceAccount:${API_SA}" --role=roles/run.invoker
  fi
  echo "API: $(svc_url "${API_SERVICE}")"
  echo "UI:  $(svc_url "${UI_SERVICE}")"
}

wire() {
  local api
  api="$(svc_url "${API_SERVICE}")"
  log "Pub/Sub -> ${api}/apps/case_pipeline/trigger/pubsub (OIDC-authenticated push)"
  gc run services add-iam-policy-binding "${API_SERVICE}" --region "${REGION}" \
    --member="serviceAccount:${PUSH_SA}" --role=roles/run.invoker >/dev/null
  PROJECT_NUMBER="$(gc projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
  gc iam service-accounts add-iam-policy-binding "${PUSH_SA}" \
    --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
    --role=roles/iam.serviceAccountTokenCreator >/dev/null
  for topic in wapsi-notices wapsi-uploads wapsi-clocks; do
    gc pubsub topics describe "${topic}" >/dev/null 2>&1 || gc pubsub topics create "${topic}" --labels=app=wapsi
    gc pubsub topics describe "${topic}-dlq" >/dev/null 2>&1 || gc pubsub topics create "${topic}-dlq"
    gc pubsub subscriptions describe "${topic}-push" >/dev/null 2>&1 || \
      gc pubsub subscriptions create "${topic}-push" --topic "${topic}" \
        --push-endpoint "${api}/apps/case_pipeline/trigger/pubsub" \
        --push-auth-service-account "${PUSH_SA}" --push-auth-token-audience "${api}" \
        --ack-deadline 300 --min-retry-delay 10s --max-retry-delay 600s \
        --dead-letter-topic "${topic}-dlq" --max-delivery-attempts 5 --labels=app=wapsi
    # dead-lettering: the Pub/Sub service agent publishes to the DLQ and acks the source subscription
    gc pubsub topics add-iam-policy-binding "${topic}-dlq" \
      --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
      --role=roles/pubsub.publisher >/dev/null
    gc pubsub subscriptions add-iam-policy-binding "${topic}-push" \
      --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
      --role=roles/pubsub.subscriber >/dev/null
  done
  # the ADK trigger route verifies the push token when these are set on the service
  gc run services update "${API_SERVICE}" --region "${REGION}" \
    --update-env-vars "WAPSI_TRIGGER_AUDIENCE=${api},WAPSI_TRIGGER_SERVICE_ACCOUNTS=${PUSH_SA}"

  log "Cloud Storage finalize notifications -> wapsi-uploads"
  gcloud storage buckets notifications list "gs://${INTAKE_BUCKET}" --format='value(topic)' | grep -q wapsi-uploads || \
    gcloud storage buckets notifications create "gs://${INTAKE_BUCKET}" --topic="projects/${PROJECT_ID}/topics/wapsi-uploads" \
      --event-types=OBJECT_FINALIZE --payload-format=json

  log "Cloud Scheduler: clock sweep twice a day on bank working days (IST)"
  gc scheduler jobs describe wapsi-clocks --location "${REGION}" >/dev/null 2>&1 || \
    gc scheduler jobs create pubsub wapsi-clocks --location "${REGION}" --schedule "0 9,15 * * 1-6" \
      --time-zone "Asia/Kolkata" --topic wapsi-clocks --message-body '{"task": "clocks"}' \
      --attributes source=cloud-scheduler
}

judging() {
  log "min-instances=$1 on ${API_SERVICE} and ${UI_SERVICE}"
  for s in "${API_SERVICE}" "${UI_SERVICE}"; do
    gc run services update "${s}" --region "${REGION}" --min-instances "$1"
  done
}

case "${1:-help}" in
  bootstrap) bootstrap ;;
  data) data ;;
  build) build ;;
  deploy) deploy ;;
  wire) wire ;;
  all) bootstrap; data; build; deploy; wire ;;
  judging-on) judging 1 ;;
  judging-off) judging 0 ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
