#!/usr/bin/env bash
# Wapsi, Google Cloud edition: ONE command from a project id to a warm, public, seeded Cloud Run demo.
# Twin of deploy.ps1 (Windows PowerShell); both print the same plan in --dry-run. Guide: editions/gcp/DEPLOY.md
#
#   bash editions/gcp/deploy/deploy.sh --project-id <PROJECT_ID>            # deploy (asia-south1, Gemini API key)
#   bash editions/gcp/deploy/deploy.sh --project-id <PROJECT_ID> --dry-run  # print every command, run nothing
#
# Options: --region asia-south1 | --gemini-backend aistudio|vertex | --model gemini-3.8-flash | --ledger bigquery|parquet
#   --min-instances 1 (0 = scale to zero) | --budget-usd 50 (0 = no budget alert) | --private-api | --update-key
#   --demo-now <ISO time> ("" = wall clock) | --tag <image tag> | --skip a,b | --only a,b | --teardown
# Steps (each idempotent, each skippable): preflight project apis budget registry iam secrets firestore bigquery
#   build deploy smoke
#
# Secrets: GEMINI_API_KEY (or GOOGLE_API_KEY) is read from YOUR environment and piped to Secret Manager on stdin;
# it is never printed, logged, written to disk or put on a command line. Cloud Run mounts it by reference.
# You run the two sign-ins yourself first: gcloud auth login ; gcloud auth application-default login
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-asia-south1}"
GEMINI_BACKEND="${GEMINI_BACKEND:-aistudio}"
MODEL="${MODEL:-gemini-3.8-flash}"
GEMINI_LOCATION="${GEMINI_LOCATION:-global}"
LEDGER="${LEDGER:-bigquery}"
MIN_INSTANCES="${MIN_INSTANCES:-1}"
BUDGET_USD="${BUDGET_USD:-50}"
DEMO_NOW="${DEMO_NOW-2026-09-16T11:00:00+05:30}"
TAG="${TAG:-}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-300}"
DRY_RUN=0 PRIVATE_API=0 UPDATE_KEY=0 TEARDOWN=0 SKIP="" ONLY=""

usage() { sed -n '2,18p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  arg="$1"; shift
  if [[ "${arg}" == --*=* ]]; then set -- "${arg#*=}" "$@"; arg="${arg%%=*}"; fi
  case "${arg}" in
    --project-id | --project) PROJECT_ID="${1:?}"; shift ;;
    --region) REGION="${1:?}"; shift ;;
    --gemini-backend) GEMINI_BACKEND="${1:?}"; shift ;;
    --model) MODEL="${1:?}"; shift ;;
    --gemini-location) GEMINI_LOCATION="${1:?}"; shift ;;
    --ledger) LEDGER="${1:?}"; shift ;;
    --min-instances) MIN_INSTANCES="${1:?}"; shift ;;
    --budget-usd) BUDGET_USD="${1:?}"; shift ;;
    --demo-now) DEMO_NOW="${1-}"; shift ;;
    --tag) TAG="${1:?}"; shift ;;
    --smoke-timeout) SMOKE_TIMEOUT="${1:?}"; shift ;;
    --skip) SKIP="${1:?}"; shift ;;
    --only) ONLY="${1:?}"; shift ;;
    --dry-run) DRY_RUN=1 ;;
    --private-api) PRIVATE_API=1 ;;
    --update-key) UPDATE_KEY=1 ;;
    --teardown) TEARDOWN=1 ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown option ${arg} (see --help)" ;;
  esac
done

