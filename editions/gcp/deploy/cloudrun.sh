#!/usr/bin/env bash
# Wapsi, Google Cloud edition: provision and deploy to Cloud Run.
#
# NOT run by CI and never run automatically. Read it, set the variables, then run one step at a time.
#
# PROFILE=free (default): a Google Cloud FREE TRIAL project. Gemini through the Gemini API with an AI Studio key
#   (free tier), BigQuery + Firestore + Cloud Run + Secret Manager inside their free tiers, no GPU, no Memory Bank,
#   Model Armor only if MODEL_ARMOR_TEMPLATE is set, a 5 USD budget alert, scale to zero.
# PROFILE=paid: billing upgraded. Gemini on Agent Platform (Vertex AI), Model Armor on by default, the intake
#   bucket, 50 USD budget; the optional Gemma GPU tier is deployed separately (gemma_vllm_service.sh).
#
#   export PROJECT_ID=wapsi-demo REGION=asia-south1          # PROFILE defaults to free
#   bash editions/gcp/deploy/cloudrun.sh bootstrap   # APIs, service accounts, IAM, data stores, budget alert
#   bash editions/gcp/deploy/cloudrun.sh gemini-key  # prints the command YOU run to store the Gemini API key
#   bash editions/gcp/deploy/cloudrun.sh data        # BigQuery tables + load the synthetic demo world
#   bash editions/gcp/deploy/cloudrun.sh build       # Cloud Build -> Artifact Registry
#   bash editions/gcp/deploy/cloudrun.sh deploy      # API + UI Cloud Run services (min-instances 0)
#   bash editions/gcp/deploy/cloudrun.sh wire        # optional: Pub/Sub, intake bucket notifications, Scheduler
#   bash editions/gcp/deploy/cloudrun.sh judging-on  # min-instances=1 (no cold starts; costs money while on)
#   bash editions/gcp/deploy/cloudrun.sh judging-off # back to scale-to-zero
#
# The Gemini API key is NEVER read, printed or embedded by this script: you store it in Secret Manager yourself
# (see `gemini-key`) and Cloud Run mounts it as the GEMINI_API_KEY env var at runtime.
# Gemma 4 on Cloud Run GPU is an optional paid add-on: editions/gcp/deploy/gemma_vllm_service.sh
# Least-privilege roles are explained in editions/gcp/deploy/iam.md.
set -euo pipefail

PROFILE="${PROFILE:-free}"
case "${PROFILE}" in free | paid) ;; *) echo "PROFILE must be free or paid" >&2; exit 1 ;; esac
paid() { [[ "${PROFILE}" == "paid" ]]; }

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-south1}"                 # Cloud Run, BigQuery, Firestore, Model Armor, GCS, Pub/Sub
GEMINI_BACKEND="${GEMINI_BACKEND:-$(paid && echo vertex || echo aistudio)}"  # aistudio = Gemini API key
GEMINI_LOCATION="${GEMINI_LOCATION:-global}"    # vertex only (asia-south1 for in-region processing)
GEMINI_SECRET="${GEMINI_SECRET:-wapsi-gemini-api-key}"  # Secret Manager secret holding the AI Studio key
MODEL="${MODEL:-gemini-3.8-flash}"              # free-tier Flash model on the Gemini API
LEDGER="${LEDGER:-bigquery}"                    # bigquery | parquet (world baked into the image, or WORLD_BUCKET)
WORLD_BUCKET="${WORLD_BUCKET:-}"                # parquet only: bucket holding world/*.parquet, mounted read-only
ADK_STORE="${ADK_STORE:-memory}"                # memory | firestore (durable ADK sessions + memory, free tier)
DATASET="${DATASET:-wapsi}"
REPO="${REPO:-wapsi}"
TAG="${TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/wapsi-gcp:${TAG}}"
API_SERVICE="${API_SERVICE:-wapsi-api}"
UI_SERVICE="${UI_SERVICE:-wapsi-ui}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"             # 0 = scale to zero; judging-on sets 1
MAX_INSTANCES="${MAX_INSTANCES:-3}"             # hard cap on concurrent cost
# in-memory ADK sessions live in one instance, so keep the API on one instance unless sessions are in Firestore
API_MAX_INSTANCES="${API_MAX_INSTANCES:-$([[ "${ADK_STORE}" == "memory" ]] && echo 1 || echo "${MAX_INSTANCES}")}"
MODEL_ARMOR_TEMPLATE="${MODEL_ARMOR_TEMPLATE-$(paid && echo wapsi-intake || true)}"  # empty = RulesGuard only
MEMORY_BANK_ID="${MEMORY_BANK_ID:-}"            # paid only: Agent Runtime (reasoning engine) id
GEMMA_URL="${GEMMA_URL:-}"                      # paid only: https://wapsi-gemma-....run.app (GPU tier)
BUDGET_USD="${BUDGET_USD:-$(paid && echo 50 || echo 5)}"
TRACE="${TRACE:-$(paid && echo 1 || echo 0)}"   # 1 = export ADK OpenTelemetry to Cloud Trace/Monitoring/Logging
INTAKE_BUCKET="${INTAKE_BUCKET:-${PROJECT_ID}-wapsi-intake}"
SUPERVISORS="${SUPERVISORS:-}"                  # comma-separated emails (IAP) that may approve
PUBLIC_UI="${PUBLIC_UI:-1}"                     # the judged live URL; synthetic data only
PUBLIC_API="${PUBLIC_API:-0}"                   # keep the agent API private unless a judge needs the ADK dev UI
TRUST_ROLE_HEADER="${TRUST_ROLE_HEADER:-0}"     # 1 only for a public judging demo without IAP (synthetic data)

