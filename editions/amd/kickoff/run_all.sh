#!/usr/bin/env bash
# Wapsi AMD ACT III: the whole event-week GPU pipeline on one MI300X as ONE resumable run (RUNBOOK.md, "Kickoff day").
#
#   editions/amd/kickoff/run_all.sh --dry-run          # print the plan (every command), run nothing
#   editions/amd/kickoff/run_all.sh                    # run every stage not yet done in today's evidence folder
#   editions/amd/kickoff/run_all.sh --stage bench      # (re)run one stage; a comma list works (--stage bench,ab)
#   editions/amd/kickoff/run_all.sh --from serve       # (re)run serve and every stage after it
#   editions/amd/kickoff/run_all.sh --rehearsal        # CPU rehearsal: tiny configs, stub LLMs, no GPU, no Docker
#   editions/amd/kickoff/run_all.sh --list             # stage names
#
# Stages: env, models, gnn, lora-data, lora-train, merge, lora-eval, serve, bench, ab, evidence.
# Each stage logs to evidence/<date>/logs/, takes an amd-smi snapshot before and after (evidence/<date>/amd_smi/),
# writes evidence/<date>/stages/NN-<stage>.json (UTC start/end, exit code, notes, budget) and, when it succeeds, a
# marker NN-<stage>.ok, so a re-run skips it. Results land in editions/amd/evidence/<UTC date>/ (the CPU rehearsal:
# editions/amd/evidence/cpu-rehearsal/<date>/, gitignored, labelled, never GPU results).
#
# Guards (GPU mode): refuses to start before the event window (2026-10-12 15:00 UTC) or without the amd-baseline tag;
# before every stage it prints the GPU spend (stagelog.py budget), warns at WAPSI_BUDGET_WARN_PCT (75%) and refuses
# new stages at WAPSI_BUDGET_STOP_PCT (90%); on exit it reminds you to DESTROY the droplet (powered off still bills).
#
# Options: --date YYYY-MM-DD   evidence folder (default: the active run's date, else today, UTC)
#          --new               start a new evidence folder today even if an earlier run is unfinished
#          --force             re-run stages that already have a marker
#          --keep-going        continue after a failed stage
#          --ignore-budget     start stages even at the stop threshold
#          --skip-preflight    do not run preflight.sh in the env stage
#          --no-baseline-check / --allow-after-window   (GPU mode; results would not be event-eligible)
#          --evidence-root DIR / --work DIR             (defaults: editions/amd/evidence, $SCRATCH/wapsi-work)
# Environment: SCRATCH (/mnt/scratch), HF_TOKEN (never printed), WAPSI_INR_PER_USD (the day's rate), WAPSI_GPU_HOUR_USD
#   (1.99), WAPSI_BUDGET_USD (100), WAPSI_SPENT_USD (earlier droplets), WAPSI_DROPLET_STARTED_AT (ISO UTC),
#   AMLWORLD_CSV, ROCM_PYTORCH_IMAGE, WAPSI_BASE_MODEL, WAPSI_VERIFIER_MODEL_ID, WAPSI_LORA_CONFIG, WAPSI_E2E_WORLD.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../../.." && pwd)"
cd "${ROOT}"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8  # the reports print ₹ and ≈ even when stdout is a pipe (tee)
KICK="editions/amd/kickoff"
AMD="editions/amd"
STAGES=(env models gnn lora-data lora-train merge lora-eval serve bench ab evidence)

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
    bench) echo "benchmarks: tokens/s, TTFT, p95 at N concurrent, cases/hour, drafts per GPU-hour, cost per case, VRAM" ;;
    ab) echo "A/B: raw LLM free-form STR vs Wapsi-bound drafting (hallucinated amounts, accounts, citations)" ;;
    evidence) echo "evidence bundle: evidence.json + evidence.md, pitch fill preview (dry-run diff)" ;;
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
    bench) echo "llm_bench against the stub endpoint, e2e_bench with FakeLLM + the GNN service (no telemetry)" ;;
    ab) echo "A/B with stub LLMs that always hallucinate (raw vs Wapsi-bound)" ;;
    evidence) echo "evidence bundle labelled CPU rehearsal; fill_pitch.py must refuse it" ;;
    *) return 1 ;;
  esac
}