STEPS=(preflight project apis budget registry iam secrets firestore bigquery build deploy smoke)
[[ -n "${PROJECT_ID}" ]] || die "--project-id is required (the id of your Google Cloud project)"
[[ "${PROJECT_ID}" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "not a project id: ${PROJECT_ID}"
[[ "${REGION}" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || die "not a region: ${REGION}"
case "${GEMINI_BACKEND}" in aistudio | vertex) ;; *) die "--gemini-backend must be aistudio or vertex" ;; esac
case "${LEDGER}" in bigquery | parquet) ;; *) die "--ledger must be bigquery or parquet" ;; esac
[[ "${MIN_INSTANCES}" =~ ^[0-3]$ ]] || die "--min-instances must be 0..3"
[[ "${BUDGET_USD}" =~ ^[0-9]+$ ]] || die "--budget-usd must be a whole number of USD (0 = no budget alert)"
for s in ${SKIP//,/ } ${ONLY//,/ }; do
  [[ " ${STEPS[*]} " == *" ${s} "* ]] || die "unknown step ${s} (steps: ${STEPS[*]})"
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../../.." && pwd)"
cd "${ROOT}"
REPO="wapsi" DATASET="wapsi" API="wapsi-api" UI="wapsi-ui"
LAST_IMAGE_FILE="${WAPSI_LAST_IMAGE_FILE:-editions/gcp/deploy/.last_image}"
API_SA="wapsi-api@${PROJECT_ID}.iam.gserviceaccount.com"
UI_SA="wapsi-ui@${PROJECT_ID}.iam.gserviceaccount.com"
BUILD_SA="wapsi-build@${PROJECT_ID}.iam.gserviceaccount.com"
GEMINI_SECRET="wapsi-gemini-api-key" ROLE_SECRET="wapsi-role-signing-key"
IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/wapsi-gcp"
WORLD_DIR="data/generated/judging"
DOCS_INDEX="data/docs/index"
CASE="CASE-31009260000101"
KEY_SHOWN="<GEMINI_API_KEY from your environment, never printed>"
G=("--project=${PROJECT_ID}" --quiet)
API_URL="" UI_URL=""

# ---------------------------------------------------------------- printing and running
SAFE='^[A-Za-z0-9_./:=,@%+()<>~-]+$'
fmt() {
  local out="" a
  for a in "$@"; do
    if [[ -n "${a}" && "${a}" =~ ${SAFE} ]]; then out+=" ${a}"; else out+=" '${a}'"; fi
  done
  printf '%s' "${out# }"
}
say() { printf '%s\n' "$*"; }
dry() { [[ "${DRY_RUN}" == "1" ]]; }
show() { local mark="$1"; shift; say "${mark} $(fmt "$@")"; }
run() { show + "$@"; dry && return 0; "$@" || die "command failed: $(fmt "$@")"; }
try() { show + "$@"; dry && return 0; "$@" || say "  WARNING: that command failed; continuing"; }
quiet() { show + "$@"; dry && return 0; "$@" >/dev/null || die "command failed: $(fmt "$@")"; }
check() { show '?' "$@"; dry && return 1; "$@" >/dev/null 2>&1; }  # exit status only; output discarded
capture() { local var="$1"; shift; show '?' "$@"; local v=""; dry || v="$("$@" 2>/dev/null || true)"; printf -v "${var}" '%s' "${v}"; }
bind() {  # IAM bindings right after a service account is created can need a few seconds to propagate
  show + "$@"; dry && return 0
  local i
  for i in 1 2 3 4 5; do "$@" >/dev/null 2>&1 && return 0; sleep $((i * 4)); done
  die "command failed: $(fmt "$@")"
}
run_masked() {  # run_masked <secret-path> <shown-as> cmd...: prints the placeholder, runs with the real path
  local real="$1" shown="$2"; shift 2
  local a disp=()
  for a in "$@"; do disp+=("${a//"${real}"/${shown}}"); done
  show + "${disp[@]}"
  dry && return 0
  "$@" || die "command failed: $(fmt "${disp[@]}")"
}
want() {
  local s="$1"
  [[ -n "${ONLY}" && ",${ONLY}," != *",${s},"* ]] && return 1
  [[ ",${SKIP}," == *",${s},"* ]] && return 1
  return 0
}

money() { awk -v x="$1" 'BEGIN { printf "%.2f", x }'; }
cost_lines() {
  # Cloud Run Tier 1 (asia-south1) idle min-instance list prices: 0.0000025 USD per vCPU-second and per GiB-second
  local per_day month
  per_day="$(awk -v m="${MIN_INSTANCES}" 'BEGIN { printf "%.6f", ((2 + 1) + (2 + 2)) * 0.0000025 * 86400 * m }')"
  month="$(awk -v d="${per_day}" 'BEGIN { printf "%.6f", d * 30 }')"
  say "Estimated cost (list prices; check https://cloud.google.com/run/pricing):"
  say "  Cloud Run warm instances (min-instances ${MIN_INSTANCES}): $(money "${month}") USD/month ($(money "${per_day}") USD/day), from the free-trial credit"
  say "  Gemini API free tier, BigQuery, Firestore, Secret Manager, Cloud Build, Logging at demo scale: 0.00 USD (free tiers)"
  say "  Artifact Registry: under 0.10 USD/month (the cleanup policy keeps 2 images)"
  if [[ "${MIN_INSTANCES}" != "0" ]]; then
    say "  After judging: re-run with min-instances 0 and only the deploy step (about 0 USD; cold starts of 10-20 s)."
  fi
}

N=0
step() {  # step <name> <title>
  N=$((N + 1))
  if want "$1"; then say ""; say "== [${N}/${#STEPS[@]}] $1: $2"; return 0; fi
  say ""; say "== [${N}/${#STEPS[@]}] $1: skipped"; return 1
}

key_help() {
  cat <<'EOF'
GEMINI_API_KEY is not set and the secret wapsi-gemini-api-key does not exist yet.
  1. Create a key at https://aistudio.google.com/api-keys (pick this project; stay on the Free tier).
  2. Put it in this shell without echoing it:   read -rs GEMINI_API_KEY && export GEMINI_API_KEY
  3. Re-run this command (finished steps are skipped automatically).
EOF
}

# ---------------------------------------------------------------- steps
step_preflight() {
  check gcloud --version || dry || die "gcloud CLI not found: install it (Windows: winget install -e --id Google.CloudSDK), open a new terminal, re-run"
  local account
  capture account gcloud auth list --filter=status:ACTIVE "--format=value(account)"
  [[ -n "${account}" ]] || dry || die "not signed in to gcloud: run   gcloud auth login"
  check uv --version || dry || die "uv not found: https://docs.astral.sh/uv/ (Windows: winget install -e --id astral-sh.uv)"
  if [[ "${LEDGER}" == "bigquery" ]] && want bigquery; then
    check gcloud auth application-default print-access-token || dry || \
      die "no Application Default Credentials (the BigQuery loader needs them): run   gcloud auth application-default login"
  fi
  if [[ "${GEMINI_BACKEND}" == "aistudio" ]]; then
    if [[ -n "${GEMINI_API_KEY:-}${GOOGLE_API_KEY:-}" ]]; then
      say "  GEMINI_API_KEY is set in this shell (value not shown)"
    else
      say "  GEMINI_API_KEY is not set in this shell (fine when the secret already exists)"
    fi
  fi
}

step_project() {
  run gcloud config set project "${PROJECT_ID}"
  local billing
  capture billing gcloud billing projects describe "${PROJECT_ID}" "--format=value(billingEnabled)" --quiet
  if ! dry; then
    case "$(tr '[:upper:]' '[:lower:]' <<<"${billing}")" in  # bash 3.2 (macOS) has no case-changing expansion
      true) say "  billing is enabled" ;;
      false) die "billing is not enabled on ${PROJECT_ID}: gcloud billing accounts list ; gcloud billing projects link ${PROJECT_ID} --billing-account=<ACCOUNT_ID>" ;;
      *) say "  WARNING: could not check billing; Cloud Run needs a linked (free-trial) billing account" ;;
    esac
  fi
}