API_SA="wapsi-api@${PROJECT_ID}.iam.gserviceaccount.com"
UI_SA="wapsi-ui@${PROJECT_ID}.iam.gserviceaccount.com"
PUSH_SA="wapsi-pubsub-push@${PROJECT_ID}.iam.gserviceaccount.com"
BUILD_SA="wapsi-build@${PROJECT_ID}.iam.gserviceaccount.com"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../../.." && pwd)"

log() { printf '\n== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
gc() { gcloud --project "${PROJECT_ID}" --quiet "$@"; }
svc_url() { gc run services describe "$1" --region "${REGION}" --format='value(status.url)'; }
json_file() { local f; f="$(mktemp)"; printf '%s' "$1" > "${f}"; echo "${f}"; }  # not <(...): Windows gcloud
bind_project() { gc projects add-iam-policy-binding "${PROJECT_ID}" --member="serviceAccount:$1" --role="$2" \
  --condition=None >/dev/null; }

check_profile() {
  case "${GEMINI_BACKEND}" in aistudio | vertex) ;; *) die "GEMINI_BACKEND must be aistudio or vertex" ;; esac
  case "${LEDGER}" in bigquery | parquet) ;; *) die "LEDGER must be bigquery or parquet" ;; esac
  case "${ADK_STORE}" in memory | firestore) ;; *) die "ADK_STORE must be memory or firestore" ;; esac
  if ! paid; then
    [[ -z "${GEMMA_URL}" ]] || die "GEMMA_URL needs PROFILE=paid: Cloud Run GPUs are blocked on the free trial"
    [[ -z "${MEMORY_BANK_ID}" ]] || die "MEMORY_BANK_ID needs PROFILE=paid (Memory Bank is billed); use ADK_STORE=firestore"
    [[ "${GEMINI_BACKEND}" == "aistudio" ]] || \
      echo "NOTE: GEMINI_BACKEND=vertex is billed per token (the trial credit may cover it; it is not free tier)."
  fi
}

apis() {  # the APIs this profile needs, nothing more
  local list=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
    firestore.googleapis.com secretmanager.googleapis.com bigquery.googleapis.com logging.googleapis.com
    iam.googleapis.com billingbudgets.googleapis.com)  # iam: service accounts; billingbudgets: the budget alert
  [[ -n "${MODEL_ARMOR_TEMPLATE}" ]] && list+=(modelarmor.googleapis.com)
  [[ "${GEMINI_BACKEND}" == "vertex" ]] && list+=(aiplatform.googleapis.com)
  [[ "${TRACE}" == "1" ]] && list+=(cloudtrace.googleapis.com telemetry.googleapis.com monitoring.googleapis.com)
  if paid; then list+=(storage.googleapis.com pubsub.googleapis.com cloudscheduler.googleapis.com iap.googleapis.com); fi
  printf '%s\n' "${list[@]}"
}

