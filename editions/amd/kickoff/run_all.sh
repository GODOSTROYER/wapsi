#!/usr/bin/env bash
set -euo pipefail
# Wapsi AMD ACT III: the event-week pipeline as ONE resumable run, in whichever way this machine can run it
# (RUNBOOK.md). Three execution modes write the same evidence bundle and feed fill_pitch.py:
#
#   endpoint   no GPU box. Everything LLM-side runs against an OpenAI-compatible endpoint (AMD Token Factory, or
#              anyone's vLLM): WAPSI_LLM_BASE_URL / WAPSI_LLM_API_KEY / WAPSI_LLM_MODEL. Golden-set evaluation with
#              the real model, raw-LLM vs Wapsi-bound A/B, latency / throughput benchmark, API-reported cost. The
#              GPU is the provider's: the evidence records the endpoint host and the model id, never a GPU name.
#   notebook   an AMD ROCm JupyterLab without Docker: GNN training and the LoRA fine-tune as plain python (ROCm
#              torch), serving with `python -m vllm.entrypoints.openai.api_server` (transformers if vLLM is absent),
#              amd-smi / rocm-smi snapshots. kickoff/wapsi_amd_kickoff.ipynb calls these same stages.
#   droplet    the AMD Developer Cloud MI300X droplet with docker compose (vLLM-ROCm images).
#
#   editions/amd/kickoff/run_all.sh --dry-run          # print the plan (every command), run nothing
#   editions/amd/kickoff/run_all.sh                    # detect the mode (preflight.sh --detect), run what is not done yet
#   editions/amd/kickoff/run_all.sh --mode endpoint    # or notebook | droplet: choose the mode yourself
#   editions/amd/kickoff/run_all.sh --stage bench      # (re)run one stage; a comma list works (--stage bench,ab)
#   editions/amd/kickoff/run_all.sh --from serve       # (re)run serve and every stage after it
#   editions/amd/kickoff/run_all.sh --rehearsal        # CPU rehearsal: tiny configs, stub LLMs, no GPU, no Docker
#   editions/amd/kickoff/run_all.sh --list             # stage names
#
# Stages: env, models, gnn, lora-data, lora-train, merge, lora-eval, serve, eval, bench, ab, evidence. Endpoint mode
# runs env, models, eval, bench, ab, evidence (the rest needs a GPU box and is recorded as skipped).
# Each stage logs to <evidence>/logs/, takes an amd-smi snapshot before and after (<evidence>/amd_smi/; not in
# endpoint mode), writes <evidence>/stages/NN-<stage>.json (UTC start/end, exit code, notes, budget) and, when it
# succeeds, a marker NN-<stage>.ok, so a re-run skips it. Evidence folders: editions/amd/evidence/<UTC date>/
# (droplet), evidence/notebook/<date>/, evidence/endpoint/<date>/, and the gitignored evidence/cpu-rehearsal/<date>/
# (labelled, never results).
#
# Guards (every mode but the rehearsal): refuses to start before the event window (2026-10-12 15:00 UTC) or without
# the amd-baseline tag. Droplet: before every stage it prints the GPU spend (stagelog.py budget), warns at
# WAPSI_BUDGET_WARN_PCT (75%) and refuses new stages at WAPSI_BUDGET_STOP_PCT (90%); on exit it reminds you to
# DESTROY the droplet (powered off still bills). Endpoint: the client keeps under the key's requests-per-minute cap
# (WAPSI_LLM_RPM, default 16 of the 20 the trial allows) and reports the API's own daily cost counter.
#
# Options: --mode endpoint|notebook|droplet|rehearsal   (default: detected; WAPSI_MODE)
#          --date YYYY-MM-DD   evidence folder (default: the active run's date, else today, UTC)
#          --new               start a new evidence folder today even if an earlier run is unfinished
#          --force             re-run stages that already have a marker
#          --keep-going        continue after a failed stage
#          --ignore-budget     start stages even at the stop threshold
#          --skip-preflight    do not run preflight.sh in the env stage
#          --no-baseline-check / --allow-after-window   (results would not be event-eligible)
#          --evidence-root DIR / --work DIR             (defaults: editions/amd/evidence, a per-mode work folder)
# Environment, every mode: WAPSI_INR_PER_USD (the day's rate), WAPSI_E2E_WORLD (demo).
#   WAPSI_SKIP_DOCS=1 (no docs-corpus fetch: nothing but the endpoint is contacted).
#   endpoint: WAPSI_LLM_BASE_URL, WAPSI_LLM_API_KEY (never printed), WAPSI_LLM_MODEL (default: the model plan,
#     Gemma-4-31B-IT first), WAPSI_JUDGE_MODEL, WAPSI_LLM_JSON_MODE (json_object), WAPSI_LLM_RPM,
#     WAPSI_LLM_MAX_CONCURRENCY, WAPSI_LLM_CACHE_DIR, WAPSI_ALLOW_MODEL_FALLBACK=1 (use the fallback list when the
#     named model is not served), WAPSI_EP_FULL=1 (every golden case), WAPSI_EP_EVAL_LIMIT (10; 0 = all),
#     WAPSI_EP_AB_LIMIT (10), WAPSI_EP_E2E_LIMIT (6), WAPSI_EP_BENCH_LEVELS ("1 4 8"), WAPSI_EP_BENCH_REQUESTS (6), WAPSI_EP_WAPSI_LEVELS
#     ("1 4"), WAPSI_EP_WAPSI_REQUESTS (4), WAPSI_LLM_MIN_INTERVAL_S (seconds between request starts),
#     WAPSI_ENDPOINT_HARDWARE (what serves an endpoint that is not AMD's own, as declared by you).
#   notebook: SCRATCH (~/wapsi-scratch), WAPSI_PYTHON (python3: the notebook's ROCm python), HF_TOKEN (never printed),
#     WAPSI_NB_SERVER (auto | vllm | transformers), WAPSI_NB_SKIP_PIP=1, WAPSI_GPU_HOUR_USD (what an hour costs, if
#     known), WAPSI_KEEP_SERVING=1 (leave the servers up on exit), WAPSI_LORA=0|1, and the model variables below.
#   droplet: SCRATCH (/mnt/scratch), HF_TOKEN, WAPSI_GPU_HOUR_USD (1.99), WAPSI_BUDGET_USD (100), WAPSI_SPENT_USD
#     (earlier droplets), WAPSI_DROPLET_STARTED_AT (ISO UTC), AMLWORLD_CSV, ROCM_PYTORCH_IMAGE.
#   models (notebook, droplet): WAPSI_BASE_MODEL, WAPSI_VERIFIER_MODEL_ID, WAPSI_LORA_CONFIG, WAPSI_BASE_PROFILE,
#     WAPSI_MERGED_NAME, VLLM_API_KEY (optional bearer token between the clients and vLLM; never printed).
# Windows (endpoint mode and the rehearsal): run it from Git Bash, `bash editions/amd/kickoff/run_all.sh ...`; in
#   PowerShell, `bash` may be WSL's, which sees neither this checkout's uv venv nor Windows paths.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../../.." && pwd)"
cd "${ROOT}"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8  # the reports print ₹ and ≈ even when stdout is a pipe (tee)
KICK="editions/amd/kickoff"
AMD="editions/amd"
STAGES=(env models gnn lora-data lora-train merge lora-eval serve eval bench ab evidence)

stage_title() {
  case "$1" in
    env) echo "environment: preflight, amd-smi static, uv sync, synthetic worlds, pinned images, training image" ;;
    models) echo "pull and cache the models (Gemma 4 31B base, Gemma 4 E4B verifier)" ;;
    gnn) echo "GNN: GraphSAGE training on ROCm (+ AMLworld pre-training), held-out and hard-world evaluation" ;;
    lora-data) echo "LoRA dataset: serve the base model as teacher, smoke test, base serving bench, verifier-filtered data" ;;
    lora-train) echo "LoRA fine-tune (TRL + PEFT, bf16)" ;;
    merge) echo "merge the adapter into a plain checkpoint (fp32 merge, bf16 save, logit check)" ;;
    lora-eval) echo "evaluate base vs tuned on held-out cases (verifier-filtered metrics)" ;;
    serve) echo "serve the merged model + verifier + GNN + app with docker compose (vLLM-ROCm), smoke tests" ;;
    eval) echo "golden-set evaluation with the served model (numeric accuracy, citations, verifier pass rate)" ;;
    bench) echo "benchmarks: tokens/s, TTFT, p95 at N concurrent, cases/hour, drafts per GPU-hour, cost per case, VRAM" ;;
    ab) echo "A/B: raw LLM free-form STR vs Wapsi-bound drafting (hallucinated amounts, accounts, citations)" ;;
    evidence) echo "evidence bundle: evidence.json + evidence.md, pitch fill preview (dry-run diff)" ;;
    *) return 1 ;;
  esac
}

endpoint_title() {  # what each stage does in endpoint mode (no GPU box; the LLM side on an OpenAI-compatible endpoint)
  case "$1" in
    env) echo "preflight (endpoint), python environment, demo world, docs index, endpoint probe (host, model, limits)" ;;
    models) echo "model plan: drafting model (Gemma 4 first) and judge, from what the endpoint serves" ;;
    eval) echo "golden-set evaluation with the real model through the endpoint (cached, rate-limited)" ;;
    bench) echo "latency and throughput at N concurrent (tokens/s, TTFT, p50/p95), cases and drafts per hour, API cost" ;;
    ab) echo "A/B: raw LLM free-form STR vs Wapsi-bound drafting, same model, same cases" ;;
    evidence) echo "evidence bundle (endpoint host + model id; GPU provider-side), pitch fill preview" ;;
    *) return 1 ;;
  esac
}

notebook_title() {  # what each stage does in an AMD ROCm notebook without Docker (plain python on the ROCm torch)
  case "$1" in
    env) echo "preflight (notebook), GPU identity (amd-smi / torch), python packages on the ROCm torch, worlds, docs index" ;;
    models) echo "model plan by GPU memory (largest Gemma 4 that fits), download the weights" ;;
    gnn) echo "GNN: GraphSAGE training as plain python on ROCm, held-out and hard-world evaluation" ;;
    lora-data) echo "LoRA dataset: serve the base model (vLLM python entrypoint, or transformers), verifier-filtered data" ;;
    lora-train) echo "LoRA fine-tune as plain python (TRL + PEFT, bf16)" ;;
    merge) echo "merge the adapter into a plain checkpoint (fp32 merge, bf16 save, logit check)" ;;
    lora-eval) echo "evaluate base vs tuned on held-out cases (in-process, transformers)" ;;
    serve) echo "serve the model (python -m vllm.entrypoints.openai.api_server, else transformers) + GNN service, smoke tests" ;;
    eval) echo "golden-set evaluation with the served model" ;;
    bench) echo "benchmarks with amd-smi / rocm-smi telemetry: tokens/s, TTFT, cases/hour, VRAM" ;;
    ab) echo "A/B: raw LLM free-form STR vs Wapsi-bound drafting" ;;
    evidence) echo "evidence bundle (the notebook's GPU as named by amd-smi / torch), pitch fill preview" ;;
    *) return 1 ;;
  esac
}

rehearsal_title() {  # what each stage does in the CPU rehearsal (same code paths, tiny inputs, stub models)
  case "$1" in
    env) echo "preflight (rehearsal), amd-smi static (absent), four tiny synthetic worlds + a tiny AMLworld-format CSV" ;;
    models) echo "nothing to download: the tiny-cpu preset builds a random 2-layer model" ;;
    gnn) echo "GNN: 2 CPU epochs on tiny worlds (+ 1 AMLworld epoch), held-out and harness evaluation" ;;
    lora-data) echo "LoRA dataset from a stub teacher behind the OpenAI-compatible mock endpoint, smoke test, bench" ;;
    lora-train) echo "LoRA fine-tune of the tiny random model (4 steps, CPU)" ;;
    merge) echo "merge the tiny adapter (logit check)" ;;
    lora-eval) echo "evaluate stub base vs tiny tuned model on a tiny held-out world" ;;
    serve) echo "local stub servers (mock OpenAI endpoints, GNN scoring service), smoke tests" ;;
    eval) echo "golden-set evaluation harness with the offline FakeLLM (plumbing check)" ;;
    bench) echo "llm_bench against the stub endpoint, e2e_bench with FakeLLM + the GNN service (no telemetry)" ;;
    ab) echo "A/B with stub LLMs that always hallucinate (raw vs Wapsi-bound)" ;;
    evidence) echo "evidence bundle labelled CPU rehearsal; fill_pitch.py must refuse it" ;;
    *) return 1 ;;
  esac
}

