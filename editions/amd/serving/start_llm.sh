#!/usr/bin/env bash
# Start vLLM (OpenAI-compatible server) for Wapsi on an AMD Instinct MI300X (ROCm).
#
# Usage:
#   start_llm.sh serve  [PROFILE]   run `vllm serve` in the foreground (inside a vLLM-ROCm container;
#                                   docker-compose.yml uses this as the llm / verifier-llm entrypoint)
#   start_llm.sh docker [PROFILE]   `docker run` the vLLM-ROCm image on the host with the ROCm device flags
#   start_llm.sh python [PROFILE]   no Docker (an AMD ROCm notebook): the same flags through
#                                   `python -m vllm.entrypoints.openai.api_server`, bound to VLLM_HOST (127.0.0.1)
#   start_llm.sh print  [PROFILE]   print both command lines and exit (dry run; safe anywhere)
#
# Profiles (WAPSI_LLM_PROFILE, default gemma4-merged):
#   gemma4-merged   merged Gemma 4 31B + STR LoRA checkpoint (/models/wapsi-gemma-4-31b-str-merged)
#   gemma4-base     google/gemma-4-31B-it (untuned baseline; gated on Hugging Face)
#   qwen-fallback   Qwen/Qwen3.6-35B-A3B (day-0 ROCm support; thinking disabled by default)
#   verifier        small independent judge model (default google/gemma-4-E4B-it) at low memory utilization
#
# Environment (all optional; defaults per profile):
#   WAPSI_LLM_MODEL          model id or path to serve
#   WAPSI_LLM_SERVED_NAME    name clients use (default wapsi-llm; wapsi-verifier for the verifier)
#   VLLM_GPU_UTIL            --gpu-memory-utilization (0.55 main, 0.15 verifier)
#   VLLM_MAX_MODEL_LEN       --max-model-len (16384 main, 8192 verifier)
#   VLLM_MAX_NUM_SEQS        --max-num-seqs (64)
#   VLLM_ATTENTION_BACKEND   Gemma 4 on ROCm needs TRITON_ATTN (default for gemma profiles; "none" to omit)
#   VLLM_ATTENTION_FLAG=0    older vLLM without --attention-backend: rely on the env var only
#   VLLM_QUANTIZATION        e.g. fp8 (on-the-fly FP8 weights) when memory is tight
#   VLLM_KV_CACHE_DTYPE      e.g. fp8
#   VLLM_LIMIT_MM            --limit-mm-per-prompt JSON (text-only default for official Gemma 4; "none" omits)
#   VLLM_ENABLE_TOOLS=1      add tool-call + reasoning parser flags (not needed by the Wapsi pipeline)
#   VLLM_EXTRA_ARGS          extra flags appended verbatim (word-split)
#   VLLM_API_KEY             require a bearer token on the OpenAI endpoints
#   VLLM_PORT                port inside the container (8000)
#   VLLM_HOST                listen address (0.0.0.0 in a container; `python` mode defaults to 127.0.0.1)
#   WAPSI_PYTHON             interpreter for `python` mode (python3: the notebook's ROCm python with vLLM installed)
#   VLLM_IMAGE               image for `docker` mode (pin by digest: serving/pull_and_pin.sh)
#   HF_CACHE_DIR / WAPSI_MODELS_DIR   host paths mounted in `docker` mode
set -euo pipefail

MODE="${1:-print}"
PROFILE="${2:-${WAPSI_LLM_PROFILE:-gemma4-merged}}"

case "${PROFILE}" in
  gemma4-merged)
    DEF_MODEL="/models/wapsi-gemma-4-31b-str-merged"; DEF_NAME="wapsi-llm"; DEF_UTIL="0.55"; DEF_LEN="16384"; FAMILY="gemma4" ;;
  gemma4-base)
    DEF_MODEL="google/gemma-4-31B-it"; DEF_NAME="wapsi-llm"; DEF_UTIL="0.55"; DEF_LEN="16384"; FAMILY="gemma4" ;;
  qwen-fallback)
    DEF_MODEL="Qwen/Qwen3.6-35B-A3B"; DEF_NAME="wapsi-llm"; DEF_UTIL="0.55"; DEF_LEN="16384"; FAMILY="qwen3" ;;
  verifier)
    DEF_MODEL="google/gemma-4-E4B-it"; DEF_NAME="wapsi-verifier"; DEF_UTIL="0.15"; DEF_LEN="8192"; FAMILY="gemma4" ;;
  *)
    echo "unknown profile '${PROFILE}' (gemma4-merged | gemma4-base | qwen-fallback | verifier)" >&2
    exit 64 ;;