step_apis() {
  local apis=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
    firestore.googleapis.com bigquery.googleapis.com logging.googleapis.com iam.googleapis.com)
  [[ "${BUDGET_USD}" != "0" ]] && apis+=(billingbudgets.googleapis.com)
  [[ "${GEMINI_BACKEND}" == "vertex" ]] && apis+=(aiplatform.googleapis.com)
  run gcloud services enable "${apis[@]}" "${G[@]}"
}

step_budget() {
  if [[ "${BUDGET_USD}" == "0" ]]; then say "  no budget alert (--budget-usd 0)"; return 0; fi
  local acct names
  capture acct gcloud billing projects describe "${PROJECT_ID}" "--format=value(billingAccountName)" --quiet
  acct="${acct#billingAccounts/}"
  dry && acct="<billing-account>"
  if [[ -z "${acct}" ]]; then
    say "  WARNING: no billing account found; create the budget in the console (Billing > Budgets & alerts)"
    return 0
  fi
  capture names gcloud billing budgets list "--billing-account=${acct}" "--format=value(displayName)" "--billing-project=${PROJECT_ID}" --quiet
  if grep -qx "wapsi-${PROJECT_ID}" <<<"${names}"; then say "  budget wapsi-${PROJECT_ID} exists"; return 0; fi
  # a budget alerts by e-mail; it never stops spending (the free trial itself never charges your card)
  try gcloud billing budgets create "--billing-account=${acct}" "--display-name=wapsi-${PROJECT_ID}" \
    "--budget-amount=${BUDGET_USD}USD" "--filter-projects=projects/${PROJECT_ID}" --threshold-rule=percent=0.5 \
    --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 --threshold-rule=percent=1.0,basis=forecasted-spend \
    "--billing-project=${PROJECT_ID}" --quiet
}