gemini_key_help() {
  # Quoted heredoc: nothing below is expanded except the @PLACEHOLDERS@ filled in by sed. The key itself is
  # typed by you, into your own shell; this script never sees it.
  sed -e "s|@SECRET@|${GEMINI_SECRET}|g" -e "s|@PROJECT@|${PROJECT_ID}|g" -e "s|@REGION@|${REGION}|g" <<'EOF'

Gemini API key (you do this yourself; the script never reads or prints the key):
  1. Open https://aistudio.google.com/api-keys, click "Create API key" and pick project @PROJECT@
     (keep it on the Free tier: do not click "Set up billing" in AI Studio).
  2. In your own terminal, paste the key at the silent prompt, then store it in Secret Manager:
       read -rs GEMINI_API_KEY && export GEMINI_API_KEY
       printf '%s' "$GEMINI_API_KEY" | gcloud secrets create @SECRET@ --project @PROJECT@ \
         --replication-policy=user-managed --locations=@REGION@ --labels=app=wapsi --data-file=-
     To rotate it later:
       printf '%s' "$GEMINI_API_KEY" | gcloud secrets versions add @SECRET@ --project @PROJECT@ --data-file=-
  3. Re-run: bash editions/gcp/deploy/cloudrun.sh deploy
EOF
}

ensure_gemini_secret() {  # aistudio: the secret must exist; grant the runtime service accounts read access
  [[ "${GEMINI_BACKEND}" == "aistudio" ]] || return 0
  if ! gc secrets describe "${GEMINI_SECRET}" >/dev/null 2>&1; then
    gemini_key_help
    die "Secret ${GEMINI_SECRET} does not exist yet; create it as shown above, then re-run deploy."
  fi
  for sa in "${API_SA}" "${UI_SA}"; do
    gc secrets add-iam-policy-binding "${GEMINI_SECRET}" --member="serviceAccount:${sa}" \
      --role=roles/secretmanager.secretAccessor >/dev/null
  done
}

intake_bucket() {  # private uploads bucket (30-day lifecycle); a few KB of storage, not in the US free tier
  log "Cloud Storage intake bucket (uploads of complaint PDFs / screenshots)"
  gcloud storage buckets describe "gs://${INTAKE_BUCKET}" >/dev/null 2>&1 || \
    gcloud storage buckets create "gs://${INTAKE_BUCKET}" --project "${PROJECT_ID}" --location "${REGION}" \
      --uniform-bucket-level-access --public-access-prevention --soft-delete-duration=7d
  local lifecycle
  lifecycle="$(json_file '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}')"
  gcloud storage buckets update "gs://${INTAKE_BUCKET}" --lifecycle-file="${lifecycle}"
  rm -f "${lifecycle}"
  gcloud storage buckets add-iam-policy-binding "gs://${INTAKE_BUCKET}" \
    --member="serviceAccount:${API_SA}" --role=roles/storage.objectViewer >/dev/null
  if [[ "${GEMINI_BACKEND}" == "vertex" ]]; then  # Gemini on Agent Platform reads gs:// inputs itself
    PROJECT_NUMBER="$(gc projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
    gcloud storage buckets add-iam-policy-binding "gs://${INTAKE_BUCKET}" \
      --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com" \
      --role=roles/storage.objectViewer >/dev/null
  fi
}