die() { echo "run_all.sh: $*" >&2; exit 2; }
usage() { sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

MODE="gpu"; DRY_RUN=0; FORCE=0; KEEP_GOING=0; IGNORE_BUDGET=0; SKIP_PREFLIGHT=0; NO_BASELINE=0; ALLOW_AFTER=0
NEW=0; ONLY=""; FROM=""; DATE="${WAPSI_EVIDENCE_DATE:-}"; LIST=0
EVID_ROOT="${WAPSI_EVIDENCE_ROOT:-${AMD}/evidence}"
WORK_OPT="${WAPSI_WORK_DIR:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --rehearsal) MODE="rehearsal" ;;
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
[[ "${MODE}" == "gpu" ]] && load_env_file

SCRATCH="${SCRATCH:-/mnt/scratch}"
if [[ "${MODE}" == "gpu" ]]; then
  WORK="${WORK_OPT:-${SCRATCH}/wapsi-work}"
  EVID_PARENT="${EVID_ROOT}"
else
  WORK="${WORK_OPT:-data/generated/kickoff-rehearsal}"
  EVID_PARENT="${EVID_ROOT}/cpu-rehearsal"
fi
if [[ -z "${DATE}" ]]; then
  if [[ "${NEW}" == "0" && -f "${EVID_PARENT}/.active" ]]; then DATE="$(head -n 1 "${EVID_PARENT}/.active")"
  else DATE="$(date -u +%Y-%m-%d)"; fi
fi
EVID="${EVID_PARENT}/${DATE}"
if [[ "${MODE}" == "gpu" && "${EVID}" == /* ]]; then
  die "GPU evidence must live inside the repository (relative --evidence-root); containers write it via /work"
fi

HF_CACHE_DIR="${HF_CACHE_DIR:-${SCRATCH}/hf}"
WAPSI_MODELS_DIR="${WAPSI_MODELS_DIR:-${SCRATCH}/models}"
BASE_MODEL="${WAPSI_BASE_MODEL:-google/gemma-4-31B-it}"
VERIFIER_MODEL="${WAPSI_VERIFIER_MODEL_ID:-google/gemma-4-E4B-it}"
LORA_CONFIG="${WAPSI_LORA_CONFIG:-gemma4-31b-mi300x}"
MERGED_NAME="${WAPSI_MERGED_NAME:-wapsi-gemma-4-31b-str-merged}"
# Qwen fallback (Oct 13 go/no-go): WAPSI_BASE_PROFILE=qwen-fallback WAPSI_BASE_MODEL=Qwen/Qwen3.6-35B-A3B
#   WAPSI_LORA_CONFIG=qwen36-35b-a3b-mi300x WAPSI_MERGED_NAME=wapsi-qwen36-str-merged (start_llm.sh picks the family from the name)
BASE_PROFILE="${WAPSI_BASE_PROFILE:-gemma4-base}"
MERGED_PROFILE="$([[ "${BASE_PROFILE}" == qwen-fallback ]] && echo qwen-fallback || echo gemma4-merged)"
SERVED_NAME="${WAPSI_LLM_SERVED_NAME:-wapsi-llm}"
ROCM_PYTORCH_IMAGE="${ROCM_PYTORCH_IMAGE:-rocm/pytorch:rocm7.1_ubuntu24.04_py3.12_pytorch_release_2.9.1}"
TRAIN_IMAGE="wapsi-amd-train:local"
AMLWORLD_CSV="${AMLWORLD_CSV:-${SCRATCH}/amlworld/HI-Small_Trans.csv}"
GPU_HOUR_USD="${WAPSI_GPU_HOUR_USD:-1.99}"
INR_PER_USD="${WAPSI_INR_PER_USD:-}"
E2E_WORLD="${WAPSI_E2E_WORLD:-demo}"
GNN_WORKERS="${WAPSI_GNN_WORKERS:-16}"
GNN_EXTRA_WORLDS="${WAPSI_GNN_EXTRA_WORLDS:-100}"
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

# ------------------------------------------------------------------ helpers
pybin() {  # path of the project interpreter (uv venv), empty before `uv sync`
  if [[ -x .venv/bin/python ]]; then echo .venv/bin/python
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
  local a out=()
  case "${1:-}" in
    slog) out+=(python3 "${KICK}/stagelog.py"); shift ;;
  esac
  for a in "$@"; do
    if [[ "${a}" == "py" ]]; then out+=(python); continue; fi
    if [[ -n "${HF_TOKEN:-}" && "${a}" == *"${HF_TOKEN}"* ]] || [[ -n "${VLLM_API_KEY:-}" && "${a}" == *"${VLLM_API_KEY}"* ]]
    then out+=("***"); else out+=("${a}"); fi
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
inr_args() { [[ -n "${INR_PER_USD}" ]] && printf '%s\n' --inr-per-usd "${INR_PER_USD}"; return 0; }

# ------------------------------------------------------------------ GPU stages (MI300X droplet)
preflight_args() {  # the operator's explicit overrides also relax the matching preflight rows (FAIL -> WARN)
  [[ "${NO_BASELINE}" == "1" ]] && printf '%s
' --allow-no-baseline
  [[ "${ALLOW_AFTER}" == "1" ]] && printf '%s
' --allow-after-window
  return 0
}

gpu_env() {
  # shellcheck disable=SC2046
  [[ "${SKIP_PREFLIGHT}" == "1" ]] || run bash "${KICK}/preflight.sh" $(preflight_args) --json "${EVID}/preflight.json"
  run slog static "${EVID}/amd_smi_static.json"
  note git_commit "$(git rev-parse HEAD)"
  note amd_baseline "$(git rev-parse -q --verify 'refs/tags/amd-baseline^{commit}' || echo missing)"
  if [[ ! -f "${ENV_FILE}" ]]; then
    run cp "${AMD}/.env.example" "${ENV_FILE}"; run chmod 600 "${ENV_FILE}"
  fi
  env_set HF_CACHE_DIR "${HF_CACHE_DIR}"
  env_set WAPSI_MODELS_DIR "${WAPSI_MODELS_DIR}"
  run mkdir -p "${WORK}/train-image" "${HF_CACHE_DIR}" "${WAPSI_MODELS_DIR}" "${EVID}/bench"
  run uv sync --frozen --no-dev
  synth_world demo demo 7
  synth_world full full 7
  synth_world hard hard 7
  local s
  for s in 101 202 303 404 505; do synth_world "heldout-${s}" demo "${s}"; done
  for s in $(seq 1001 1010) 2001 2002; do synth_world "gnn-${s}" demo "${s}"; done
  run_sh "seq 3001 $((3000 + GNN_EXTRA_WORLDS)) | xargs -P 32 -I{} sh -c 'test -f data/generated/gnn-{}/transactions.parquet || .venv/bin/python -m wapsi.synth --size demo --seed {} --name gnn-{} --out data/generated/gnn-{} --golden-dir \"\" >/dev/null'"
  run_soft py -m wapsi.search.build
  run bash "${AMD}/serving/pull_and_pin.sh" "ROCM_PYTORCH_IMAGE=${ROCM_PYTORCH_IMAGE}"
  run_sh "grep -E '^[A-Z_]+_IMAGE(_TAG)?=' '${ENV_FILE}' > '${EVID}/images.txt'"
  run_sh "uv export --frozen --no-dev --extra amd --extra ml --extra amd-app --no-hashes --no-emit-project --no-header --no-annotate | grep -Ev '^(torch|triton|pytorch-triton|nvidia-|cuda-)' > '${WORK}/train-image/requirements-rocm.txt'"
  run docker build -f "${KICK}/Dockerfile.train" --build-arg "BASE_IMAGE=${ROCM_PYTORCH_IMAGE}" -t "${TRAIN_IMAGE}" "${WORK}/train-image"
  in_rocm python3 -c "import torch; assert torch.cuda.is_available(), 'no GPU visible in the container'; print('torch', torch.__version__, 'hip', torch.version.hip, torch.cuda.get_device_name(0))"
  run "${COMPOSE[@]}" --profile gpu --profile gnn build
  run install -d -o 10001 -g 10001 "${AMD}/bench/results"
}

gpu_models() {
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
  run py -m editions.amd.bench.llm_bench --base-url "${LLM_URL}" --concurrency 1 4 16 64 --name llm_bench_base \
    --out "${EVID}/bench"
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

gpu_serve() {
  local profile="${MERGED_PROFILE}" model="/models/${MERGED_NAME}"
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

gpu_bench() {
  local out="${EVID}/bench" c
  wait_http "${LLM_URL}/models" 60 5  # fails fast when nothing is serving: run --stage serve first
  run mkdir -p "${out}"
  run_sh "amd-smi metric --json > '${out}/amd_smi_metric_raw.json'; amd-smi process --json > '${out}/amd_smi_process_raw.json'"
  bg_start telemetry python3 "${AMD}/bench/gpu_telemetry.py" --interval 2 --out "${out}"
  run py -m editions.amd.bench.llm_bench --base-url "${LLM_URL}" --concurrency 1 4 16 64 --input-len 1024 \
    --output-len 256 --out "${out}"
  run py -m editions.amd.bench.llm_bench --base-url "${LLM_URL}" --prompts wapsi --concurrency 1 8 32 \
    --name llm_bench_wapsi_prompts --out "${out}"
  run py -m editions.amd.bench.llm_bench --base-url "${VERIFIER_URL}" --concurrency 1 4 16 --name llm_bench_verifier \
    --out "${out}"
  for c in 1 8 16; do
    run py -m editions.amd.bench.e2e_bench --world "${E2E_WORLD}" --concurrency "${c}" --llm-url "${LLM_URL}" \
      --verifier-url "${VERIFIER_URL}" --gnn-url "${GNN_URL}" --gpu-hour-usd "${GPU_HOUR_USD}" $(inr_args) \
      --name "e2e_c${c}" --out "${out}"
  done
  bg_stop telemetry INT
  run py -m editions.amd.bench.report --results-dir "${out}" --out "${EVID}/AMD_RESULTS.md"
  run cp "${EVID}/AMD_RESULTS.md" "${AMD}/AMD_RESULTS.md"
}

gpu_ab() {
  wait_http "${LLM_URL}/models" 60 5
  run py -m editions.amd.kickoff.ab_hallucination --llm-url "${LLM_URL}" --llm-model "${SERVED_NAME}" \
    --world "${E2E_WORLD}" --limit 20 --workers 8 --label "MI300X run (AMD Developer Cloud), ${DATE}" \
    --out "${EVID}/ab_hallucination.json"
}

gpu_evidence() {
  run py "${KICK}/collect_evidence.py" --evidence-dir "${EVID}" --mode gpu
  run_soft py "${KICK}/fill_pitch.py" --evidence "${EVID}/evidence.json"
  say "review the diff above, then: py ${KICK}/fill_pitch.py --evidence ${EVID}/evidence.json --write"
  say "commit: git add ${EVID} ${AMD}/AMD_RESULTS.md ${AMD}/gnn/results && git commit -m 'AMD: MI300X run ${DATE}'"
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
selected() {
  local name="$1"
  if [[ -n "${ONLY}" ]]; then [[ ",${ONLY}," == *",${name},"* ]]; return; fi
  if [[ -n "${FROM}" ]]; then (( $(stage_index "${name}") >= $(stage_index "${FROM}") )); return; fi
  return 0
}
EXPLICIT=0
[[ -n "${ONLY}${FROM}" ]] && EXPLICIT=1

RESULTS=()
FAILED=0
on_exit() {
  local rc=$?
  stop_all_bg
  if [[ "${DRY_RUN}" == "0" && ${#RESULTS[@]} -gt 0 ]]; then
    echo; echo "== summary (${MODE}, evidence ${EVID})"
    printf '   %s\n' "${RESULTS[@]}"
    if [[ "${MODE}" == "gpu" ]]; then
      slog budget --evid "${EVID}" --mode gpu || true
      echo "REMINDER: a powered-off droplet STILL BILLS. When the GPU work is done: git push the evidence, copy the"
      echo "          adapter and checkpoints off the box, docker compose down, then DESTROY the droplet in the console."
    fi
  fi
  exit "${rc}"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

echo "Wapsi AMD kickoff pipeline: mode=${MODE}$([[ "${DRY_RUN}" == "1" ]] && echo " (dry run)") date=${DATE}"
echo "  evidence: ${EVID}"
echo "  work:     ${WORK}"
if [[ "${MODE}" == "gpu" ]]; then
  echo "  models:   base ${BASE_MODEL} | verifier ${VERIFIER_MODEL} | LoRA preset ${LORA_CONFIG} | merged /models/${MERGED_NAME}"
  echo "  budget:   \$${GPU_HOUR_USD}/GPU-hour, cap \$${WAPSI_BUDGET_USD:-100}, warn ${WAPSI_BUDGET_WARN_PCT:-75}%, stop ${WAPSI_BUDGET_STOP_PCT:-90}% (stagelog.py budget)"
  echo "  guard:    GPU runs only inside 2026-10-12T15:00Z .. 2026-10-18T14:00Z, after the amd-baseline tag"
else
  echo "  label:    ${REHEARSAL_LABEL}"
fi

if [[ "${MODE}" == "gpu" && "${DRY_RUN}" == "0" ]]; then
  wrc=0; slog window || wrc=$?
  if [[ ${wrc} -eq 3 ]]; then die "refusing GPU runs before kickoff (2026-10-12 15:00 UTC); rehearse with --rehearsal"; fi
  if [[ ${wrc} -eq 4 && "${ALLOW_AFTER}" == "0" ]]; then die "the event window has closed; --allow-after-window runs anyway (not event results)"; fi
  if [[ "${NO_BASELINE}" == "0" ]] && ! git rev-parse -q --verify refs/tags/amd-baseline >/dev/null; then
    die "tag amd-baseline is missing. At kickoff: git tag -a amd-baseline -m 'State of the repo at AMD ACT III kickoff' && git push origin amd-baseline"
  fi
fi
if [[ "${DRY_RUN}" == "0" ]]; then
  mkdir -p "${EVID}/stages" "${EVID}/logs" "${EVID}/amd_smi" "${WORK}"
  echo "${DATE}" > "${EVID_PARENT}/.active"
fi

for name in "${STAGES[@]}"; do
  selected "${name}" || continue
  idx="$(stage_index "${name}")"; num="$(printf '%02d' "${idx}")"
  marker="${EVID}/stages/${num}-${name}.ok"
  if [[ -f "${marker}" && "${FORCE}" == "0" && "${EXPLICIT}" == "0" ]]; then
    echo "== [${num}/${#STAGES[@]}] ${name}: done earlier (${marker}), skipping"; RESULTS+=("${num} ${name}: done earlier"); continue
  fi
  echo; echo "== [${num}/${#STAGES[@]}] ${name}: $([[ "${MODE}" == "gpu" ]] && stage_title "${name}" || rehearsal_title "${name}")"
  fn="$([[ "${MODE}" == "gpu" ]] && echo gpu || echo cpu)_${name//-/_}"
  # the env stage writes .env (pinned digests, paths) while this process runs: later stages must see it
  if [[ "${MODE}" == "gpu" && "${DRY_RUN}" == "0" ]]; then load_env_file; fi
  if [[ "${DRY_RUN}" == "1" ]]; then "${fn}"; continue; fi

  brc=0; slog budget --evid "${EVID}" --mode "${MODE}" --stage "${name}" || brc=$?
  if [[ ${brc} -eq 20 && "${IGNORE_BUDGET}" == "0" ]]; then RESULTS+=("${num} ${name}: NOT STARTED (budget)"); FAILED=1; break; fi
  log="${EVID}/logs/${num}-${name}.log"; NOTES="${EVID}/stages/${num}-${name}.notes"
  : > "${NOTES}"
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  slog snapshot "${EVID}/amd_smi/${num}-${name}-before.json" || true
  echo "[${started}] stage ${name} start (${MODE})" >> "${log}"
  set +e
  ( set -e; "${fn}" ) 2>&1 | tee -a "${log}"
  rc=${PIPESTATUS[0]}
  set -e
  ended="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[${ended}] stage ${name} end, exit ${rc}" >> "${log}"
  slog snapshot "${EVID}/amd_smi/${num}-${name}-after.json" || true
  status="$([[ ${rc} -eq 0 ]] && echo ok || echo failed)"
  slog record --evid "${EVID}" --index "${idx}" --stage "${name}" --mode "${MODE}" --status "${status}" \
    --exit-code "${rc}" --started "${started}" --ended "${ended}" --log "logs/${num}-${name}.log" \
    --before "amd_smi/${num}-${name}-before.json" --after "amd_smi/${num}-${name}-after.json" --notes "${NOTES}"
  if [[ ${rc} -eq 0 ]]; then
    touch "${marker}"; RESULTS+=("${num} ${name}: ok (${started} .. ${ended})")
    [[ "${name}" == "evidence" ]] && rm -f "${EVID_PARENT}/.active"
  else
    rm -f "${marker}"; RESULTS+=("${num} ${name}: FAILED (exit ${rc}; log ${log})"); FAILED=1
    if [[ "${KEEP_GOING}" == "0" ]]; then
      echo "stage ${name} failed (exit ${rc}). Fix it, then resume: ${KICK}/run_all.sh$([[ "${MODE}" == "rehearsal" ]] && echo " --rehearsal") (done stages are skipped)"
      break
    fi
  fi
done

[[ "${DRY_RUN}" == "1" ]] && echo && echo "(dry run: nothing was executed)"
exit "${FAILED}"