step_registry() {
  check gcloud artifacts repositories describe "${REPO}" "--location=${REGION}" "${G[@]}" || \
    run gcloud artifacts repositories create "${REPO}" --repository-format=docker "--location=${REGION}" \
      --description=Wapsi "${G[@]}"
  local f
  f="$(mktemp)"
  printf '%s' '[{"name":"keep-2","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":2}},{"name":"delete-rest","action":{"type":"Delete"},"condition":{"tagState":"any","olderThan":"1d"}}]' > "${f}"
  show + gcloud artifacts repositories set-cleanup-policies "${REPO}" "--location=${REGION}" \
    "--policy=<cleanup-policy.json>" --no-dry-run "${G[@]}"
  if ! dry; then
    gcloud artifacts repositories set-cleanup-policies "${REPO}" "--location=${REGION}" "--policy=${f}" \
      --no-dry-run "${G[@]}" >/dev/null 2>&1 || say "  WARNING: set a cleanup policy in the console (0.5 GB is free)"
  fi
  rm -f "${f}"
}

step_iam() {
  local sa role
  for sa in wapsi-api wapsi-ui wapsi-build; do
    check gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" "${G[@]}" || \
      run gcloud iam service-accounts create "${sa}" "--display-name=${sa}" "${G[@]}"
  done
  local roles=(roles/bigquery.jobUser roles/datastore.user roles/logging.logWriter)
  [[ "${GEMINI_BACKEND}" == "vertex" ]] && roles+=(roles/aiplatform.user)
  for sa in "${API_SA}" "${UI_SA}"; do
    for role in "${roles[@]}"; do
      bind gcloud projects add-iam-policy-binding "${PROJECT_ID}" "--member=serviceAccount:${sa}" "--role=${role}" \
        --condition=None --quiet
    done
  done
  for role in roles/cloudbuild.builds.builder roles/artifactregistry.writer roles/logging.logWriter; do
    bind gcloud projects add-iam-policy-binding "${PROJECT_ID}" "--member=serviceAccount:${BUILD_SA}" "--role=${role}" \
      --condition=None --quiet
  done
}

step_secrets() {
  local sa
  if [[ "${GEMINI_BACKEND}" == "aistudio" ]]; then
    local have_key=0
    [[ -n "${GEMINI_API_KEY:-}${GOOGLE_API_KEY:-}" ]] && have_key=1
    if check gcloud secrets describe "${GEMINI_SECRET}" "${G[@]}"; then
      if [[ "${UPDATE_KEY}" == "1" ]]; then
        [[ "${have_key}" == "1" ]] || die "--update-key needs GEMINI_API_KEY in this shell"
        show + gcloud secrets versions add "${GEMINI_SECRET}" "--data-file=${KEY_SHOWN}" "${G[@]}"
        printf '%s' "${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}" | \
          gcloud secrets versions add "${GEMINI_SECRET}" --data-file=- "${G[@]}" >/dev/null || \
          die "could not add a secret version"
      else
        say "  secret ${GEMINI_SECRET} exists; kept (--update-key adds a new version from GEMINI_API_KEY)"
      fi
    else
      [[ "${have_key}" == "1" ]] || dry || die "$(key_help)"
      show + gcloud secrets create "${GEMINI_SECRET}" --replication-policy=user-managed "--locations=${REGION}" \
        --labels=app=wapsi "--data-file=${KEY_SHOWN}" "${G[@]}"
      if ! dry; then
        printf '%s' "${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}" | \
          gcloud secrets create "${GEMINI_SECRET}" --replication-policy=user-managed "--locations=${REGION}" \
            --labels=app=wapsi --data-file=- "${G[@]}" >/dev/null || die "could not create the secret"
      fi
    fi
    for sa in "${API_SA}" "${UI_SA}"; do
      bind gcloud secrets add-iam-policy-binding "${GEMINI_SECRET}" "--member=serviceAccount:${sa}" \
        --role=roles/secretmanager.secretAccessor "${G[@]}"
    done
  fi
  if ! check gcloud secrets describe "${ROLE_SECRET}" "${G[@]}"; then
    show + gcloud secrets create "${ROLE_SECRET}" --replication-policy=user-managed "--locations=${REGION}" \
      --labels=app=wapsi "--data-file=<48 random bytes made here, never printed>" "${G[@]}"
    if ! dry; then
      head -c 48 /dev/urandom | base64 | tr -d '\n' | \
        gcloud secrets create "${ROLE_SECRET}" --replication-policy=user-managed "--locations=${REGION}" \
          --labels=app=wapsi --data-file=- "${G[@]}" >/dev/null || die "could not create the role-signing secret"
    fi
  fi
  for sa in "${API_SA}" "${UI_SA}"; do
    bind gcloud secrets add-iam-policy-binding "${ROLE_SECRET}" "--member=serviceAccount:${sa}" \
      --role=roles/secretmanager.secretAccessor "${G[@]}"
  done
}