budget() {
  log "Budget alert (${BUDGET_USD} USD; e-mails at 50/90/100% of actual and 100% of forecast spend)"
  local account="${BILLING_ACCOUNT:-}"
  if [[ -z "${account}" ]]; then
    account="$(gc billing projects describe "${PROJECT_ID}" --format='value(billingAccountName)' 2>/dev/null || true)"
    account="${account#billingAccounts/}"
  fi
  if [[ -z "${account}" ]]; then
    echo "No billing account linked to ${PROJECT_ID}: link the free-trial billing account, or create the budget"
    echo "in the console (Billing > Budgets & alerts)."
    return 0
  fi
  if gcloud billing budgets list --billing-account="${account}" --format='value(displayName)' 2>/dev/null | \
      grep -x "wapsi-${PROJECT_ID}" >/dev/null; then
    echo "budget wapsi-${PROJECT_ID} already exists"
    return 0
  fi
  # a budget only alerts: it does not stop spending (the free trial itself never charges your card)
  gcloud billing budgets create --billing-account="${account}" --display-name="wapsi-${PROJECT_ID}" \
    --budget-amount="${BUDGET_USD}USD" --filter-projects="projects/${PROJECT_ID}" \
    --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
    --threshold-rule=percent=1.0,basis=forecasted-spend || \
    echo "Could not create the budget (needs Billing Account Administrator); create it in the console."
}

bootstrap() {
  check_profile
  log "APIs (${PROFILE} profile)"
  # shellcheck disable=SC2046
  gc services enable $(apis)

  log "Artifact Registry (keep the 2 newest images: the free tier is 0.5 GB)"
  gc artifacts repositories describe "${REPO}" --location "${REGION}" >/dev/null 2>&1 || \
    gc artifacts repositories create "${REPO}" --location "${REGION}" --repository-format docker \
      --description "Wapsi images"
  local policy
  policy="$(json_file '[{"name":"keep-2","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":2}},
    {"name":"delete-rest","action":{"type":"Delete"},"condition":{"tagState":"any","olderThan":"1d"}}]')"
  gc artifacts repositories set-cleanup-policies "${REPO}" --location "${REGION}" --no-dry-run \
    --policy="${policy}" >/dev/null || echo "Set an Artifact Registry cleanup policy in the console (0.5 GB free)."
  rm -f "${policy}"

  log "Service accounts (one per workload; see iam.md)"
  for sa in wapsi-api wapsi-ui wapsi-build; do
    gc iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 || \
      gc iam service-accounts create "${sa}" --display-name "Wapsi ${sa#wapsi-}"
  done
  local runtime_roles=(roles/bigquery.jobUser roles/datastore.user roles/logging.logWriter)
  [[ "${GEMINI_BACKEND}" == "vertex" ]] && runtime_roles+=(roles/aiplatform.user)
  [[ -n "${MODEL_ARMOR_TEMPLATE}" ]] && runtime_roles+=(roles/modelarmor.user)
  for role in "${runtime_roles[@]}"; do
    bind_project "${API_SA}" "${role}"
    bind_project "${UI_SA}" "${role}"
  done
  if [[ "${TRACE}" == "1" ]]; then
    for role in roles/cloudtrace.agent roles/monitoring.metricWriter roles/telemetry.tracesWriter; do
      bind_project "${API_SA}" "${role}" || echo "could not grant ${role} (check the role name in iam.md)"
    done
  fi
  for role in roles/cloudbuild.builds.builder roles/artifactregistry.writer roles/logging.logWriter; do
    bind_project "${BUILD_SA}" "${role}"
  done

  if [[ "${LEDGER}" == "bigquery" ]]; then
    log "BigQuery dataset (read-only for the app; the demo world is ~2 MB, far inside the free tier)"
    bq --project_id "${PROJECT_ID}" --location "${REGION}" show "${DATASET}" >/dev/null 2>&1 || \
      bq --project_id "${PROJECT_ID}" --location "${REGION}" mk --dataset \
        --description "Wapsi synthetic ledger" --label app:wapsi "${PROJECT_ID}:${DATASET}"
    # dataset-scoped read access (BigQuery DCL), jobs are billed through roles/bigquery.jobUser above
    bq --project_id "${PROJECT_ID}" query --use_legacy_sql=false --location "${REGION}" \
      "GRANT \`roles/bigquery.dataViewer\` ON SCHEMA \`${PROJECT_ID}.${DATASET}\`
       TO \"serviceAccount:${API_SA}\", \"serviceAccount:${UI_SA}\""
  fi

  log "Firestore (Native mode, the (default) database: the only one with a free tier) for cases and audit"
  gc firestore databases describe --database="(default)" >/dev/null 2>&1 || \
    gc firestore databases create --database="(default)" --location="${REGION}" --type=firestore-native

  log "Secret Manager: HMAC key for signed role grants (random, generated here, never printed)"
  if ! gc secrets describe wapsi-role-signing-key >/dev/null 2>&1; then
    head -c 48 /dev/urandom | base64 | tr -d '\n' | \
      gc secrets create wapsi-role-signing-key --replication-policy=user-managed --locations="${REGION}" \
        --data-file=- --labels=app=wapsi >/dev/null
  fi
  for sa in "${API_SA}" "${UI_SA}"; do
    gc secrets add-iam-policy-binding wapsi-role-signing-key --member="serviceAccount:${sa}" \
      --role=roles/secretmanager.secretAccessor >/dev/null
  done
  if [[ "${GEMINI_BACKEND}" == "aistudio" ]] && ! gc secrets describe "${GEMINI_SECRET}" >/dev/null 2>&1; then
    gemini_key_help
  fi

  if [[ -n "${MODEL_ARMOR_TEMPLATE}" ]]; then
    log "Model Armor template ${MODEL_ARMOR_TEMPLATE} (${REGION}; free up to 2M tokens a month, then billed)"
    # gcloud model-armor needs the regional endpoint; set it for this call only
    if ! CLOUDSDK_API_ENDPOINT_OVERRIDES_MODELARMOR="https://modelarmor.${REGION}.rep.googleapis.com/" \
         gc model-armor templates describe "${MODEL_ARMOR_TEMPLATE}" --location="${REGION}" >/dev/null 2>&1; then
      curl -fsS -X POST \
        -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "Content-Type: application/json" \
        "https://modelarmor.${REGION}.rep.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/templates?templateId=${MODEL_ARMOR_TEMPLATE}" \
        --data-binary "@${ROOT}/editions/gcp/deploy/model_armor_template.json"
    fi
  fi

  if paid; then intake_bucket; fi
  budget
}