esac

if [[ "${PROFILE}" == "verifier" ]]; then
  MODEL="${WAPSI_VERIFIER_MODEL_ID:-${DEF_MODEL}}"
  NAME="${WAPSI_VERIFIER_SERVED_NAME:-${DEF_NAME}}"
  UTIL="${VERIFIER_GPU_UTIL:-${DEF_UTIL}}"
  MAXLEN="${VERIFIER_MAX_MODEL_LEN:-${DEF_LEN}}"
else
  MODEL="${WAPSI_LLM_MODEL:-${DEF_MODEL}}"
  NAME="${WAPSI_LLM_SERVED_NAME:-${DEF_NAME}}"
  UTIL="${VLLM_GPU_UTIL:-${DEF_UTIL}}"
  MAXLEN="${VLLM_MAX_MODEL_LEN:-${DEF_LEN}}"
fi
case "${MODEL}" in
  *qwen*|*Qwen*) FAMILY="qwen3" ;;
  *gemma*|*Gemma*) FAMILY="gemma4" ;;
esac
PORT="${VLLM_PORT:-8000}"

ARGS=(serve "${MODEL}"
  --host "${VLLM_HOST:-0.0.0.0}" --port "${PORT}"
  --served-model-name "${NAME}"
  --gpu-memory-utilization "${UTIL}"
  --max-model-len "${MAXLEN}"
  --max-num-seqs "${VLLM_MAX_NUM_SEQS:-64}"
  --dtype auto
  --enable-prefix-caching
  --disable-uvicorn-access-log)

if [[ "${FAMILY}" == "gemma4" ]]; then
  ATTN="${VLLM_ATTENTION_BACKEND:-TRITON_ATTN}"
  # VLLM_ATTENTION_FLAG=0: vLLM builds without --attention-backend read the VLLM_ATTENTION_BACKEND env var instead
  if [[ "${ATTN}" != "none" && "${VLLM_ATTENTION_FLAG:-1}" == "1" ]]; then
    ARGS+=(--attention-backend "${ATTN}")
  fi
  # Official Gemma 4 checkpoints are multimodal: serve them text-only (no image/audio encoder budget; ROCm
  # images ship without vllm[audio]). A merged checkpoint may be saved text-only, so it gets no limit unless
  # VLLM_LIMIT_MM is set ("none" omits the flag everywhere).
  MM_DEFAULT='{"image": 0, "audio": 0}'
  if [[ "${PROFILE}" == "gemma4-merged" ]]; then MM_DEFAULT="none"; fi
  MM="${VLLM_LIMIT_MM:-${MM_DEFAULT}}"
  if [[ "${MM}" != "none" ]]; then
    ARGS+=(--limit-mm-per-prompt "${MM}")
  fi
  if [[ "${VLLM_ENABLE_TOOLS:-0}" == "1" ]]; then
    ARGS+=(--enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4)
  fi
else
  # Qwen3.6 thinks by default; Wapsi wants short prose, so thinking is off unless a request asks for it
  ARGS+=(--reasoning-parser qwen3 --default-chat-template-kwargs '{"enable_thinking": false}')
  if [[ "${VLLM_ENABLE_TOOLS:-0}" == "1" ]]; then
    ARGS+=(--enable-auto-tool-choice --tool-call-parser qwen3_coder)
  fi
fi
if [[ -n "${VLLM_QUANTIZATION:-}" ]]; then
  ARGS+=(--quantization "${VLLM_QUANTIZATION}")
fi
if [[ -n "${VLLM_KV_CACHE_DTYPE:-}" ]]; then
  ARGS+=(--kv-cache-dtype "${VLLM_KV_CACHE_DTYPE}")
fi
if [[ -n "${VLLM_API_KEY:-}" ]]; then
  ARGS+=(--api-key "${VLLM_API_KEY}")
fi
if [[ -n "${VLLM_EXTRA_ARGS:-}" ]]; then
  read -r -a EXTRA <<< "${VLLM_EXTRA_ARGS}"
  ARGS+=("${EXTRA[@]}")
fi