step_firestore() {
  check gcloud firestore databases describe "${G[@]}" || \
    run gcloud firestore databases create "--location=${REGION}" --type=firestore-native "${G[@]}"
}

step_bigquery() {
  if [[ "${LEDGER}" != "bigquery" ]]; then say "  ledger parquet: the image carries the judging world; nothing to load"; return 0; fi
  if ! dry && [[ -f "${WORLD_DIR}/transactions.parquet" ]]; then
    say "  judging world already built: ${WORLD_DIR}"
  else
    run uv run --extra gcp python -m editions.gcp.app.seed world --out "${WORLD_DIR}"
  fi
  run uv run --extra gcp python -m wapsi.adapters.bigquery_ledger load --world "${WORLD_DIR}" --project "${PROJECT_ID}" \
    --dataset "${DATASET}" --location "${REGION}" --reader "serviceAccount:${API_SA}" --reader "serviceAccount:${UI_SA}"
}

step_build() {
  # the docs search index travels with the source (it is gitignored, never committed); the image build validates
  # it, or builds it from the public sources when it is missing (python -m wapsi.search.bake)
  if ! dry && [[ -f "${DOCS_INDEX}/chunks.jsonl" ]]; then
    say "  docs index found: ${DOCS_INDEX} (uploaded with the source; the image build validates it)"
  else
    try uv run --extra docs python -m wapsi.search.bake --fetch
  fi
  run gcloud builds submit . "--region=${REGION}" --config=editions/gcp/deploy/cloudbuild.yaml \
    "--substitutions=_IMAGE=${IMAGE},_BAKE_WORLD=1" "--service-account=projects/${PROJECT_ID}/serviceAccounts/${BUILD_SA}" \
    "${G[@]}"
  dry || printf '%s\n' "${IMAGE}" > "${LAST_IMAGE_FILE}"
}

env_yaml() {  # no secrets in here: those are mounted from Secret Manager by reference
  local world="" web="1"
  [[ "${LEDGER}" == "parquet" ]] && world="/app/data/world"
  [[ "${PRIVATE_API}" == "1" ]] && web="0"
  cat <<EOF
WAPSI_DEMO_MODE: "0"
WAPSI_PROJECT: "${PROJECT_ID}"
WAPSI_REGION: "${REGION}"
WAPSI_GEMINI_BACKEND: "${GEMINI_BACKEND}"
WAPSI_LOCATION: "${GEMINI_LOCATION}"
WAPSI_MODEL: "${MODEL}"
WAPSI_LEDGER: "${LEDGER}"
WAPSI_WORLD: "${world}"
WAPSI_BQ_DATASET: "${DATASET}"
WAPSI_ADK_STORE: "memory"
WAPSI_SEED_CASES: "showcase"
WAPSI_TRUST_ROLE_HEADER: "1"
WAPSI_ADK_WEB: "${web}"
WAPSI_OTEL_TO_CLOUD: "0"
WAPSI_NOW: "${DEMO_NOW}"
GOOGLE_CLOUD_PROJECT: "${PROJECT_ID}"
EOF
  if [[ "${GEMINI_BACKEND}" == "vertex" ]]; then
    printf 'GOOGLE_CLOUD_LOCATION: "%s"\nGOOGLE_GENAI_USE_VERTEXAI: "TRUE"\n' "${GEMINI_LOCATION}"
  fi
}