die() { echo "run_all.sh: $*" >&2; exit 2; }
usage() { sed -n '3,63p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

MODE="${WAPSI_MODE:-auto}"; DRY_RUN=0; FORCE=0; KEEP_GOING=0; IGNORE_BUDGET=0; SKIP_PREFLIGHT=0; NO_BASELINE=0; ALLOW_AFTER=0
NEW=0; ONLY=""; FROM=""; DATE="${WAPSI_EVIDENCE_DATE:-}"; LIST=0
EVID_ROOT="${WAPSI_EVIDENCE_ROOT:-${AMD}/evidence}"
WORK_OPT="${WAPSI_WORK_DIR:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --rehearsal) MODE="rehearsal" ;;
    --mode) MODE="${2:?--mode needs endpoint | notebook | droplet | rehearsal}"; shift ;;
    --mode=*) MODE="${1#*=}" ;;
    --force) FORCE=1 ;;
    --keep-going) KEEP_GOING=1 ;;
    --ignore-budget) IGNORE_BUDGET=1 ;;
    --skip-preflight) SKIP_PREFLIGHT=1 ;;
    --no-baseline-check) NO_BASELINE=1 ;;
    --allow-after-window) ALLOW_AFTER=1 ;;
    --new) NEW=1 ;;
    --list) LIST=1 ;;
    --stage) ONLY="${2:?--stage needs a name}"; shift ;;
    --stage=*) ONLY="${1#*=}" ;;
    --from) FROM="${2:?--from needs a name}"; shift ;;
    --from=*) FROM="${1#*=}" ;;
    --date) DATE="${2:?--date needs YYYY-MM-DD}"; shift ;;
    --date=*) DATE="${1#*=}" ;;
    --evidence-root) EVID_ROOT="${2:?}"; shift ;;
    --work) WORK_OPT="${2:?}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done

stage_index() {  # 1-based index of a stage name, or exit 1
  local i
  for i in "${!STAGES[@]}"; do [[ "${STAGES[$i]}" == "$1" ]] && { echo $((i + 1)); return 0; }; done
  return 1
}
if [[ "${LIST}" == "1" ]]; then
  for s in "${STAGES[@]}"; do printf '%02d %-11s %s\n' "$(stage_index "$s")" "$s" "$(stage_title "$s")"; done
  exit 0
fi
if [[ -n "${ONLY}" ]]; then
  IFS=',' read -r -a _only <<< "${ONLY}"
  for s in "${_only[@]}"; do stage_index "$s" >/dev/null || die "unknown stage '$s' (--list)"; done
fi
[[ -z "${FROM}" ]] || stage_index "${FROM}" >/dev/null || die "unknown stage '${FROM}' (--list)"
[[ -z "${ONLY}" || -z "${FROM}" ]] || die "use --stage or --from, not both"
[[ -z "${DATE}" || "${DATE}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "--date must be YYYY-MM-DD"
[[ "${MODE}" == "gpu" ]] && MODE="droplet"  # the droplet's old name
case "${MODE}" in
  auto)
    MODE="$(bash "${KICK}/preflight.sh" --detect 2>/dev/null || true)"; MODE="${MODE%%$'\r'}"
    case "${MODE}" in
      droplet|notebook|endpoint) ;;
      *) die "cannot tell how to run here: no AMD GPU and no WAPSI_LLM_BASE_URL. Endpoint mode: export WAPSI_LLM_BASE_URL, WAPSI_LLM_API_KEY (and optionally WAPSI_LLM_MODEL), or pass --mode endpoint | notebook | droplet; on a laptop without a key: --rehearsal" ;;
    esac ;;
  droplet|notebook|endpoint|rehearsal) ;;
  *) die "unknown mode '${MODE}' (endpoint | notebook | droplet | rehearsal)" ;;
esac

# ------------------------------------------------------------------ configuration
ENV_FILE="${AMD}/.env"
SHELL_ENV_KEYS=" $(compgen -e | tr '\n' ' ') "  # what the operator exported: it always wins over .env
load_env_file() {  # (re)export editions/amd/.env KEY=VALUE lines the shell did not set (never evaluated as code)
  [[ -f "${ENV_FILE}" ]] || return 0
  local line key value
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ "${line}" =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
    [[ "${SHELL_ENV_KEYS}" == *" ${key} "* ]] && continue
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    export "${key}=${value}"
  done < "${ENV_FILE}"
}
# A GPU box serves the model itself. If the shell also carries a provider endpoint (Token Factory variables from a
# secrets file), set it aside: WAPSI_LLM_MODEL means "the checkpoint vLLM serves" here, and the local server must
# never be sent the provider's key.
SET_ASIDE=""
if [[ "${MODE}" == "droplet" || "${MODE}" == "notebook" ]]; then
  case "${WAPSI_LLM_BASE_URL:-}" in
    ""|http://127.0.0.1*|http://localhost*|http://llm:*) ;;
    *) SET_ASIDE="${WAPSI_LLM_BASE_URL}"; unset WAPSI_LLM_BASE_URL WAPSI_LLM_MODEL WAPSI_LLM_API_KEY WAPSI_LLM_JSON_MODE ;;
  esac
fi
[[ "${MODE}" == "droplet" ]] && load_env_file

case "${MODE}" in
  droplet)
    SCRATCH="${SCRATCH:-/mnt/scratch}"
    WORK="${WORK_OPT:-${SCRATCH}/wapsi-work}"; EVID_PARENT="${EVID_ROOT}" ;;
  notebook)
    SCRATCH="${SCRATCH:-${HOME}/wapsi-scratch}"
    WORK="${WORK_OPT:-${SCRATCH}/wapsi-work}"; EVID_PARENT="${EVID_ROOT}/notebook" ;;
  endpoint)
    SCRATCH="${SCRATCH:-/mnt/scratch}"
    WORK="${WORK_OPT:-data/generated/kickoff-endpoint}"; EVID_PARENT="${EVID_ROOT}/endpoint" ;;
  *)
    SCRATCH="${SCRATCH:-/mnt/scratch}"
    WORK="${WORK_OPT:-data/generated/kickoff-rehearsal}"; EVID_PARENT="${EVID_ROOT}/cpu-rehearsal" ;;
esac
if [[ -z "${DATE}" ]]; then
  if [[ "${NEW}" == "0" && -f "${EVID_PARENT}/.active" ]]; then DATE="$(head -n 1 "${EVID_PARENT}/.active")"
  else DATE="$(date -u +%Y-%m-%d)"; fi