# `python` mode (no container around the server): the same flags, on loopback unless VLLM_HOST says otherwise
PY_ARGS=(); PREV=""
for a in "${ARGS[@]:2}"; do
  if [[ "${PREV}" == "--host" ]]; then a="${VLLM_HOST:-127.0.0.1}"; fi
  PY_ARGS+=("${a}"); PREV="${a}"
done

IMAGE="${VLLM_IMAGE:-rocm/vllm@sha256:FILL_ME_WITH_pull_and_pin.sh}"
CONTAINER="${VLLM_CONTAINER_NAME:-wapsi-${NAME#wapsi-}}"
DEF_HOST_PORT="8000"
if [[ "${PROFILE}" == "verifier" ]]; then DEF_HOST_PORT="8001"; fi
HOST_PORT="${VLLM_HOST_PORT:-${DEF_HOST_PORT}}"
DOCKER=(docker run -d --name "${CONTAINER}"
  --device=/dev/kfd --device=/dev/dri --group-add=video --ipc=host --security-opt seccomp=unconfined
  -p "127.0.0.1:${HOST_PORT}:${PORT}"
  -e HF_TOKEN -e VLLM_API_KEY -e HF_HOME=/root/.cache/huggingface
  -v "${HF_CACHE_DIR:-${HOME}/.cache/huggingface}:/root/.cache/huggingface"
  -v "${WAPSI_MODELS_DIR:-/mnt/models}:/models:ro"
  --entrypoint vllm)
if [[ "${FAMILY}" == "gemma4" && "${VLLM_ATTENTION_FLAG:-1}" != "1" && "${ATTN:-none}" != "none" ]]; then
  DOCKER+=(-e "VLLM_ATTENTION_BACKEND=${ATTN}")
fi
DOCKER+=("${IMAGE}")

redact() {  # never print the API key
  local out=() a
  for a in "$@"; do
    if [[ -n "${VLLM_API_KEY:-}" && "${a}" == "${VLLM_API_KEY}" ]]; then out+=("***"); else out+=("${a}"); fi
  done
  printf '%q ' "${out[@]}"
  printf '\n'
}

case "${MODE}" in
  serve)
    # the backend goes in as a flag; keep the env var only for older vLLM (VLLM_ATTENTION_FLAG=0, Gemma 4)
    if [[ "${FAMILY}" != "gemma4" || "${VLLM_ATTENTION_FLAG:-1}" == "1" ]]; then
      unset VLLM_ATTENTION_BACKEND
    fi
    echo "[start_llm] profile=${PROFILE} model=${MODEL} served-as=${NAME} util=${UTIL} max-len=${MAXLEN}" >&2
    exec vllm "${ARGS[@]}" ;;
  docker)
    if [[ "${IMAGE}" == *FILL_ME* ]]; then
      echo "VLLM_IMAGE is not pinned; run editions/amd/serving/pull_and_pin.sh and source editions/amd/.env" >&2
      exit 65
    fi
    "${DOCKER[@]}" "${ARGS[@]}"
    echo "started ${CONTAINER}; follow with: docker logs -f ${CONTAINER}" >&2 ;;
  python)
    # `vllm serve MODEL <flags>` and `python -m vllm.entrypoints.openai.api_server --model MODEL <flags>` take the
    # same flags; the module form needs no `vllm` launcher on PATH (a notebook kernel's environment)
    if [[ "${FAMILY}" != "gemma4" || "${VLLM_ATTENTION_FLAG:-1}" == "1" ]]; then
      unset VLLM_ATTENTION_BACKEND
    fi
    echo "[start_llm] python profile=${PROFILE} model=${MODEL} served-as=${NAME} util=${UTIL} max-len=${MAXLEN}" >&2
    exec "${WAPSI_PYTHON:-python3}" -m vllm.entrypoints.openai.api_server --model "${MODEL}" "${PY_ARGS[@]}" ;;
  print)
    echo "# inside a vLLM-ROCm container (profile ${PROFILE}):"
    redact vllm "${ARGS[@]}"
    echo "# on the host:"
    redact "${DOCKER[@]}" "${ARGS[@]}"
    echo "# without Docker (start_llm.sh python; binds 127.0.0.1 unless VLLM_HOST is set):"
    redact "${WAPSI_PYTHON:-python3}" -m vllm.entrypoints.openai.api_server --model "${MODEL}" "${PY_ARGS[@]}" ;;
  *)
    echo "usage: $0 serve|docker|python|print [gemma4-merged|gemma4-base|qwen-fallback|verifier]" >&2
    exit 64 ;;
esac