service_urls() {
  capture API_URL gcloud run services describe "${API}" "--region=${REGION}" "--format=value(status.url)" "${G[@]}"
  capture UI_URL gcloud run services describe "${UI}" "--region=${REGION}" "--format=value(status.url)" "${G[@]}"
  if dry; then API_URL="<wapsi-api-url>" UI_URL="<wapsi-ui-url>"; fi
  [[ -n "${API_URL}" && -n "${UI_URL}" ]] || die "could not read the service URLs (deploy first)"
}

step_deploy() {
  local f secrets auth_api="--allow-unauthenticated"
  f="$(mktemp)"
  env_yaml > "${f}"
  say "  env (no secrets):"
  sed 's/^/    /' "${f}"
  secrets="WAPSI_ROLE_SIGNING_KEY=${ROLE_SECRET}:latest"
  [[ "${GEMINI_BACKEND}" == "aistudio" ]] && secrets+=",GEMINI_API_KEY=${GEMINI_SECRET}:latest"
  [[ "${PRIVATE_API}" == "1" ]] && auth_api="--no-allow-unauthenticated"
  # API: ADK agents + /healthz + /demo; one instance because ADK sessions are in memory
  run_masked "${f}" "<env.yaml>" gcloud run deploy "${API}" "--image=${IMAGE}" "--region=${REGION}" \
    "--service-account=${API_SA}" "--env-vars-file=${f}" "--set-secrets=${secrets}" \
    "--min-instances=${MIN_INSTANCES}" --max-instances=1 --cpu=2 --memory=2Gi --concurrency=20 --timeout=300 \
    --cpu-boost --execution-environment=gen2 --labels=app=wapsi,component=api,edition=gcp "${auth_api}" "${G[@]}"
  # UI: the case room judges open (Streamlit on the same Firestore case store)
  run_masked "${f}" "<env.yaml>" gcloud run deploy "${UI}" "--image=${IMAGE}" "--region=${REGION}" \
    "--service-account=${UI_SA}" "--env-vars-file=${f}" "--set-secrets=${secrets}" --command=streamlit \
    "--args=run,editions/gcp/ui_app.py,--server.port=8080,--server.address=0.0.0.0,--server.headless=true" \
    "--min-instances=${MIN_INSTANCES}" --max-instances=2 --cpu=1 --memory=2Gi --concurrency=40 --timeout=3600 \
    --session-affinity --cpu-boost --labels=app=wapsi,component=ui,edition=gcp --allow-unauthenticated "${G[@]}"
  rm -f "${f}"
  service_urls
}

step_smoke() {
  [[ -n "${API_URL}" ]] || service_urls
  local h c u n fb all
  say "+ GET ${API_URL}/healthz"
  if ! dry; then
    h="$(curl -fsS --max-time 60 "${API_URL}/healthz")" || \
      die "health check failed: ${API_URL}/healthz (logs: gcloud run services logs read ${API} --region ${REGION})"
    grep -Eq '"status": *"ok"' <<<"${h}" || die "unexpected /healthz answer"
    if [[ "${GEMINI_BACKEND}" == "aistudio" ]] && ! grep -Eq '"gemini_api_key_set": *true' <<<"${h}"; then
      say "  WARNING: the API does not see GEMINI_API_KEY (check the secret wapsi-gemini-api-key)"
    fi
    if grep -Eq '"docs_fallback": *true' <<<"${h}"; then
      say "  WARNING: the image serves the citations-only search corpus (the docs index was not baked): build it here with   uv run --extra docs python -m wapsi.search.bake --fetch   then re-run with --only build,deploy,smoke"
    fi
    say "  healthy"
  fi
  # seeds the showcase cases, or resets them to their freshly seeded state: every judge starts on a fresh case
  say "+ POST ${API_URL}/demo/seed?wait_s=240&redo_fallback=1&reset=1"
  if ! dry; then
    curl -fsS -X POST --max-time "${SMOKE_TIMEOUT}" "${API_URL}/demo/seed?wait_s=240&redo_fallback=1&reset=1" \
      >/dev/null || say "  WARNING: seeding did not answer in time; it continues in the background"
  fi
  say "+ GET ${API_URL}/demo/cases/${CASE}"
  if ! dry; then
    c="$(curl -fsS --max-time 60 "${API_URL}/demo/cases/${CASE}")" || \
      die "the showcase case is not in the case store yet; re-run with --only smoke in a minute"
    n="$(grep -Eo '"drafts_verified": *[0-9]+' <<<"${c}" | grep -Eo '[0-9]+$' || true)"
    [[ "${n:-0}" -gt 0 ]] || die "the showcase case has no verified drafts"
    say "  ${CASE}: ${n} verified drafts"
    fb="$( { grep -Eo '"prose_fallback": *true' <<<"${c}" || true; } | wc -l | tr -d ' ')"
    all="$( { grep -Eo '"prose_fallback": *(true|false)' <<<"${c}" || true; } | wc -l | tr -d ' ')"
    if [[ "${fb}" -gt 0 ]]; then
      say "  WARNING: ${fb} of ${all} drafts use the deterministic fallback paragraph (Gemini was not reached): check /healthz and the API logs, fix the key or the model, redeploy, then run the smoke step again (it re-processes such cases)"
    fi
  fi
  say "+ GET ${UI_URL}/_stcore/health"
  if ! dry; then
    u="$(curl -fsS --max-time 120 "${UI_URL}/_stcore/health")" || die "the UI is not healthy: ${UI_URL}"
    [[ "${u}" == *ok* ]] || die "unexpected UI health answer"
    say "  UI healthy"
  fi
}

