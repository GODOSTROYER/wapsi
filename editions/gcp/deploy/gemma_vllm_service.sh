#!/usr/bin/env bash
# Private tier: Gemma 4 served by vLLM on Cloud Run GPU (OpenAI-compatible API at <url>/v1).
# Wapsi uses it for draft prose over the case fact sheet (victim PII stays on a service we control) when
# WAPSI_PRIVATE_LLM_URL is set on the API service; WAPSI_AGENT_ON_PRIVATE=1 also runs the agents on it.
#
# NOT run by CI. Follows the Cloud Run "Run Gemma 4 with vLLM" guide/codelab (image, flags, startup probe).
# Needs billing upgraded (the free trial blocks GPUs) and Cloud Run GPU quota in the chosen region.
#
#   export PROJECT_ID=wapsi-aibc
#   TIER=l4  bash editions/gcp/deploy/gemma_vllm_service.sh   # default: NVIDIA L4, us-central1, gemma-4-E4B-it
#   TIER=rtx bash editions/gcp/deploy/gemma_vllm_service.sh   # RTX PRO 6000, asia-south2 (Delhi) data residency
#
# Cost guardrails: scale to zero by default (MIN_INSTANCES=0, MAX_INSTANCES small). During judging set
# MIN_INSTANCES=1: a cold start can take ~4 minutes (the startup probe allows 240 s). An idle GPU instance
# is billed for every second it is up, so switch it back off after judging:
#   gcloud run services update wapsi-gemma --region REGION --min-instances 0
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
TIER="${TIER:-l4}"
SERVICE="${SERVICE:-wapsi-gemma}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-1}"
IMAGE="${IMAGE:-us-docker.pkg.dev/vertex-ai/vertex-vision-model-garden-dockers/pytorch-vllm-serve:gemma4}"
SA="${SA:-wapsi-gemma@${PROJECT_ID}.iam.gserviceaccount.com}"

case "${TIER}" in
  l4)   # 1x NVIDIA L4 (24 GB): the small instruction-tuned model; 8 vCPU / 32 GiB recommended
    REGION="${REGION:-us-central1}"
    GPU_TYPE="nvidia-l4"
    CPU="${CPU:-8}"; MEMORY="${MEMORY:-32Gi}"
    MODEL="${MODEL:-google/gemma-4-E4B-it}"
    MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
    MAX_SEQS="${MAX_SEQS:-16}"
    ;;
  rtx)  # 1x NVIDIA RTX PRO 6000 (96 GB); 20 vCPU / 80 GiB minimum. asia-south2 keeps inference in India.
    REGION="${REGION:-asia-south2}"
    GPU_TYPE="nvidia-rtx-pro-6000"
    CPU="${CPU:-20}"; MEMORY="${MEMORY:-80Gi}"
    MODEL="${MODEL:-google/gemma-4-E4B-it}"
    # google/gemma-4-26B-A4B-it or -31B-it also fit, but the guide loads big weights from Cloud Storage over
    # Direct VPC egress: set MODEL=gs://BUCKET/gemma-4-26B-A4B-it plus NETWORK/SUBNET (see README) [verify].
    MAX_MODEL_LEN="${MAX_MODEL_LEN:-65536}"
    MAX_SEQS="${MAX_SEQS:-64}"
    ;;
  *) echo "TIER must be l4 or rtx" >&2; exit 1 ;;
esac

gc() { gcloud --project "${PROJECT_ID}" --quiet "$@"; }

gc services enable run.googleapis.com
gc iam service-accounts describe "${SA}" >/dev/null 2>&1 || \
  gc iam service-accounts create "${SA%%@*}" --display-name "Wapsi Gemma (vLLM)"

VPC_FLAGS=()
if [[ -n "${NETWORK:-}" ]]; then
  VPC_FLAGS=(--network "${NETWORK}" --subnet "${SUBNET:?set SUBNET with NETWORK}" --vpc-egress all-traffic)
fi

ARGS="serve,${MODEL},--enable-chunked-prefill,--enable-prefix-caching,--generation-config=auto"
ARGS+=",--enable-auto-tool-choice,--tool-call-parser=gemma4,--reasoning-parser=gemma4,--dtype=bfloat16"
ARGS+=",--max-model-len=${MAX_MODEL_LEN},--max-num-seqs=${MAX_SEQS},--gpu-memory-utilization=0.95"
ARGS+=",--tensor-parallel-size=1,--port=8080,--host=0.0.0.0"
if [[ "${MODEL}" == gs://* ]]; then
  ARGS+=",--load-format=runai_streamer,--served-model-name=${SERVED_NAME:-google/$(basename "${MODEL}")}"
fi

gc beta run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --service-account "${SA}" \
  --execution-environment gen2 \
  --no-allow-unauthenticated \
  --cpu "${CPU}" --memory "${MEMORY}" \
  --gpu 1 --gpu-type "${GPU_TYPE}" --no-gpu-zonal-redundancy \
  --no-cpu-throttling \
  --min-instances "${MIN_INSTANCES}" --max-instances "${MAX_INSTANCES}" \
  --concurrency 64 --timeout 600 \
  --startup-probe "tcpSocket.port=8080,initialDelaySeconds=240,failureThreshold=1,timeoutSeconds=240,periodSeconds=240" \
  --labels app=wapsi,component=gemma,event=aibc \
  ${VPC_FLAGS[@]+"${VPC_FLAGS[@]}"} \
  --command "vllm" \
  --args="${ARGS}"

URL="$(gc run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
cat <<EOF

Gemma 4 (${MODEL}) on ${GPU_TYPE} in ${REGION}: ${URL}
Allow the Wapsi API to call it and point the API at it:
  gcloud run services add-iam-policy-binding ${SERVICE} --region ${REGION} \\
    --member=serviceAccount:wapsi-api@${PROJECT_ID}.iam.gserviceaccount.com --role=roles/run.invoker
  GEMMA_URL=${URL} GEMMA_REGION=${REGION} bash editions/gcp/deploy/cloudrun.sh deploy
Smoke test (your own identity needs roles/run.invoker):
  curl -s -H "Authorization: Bearer \$(gcloud auth print-identity-token)" -H 'Content-Type: application/json' \\
    ${URL}/v1/chat/completions -d '{"model":"${MODEL}","messages":[{"role":"user","content":"Say ok"}]}'
EOF