data() {
  check_profile
  WORLD="${WORLD:-${ROOT}/data/generated/demo}"
  [[ -f "${WORLD}/transactions.parquet" ]] || \
    (cd "${ROOT}" && uv run python -m wapsi.synth --size demo --seed 7 --out "${WORLD}")
  if [[ "${LEDGER}" == "parquet" ]]; then
    if [[ -n "${WORLD_BUCKET}" ]]; then
      log "Parquet world -> gs://${WORLD_BUCKET}/world/ (mounted read-only into Cloud Run)"
      gcloud storage buckets describe "gs://${WORLD_BUCKET}" >/dev/null 2>&1 || \
        gcloud storage buckets create "gs://${WORLD_BUCKET}" --project "${PROJECT_ID}" --location "${REGION}" \
          --uniform-bucket-level-access --public-access-prevention
      gcloud storage cp "${WORLD}"/*.parquet "gs://${WORLD_BUCKET}/world/"
      for sa in "${API_SA}" "${UI_SA}"; do
        gcloud storage buckets add-iam-policy-binding "gs://${WORLD_BUCKET}" --member="serviceAccount:${sa}" \
          --role=roles/storage.objectViewer >/dev/null
      done
    else
      echo "LEDGER=parquet: the demo world is baked into the image at build time (BAKE_WORLD=1); nothing to load."
    fi
    return 0
  fi
  log "BigQuery tables (partitioned + clustered) and the synthetic demo world (load jobs are free)"
  sed "s/PROJECT_ID/${PROJECT_ID}/g" "${ROOT}/editions/gcp/deploy/bigquery_setup.sql" | \
    awk '/^-- Optional pre-filter/{exit} {print}' | \
    bq --project_id "${PROJECT_ID}" query --use_legacy_sql=false --location "${REGION}"
  (cd "${ROOT}" && uv run python -m wapsi.adapters.bigquery_ledger load --world "${WORLD}" \
    --project "${PROJECT_ID}" --dataset "${DATASET}" --location "${REGION}")
}

build() {
  log "Cloud Build -> ${IMAGE}"
  local machine=()
  # free profile: the default e2-standard-2 machine (2,500 free build-minutes a month); paid: a faster machine
  if paid; then machine=(--machine-type=e2-highcpu-8); fi
  gc builds submit "${ROOT}" --region "${REGION}" --config "${ROOT}/editions/gcp/deploy/cloudbuild.yaml" \
    --substitutions "_IMAGE=${IMAGE},_BAKE_WORLD=${BAKE_WORLD:-1}" \
    --service-account "projects/${PROJECT_ID}/serviceAccounts/${BUILD_SA}" ${machine[@]+"${machine[@]}"}
  echo "${IMAGE}" > "${ROOT}/editions/gcp/deploy/.last_image"
}

world_path() {
  if [[ "${LEDGER}" != "parquet" ]]; then echo ""
  elif [[ -n "${WORLD_BUCKET}" ]]; then echo "/mnt/world/world"
  else echo "/app/data/world"; fi
}

env_file() {  # YAML env file: values may contain commas (WAPSI_SUPERVISORS). No secrets in here.
  local f
  f="$(mktemp)"
  cat > "${f}" <<EOF
WAPSI_DEMO_MODE: "0"
WAPSI_PROJECT: "${PROJECT_ID}"
WAPSI_REGION: "${REGION}"
WAPSI_GEMINI_BACKEND: "${GEMINI_BACKEND}"
WAPSI_LOCATION: "${GEMINI_LOCATION}"
WAPSI_MODEL: "${MODEL}"
WAPSI_LEDGER: "${LEDGER}"
WAPSI_WORLD: "$(world_path)"
WAPSI_BQ_DATASET: "${DATASET}"
WAPSI_ADK_STORE: "${ADK_STORE}"
WAPSI_MODEL_ARMOR_TEMPLATE: "${MODEL_ARMOR_TEMPLATE}"
WAPSI_MEMORY_BANK_ID: "${MEMORY_BANK_ID}"
WAPSI_PRIVATE_LLM_URL: "${GEMMA_URL}"
WAPSI_PRIVATE_LLM_MODEL: "${GEMMA_MODEL:-google/gemma-4-E4B-it}"
WAPSI_INTAKE_BUCKETS: "${INTAKE_BUCKET}"
WAPSI_SUPERVISORS: "${SUPERVISORS}"
WAPSI_TRUST_ROLE_HEADER: "${TRUST_ROLE_HEADER}"
WAPSI_ADK_WEB: "${PUBLIC_API}"
WAPSI_OTEL_TO_CLOUD: "${TRACE}"
GOOGLE_CLOUD_PROJECT: "${PROJECT_ID}"
EOF
  if [[ "${GEMINI_BACKEND}" == "vertex" ]]; then
    printf 'GOOGLE_CLOUD_LOCATION: "%s"\nGOOGLE_GENAI_USE_VERTEXAI: "TRUE"\n' "${GEMINI_LOCATION}" >> "${f}"
  fi
  echo "${f}"
}

deploy() {
  check_profile
  ensure_gemini_secret
  local image envs auth_api auth_ui secrets volume=()
  image="$(cat "${ROOT}/editions/gcp/deploy/.last_image" 2>/dev/null || echo "${IMAGE}")"
  envs="$(env_file)"
  auth_api=$([[ "${PUBLIC_API}" == "1" ]] && echo --allow-unauthenticated || echo --no-allow-unauthenticated)
  auth_ui=$([[ "${PUBLIC_UI}" == "1" ]] && echo --allow-unauthenticated || echo --no-allow-unauthenticated)
  # secrets are mounted by reference (name:version); their values never pass through this script
  secrets="WAPSI_ROLE_SIGNING_KEY=wapsi-role-signing-key:latest"
  [[ "${GEMINI_BACKEND}" == "aistudio" ]] && secrets+=",GEMINI_API_KEY=${GEMINI_SECRET}:latest"
  if [[ "${LEDGER}" == "parquet" && -n "${WORLD_BUCKET}" ]]; then
    volume=(--add-volume "name=world,type=cloud-storage,bucket=${WORLD_BUCKET},readonly=true"
      --add-volume-mount "volume=world,mount-path=/mnt/world")
  fi

  log "API service ${API_SERVICE} (ADK agents + Pub/Sub trigger), min-instances ${MIN_INSTANCES}"
  gc beta run deploy "${API_SERVICE}" --image "${image}" --region "${REGION}" \
    --service-account "${API_SA}" --env-vars-file "${envs}" --set-secrets "${secrets}" \
    --min-instances "${MIN_INSTANCES}" --max-instances "${API_MAX_INSTANCES}" \
    --cpu 2 --memory 2Gi --concurrency 20 --timeout 300 --cpu-boost --execution-environment gen2 \
    --startup-probe "httpGet.path=/healthz,httpGet.port=8080,periodSeconds=5,failureThreshold=24,timeoutSeconds=3" \
    --liveness-probe "httpGet.path=/healthz,httpGet.port=8080,periodSeconds=30,failureThreshold=3,timeoutSeconds=5" \
    --labels app=wapsi,component=api,edition=gcp ${volume[@]+"${volume[@]}"} ${auth_api}

  log "UI service ${UI_SERVICE} (Streamlit on the same case store), min-instances ${MIN_INSTANCES}"
  gc beta run deploy "${UI_SERVICE}" --image "${image}" --region "${REGION}" \
    --service-account "${UI_SA}" --env-vars-file "${envs}" --set-secrets "${secrets}" \
    --command streamlit \
    --args "run,editions/gcp/ui_app.py,--server.port=8080,--server.address=0.0.0.0,--server.headless=true,--server.enableXsrfProtection=true" \
    --min-instances "${MIN_INSTANCES}" --max-instances "${MAX_INSTANCES}" \
    --cpu 1 --memory 2Gi --concurrency 40 --timeout 3600 --session-affinity --cpu-boost \
    --startup-probe "httpGet.path=/_stcore/health,httpGet.port=8080,periodSeconds=5,failureThreshold=24,timeoutSeconds=3" \
    --labels app=wapsi,component=ui,edition=gcp ${volume[@]+"${volume[@]}"} ${auth_ui}
  rm -f "${envs}"

  if [[ -n "${GEMMA_URL}" ]]; then
    log "Allow the API to call the private Gemma service (paid add-on)"
    gc run services add-iam-policy-binding "${GEMMA_SERVICE:-wapsi-gemma}" --region "${GEMMA_REGION:-${REGION}}" \
      --member="serviceAccount:${API_SA}" --role=roles/run.invoker
  fi
  echo "API: $(svc_url "${API_SERVICE}")"
  echo "UI:  $(svc_url "${UI_SERVICE}")"
}

wire() {  # optional event path; Pub/Sub (10 GiB/month) and Scheduler (3 jobs) stay inside their free tiers
  check_profile
  local api
  if ! paid; then
    log "APIs for the event path"
    gc services enable storage.googleapis.com pubsub.googleapis.com cloudscheduler.googleapis.com
    intake_bucket
  fi
  gc iam service-accounts describe "${PUSH_SA}" >/dev/null 2>&1 || \
    gc iam service-accounts create wapsi-pubsub-push --display-name "Wapsi pubsub-push"
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
  gcloud storage buckets notifications list "gs://${INTAKE_BUCKET}" --format='value(topic)' | grep wapsi-uploads >/dev/null || \
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
  if [[ "$1" != "0" ]]; then
    echo "COST: one idle warm instance of each service (API 2 vCPU/2 GiB + UI 1 vCPU/2 GiB) is billed at the"
    echo "Cloud Run idle rate, roughly 1.5 USD a day in asia-south1 beyond the free tier (paid from the trial"
    echo "credit). Run 'judging-off' as soon as judging ends."
  fi
  for s in "${API_SERVICE}" "${UI_SERVICE}"; do
    gc run services update "${s}" --region "${REGION}" --min-instances "$1"
  done
}

case "${1:-help}" in
  bootstrap) bootstrap ;;
  gemini-key) gemini_key_help ;;
  apis) apis ;;
  data) data ;;
  build) build ;;
  deploy) deploy ;;
  wire) wire ;;
  all) bootstrap; data; build; deploy ;;
  judging-on) judging 1 ;;
  judging-off) judging 0 ;;
  *) sed -n '2,25p' "${BASH_SOURCE[0]:-$0}"; exit 1 ;;
esac