teardown() {
  say ""
  say "== teardown: delete the two Cloud Run services (stops the warm-instance cost)"
  try gcloud run services delete "${API}" "--region=${REGION}" "${G[@]}"
  try gcloud run services delete "${UI}" "--region=${REGION}" "${G[@]}"
  say "Kept (inside free tiers): the BigQuery dataset, Firestore, secrets, images. To remove everything:"
  say "  gcloud projects delete ${PROJECT_ID}"
}

# ---------------------------------------------------------------- main
if [[ -n "${TAG}" ]]; then IMAGE="${IMAGE_BASE}:${TAG}"
elif dry; then IMAGE="${IMAGE_BASE}:dryrun"
elif want build || [[ "${TEARDOWN}" == "1" ]]; then IMAGE="${IMAGE_BASE}:$(date -u +%Y%m%d-%H%M%S)"
elif [[ -s "${LAST_IMAGE_FILE}" && "$(head -n 1 "${LAST_IMAGE_FILE}")" == "${IMAGE_BASE}:"* ]]; then
  IMAGE="$(head -n 1 "${LAST_IMAGE_FILE}")"
else
  die "the build step is skipped and no image was built from here before: pass --tag <tag of an existing image>"
fi
say "Wapsi one-command deploy (Google Cloud)"
say "  project   ${PROJECT_ID}"
say "  region    ${REGION}"
say "  gemini    ${GEMINI_BACKEND}, model ${MODEL}"
say "  ledger    ${LEDGER} (judging world: demo seed 7 + showcase)"
say "  services  ${API} (2 vCPU, 2 GiB) + ${UI} (1 vCPU, 2 GiB), min-instances ${MIN_INSTANCES}, public, no login"
say "  image     ${IMAGE}"
if dry; then say "  mode      DRY RUN: every command is printed, nothing is executed"; else say "  mode      live"; fi
if [[ "${TEARDOWN}" == "1" ]]; then teardown; exit 0; fi
say ""
cost_lines

step preflight "tools and sign-in" && step_preflight
step project "project and billing" && step_project
step apis "enable the APIs" && step_apis
step budget "budget alert (${BUDGET_USD} USD)" && step_budget
step registry "Artifact Registry repository ${REPO} (${REGION})" && step_registry
step iam "least-privilege service accounts" && step_iam
step secrets "Secret Manager (Gemini API key, role-signing key)" && step_secrets
step firestore "Firestore (Native mode) case store" && step_firestore
step bigquery "BigQuery dataset ${DATASET} + the synthetic judging world" && step_bigquery
step build "Cloud Build image" && step_build
step deploy "Cloud Run services" && step_deploy
step smoke "smoke test" && step_smoke

say ""
if dry; then say "Dry run complete: nothing was executed."; else say "Deployed."; fi
say "  Judged URL (case room, no login): ${UI_URL:-<wapsi-ui-url>}/?page=case-room&case=${CASE}"
if [[ "${PRIVATE_API}" != "1" ]]; then say "  Agent (ADK dev UI):               ${API_URL:-<wapsi-api-url>}/dev-ui/"; fi
say "  Health:                           ${API_URL:-<wapsi-api-url>}/healthz"
say "  Showcase case (JSON):             ${API_URL:-<wapsi-api-url>}/demo/cases/${CASE}"
cost_lines
say "Tear down: bash editions/gcp/deploy/deploy.sh --project-id ${PROJECT_ID} --teardown"