fi
EVID="${EVID_PARENT}/${DATE}"
if [[ "${MODE}" == "droplet" && "${EVID}" == /* ]]; then
  die "GPU evidence must live inside the repository (relative --evidence-root); containers write it via /work"
fi

HF_CACHE_DIR="${HF_CACHE_DIR:-${SCRATCH}/hf}"
WAPSI_MODELS_DIR="${WAPSI_MODELS_DIR:-${SCRATCH}/models}"
BASE_MODEL="${WAPSI_BASE_MODEL:-google/gemma-4-31B-it}"
VERIFIER_MODEL="${WAPSI_VERIFIER_MODEL_ID:-google/gemma-4-E4B-it}"
LORA_CONFIG="${WAPSI_LORA_CONFIG:-gemma4-31b-mi300x}"
MERGED_NAME="${WAPSI_MERGED_NAME:-wapsi-gemma-4-31b-str-merged}"
# Qwen fallback (only if Gemma 4 will not serve): WAPSI_BASE_PROFILE=qwen-fallback WAPSI_BASE_MODEL=Qwen/Qwen3.6-35B-A3B
#   WAPSI_LORA_CONFIG=qwen36-35b-a3b-mi300x WAPSI_MERGED_NAME=wapsi-qwen36-str-merged (start_llm.sh picks the family from the name)
BASE_PROFILE="${WAPSI_BASE_PROFILE:-gemma4-base}"
MERGED_PROFILE="$([[ "${BASE_PROFILE}" == qwen-fallback ]] && echo qwen-fallback || echo gemma4-merged)"
SERVED_NAME="${WAPSI_LLM_SERVED_NAME:-wapsi-llm}"
VERIFIER_NAME="${WAPSI_VERIFIER_SERVED_NAME:-wapsi-verifier}"
ROCM_PYTORCH_IMAGE="${ROCM_PYTORCH_IMAGE:-rocm/pytorch:rocm7.1_ubuntu24.04_py3.12_pytorch_release_2.9.1}"
TRAIN_IMAGE="wapsi-amd-train:local"
AMLWORLD_CSV="${AMLWORLD_CSV:-${SCRATCH}/amlworld/HI-Small_Trans.csv}"
GPU_HOUR_USD="${WAPSI_GPU_HOUR_USD:-1.99}"
INR_PER_USD="${WAPSI_INR_PER_USD:-}"
E2E_WORLD="${WAPSI_E2E_WORLD:-demo}"
GNN_WORKERS="${WAPSI_GNN_WORKERS:-16}"
GNN_EXTRA_WORLDS="${WAPSI_GNN_EXTRA_WORLDS:-$([[ "${MODE}" == "notebook" ]] && echo 20 || echo 100)}"
HF_HUB_VERSION="1.31.0"  # = uv.lock
LLM_URL="http://127.0.0.1:8000/v1"
VERIFIER_URL="http://127.0.0.1:8001/v1"
GNN_URL="http://127.0.0.1:8081"
APP_URL="http://127.0.0.1:8080"
COMPOSE=(docker compose -f "${AMD}/docker-compose.yml")
ROCM_RUN=(docker run --rm --device=/dev/kfd --device=/dev/dri --group-add video --group-add render --ipc=host
  --security-opt seccomp=unconfined --shm-size 32g -e HF_TOKEN -e HF_HOME=/hf -e PYTHONPATH=/work/src:/work
  -v "${ROOT}:/work" -v "${HF_CACHE_DIR}:/hf" -v "${WAPSI_MODELS_DIR}:/models" -v "${WORK}:/wk"
  -v "$(dirname "${AMLWORLD_CSV}"):/aml:ro" -w /work "${TRAIN_IMAGE}")
# endpoint mode: sizes that fit a 20 requests/min trial key in about a quarter of an hour (about 100 calls);
# WAPSI_EP_FULL=1 takes every golden case instead (0 = all; about an hour on the same key)
EP_URL="${WAPSI_LLM_BASE_URL:-}"
EP_MODEL="${WAPSI_LLM_MODEL:-Gemma-4-31B-IT}"
EP_RPM="${WAPSI_LLM_RPM:-16}"
EP_FULL="${WAPSI_EP_FULL:-0}"
EP_EVAL_LIMIT="${WAPSI_EP_EVAL_LIMIT:-$([[ "${EP_FULL}" == "1" ]] && echo 0 || echo 10)}"
EP_AB_LIMIT="${WAPSI_EP_AB_LIMIT:-$([[ "${EP_FULL}" == "1" ]] && echo 0 || echo 10)}"
EP_E2E_LIMIT="${WAPSI_EP_E2E_LIMIT:-$([[ "${EP_FULL}" == "1" ]] && echo 20 || echo 6)}"
EP_E2E_CONCURRENCY="${WAPSI_EP_E2E_CONCURRENCY:-2}"
EP_BENCH_LEVELS="${WAPSI_EP_BENCH_LEVELS:-1 4 8}"
EP_BENCH_REQUESTS="${WAPSI_EP_BENCH_REQUESTS:-6}"
EP_WAPSI_LEVELS="${WAPSI_EP_WAPSI_LEVELS:-1 4}"
EP_WAPSI_REQUESTS="${WAPSI_EP_WAPSI_REQUESTS:-4}"
EP_WORKERS="${WAPSI_EP_WORKERS:-2}"
# notebook mode: the notebook's own python (the ROCm torch lives there), the repository on PYTHONPATH
NB_PY="${WAPSI_PYTHON:-python3}"
if [[ "${MODE}" == "notebook" && -z "${WAPSI_PYTHON:-}" ]] \
  && ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
  # no usable python3 on PATH (e.g. a Windows launcher stub): fall back to the project interpreter, then `python`
  for _c in .venv/bin/python .venv/Scripts/python.exe python; do
    if "${_c}" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then NB_PY="${_c}"; break; fi
  done
fi
NB_SERVER="${WAPSI_NB_SERVER:-auto}"
NB_LABEL="AMD ROCm notebook run, ${DATE}"
if [[ "${MODE}" == "notebook" ]]; then
  export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}" HF_HOME="${HF_CACHE_DIR}"
fi

# ------------------------------------------------------------------ helpers
pybin() {  # path of the project interpreter (uv venv; the notebook's python in notebook mode), empty before `uv sync`
  if [[ "${MODE}" == "notebook" ]]; then echo "${NB_PY}"
  elif [[ -x .venv/bin/python ]]; then echo .venv/bin/python
  elif [[ -x .venv/Scripts/python.exe ]]; then echo .venv/Scripts/python.exe; fi
}
py() {  # the project interpreter, falling back to `uv run`
  local bin; bin="$(pybin)"
  if [[ -n "${bin}" ]]; then "${bin}" "$@"; else uv run python "$@"; fi
}
sys_py() {  # standard-library helpers: system python3 >= 3.11 (works before uv sync), else the project interpreter
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1
  then python3 "$@"; else py "$@"; fi
}
slog() { sys_py "${KICK}/stagelog.py" "$@"; }

show() {  # print a command (copy-pasteable: py -> python, slog -> stagelog.py), secrets redacted
  local a s out=() secrets=()
  for s in "${HF_TOKEN:-}" "${VLLM_API_KEY:-}" "${WAPSI_LLM_API_KEY:-}" "${RADEON_API_KEY:-}"; do
    [[ -n "${s}" ]] && secrets+=("${s}")
  done
  case "${1:-}" in
    slog) out+=(python3 "${KICK}/stagelog.py"); shift ;;
    sys_py) out+=(python3); shift ;;
  esac
  for a in "$@"; do
    if [[ "${a}" == "py" ]]; then out+=(python); continue; fi
    for s in ${secrets[@]+"${secrets[@]}"}; do
      if [[ "${a}" == *"${s}"* ]]; then a="***"; fi
    done
    out+=("${a}")
  done
  printf '  $'; printf ' %q' "${out[@]}"; printf '\n'
}
run() { show "$@"; [[ "${DRY_RUN}" == "1" ]] && return 0; "$@"; }
run_soft() {  # like run, but a failure is reported and ignored
  show "$@"; [[ "${DRY_RUN}" == "1" ]] && return 0
  local rc=0; "$@" || rc=$?
  [[ ${rc} -eq 0 ]] || echo "  (non-fatal: exit ${rc})"
  return 0
}
run_sh() { printf '  $ %s\n' "$1"; [[ "${DRY_RUN}" == "1" ]] && return 0; bash -c "$1"; }
note() { if [[ "${DRY_RUN}" == "1" ]]; then echo "  # note $1=$2"; else echo "$1=$2" >> "${NOTES}"; fi; }
MEASURE_FAILS=0
MEASURE_FAILED=""
measure() {  # measure NAME CMD...: one measurement (eval, bench, A/B). On a real run (endpoint, notebook, droplet) a
  # non-zero exit (a failed request or case; the result file is still written) is recorded in the stage notes
  # (measure_failures, measure_failed) and the stage goes on, so one error cannot stop an unattended run. The CPU
  # rehearsal stays strict.
  local name="$1"; shift
  if [[ "${MODE}" == "rehearsal" ]]; then run "$@"; return; fi
  show "$@"; [[ "${DRY_RUN}" == "1" ]] && return 0
  local rc=0; "$@" || rc=$?
  if [[ ${rc} -ne 0 ]]; then
    MEASURE_FAILS=$((MEASURE_FAILS + 1)); MEASURE_FAILED="${MEASURE_FAILED:+${MEASURE_FAILED},}${name}"
    echo "  (measurement ${name} exited ${rc}: recorded in the stage notes; the stage goes on)"
    note measure_failures "${MEASURE_FAILS}"
    note measure_failed "${MEASURE_FAILED}"
  fi
  return 0
}
say() { echo "  # $*"; }
in_rocm() { run "${ROCM_RUN[@]}" "$@"; }
wait_http() { run slog wait-http "$1" --timeout "${2:-1800}" --interval "${3:-10}"; }

env_set() {  # env_set KEY VALUE: persist in editions/amd/.env so later `docker compose` commands agree
  show set "${ENV_FILE}" "$1=$2"; [[ "${DRY_RUN}" == "1" ]] && return 0
  if [[ ! -f "${ENV_FILE}" ]]; then cp "${AMD}/.env.example" "${ENV_FILE}"; chmod 600 "${ENV_FILE}"; fi
  local tmp; tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  awk -v k="$1" -v v="$2" 'BEGIN { d = 0 } $0 ~ "^" k "=" { print k "=" v; d = 1; next } { print } END { if (!d) print k "=" v }' \
    "${ENV_FILE}" > "${tmp}" && mv "${tmp}" "${ENV_FILE}"
  export "$1=$2"
}

PID_DIR="${WORK}/pids"
bg_start() {  # bg_start NAME [K=V ...] CMD...: background process (exec'd, so the pid is the process itself)
  local name="$1"; shift
  show "$@"; echo "    (background: ${name}, log ${EVID}/logs/bg-${name}.log)"
  [[ "${DRY_RUN}" == "1" ]] && return 0
  mkdir -p "${PID_DIR}" "${EVID}/logs"
  (
    while [[ "${1:-}" == *=* ]]; do export "${1?}"; shift; done
    if [[ "$1" == "py" ]]; then shift; set -- "$(pybin)" "$@"; fi
    exec "$@"
  ) > "${EVID}/logs/bg-${name}.log" 2>&1 < /dev/null &
  echo $! > "${PID_DIR}/${name}"
}
bg_alive() { [[ -f "${PID_DIR}/$1" ]] && kill -0 "$(cat "${PID_DIR}/$1")" 2>/dev/null; }
bg_stop() {  # bg_stop NAME [SIGNAL]
  local name="$1" sig="${2:-TERM}" pid
  echo "  \$ kill -${sig} <pid of ${name}>"; [[ "${DRY_RUN}" == "1" ]] && return 0
  [[ -f "${PID_DIR}/${name}" ]] || return 0
  pid="$(cat "${PID_DIR}/${name}")"
  kill "-${sig}" "${pid}" 2>/dev/null || true
  for _ in $(seq 1 30); do kill -0 "${pid}" 2>/dev/null || break; sleep 1; done
  kill -KILL "${pid}" 2>/dev/null || true
  rm -f "${PID_DIR}/${name}"
}
stop_all_bg() {  # never in a dry run: it started nothing and must not touch a real run's servers
  [[ "${DRY_RUN}" == "0" && -d "${PID_DIR}" ]] || return 0
  if [[ "${MODE}" == "notebook" && "${WAPSI_KEEP_SERVING:-0}" == "1" ]]; then
    echo "WAPSI_KEEP_SERVING=1: the notebook's servers stay up (pids in ${PID_DIR}); stop them with: kill \$(cat ${PID_DIR}/*)"
    return 0
  fi
  local f
  for f in "${PID_DIR}"/*; do
    if [[ -e "${f}" ]]; then bg_stop "$(basename "${f}")" TERM >/dev/null || true; fi
  done
  return 0
}

synth_world() {  # synth_world NAME SIZE SEED
  if [[ -f "data/generated/$1/transactions.parquet" ]]; then say "world $1 exists"
  else run py -m wapsi.synth --size "$2" --seed "$3" --name "$1" --out "data/generated/$1" --golden-dir ""; fi
}
hf_download() { run env "HF_HOME=${HF_CACHE_DIR}" uvx --from "huggingface_hub==${HF_HUB_VERSION}" hf download "$1"; }
DOCS_INDEX="data/docs/index/chunks.jsonl"
docs_index() {  # the legal-corpus index (docs search for the cases and the app). data/docs/raw is not in git, so a
  # fresh clone fetches the public sources first; a failure is non-fatal (the cases then run without docs search,
  # recorded as docs_index=missing). Faster and independent of the sites: copy data/docs/raw from a laptop first.
  # WAPSI_SKIP_DOCS=1 makes no network call at all (offline runs, tests).
  if [[ "${WAPSI_SKIP_DOCS:-0}" == "1" ]]; then say "WAPSI_SKIP_DOCS=1: no docs index"; note docs_index "skipped (WAPSI_SKIP_DOCS=1)"; return 0; fi
  if [[ "${DRY_RUN}" == "0" && -f "${DOCS_INDEX}" ]]; then say "docs index exists (${DOCS_INDEX})"; note docs_index present; return 0; fi
  if [[ "${DRY_RUN}" == "1" || ! -f data/docs/raw/_fetch.json ]]; then run_soft py -m wapsi.search.fetch; fi
  run_soft py -m wapsi.search.build
  run_soft git checkout -- data/docs/MANIFEST.md  # fetch/build refresh the committed manifest: keep the tree clean
  if [[ "${DRY_RUN}" == "1" || -f "${DOCS_INDEX}" ]]; then note docs_index built
  else
    note docs_index "missing (the cases run without docs search)"
    echo "  WARNING: no docs index: the cases run without docs search. Copy the corpus from a laptop:"
    echo "           scp -r data/docs/raw <this machine>:${ROOT}/data/docs/   then: uv run python -m wapsi.search.build"
  fi
}
inr_args() { [[ -n "${INR_PER_USD}" ]] && printf '%s\n' --inr-per-usd "${INR_PER_USD}"; return 0; }
run_notes() {  # the commit and the baseline tag this stage ran at
  note git_commit "$(git rev-parse HEAD)"
  note amd_baseline "$(git rev-parse -q --verify 'refs/tags/amd-baseline^{commit}' || echo missing)"
}
plan_get() {  # plan_get FILE KEY: one value of a model_plan.env (KEY=value lines; never evaluated as code)
  [[ -f "$1" ]] || return 0
  local line; line="$(grep -E "^$2=" "$1" | tail -n 1 || true)"
  printf '%s' "${line#*=}" | tr -d '\r'
}

# ------------------------------------------------------------------ preflight flags
preflight_args() {  # the operator's explicit overrides also relax the matching preflight rows (FAIL -> WARN)
  [[ "${NO_BASELINE}" == "1" ]] && printf '%s
' --allow-no-baseline
  [[ "${ALLOW_AFTER}" == "1" ]] && printf '%s
' --allow-after-window
  return 0
}

# ------------------------------------------------------------------ droplet stages (MI300X, docker compose)
gpu_env() {
  # shellcheck disable=SC2046
  [[ "${SKIP_PREFLIGHT}" == "1" ]] || run bash "${KICK}/preflight.sh" --mode droplet $(preflight_args) --json "${EVID}/preflight.json"
  run slog static "${EVID}/amd_smi_static.json"
  run_notes
  if [[ ! -f "${ENV_FILE}" ]]; then
    run cp "${AMD}/.env.example" "${ENV_FILE}"; run chmod 600 "${ENV_FILE}"
  fi
  env_set HF_CACHE_DIR "${HF_CACHE_DIR}"
  env_set WAPSI_MODELS_DIR "${WAPSI_MODELS_DIR}"
  run mkdir -p "${WORK}/train-image" "${HF_CACHE_DIR}" "${WAPSI_MODELS_DIR}" "${EVID}/bench"
  run uv sync --frozen --no-dev --extra docs  # docs: pypdf, to build the legal-corpus index below
  synth_world demo demo 7
  synth_world full full 7
  synth_world hard hard 7
  local s
  for s in 101 202 303 404 505; do synth_world "heldout-${s}" demo "${s}"; done
  for s in $(seq 1001 1010) 2001 2002; do synth_world "gnn-${s}" demo "${s}"; done
  run_sh "seq 3001 $((3000 + GNN_EXTRA_WORLDS)) | xargs -P 32 -I{} sh -c 'test -f data/generated/gnn-{}/transactions.parquet || .venv/bin/python -m wapsi.synth --size demo --seed {} --name gnn-{} --out data/generated/gnn-{} --golden-dir \"\" >/dev/null'"
  docs_index  # before the compose build: the app image copies data/docs
  run bash "${AMD}/serving/pull_and_pin.sh" "ROCM_PYTORCH_IMAGE=${ROCM_PYTORCH_IMAGE}"
  run_sh "grep -E '^[A-Z_]+_IMAGE(_TAG)?=' '${ENV_FILE}' > '${EVID}/images.txt'"
  run_sh "uv export --frozen --no-dev --extra amd --extra ml --extra amd-app --no-hashes --no-emit-project --no-header --no-annotate | grep -Ev '^(torch|triton|pytorch-triton|nvidia-|cuda-)' > '${WORK}/train-image/requirements-rocm.txt'"
  run docker build -f "${KICK}/Dockerfile.train" --build-arg "BASE_IMAGE=${ROCM_PYTORCH_IMAGE}" -t "${TRAIN_IMAGE}" "${WORK}/train-image"
  # day-1 check of the uv.lock library set (resolved against a newer torch) on the image's ROCm torch: imports, a
  # LoRA forward/backward step and the versions, into the evidence. A failure stops here, not hours later in lora-train.
  in_rocm python3 -m editions.amd.kickoff.train_image_check --require-gpu --out "${EVID}/train_image_check.json"
  run "${COMPOSE[@]}" --profile gpu --profile gnn build
  run install -d -o 10001 -g 10001 "${AMD}/bench/results"
}

gpu_models() {
  # the plan is recorded with the evidence (drafting model, judge, whether the drafting model is a Gemma model);
  # on the droplet the variables above already decide what is downloaded
  run_soft sys_py "${KICK}/model_plan.py" --mode droplet --detect-vram --out "${EVID}/model_plan.json"
  hf_download "${BASE_MODEL}"
  hf_download "${VERIFIER_MODEL}"
  run_sh "du -sh '${HF_CACHE_DIR}' | tee '${EVID}/models_du.txt'"
  note base_model "${BASE_MODEL}"
  note verifier_model "${VERIFIER_MODEL}"
}

gpu_gnn() {
  local ck="${AMD}/gnn/checkpoints" s
  local worlds=(data/generated/full data/generated/demo)
  for s in $(seq 1001 1010) $(seq 3001 $((3000 + GNN_EXTRA_WORLDS))); do worlds+=("data/generated/gnn-${s}"); done
  local common=(--worlds "${worlds[@]}" --val-worlds data/generated/gnn-2001 data/generated/gnn-2002
    --test-worlds data/generated/heldout-404 data/generated/heldout-505 --device cuda --epochs 100 --batch-size 256
    --hidden 128 --layers 3 --workers "${GNN_WORKERS}")
  local aml=()
  if [[ -f "${AMLWORLD_CSV}" ]]; then aml=(--amlworld "/aml/$(basename "${AMLWORLD_CSV}")" --amlworld-epochs 50)
  else say "AMLworld CSV not found at ${AMLWORLD_CSV}: training without AMLworld pre-training"; fi
  note amlworld "$([[ ${#aml[@]} -gt 0 ]] && echo used || echo skipped)"
  in_rocm python3 -m editions.amd.gnn.train "${common[@]}" "${aml[@]}" --out "${ck}/mi300x-base"
  in_rocm python3 -m editions.amd.gnn.train "${common[@]}" --drop-features fri_risk mulehunter_score ncrp_prior_reports \
    --out "${ck}/mi300x-noext"
  in_rocm python3 -m editions.amd.gnn.evaluate --gnn "${ck}/mi300x-base" --gnn-ablated "${ck}/mi300x-noext" \
    --ablate-external --device cuda --secondary-worlds data/generated/heldout-101 data/generated/heldout-202 \
    data/generated/heldout-303 --label "MI300X run (AMD Developer Cloud), ${DATE}" --out "${EVID}/gnn_eval.json"
  in_rocm python3 -m evals.harness --world hard --no-draft --scorer heuristic --scorer "gnn:${ck}/mi300x-base" \
    --out "${EVID}" --report-name gnn_hard_eval
  run cp "${ck}/mi300x-base/metrics.json" "${EVID}/gnn_metrics.json"
  run cp "${ck}/mi300x-noext/metrics.json" "${EVID}/gnn_noext_metrics.json"
}

gpu_lora_data() {
  run env "WAPSI_LLM_PROFILE=${BASE_PROFILE}" "WAPSI_LLM_MODEL=${BASE_MODEL}" VLLM_GPU_UTIL=0.90 COMPOSE_PROFILES=gpu \
    "${COMPOSE[@]}" up -d llm
  wait_http "${LLM_URL}/models" 2700
  run_soft py -m editions.amd.serving.smoke_test --base-url "${LLM_URL}"
  measure llm_bench_base py -m editions.amd.bench.llm_bench --base-url "${LLM_URL}" --concurrency 1 4 16 64 \
    --name llm_bench_base --out "${EVID}/bench"
  run py -m editions.amd.lora.dataset --teacher "openai:${LLM_URL}|${SERVED_NAME}|VLLM_API_KEY" --samples 2 \
    --sample-temperature 0.7 --workers 32 --out "${WORK}/lora-data" --summary-out "${EVID}/lora_dataset.json"
  run "${COMPOSE[@]}" stop llm
}

gpu_lora_train() {
  run_soft "${COMPOSE[@]}" stop llm verifier-llm gnn app
  in_rocm python3 -m editions.amd.lora.train --config "${LORA_CONFIG}" --data /wk/lora-data --out /wk/lora/run --dry-run
  in_rocm python3 -m editions.amd.lora.train --config "${LORA_CONFIG}" --data /wk/lora-data --out /wk/lora/run
  run cp "${WORK}/lora/run/train_metrics.json" "${EVID}/lora_train_metrics.json"
  say "BACK UP THE ADAPTER NOW: scp -r root@DROPLET_IP:${WORK}/lora/run/adapter ./  (or a private HF repo)"
}

gpu_merge() {
  in_rocm python3 -m editions.amd.lora.merge --run /wk/lora/run --out "/models/${MERGED_NAME}" --device cuda \
    --merge-dtype fp32 --save-dtype bf16
  run cp "${WAPSI_MODELS_DIR}/${MERGED_NAME}/merge_report.json" "${EVID}/lora_merge_report.json"
}

gpu_lora_eval() {
  local common=(env "HF_CACHE_DIR=${HF_CACHE_DIR}" "WAPSI_MODELS_DIR=${WAPSI_MODELS_DIR}" VLLM_GPU_UTIL=0.42)
  run_soft docker rm -f wapsi-eval-base wapsi-eval-tuned
  run "${common[@]}" "WAPSI_LLM_MODEL=${BASE_MODEL}" WAPSI_LLM_SERVED_NAME=base VLLM_HOST_PORT=8000 \
    VLLM_CONTAINER_NAME=wapsi-eval-base bash "${AMD}/serving/start_llm.sh" docker "${BASE_PROFILE}"
  wait_http "http://127.0.0.1:8000/v1/models" 2700
  run "${common[@]}" "WAPSI_LLM_MODEL=/models/${MERGED_NAME}" WAPSI_LLM_SERVED_NAME=tuned VLLM_HOST_PORT=8002 \
    VLLM_CONTAINER_NAME=wapsi-eval-tuned bash "${AMD}/serving/start_llm.sh" docker "${MERGED_PROFILE}"
  wait_http "http://127.0.0.1:8002/v1/models" 2700
  local ref=()
  if [[ -n "${WAPSI_EVAL_GEMINI_MODEL:-}" && -n "${GEMINI_API_KEY:-}" ]]; then  # evaluation reference only, never training data
    ref=(--teacher "openai:https://generativelanguage.googleapis.com/v1beta/openai|${WAPSI_EVAL_GEMINI_MODEL}|GEMINI_API_KEY")
  fi
  run py -m editions.amd.lora.evaluate --base "openai:http://127.0.0.1:8000/v1|base|VLLM_API_KEY" \
    --tuned "openai:http://127.0.0.1:8002/v1|tuned|VLLM_API_KEY" "${ref[@]}" --workers 8 --out "${WORK}/lora-eval" \
    --details "${WORK}/lora-eval/details.jsonl"
  run cp "${WORK}/lora-eval/eval.json" "${EVID}/lora_eval.json"
  run cp "${WORK}/lora-eval/eval.md" "${EVID}/lora_eval.md"
  run docker rm -f wapsi-eval-base wapsi-eval-tuned
}
gpu_lora_eval_on_failure() {  # the runner calls <stage fn>_on_failure after a failed stage: free port 8000 for serve
  run_soft docker rm -f wapsi-eval-base wapsi-eval-tuned
}

gpu_serve() {
  local profile="${MERGED_PROFILE}" model="/models/${MERGED_NAME}"
  run_soft docker rm -f wapsi-eval-base wapsi-eval-tuned  # a failed or interrupted lora-eval may still hold port 8000
  if [[ "${DRY_RUN}" == "0" && ! -f "${WAPSI_MODELS_DIR}/${MERGED_NAME}/config.json" ]]; then
    echo "  WARNING: no merged checkpoint at ${WAPSI_MODELS_DIR}/${MERGED_NAME}: serving the BASE model (say so in results)"
    profile="${BASE_PROFILE}"; model="${BASE_MODEL}"
  fi
  note served_profile "${profile}"
  env_set WAPSI_LLM_PROFILE "${profile}"
  env_set WAPSI_LLM_MODEL "${model}"
  env_set COMPOSE_PROFILES gpu,verifier,gnn
  env_set WAPSI_VERIFIER_BASE_URL http://verifier-llm:8000/v1
  env_set WAPSI_GNN_URL http://gnn:8081
  env_set WAPSI_GNN_MODEL mi300x-base
  run "${COMPOSE[@]}" up -d llm
  wait_http "${LLM_URL}/models" 2700
  run "${COMPOSE[@]}" up -d
  wait_http "${VERIFIER_URL}/models" 1800
  wait_http "${GNN_URL}/healthz" 600
  wait_http "${APP_URL}/healthz" 600
  run py -m editions.amd.serving.smoke_test --base-url "${LLM_URL}"
  run py -m editions.amd.serving.smoke_test --base-url "${VERIFIER_URL}" --skip-pipeline
  run_sh "curl -s ${APP_URL}/healthz > '${EVID}/serve_healthz.json'"
  run_sh "${COMPOSE[*]} ps > '${EVID}/serve_compose_ps.txt'"
}

gpu_eval() {
  wait_http "${LLM_URL}/models" 60 5  # fails fast when nothing is serving: run --stage serve first
  measure golden_eval py -m editions.amd.kickoff.golden_eval --llm-url "${LLM_URL}" --llm-model "${SERVED_NAME}" \
    --world "${E2E_WORLD}" --label "MI300X run (AMD Developer Cloud), ${DATE}" --out "${EVID}"
}

gpu_bench() {
  local out="${EVID}/bench" c
  wait_http "${LLM_URL}/models" 60 5  # fails fast when nothing is serving: run --stage serve first
  run mkdir -p "${out}"
  run_sh "amd-smi metric --json > '${out}/amd_smi_metric_raw.json' || true; amd-smi process --json > '${out}/amd_smi_process_raw.json' || true"
  bg_start telemetry python3 "${AMD}/bench/gpu_telemetry.py" --interval 2 --out "${out}"
  # every measurement below is `measure`: a failed request or case is recorded (stage notes, the result file) and
  # the rest still runs, so the telemetry is always stopped and the report always written
  measure llm_bench py -m editions.amd.bench.llm_bench --base-url "${LLM_URL}" --concurrency 1 4 16 64 \
    --input-len 1024 --output-len 256 --out "${out}"
  measure llm_bench_wapsi_prompts py -m editions.amd.bench.llm_bench --base-url "${LLM_URL}" --prompts wapsi \
    --concurrency 1 8 32 --name llm_bench_wapsi_prompts --out "${out}"
  measure llm_bench_verifier py -m editions.amd.bench.llm_bench --base-url "${VERIFIER_URL}" --concurrency 1 4 16 \
    --name llm_bench_verifier --out "${out}"
  for c in 1 8 16; do
    # shellcheck disable=SC2046
    measure "e2e_c${c}" py -m editions.amd.bench.e2e_bench --world "${E2E_WORLD}" --concurrency "${c}" \
      --llm-url "${LLM_URL}" --verifier-url "${VERIFIER_URL}" --gnn-url "${GNN_URL}" --gpu-hour-usd "${GPU_HOUR_USD}" \
      $(inr_args) --name "e2e_c${c}" --out "${out}"
  done
  bg_stop telemetry INT
  run py -m editions.amd.bench.report --results-dir "${out}" --out "${EVID}/AMD_RESULTS.md"
  run cp "${EVID}/AMD_RESULTS.md" "${AMD}/AMD_RESULTS.md"
}

gpu_ab() {
  wait_http "${LLM_URL}/models" 60 5
  # a case error is recorded in the report (both arms are then compared on the cases both completed); the A/B exits
  # non-zero only when no case could be compared, and even then the stage records it and goes on to evidence
  measure ab_hallucination py -m editions.amd.kickoff.ab_hallucination --llm-url "${LLM_URL}" \
    --llm-model "${SERVED_NAME}" --world "${E2E_WORLD}" --limit 20 --workers 8 \
    --label "MI300X run (AMD Developer Cloud), ${DATE}" --out "${EVID}/ab_hallucination.json"
}

ensure_static() {  # the GPU identity for this evidence folder: a later-week --stage bench,ab,evidence run skips env
  if [[ "${DRY_RUN}" == "1" ]] || ! grep -qs '"available": true' "${EVID}/amd_smi_static.json"; then
    run slog static "${EVID}/amd_smi_static.json"
  else
    say "amd-smi static already captured (${EVID}/amd_smi_static.json)"
  fi
}

evidence_hints() {  # evidence_hints WHAT-TO-COMMIT: the last steps, the same in every mode
  run_soft py "${KICK}/fill_pitch.py" --evidence "${EVID}/evidence.json"
  say "review the diff above, then: uv run python ${KICK}/fill_pitch.py --evidence ${EVID}/evidence.json --write"
  say "then: uv run python pitch/check_numbers.py (must print PASS)"
  say "commit: git add $1 pitch && git commit -m 'AMD: ${MODE} run ${DATE}'"
}

gpu_evidence() {
  ensure_static
  run_notes
  run py "${KICK}/collect_evidence.py" --evidence-dir "${EVID}" --mode droplet
  evidence_hints "${EVID} ${AMD}/AMD_RESULTS.md ${AMD}/gnn/results"
}

# ------------------------------------------------------------------ endpoint stages (no GPU box)
# The tools read WAPSI_LLM_BASE_URL / WAPSI_LLM_API_KEY from the environment (the key is never on a command line).
ep_plan() {  # after the models stage: the plan's drafting model and judge are what every later stage uses
  [[ "${DRY_RUN}" == "0" ]] || return 0
  local f="${EVID}/model_plan.env" v
  v="$(plan_get "${f}" PLAN_DRAFT_MODEL)"; if [[ -n "${v}" ]]; then EP_MODEL="${v}"; fi
  export WAPSI_LLM_MODEL="${EP_MODEL}" WAPSI_LLM_RPM="${EP_RPM}"
  if [[ -z "${WAPSI_JUDGE_MODEL:-}" ]]; then
    v="$(plan_get "${f}" PLAN_JUDGE_MODEL)"; if [[ -n "${v}" ]]; then export WAPSI_JUDGE_MODEL="${v}"; fi
  fi
}

ep_env() {
  # shellcheck disable=SC2046
  [[ "${SKIP_PREFLIGHT}" == "1" ]] || run bash "${KICK}/preflight.sh" --mode endpoint $(preflight_args) --json "${EVID}/preflight.json"
  run_notes
  run mkdir -p "${WORK}" "${EVID}/bench"
  # this checkout's environment as it is: never an exact or --no-dev sync here (on a laptop it would remove extras)
  if [[ "${DRY_RUN}" == "1" ]] || ! py -c 'import wapsi, httpx, pyarrow, pypdf' >/dev/null 2>&1; then
    say "only when the environment cannot import wapsi, httpx, pyarrow and pypdf:"
    run uv sync --frozen --inexact --extra docs
  else
    say "python environment ready ($(pybin))"
  fi
  case "${E2E_WORLD}" in
    showcase-memory) say "world showcase-memory: the in-memory showcase ledger (nothing to generate)" ;;
    demo|full|hard) synth_world "${E2E_WORLD}" "${E2E_WORLD}" 7 ;;
    *) say "world ${E2E_WORLD}: expected under data/generated/${E2E_WORLD} with its golden set" ;;
  esac
  docs_index
  # who answers: endpoint host, provider, the model's card and price list, the limits, one short completion
  run py "${KICK}/endpoint.py" probe --out "${EVID}/endpoint.json"
}

ep_models() {
  run py "${KICK}/model_plan.py" --mode endpoint --endpoint-json "${EVID}/endpoint.json" \
    --out "${EVID}/model_plan.json" --shell "${EVID}/model_plan.env"
  ep_plan
  note drafting_model "${EP_MODEL}"
  note judge_model "${WAPSI_JUDGE_MODEL:-none (rules-only screening)}"
  note endpoint_host "$(printf '%s' "${EP_URL}" | sed -E 's#^[a-z]+://([^/]+).*#\1#')"
}

ep_eval() {
  ep_plan
  measure golden_eval py -m editions.amd.kickoff.golden_eval --endpoint --world "${E2E_WORLD}" \
    --limit "${EP_EVAL_LIMIT}" --out "${EVID}"
}

ep_bench() {
  local out="${EVID}/bench" c
  ep_plan
  run mkdir -p "${out}"
  say "no GPU on this machine: no amd-smi telemetry (the GPU is the provider's)"
  note telemetry "none (endpoint mode: provider-side GPU)"
  # each level starts when the last 60 s leave room for its whole burst (--rpm), so the concurrency is real
  # shellcheck disable=SC2086
  measure llm_bench py -m editions.amd.bench.llm_bench --base-url "${EP_URL:-WAPSI_LLM_BASE_URL}" --model "${EP_MODEL}" \
    --concurrency ${EP_BENCH_LEVELS} --requests "${EP_BENCH_REQUESTS}" --rpm "${EP_RPM}" --no-ignore-eos \
    --input-len 1024 --output-len 256 --warmup 1 --label auto --out "${out}"
  # the real drafting requests of the showcase cases (about 4k tokens in, JSON out without a strict schema)
  # shellcheck disable=SC2086
  measure llm_bench_wapsi_prompts py -m editions.amd.bench.llm_bench --base-url "${EP_URL:-WAPSI_LLM_BASE_URL}" \
    --model "${EP_MODEL}" --prompts wapsi --concurrency ${EP_WAPSI_LEVELS} --requests "${EP_WAPSI_REQUESTS}" \
    --rpm "${EP_RPM}" --warmup 0 --label auto --name llm_bench_wapsi_prompts --out "${out}"
  for c in ${EP_E2E_CONCURRENCY}; do
    # shellcheck disable=SC2046
    measure "e2e_c${c}" py -m editions.amd.bench.e2e_bench --endpoint --world "${E2E_WORLD}" \
      --limit "${EP_E2E_LIMIT}" --concurrency "${c}" $(inr_args) --name "e2e_c${c}" --out "${out}"
  done
}

ep_ab() {
  ep_plan
  # the bound arm's drafting requests for cases the eval stage already ran come from the disk cache
  measure ab_hallucination py -m editions.amd.kickoff.ab_hallucination --endpoint --world "${E2E_WORLD}" \
    --limit "${EP_AB_LIMIT}" --workers "${EP_WORKERS}" --out "${EVID}/ab_hallucination.json"
}

ep_evidence() {
  run_notes
  if [[ "${DRY_RUN}" == "1" || ! -f "${EVID}/endpoint.json" ]]; then  # a later --stage evidence run in a new folder
    run py "${KICK}/endpoint.py" probe --out "${EVID}/endpoint.json"
  fi
  run py "${KICK}/collect_evidence.py" --evidence-dir "${EVID}" --mode endpoint
  evidence_hints "${EVID}"
}

# ------------------------------------------------------------------ notebook stages (AMD ROCm JupyterLab, no Docker)
NB_CK="${AMD}/gnn/checkpoints"
NB_LORA=1
nb_plan() {  # after the models stage: the plan (by GPU memory) decides the model, the LoRA preset and whether to fine-tune
  [[ "${DRY_RUN}" == "0" ]] || return 0
  local f="${WORK}/model_plan.env" v
  v="$(plan_get "${f}" PLAN_DRAFT_MODEL)"; if [[ -n "${v}" ]]; then BASE_MODEL="${v}"; fi
  if [[ -f "${f}" ]]; then VERIFIER_MODEL="$(plan_get "${f}" PLAN_JUDGE_MODEL)"; fi
  v="$(plan_get "${f}" PLAN_LORA_CONFIG)"; if [[ -n "${v}" ]]; then LORA_CONFIG="${v}"; fi
  v="$(plan_get "${f}" PLAN_MERGED_NAME)"; if [[ -n "${v}" ]]; then MERGED_NAME="${v}"; fi
  v="$(plan_get "${f}" PLAN_BASE_PROFILE)"; if [[ -n "${v}" ]]; then BASE_PROFILE="${v}"; fi
  v="$(plan_get "${f}" PLAN_MERGED_PROFILE)"; if [[ -n "${v}" ]]; then MERGED_PROFILE="${v}"; fi
  v="$(plan_get "${f}" PLAN_LORA)"; if [[ -n "${v}" ]]; then NB_LORA="${v}"; fi
}
nb_lora_on() {  # the LoRA stages run unless the plan found too little GPU memory to fine-tune (WAPSI_LORA=1 forces)
  [[ "${NB_LORA}" == "1" ]] && return 0
  say "the model plan skips the LoRA fine-tune on this GPU (too little memory for ${BASE_MODEL}); the base model is served"
  note skipped "LoRA not planned on this GPU (model plan); WAPSI_LORA=1 forces it"
  return 1
}
nb_stack() {  # vllm | transformers: what serves the model in this notebook (WAPSI_NB_SERVER overrides the detection)
  case "${NB_SERVER}" in
    vllm|transformers) echo "${NB_SERVER}" ;;
    *) if [[ "${DRY_RUN}" == "1" ]] || "${NB_PY}" -c 'import vllm' >/dev/null 2>&1; then echo vllm; else echo transformers; fi ;;
  esac
}
nb_start_server() {  # nb_start_server NAME PROFILE MODEL SERVED-NAME PORT GPU-SHARE: background, no Docker
  local name="$1" profile="$2" model="$3" served="$4" port="$5" util="$6"
  if [[ "$(nb_stack)" == "vllm" ]]; then
    # start_llm.sh python: the same flags as the container (Gemma 4 attention backend, text-only limits), run as
    # `python -m vllm.entrypoints.openai.api_server`, bound to 127.0.0.1
    bg_start "${name}" "WAPSI_PYTHON=${NB_PY}" "WAPSI_LLM_MODEL=${model}" "WAPSI_LLM_SERVED_NAME=${served}" \
      "WAPSI_VERIFIER_MODEL_ID=${model}" "WAPSI_VERIFIER_SERVED_NAME=${served}" "VLLM_GPU_UTIL=${util}" \
      "VERIFIER_GPU_UTIL=${util}" "VLLM_PORT=${port}" VLLM_HOST=127.0.0.1 bash "${AMD}/serving/start_llm.sh" python "${profile}"
    note serving_stack "vLLM (python -m vllm.entrypoints.openai.api_server)"
  else
    say "vLLM is not importable here: serving ${model} with transformers (one request at a time)"
    bg_start "${name}" py -m editions.amd.lora.mock_endpoint --llm "hf:${model}" --port "${port}" --served-name "${served}"
    note serving_stack "transformers (no vLLM in this notebook: one request at a time)"
  fi
}
nb_levels() { if [[ "$(nb_stack)" == "vllm" ]]; then echo "$1"; else echo 1; fi; }  # a single-stream server: level 1 only
nb_serving_model() {  # prints "PROFILE MODEL": the merged checkpoint when it exists, else the base model
  if [[ "${DRY_RUN}" == "1" || -f "${WAPSI_MODELS_DIR}/${MERGED_NAME}/config.json" ]]; then
    echo "${MERGED_PROFILE} ${WAPSI_MODELS_DIR}/${MERGED_NAME}"
  else echo "${BASE_PROFILE} ${BASE_MODEL}"; fi
}
nb_gnn_ready() { [[ "${DRY_RUN}" == "1" || -f "${NB_CK}/nb-base/metrics.json" ]]; }  # the gnn stage's checkpoint
nb_ensure_serving() {  # the drafting model (+ judge when planned and vLLM serves) and the GNN service, if not already up
  local pm profile model
  if [[ "${DRY_RUN}" == "1" && "${1:-}" != "show" ]]; then
    say "starts the serve stage's servers first if they are not running (drafting model, judge, GNN service)"
    return 0
  fi
  pm="$(nb_serving_model)"; profile="${pm%% *}"; model="${pm#* }"
  if [[ "${DRY_RUN}" == "1" ]] || ! bg_alive llm; then
    [[ "${model}" == "${BASE_MODEL}" ]] && say "no merged checkpoint: serving the BASE model ${BASE_MODEL} (say so in results)"
    note served_profile "${profile}"
    nb_start_server llm "${profile}" "${model}" "${SERVED_NAME}" 8000 "${VLLM_GPU_UTIL:-0.55}"
    wait_http "${LLM_URL}/models" 2700
  fi
  if [[ -n "${VERIFIER_MODEL}" && "$(nb_stack)" == "vllm" ]] && { [[ "${DRY_RUN}" == "1" ]] || ! bg_alive verifier; }; then
    nb_start_server verifier verifier "${VERIFIER_MODEL}" "${VERIFIER_NAME}" 8001 "${VERIFIER_GPU_UTIL:-0.15}"
    wait_http "${VERIFIER_URL}/models" 1800
  fi
  if ! nb_gnn_ready; then
    say "no GNN checkpoint at ${NB_CK}/nb-base (run --stage gnn): the cases use the heuristic scorer"
    note gnn_service "not served: no checkpoint"
  elif [[ "${DRY_RUN}" == "1" ]] || ! bg_alive gnn; then
    bg_start gnn "WAPSI_GNN_CHECKPOINT=${NB_CK}/nb-base" "WAPSI_GNN_WORLD=data/generated/${E2E_WORLD}" \
      WAPSI_GNN_DEVICE=cuda HOST=127.0.0.1 PORT=8081 py -m editions.amd.gnn.service
    wait_http "${GNN_URL}/healthz" 600
  fi
}
nb_gnn_args() { nb_gnn_ready && printf '%s\n' --gnn-url "${GNN_URL}"; return 0; }
nb_judge_args() {  # the judge is served only by vLLM, next to the drafting model
  [[ -n "${VERIFIER_MODEL}" && "$(nb_stack)" == "vllm" ]] && printf '%s\n' --verifier-url "${VERIFIER_URL}" --verifier-model "${VERIFIER_NAME}"
  return 0
}

nb_env() {
  # shellcheck disable=SC2046
  [[ "${SKIP_PREFLIGHT}" == "1" ]] || run bash "${KICK}/preflight.sh" --mode notebook $(preflight_args) --json "${EVID}/preflight.json"
  run mkdir -p "${WORK}" "${HF_CACHE_DIR}" "${WAPSI_MODELS_DIR}" "${EVID}/bench"
  run slog static "${EVID}/amd_smi_static.json"
  run py "${KICK}/stagelog.py" torch-info "${EVID}/torch_info.json"  # the GPU as the notebook's ROCm torch names it
  run_notes
  if [[ "${WAPSI_NB_SKIP_PIP:-0}" == "1" ]]; then
    say "WAPSI_NB_SKIP_PIP=1: using the notebook's python packages as they are"
  else
    # the notebook image's ROCm torch is never replaced: the uv.lock pins of everything else, with --no-deps so pip
    # cannot pull a CUDA wheel back in (the same rule as the droplet's training image)
    run_sh "command -v uv >/dev/null 2>&1 || ${NB_PY} -m pip install -q uv"
    run_sh "uv export --frozen --no-dev --extra amd --extra ml --extra amd-app --extra docs --no-hashes --no-emit-project --no-header --no-annotate | grep -Ev '^(torch|triton|pytorch-triton|nvidia-|cuda-)' > '${WORK}/requirements-rocm.txt'"
    run "${NB_PY}" -m pip install -q --no-deps -r "${WORK}/requirements-rocm.txt"
  fi
  # the library set on this ROCm torch: imports, declared torch ranges, one LoRA step on the GPU, versions
  run py -m editions.amd.kickoff.train_image_check --require-gpu --out "${EVID}/train_image_check.json"
  synth_world demo demo 7
  synth_world full full 7
  synth_world hard hard 7
  local s
  for s in 101 202 303 404 505; do synth_world "heldout-${s}" demo "${s}"; done
  for s in $(seq 1001 1010) 2001 2002; do synth_world "gnn-${s}" demo "${s}"; done
  run_sh "seq 3001 $((3000 + GNN_EXTRA_WORLDS)) | xargs -P 8 -I{} sh -c 'test -f data/generated/gnn-{}/transactions.parquet || ${NB_PY} -m wapsi.synth --size demo --seed {} --name gnn-{} --out data/generated/gnn-{} --golden-dir \"\" >/dev/null'"
  docs_index
}

nb_models() {
  # the largest Gemma 4 this GPU serves, and whether it can also fine-tune it (memory from torch_info.json)
  run py "${KICK}/model_plan.py" --mode notebook --torch-info "${EVID}/torch_info.json" --out "${EVID}/model_plan.json" \
    --shell "${WORK}/model_plan.env"
  nb_plan
  local m
  for m in "${BASE_MODEL}" ${VERIFIER_MODEL:+"${VERIFIER_MODEL}"}; do
    run "${NB_PY}" -c 'import sys; from huggingface_hub import snapshot_download; print(snapshot_download(sys.argv[1]))' "${m}"
  done
  run_sh "du -sh '${HF_CACHE_DIR}' | tee '${EVID}/models_du.txt'"
  note base_model "${BASE_MODEL}"
  note verifier_model "${VERIFIER_MODEL:-none (rules-only screening)}"
  note lora "$([[ "${NB_LORA}" == "1" ]] && echo "${LORA_CONFIG}" || echo skipped)"
}

nb_gnn() {
  local s
  local worlds=(data/generated/full data/generated/demo)
  for s in $(seq 1001 1010) $(seq 3001 $((3000 + GNN_EXTRA_WORLDS))); do worlds+=("data/generated/gnn-${s}"); done
  local common=(--worlds "${worlds[@]}" --val-worlds data/generated/gnn-2001 data/generated/gnn-2002
    --test-worlds data/generated/heldout-404 data/generated/heldout-505 --device cuda --epochs 100 --batch-size 256
    --hidden 128 --layers 3 --workers "${GNN_WORKERS}")
  local aml=()
  if [[ -f "${AMLWORLD_CSV}" ]]; then aml=(--amlworld "${AMLWORLD_CSV}" --amlworld-epochs 50)
  else say "AMLworld CSV not found at ${AMLWORLD_CSV}: training without AMLworld pre-training"; fi
  note amlworld "$([[ ${#aml[@]} -gt 0 ]] && echo used || echo skipped)"
  run py -m editions.amd.gnn.train "${common[@]}" "${aml[@]}" --out "${NB_CK}/nb-base"
  run py -m editions.amd.gnn.train "${common[@]}" --drop-features fri_risk mulehunter_score ncrp_prior_reports \
    --out "${NB_CK}/nb-noext"
  run py -m editions.amd.gnn.evaluate --gnn "${NB_CK}/nb-base" --gnn-ablated "${NB_CK}/nb-noext" --ablate-external \
    --device cuda --secondary-worlds data/generated/heldout-101 data/generated/heldout-202 data/generated/heldout-303 \
    --label "${NB_LABEL}" --out "${EVID}/gnn_eval.json"
  run py -m evals.harness --world hard --no-draft --scorer heuristic --scorer "gnn:${NB_CK}/nb-base" --out "${EVID}" \
    --report-name gnn_hard_eval
  run cp "${NB_CK}/nb-base/metrics.json" "${EVID}/gnn_metrics.json"
  run cp "${NB_CK}/nb-noext/metrics.json" "${EVID}/gnn_noext_metrics.json"
}

nb_lora_data() {
  nb_plan; nb_lora_on || return 0
  local workers; workers="$([[ "$(nb_stack)" == "vllm" ]] && echo 32 || echo 1)"
  nb_start_server llm "${BASE_PROFILE}" "${BASE_MODEL}" "${SERVED_NAME}" 8000 0.90
  wait_http "${LLM_URL}/models" 2700
  run_soft py -m editions.amd.serving.smoke_test --base-url "${LLM_URL}"
  # shellcheck disable=SC2046
  measure llm_bench_base py -m editions.amd.bench.llm_bench --base-url "${LLM_URL}" --concurrency $(nb_levels "1 4 16 64") \
    --name llm_bench_base --label "${NB_LABEL}" --out "${EVID}/bench"
  run py -m editions.amd.lora.dataset --teacher "openai:${LLM_URL}|${SERVED_NAME}|VLLM_API_KEY" --samples 2 \
    --sample-temperature 0.7 --workers "${workers}" --out "${WORK}/lora-data" --summary-out "${EVID}/lora_dataset.json"
  bg_stop llm
}
nb_lora_data_on_failure() { bg_stop llm; }

nb_lora_train() {
  nb_plan; nb_lora_on || return 0
  local n
  for n in llm verifier gnn; do bg_alive "${n}" && bg_stop "${n}"; done  # the fine-tune needs the whole GPU
  run py -m editions.amd.lora.train --config "${LORA_CONFIG}" --data "${WORK}/lora-data" --out "${WORK}/lora/run" --dry-run
  run py -m editions.amd.lora.train --config "${LORA_CONFIG}" --data "${WORK}/lora-data" --out "${WORK}/lora/run"
  run cp "${WORK}/lora/run/train_metrics.json" "${EVID}/lora_train_metrics.json"
  say "BACK UP THE ADAPTER NOW: download ${WORK}/lora/run/adapter from the notebook's file browser (or a private HF repo)"
}

nb_merge() {
  nb_plan; nb_lora_on || return 0
  run py -m editions.amd.lora.merge --run "${WORK}/lora/run" --out "${WAPSI_MODELS_DIR}/${MERGED_NAME}" --device cuda \
    --merge-dtype fp32 --save-dtype bf16
  run cp "${WAPSI_MODELS_DIR}/${MERGED_NAME}/merge_report.json" "${EVID}/lora_merge_report.json"
}

nb_lora_eval() {
  nb_plan; nb_lora_on || return 0
  # in-process (transformers): one model at a time, no second server
  run py -m editions.amd.lora.evaluate --model "base=hf:${BASE_MODEL}" --model "tuned=hf:${WAPSI_MODELS_DIR}/${MERGED_NAME}" \
    --device cuda --out "${WORK}/lora-eval" --details "${WORK}/lora-eval/details.jsonl"
  run cp "${WORK}/lora-eval/eval.json" "${EVID}/lora_eval.json"
  run cp "${WORK}/lora-eval/eval.md" "${EVID}/lora_eval.md"
}

nb_serve() {
  nb_plan
  nb_ensure_serving show
  run py -m editions.amd.serving.smoke_test --base-url "${LLM_URL}"
  if nb_gnn_ready; then run_sh "curl -s ${GNN_URL}/healthz > '${EVID}/serve_healthz.json'"; fi
}

nb_eval() {
  nb_plan
  nb_ensure_serving
  measure golden_eval py -m editions.amd.kickoff.golden_eval --llm-url "${LLM_URL}" --llm-model "${SERVED_NAME}" \
    --world "${E2E_WORLD}" --label "${NB_LABEL}" --out "${EVID}"
}

nb_bench() {
  local out="${EVID}/bench" c cost=(--no-gpu-cost)
  nb_plan
  nb_ensure_serving
  run mkdir -p "${out}"
  # a GPU-hour price only when the operator gave one: otherwise the cost per case stays empty (a placeholder)
  if [[ -n "${WAPSI_GPU_HOUR_USD:-}" ]]; then cost=(--gpu-hour-usd "${GPU_HOUR_USD}"); fi
  run_sh "amd-smi metric --json > '${out}/amd_smi_metric_raw.json' 2>/dev/null || true; amd-smi process --json > '${out}/amd_smi_process_raw.json' 2>/dev/null || true"
  bg_start telemetry "${NB_PY}" "${AMD}/bench/gpu_telemetry.py" --interval 2 --out "${out}"
  # shellcheck disable=SC2046
  measure llm_bench py -m editions.amd.bench.llm_bench --base-url "${LLM_URL}" --concurrency $(nb_levels "1 4 16 64") \
    --input-len 1024 --output-len 256 --label "${NB_LABEL}" --out "${out}"
  # shellcheck disable=SC2046
  measure llm_bench_wapsi_prompts py -m editions.amd.bench.llm_bench --base-url "${LLM_URL}" --prompts wapsi \
    --concurrency $(nb_levels "1 8 32") --name llm_bench_wapsi_prompts --label "${NB_LABEL}" --out "${out}"
  if [[ -n "${VERIFIER_MODEL}" && "$(nb_stack)" == "vllm" ]]; then
    measure llm_bench_verifier py -m editions.amd.bench.llm_bench --base-url "${VERIFIER_URL}" --concurrency 1 4 16 \
      --name llm_bench_verifier --label "${NB_LABEL}" --out "${out}"
  fi
  for c in $(nb_levels "1 8 16"); do
    # shellcheck disable=SC2046
    measure "e2e_c${c}" py -m editions.amd.bench.e2e_bench --world "${E2E_WORLD}" --concurrency "${c}" \
      --llm-url "${LLM_URL}" $(nb_judge_args) $(nb_gnn_args) "${cost[@]}" $(inr_args) --label "${NB_LABEL}" \
      --name "e2e_c${c}" --out "${out}"
  done
  bg_stop telemetry INT
  run py -m editions.amd.bench.report --results-dir "${out}" --out "${EVID}/AMD_RESULTS.md"
}

nb_ab() {
  nb_plan
  nb_ensure_serving
  measure ab_hallucination py -m editions.amd.kickoff.ab_hallucination --llm-url "${LLM_URL}" \
    --llm-model "${SERVED_NAME}" --world "${E2E_WORLD}" --limit 20 --workers "$([[ "$(nb_stack)" == "vllm" ]] && echo 8 || echo 1)" \
    --label "${NB_LABEL}" --out "${EVID}/ab_hallucination.json"
}

nb_evidence() {
  ensure_static
  if [[ "${DRY_RUN}" == "1" || ! -f "${EVID}/torch_info.json" ]]; then run py "${KICK}/stagelog.py" torch-info "${EVID}/torch_info.json"; fi
  run_notes
  run py "${KICK}/collect_evidence.py" --evidence-dir "${EVID}" --mode notebook
  evidence_hints "${EVID} ${AMD}/gnn/results"
}

# ------------------------------------------------------------------ CPU rehearsal stages (any laptop)
RW="${WORK}/worlds"
REHEARSAL_LABEL="CPU rehearsal (tiny worlds, tiny random model, stub LLMs): NOT an MI300X result"
PORTS_FILE="${WORK}/ports.env"
load_ports() {  # ports of the rehearsal's local servers (serve stage), or placeholders in a dry run
  if [[ "${DRY_RUN}" == "0" && -f "${PORTS_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${PORTS_FILE}"
  else
    LLM_PORT="${LLM_PORT:-LLM_PORT}"; VER_PORT="${VER_PORT:-VER_PORT}"; GNN_PORT="${GNN_PORT:-GNN_PORT}"
  fi
}
free_port() { if [[ "${DRY_RUN}" == "1" ]]; then echo "$1"; else slog free-port; fi; }

cpu_env() {
  [[ "${SKIP_PREFLIGHT}" == "1" ]] || run bash "${KICK}/preflight.sh" --rehearsal --json "${EVID}/preflight.json"
  run slog static "${EVID}/amd_smi_static.json"
  note git_commit "$(git rev-parse HEAD)"
  run mkdir -p "${WORK}" "${EVID}/bench"
  run py -m editions.amd.kickoff.rehearsal worlds --out "${RW}"
  # the day-1 training-image check, on this machine's torch (imports, torch ranges, one tiny LoRA step on CPU)
  run py -m editions.amd.kickoff.train_image_check --device cpu --out "${EVID}/train_image_check.json"
}

cpu_models() {
  say "CPU rehearsal: nothing to download; the tiny-cpu LoRA preset builds a random 2-layer model at train time"
  note base_model "tiny-cpu (random weights)"
  note verifier_model "stub (FakeLLM behind the OpenAI-compatible mock endpoint)"
}

cpu_gnn() {
  local ck="${WORK}/gnn"
  run py -m editions.amd.gnn.train --worlds "${RW}/kr-train" --val-worlds "${RW}/kr-val" --test-worlds "${RW}/kr-test" \
    --device cpu --threads 2 --epochs 2 --hidden 16 --edge-hidden 8 --limit-cases 3 --cache-dir "" \
    --amlworld "${RW}/aml-tiny.csv" --amlworld-epochs 1 --out "${ck}/rehearsal-base"
  run py -m editions.amd.gnn.evaluate --gnn "${ck}/rehearsal-base" --worlds "${RW}/kr-test" --xgb "" \
    --no-xgb-same-train --limit 2 --device cpu --label "${REHEARSAL_LABEL}" --out "${EVID}/gnn_eval.json"
  run py -m evals.harness --world kr-test --world-dir "${RW}/kr-test" --no-draft --limit 2 --scorer heuristic \
    --scorer "gnn:${ck}/rehearsal-base" --out "${EVID}" --report-name gnn_hard_eval
  run cp "${ck}/rehearsal-base/metrics.json" "${EVID}/gnn_metrics.json"
}

cpu_lora_data() {
  local port; port="$(free_port TEACHER_PORT)"
  bg_start teacher py -m editions.amd.lora.mock_endpoint --port "${port}" --llm fake --served-name rehearsal-fake-teacher
  wait_http "http://127.0.0.1:${port}/v1/models" 300 1
  run py -m editions.amd.serving.smoke_test --base-url "http://127.0.0.1:${port}/v1"
  run py -m editions.amd.bench.llm_bench --base-url "http://127.0.0.1:${port}/v1" --concurrency 1 2 --requests 4 \
    --input-len 64 --output-len 16 --warmup 1 --name llm_bench_base --out "${EVID}/bench"
  run py -m editions.amd.lora.dataset --teacher "openai:http://127.0.0.1:${port}/v1|rehearsal-fake-teacher" \
    --train-worlds showcase-memory --heldout-worlds "${RW}/heldout-kr" --limit-cases 2 --variants clean,injection \
    --corrections-frac 0.5 --out "${WORK}/lora-data" --summary-out "${EVID}/lora_dataset.json"
  bg_stop teacher
}

cpu_lora_train() {
  run py -m editions.amd.lora.train --config tiny-cpu --data "${WORK}/lora-data" --val-file "${WORK}/lora-data/test.jsonl" \
    --out "${WORK}/lora/run" --max-steps 4 --batch-size 1 --tiny-vocab-size 8192 --max-eval-examples 2 \
    --eval-steps 1000 --logging-steps 1
  run cp "${WORK}/lora/run/train_metrics.json" "${EVID}/lora_train_metrics.json"
}

cpu_merge() {
  run py -m editions.amd.lora.merge --run "${WORK}/lora/run" --out "${WORK}/models/rehearsal-merged" --check-prompts 1 \
    --check-max-tokens 256
  run cp "${WORK}/models/rehearsal-merged/merge_report.json" "${EVID}/lora_merge_report.json"
}

cpu_lora_eval() {
  run py -m editions.amd.lora.evaluate --model base=mock:fake --model "tuned=hf:${WORK}/models/rehearsal-merged" \
    --heldout-worlds "${RW}/heldout-kr" --limit-cases 1 --variants clean,injection --max-new-tokens 12 --device cpu \
    --out "${WORK}/lora-eval"
  run cp "${WORK}/lora-eval/eval.json" "${EVID}/lora_eval.json"
  run cp "${WORK}/lora-eval/eval.md" "${EVID}/lora_eval.md"
}

cpu_serve() {
  local llm ver gnn
  llm="$(free_port LLM_PORT)"; ver="$(free_port VER_PORT)"; gnn="$(free_port GNN_PORT)"
  run_sh "printf 'LLM_PORT=%s\nVER_PORT=%s\nGNN_PORT=%s\n' ${llm} ${ver} ${gnn} > '${PORTS_FILE}'"
  bg_start llm py -m editions.amd.lora.mock_endpoint --port "${llm}" --llm fake --served-name rehearsal-fake-llm
  bg_start verifier py -m editions.amd.lora.mock_endpoint --port "${ver}" --llm fake --served-name rehearsal-fake-verifier
  bg_start gnn "WAPSI_GNN_CHECKPOINT=${WORK}/gnn/rehearsal-base" "WAPSI_GNN_WORLD=${RW}/kr-test" \
    WAPSI_GNN_DEVICE=cpu "PORT=${gnn}" py -m editions.amd.gnn.service
  wait_http "http://127.0.0.1:${llm}/v1/models" 300 1
  wait_http "http://127.0.0.1:${ver}/v1/models" 300 1
  wait_http "http://127.0.0.1:${gnn}/healthz" 600 2
  run py -m editions.amd.serving.smoke_test --base-url "http://127.0.0.1:${llm}/v1"
  run py -m editions.amd.serving.smoke_test --base-url "http://127.0.0.1:${ver}/v1" --skip-pipeline
  run_sh "curl -s http://127.0.0.1:${gnn}/healthz > '${EVID}/serve_healthz.json'"
}

cpu_require_serving() {
  load_ports
  [[ "${DRY_RUN}" == "1" ]] && return 0
  local n
  for n in llm verifier gnn; do
    bg_alive "${n}" || { echo "  rehearsal server '${n}' is not running: run --stage serve first (or the whole rehearsal)"; return 1; }
  done
}

cpu_eval() {
  # the golden-set harness with the offline FakeLLM: the code path of the eval stage, never a model result
  run py -m editions.amd.kickoff.golden_eval --fake --world showcase-memory --limit 2 --out "${EVID}"
}

cpu_bench() {
  local out="${EVID}/bench" c
  cpu_require_serving
  run mkdir -p "${out}"
  say "no amd-smi on this machine: GPU telemetry is not sampled in the CPU rehearsal"
  note telemetry "skipped (CPU rehearsal, no amd-smi)"
  run py -m editions.amd.bench.llm_bench --base-url "http://127.0.0.1:${LLM_PORT}/v1" --concurrency 1 4 --requests 8 \
    --input-len 128 --output-len 32 --warmup 1 --out "${out}"
  run py -m editions.amd.bench.llm_bench --base-url "http://127.0.0.1:${LLM_PORT}/v1" --prompts wapsi --concurrency 1 2 \
    --requests 2 --warmup 0 --name llm_bench_wapsi_prompts --out "${out}"
  run py -m editions.amd.bench.llm_bench --base-url "http://127.0.0.1:${VER_PORT}/v1" --concurrency 1 4 --requests 8 \
    --input-len 128 --output-len 32 --warmup 1 --name llm_bench_verifier --out "${out}"
  for c in 1 4; do
    run py -m editions.amd.bench.e2e_bench --world showcase-memory --fake-llm --gnn-url "http://127.0.0.1:${GNN_PORT}" \
      --concurrency "${c}" --gpu-hour-usd "${GPU_HOUR_USD}" $(inr_args) --no-search --name "e2e_c${c}" --out "${out}"
  done
  run py -m editions.amd.bench.report --results-dir "${out}" --out "${EVID}/AMD_RESULTS.md"
}

cpu_ab() {
  run py -m editions.amd.kickoff.ab_hallucination --fake --world showcase-memory --limit 2 --workers 2 \
    --out "${EVID}/ab_hallucination.json"
}

cpu_evidence() {
  run py "${KICK}/collect_evidence.py" --evidence-dir "${EVID}" --mode rehearsal
  say "fill_pitch.py must refuse rehearsal evidence (exit 3): proves CPU numbers can never reach the pitch"
  if [[ "${DRY_RUN}" == "1" ]]; then show py "${KICK}/fill_pitch.py" --evidence "${EVID}/evidence.json"; return 0; fi
  local rc=0
  py "${KICK}/fill_pitch.py" --evidence "${EVID}/evidence.json" || rc=$?
  [[ ${rc} -eq 3 ]] || { echo "  fill_pitch.py accepted rehearsal evidence (exit ${rc}): BUG"; return 1; }
  echo "  ok: fill_pitch.py refused the rehearsal evidence"
}

# ------------------------------------------------------------------ the run
# Sourced (tests: `source run_all.sh --rehearsal --dry-run`), stop here: the helpers and stage functions are defined
# and nothing has run.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then return 0; fi

selected() {
  local name="$1"
  if [[ -n "${ONLY}" ]]; then [[ ",${ONLY}," == *",${name},"* ]]; return; fi
  if [[ -n "${FROM}" ]]; then (( $(stage_index "${name}") >= $(stage_index "${FROM}") )); return; fi
  return 0
}
EXPLICIT=0
[[ -n "${ONLY}${FROM}" ]] && EXPLICIT=1
case "${MODE}" in droplet) PREFIX=gpu ;; notebook) PREFIX=nb ;; endpoint) PREFIX=ep ;; *) PREFIX=cpu ;; esac
title_of() {
  case "${MODE}" in
    droplet) stage_title "$1" ;; notebook) notebook_title "$1" ;; endpoint) endpoint_title "$1" ;; *) rehearsal_title "$1" ;;
  esac
}

RESULTS=()
FAILED=0
on_exit() {
  local rc=$?
  stop_all_bg
  if [[ "${DRY_RUN}" == "0" && ${#RESULTS[@]} -gt 0 ]]; then
    echo; echo "== summary (${MODE}, evidence ${EVID})"
    printf '   %s\n' "${RESULTS[@]}"
    case "${MODE}" in
      droplet)
        slog budget --evid "${EVID}" --mode droplet || true
        echo "REMINDER: a powered-off droplet STILL BILLS. When the GPU work is done: git push the evidence, copy the"
        echo "          adapter and checkpoints off the box, docker compose down, then DESTROY the droplet in the console." ;;
      notebook)
        slog budget --evid "${EVID}" --mode notebook || true
        echo "REMINDER: a running notebook instance spends credits. When the GPU work is done: git push the evidence,"
        echo "          download the adapter and checkpoints, then stop and DELETE the notebook instance in the portal." ;;
      endpoint)
        echo "Endpoint mode: nothing to shut down. The API's daily cost counter is in ${EVID}/endpoint.json (limits)." ;;
    esac
  fi
  exit "${rc}"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

echo "Wapsi AMD kickoff pipeline: mode=${MODE}$([[ "${DRY_RUN}" == "1" ]] && echo " (dry run)") date=${DATE}"
echo "  evidence: ${EVID}"
echo "  work:     ${WORK}"
case "${MODE}" in
  droplet)
    echo "  models:   base ${BASE_MODEL} | verifier ${VERIFIER_MODEL} | LoRA preset ${LORA_CONFIG} | merged /models/${MERGED_NAME}"
    echo "  budget:   \$${GPU_HOUR_USD}/GPU-hour, cap \$${WAPSI_BUDGET_USD:-100}, warn ${WAPSI_BUDGET_WARN_PCT:-75}%, stop ${WAPSI_BUDGET_STOP_PCT:-90}% (stagelog.py budget)" ;;
  notebook)
    echo "  models:   planned by GPU memory in the models stage (default ${BASE_MODEL}; judge ${VERIFIER_MODEL}); python ${NB_PY}"
    echo "  serving:  ${NB_SERVER} (vLLM python entrypoint when importable, else transformers); no Docker" ;;
  endpoint)
    KEY_STATE="NOT SET"
    if [[ -n "${WAPSI_LLM_API_KEY:-}${RADEON_API_KEY:-}${VLLM_API_KEY:-}" ]]; then KEY_STATE="set (value not shown)"; fi
    echo "  endpoint: ${EP_URL:-<WAPSI_LLM_BASE_URL is not set>} | model ${EP_MODEL} (the model plan confirms it is served) | key ${KEY_STATE}"
    echo "  limits:   ${EP_RPM} requests/min client-side cap; eval ${EP_EVAL_LIMIT} cases, A/B ${EP_AB_LIMIT}, e2e ${EP_E2E_LIMIT} (0 = all); the GPU is the provider's" ;;
  *) echo "  label:    ${REHEARSAL_LABEL}" ;;
esac
if [[ "${MODE}" != "rehearsal" ]]; then
  echo "  guard:    runs only inside 2026-10-12T15:00Z .. 2026-10-18T14:00Z, after the amd-baseline tag"
fi
if [[ -n "${SET_ASIDE}" ]]; then
  echo "  note:     the provider endpoint in this shell (${SET_ASIDE}) is set aside: in ${MODE} mode the model is served on this machine"
fi

if [[ "${MODE}" != "rehearsal" && "${DRY_RUN}" == "0" ]]; then
  wrc=0; slog window || wrc=$?
  case ${wrc} in
    0) ;;
    3) die "refusing runs before kickoff (2026-10-12 15:00 UTC); rehearse with --rehearsal" ;;
    4) [[ "${ALLOW_AFTER}" == "1" ]] || die "the event window has closed; --allow-after-window runs anyway (not event results)" ;;
    # anything else: the check itself did not run (no usable python). Never start a run the guard could not clear.
    *) die "cannot check the event window (stagelog.py window exited ${wrc}): is python3 >= 3.11 usable here? notebook mode: export WAPSI_PYTHON=<the notebook's python>" ;;
  esac
  if [[ "${NO_BASELINE}" == "0" ]] && ! git rev-parse -q --verify refs/tags/amd-baseline >/dev/null; then
    die "tag amd-baseline is missing in this clone (git fetch --tags). It marks the repository at kickoff; --no-baseline-check runs anyway (not event results)"
  fi
  if [[ "${MODE}" == "endpoint" && -z "${EP_URL}" ]]; then
    die "endpoint mode needs WAPSI_LLM_BASE_URL (Token Factory: https://developer.amd.com.cn/radeon/api/v1) and WAPSI_LLM_API_KEY in this shell"
  fi
fi
if [[ "${DRY_RUN}" == "0" ]]; then
  mkdir -p "${EVID}/stages" "${EVID}/logs" "${WORK}"
  [[ "${MODE}" == "endpoint" ]] || mkdir -p "${EVID}/amd_smi"
  echo "${DATE}" > "${EVID_PARENT}/.active"
fi

for name in "${STAGES[@]}"; do
  selected "${name}" || continue
  idx="$(stage_index "${name}")"; num="$(printf '%02d' "${idx}")"
  marker="${EVID}/stages/${num}-${name}.ok"
  fn="${PREFIX}_${name//-/_}"
  if ! declare -F "${fn}" >/dev/null; then  # e.g. endpoint mode has no GNN / LoRA / serve stage: no GPU box
    echo "== [${num}/${#STAGES[@]}] ${name}: not part of ${MODE} mode (it needs a GPU box: --mode notebook or droplet), skipped"
    if [[ "${DRY_RUN}" == "0" && ! -f "${marker}" ]]; then
      t="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      slog record --evid "${EVID}" --index "${idx}" --stage "${name}" --mode "${MODE}" --status skipped --exit-code 0 \
        --started "${t}" --ended "${t}" >/dev/null
      touch "${marker}"
    fi
    continue
  fi
  if [[ -f "${marker}" && "${FORCE}" == "0" && "${EXPLICIT}" == "0" ]]; then
    echo "== [${num}/${#STAGES[@]}] ${name}: done earlier (${marker}), skipping"; RESULTS+=("${num} ${name}: done earlier"); continue
  fi
  echo; echo "== [${num}/${#STAGES[@]}] ${name}: $(title_of "${name}")"
  # the env stage writes .env (pinned digests, paths) while this process runs: later stages must see it
  if [[ "${MODE}" == "droplet" && "${DRY_RUN}" == "0" ]]; then load_env_file; fi
  if [[ "${DRY_RUN}" == "1" ]]; then "${fn}"; continue; fi

  brc=0; slog budget --evid "${EVID}" --mode "${MODE}" --stage "${name}" || brc=$?
  if [[ ${brc} -eq 20 && "${IGNORE_BUDGET}" == "0" ]]; then RESULTS+=("${num} ${name}: NOT STARTED (budget)"); FAILED=1; break; fi
  log="${EVID}/logs/${num}-${name}.log"; NOTES="${EVID}/stages/${num}-${name}.notes"
  : > "${NOTES}"
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  snaps=()
  if [[ "${MODE}" != "endpoint" ]]; then  # no GPU on this machine in endpoint mode: nothing to snapshot
    slog snapshot "${EVID}/amd_smi/${num}-${name}-before.json" || true
    snaps=(--before "amd_smi/${num}-${name}-before.json" --after "amd_smi/${num}-${name}-after.json")
  fi
  echo "[${started}] stage ${name} start (${MODE})" >> "${log}"
  set +e
  ( set -e; "${fn}" ) 2>&1 | tee -a "${log}"
  rc=${PIPESTATUS[0]}
  set -e
  ended="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[${ended}] stage ${name} end, exit ${rc}" >> "${log}"
  [[ "${MODE}" == "endpoint" ]] || slog snapshot "${EVID}/amd_smi/${num}-${name}-after.json" || true
  status="$([[ ${rc} -eq 0 ]] && echo ok || echo failed)"
  slog record --evid "${EVID}" --index "${idx}" --stage "${name}" --mode "${MODE}" --status "${status}" \
    --exit-code "${rc}" --started "${started}" --ended "${ended}" --log "logs/${num}-${name}.log" \
    ${snaps[@]+"${snaps[@]}"} --notes "${NOTES}"
  if [[ ${rc} -eq 0 ]]; then
    touch "${marker}"; RESULTS+=("${num} ${name}: ok (${started} .. ${ended})")
    [[ "${name}" == "evidence" ]] && rm -f "${EVID_PARENT}/.active"
  else
    if declare -F "${fn}_on_failure" >/dev/null; then  # e.g. lora-eval: remove its vLLM containers (port 8000)
      echo "== ${name} failed: cleaning up (${fn}_on_failure)" | tee -a "${log}"
      ( "${fn}_on_failure" ) 2>&1 | tee -a "${log}" || true
    fi
    rm -f "${marker}"; RESULTS+=("${num} ${name}: FAILED (exit ${rc}; log ${log})"); FAILED=1
    if [[ "${KEEP_GOING}" == "0" ]]; then
      echo "stage ${name} failed (exit ${rc}). Fix it, then resume: ${KICK}/run_all.sh --mode ${MODE} (done stages are skipped)"
      break
    fi
  fi
done

[[ "${DRY_RUN}" == "1" ]] && echo && echo "(dry run: nothing was executed)"
exit "${FAILED}"
