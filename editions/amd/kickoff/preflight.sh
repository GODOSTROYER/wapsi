#!/usr/bin/env bash
# Wapsi AMD ACT III preflight: is this droplet ready for run_all.sh? Prints a green/amber/red table.
#
#   editions/amd/kickoff/preflight.sh                       # on the MI300X droplet (exit 1 if any check FAILs)
#   editions/amd/kickoff/preflight.sh --rehearsal           # laptop: checks the CPU rehearsal prerequisites
#   editions/amd/kickoff/preflight.sh --offline             # skip the network checks (Hugging Face reachability, model access)
#   editions/amd/kickoff/preflight.sh --json OUT.json       # also write the results as JSON (run_all.sh: evidence/<date>/preflight.json)
#
# Checks: amd-smi / rocm-smi and the GPU name, ROCm version, GPU memory (total, already in use), docker daemon and
# compose >= 2.20.2, SCRATCH exists (a missing path is FAIL, never the root disk's free space) with free disk there
# and on the docker volume, HF_TOKEN present (the value is never printed or put on a command line), network to
# huggingface.co, access to each gated model, uv + python3 >= 3.11, the amd-baseline tag, the event window, pinned
# image digests, optional inputs (docs corpus, AMLworld CSV, the day's INR rate, droplet start).
# Every check below tolerates its own failure (|| true, if), so a red row never aborts the table.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AMD_DIR="$(dirname "${HERE}")"
ROOT="$(cd "${AMD_DIR}/../.." && pwd)"
MODE="gpu"
OFFLINE=0
JSON_OUT=""
ALLOW_NO_BASELINE=0  # run_all.sh --no-baseline-check: a missing amd-baseline tag is WARN, not FAIL
ALLOW_AFTER=0        # run_all.sh --allow-after-window: after the event window is WARN, not FAIL

for arg in "$@"; do
  case "${arg}" in
    --rehearsal) MODE="rehearsal" ;;
    --offline) OFFLINE=1 ;;
    --allow-no-baseline) ALLOW_NO_BASELINE=1 ;;
    --allow-after-window) ALLOW_AFTER=1 ;;
    --json=*) JSON_OUT="${arg#--json=}" ;;
    --json) JSON_OUT="__next__" ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)
      if [[ "${JSON_OUT}" == "__next__" ]]; then JSON_OUT="${arg}"; else echo "unexpected argument: ${arg}" >&2; exit 64; fi ;;
  esac
done
[[ "${JSON_OUT}" == "__next__" ]] && { echo "--json needs a path" >&2; exit 64; }

# defaults the run uses (editions/amd/.env and the shell may override; the shell wins, as in docker compose)
ENV_FILE="${AMD_DIR}/.env"
env_get() {  # env_get NAME: value from the shell, else from editions/amd/.env (never evaluated as code)
  local name="$1" line
  if [[ -n "${!name:-}" ]]; then printf '%s' "${!name}"; return; fi
  [[ -f "${ENV_FILE}" ]] || return 0
  line="$(grep -E "^${name}=" "${ENV_FILE}" | tail -n 1 || true)"
  line="${line#*=}"; line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
  printf '%s' "${line}"
}
SCRATCH="${SCRATCH:-/mnt/scratch}"
BASE_MODEL="$(env_get WAPSI_BASE_MODEL)"; BASE_MODEL="${BASE_MODEL:-google/gemma-4-31B-it}"
VERIFIER_MODEL="$(env_get WAPSI_VERIFIER_MODEL_ID)"; VERIFIER_MODEL="${VERIFIER_MODEL:-google/gemma-4-E4B-it}"
AMLWORLD_CSV="${AMLWORLD_CSV:-${SCRATCH}/amlworld/HI-Small_Trans.csv}"

if [[ -t 1 || -n "${FORCE_COLOR:-}" ]] && [[ -z "${NO_COLOR:-}" ]]; then
  C_PASS=$'\e[32m'; C_WARN=$'\e[33m'; C_FAIL=$'\e[31m'; C_SKIP=$'\e[90m'; C_OFF=$'\e[0m'
else
  C_PASS=""; C_WARN=""; C_FAIL=""; C_SKIP=""; C_OFF=""
fi
ROWS=()
FAILS=0
WARNS=0
add() {  # add CHECK STATUS DETAIL
  local check="$1" status="$2" detail="$3"
  ROWS+=("${check}"$'\t'"${status}"$'\t'"${detail}")
  [[ "${status}" == "FAIL" ]] && FAILS=$((FAILS + 1))
  [[ "${status}" == "WARN" ]] && WARNS=$((WARNS + 1))
  return 0
}
have() { command -v "$1" >/dev/null 2>&1; }

pick_python() {  # an interpreter >= 3.11 for the standard-library helpers
  local c
  for c in "${ROOT}/.venv/bin/python" "${ROOT}/.venv/Scripts/python.exe" python3 python; do
    if { [[ -x "${c}" ]] || have "${c}"; } && "${c}" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
      printf '%s' "${c}"; return 0
    fi
  done
  return 1
}
PY="$(pick_python || true)"

version_ge() {  # version_ge A B: A >= B (dotted numbers)
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" == "$2" ]]
}

check_python() {
  if [[ -n "${PY}" ]]; then add "python >= 3.11" PASS "$("${PY}" -c 'import sys; print(sys.version.split()[0])') (${PY})"
  else add "python >= 3.11" FAIL "no python3 >= 3.11 on PATH"; fi
  if have uv; then add "uv" PASS "$(uv --version 2>/dev/null)"
  else add "uv" FAIL "missing: curl -LsSf https://astral.sh/uv/install.sh | sh"; fi
}

check_repo() {
  if git -C "${ROOT}" rev-parse -q --verify refs/tags/amd-baseline >/dev/null 2>&1; then
    add "amd-baseline tag" PASS "$(git -C "${ROOT}" rev-parse --short amd-baseline^{commit})"
  elif [[ "${MODE}" == "gpu" && "${ALLOW_NO_BASELINE}" == "1" ]]; then
    add "amd-baseline tag" WARN "missing (--no-baseline-check): results are not event-eligible"
  elif [[ "${MODE}" == "gpu" ]]; then
    add "amd-baseline tag" FAIL "create it at kickoff before any GPU run (RUNBOOK.md step 1)"
  else
    add "amd-baseline tag" SKIP "created at kickoff (2026-10-12 15:00 UTC), not before"
  fi
  if [[ -n "${PY}" ]]; then
    local w rc=0; w="$("${PY}" "${HERE}/stagelog.py" window 2>/dev/null)" || rc=$?
    if [[ "${MODE}" == "rehearsal" ]]; then add "event window" SKIP "CPU rehearsal: no GPU numbers (${w##*: })"
    elif [[ ${rc} -eq 0 ]]; then add "event window" PASS "${w##*: }"
    elif [[ ${rc} -eq 4 && "${ALLOW_AFTER}" == "1" ]]; then add "event window" WARN "${w##*: } (--allow-after-window: not event results)"
    else add "event window" FAIL "${w##*: } (GPU runs and GPU numbers only 2026-10-12 15:00 UTC .. 2026-10-18 14:00 UTC)"; fi
  fi
}

check_gpu() {
  if [[ "${MODE}" == "rehearsal" ]]; then
    add "amd-smi / rocm-smi" SKIP "CPU rehearsal"; add "ROCm version" SKIP "CPU rehearsal"
    add "GPU memory" SKIP "CPU rehearsal"; return
  fi
  if have amd-smi; then add "amd-smi" PASS "$(command -v amd-smi)"
  elif have rocm-smi; then add "amd-smi" WARN "missing; rocm-smi present (telemetry falls back to it)"
  else add "amd-smi / rocm-smi" FAIL "neither found: is this the MI300X droplet with a ROCm image?"; fi
  local rocm=""
  [[ -r /opt/rocm/.info/version ]] && rocm="$(head -n 1 /opt/rocm/.info/version)"
  if [[ -n "${rocm}" ]]; then add "ROCm version" PASS "${rocm}"
  else add "ROCm version" WARN "/opt/rocm/.info/version not found (containers bring their own ROCm user space)"; fi
  local mem=""; [[ -n "${PY}" ]] && mem="$("${PY}" "${HERE}/stagelog.py" gpu-mem 2>/dev/null || true)"
  if [[ -z "${mem}" ]]; then add "GPU memory" FAIL "no GPU visible to amd-smi / rocm-smi"; return; fi
  local gpu name total used
  while read -r gpu name total used; do
    if [[ "${name}" != *MI300* ]]; then add "GPU ${gpu}" WARN "${name//_/ } (expected an AMD Instinct MI300X)"
    else add "GPU ${gpu}" PASS "${name//_/ }"; fi
    if [[ "${total}" =~ ^[0-9]+$ ]] && (( total >= 180000 )); then add "GPU ${gpu} VRAM" PASS "$(( total / 1024 )) GB total"
    else add "GPU ${gpu} VRAM" WARN "${total} MB total (MI300X has 192 GB)"; fi
    if [[ "${used}" =~ ^[0-9]+$ && "${total}" =~ ^[0-9]+$ ]] && (( used * 100 < total * 5 )); then
      add "GPU ${gpu} free" PASS "$(( used / 1024 )) GB in use"
    else add "GPU ${gpu} free" WARN "${used} MB already in use: stop pre-installed containers (docker ps; docker stop <name>)"; fi
  done <<< "${mem}"
}

check_docker() {
  if [[ "${MODE}" == "rehearsal" ]]; then add "docker" SKIP "CPU rehearsal runs without Docker"; add "docker compose" SKIP "CPU rehearsal"; return; fi
  if ! have docker; then add "docker" FAIL "missing: apt-get install -y docker.io docker-compose-v2"; add "docker compose" FAIL "missing"; return; fi
  local server; server="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
  if [[ -n "${server}" ]]; then add "docker" PASS "server ${server}"; else add "docker" FAIL "daemon not reachable"; fi
  local compose; compose="$(docker compose version --short 2>/dev/null || true)"; compose="${compose#v}"
  if [[ -z "${compose}" ]]; then add "docker compose" FAIL "plugin missing (apt-get install -y docker-compose-v2)"
  elif version_ge "${compose}" 2.20.2; then add "docker compose" PASS "${compose}"
  else add "docker compose" FAIL "${compose} < 2.20.2 ('required: false' in depends_on)"; fi
  if [[ -f "${ENV_FILE}" ]] && grep -Eq '^VLLM_IMAGE=.*@sha256:0{64}' "${ENV_FILE}"; then
    add "image digests" WARN "VLLM_IMAGE not pinned yet (run_all.sh env stage runs serving/pull_and_pin.sh)"
  elif [[ -f "${ENV_FILE}" ]]; then add "image digests" PASS "editions/amd/.env pinned"
  else add "image digests" WARN "editions/amd/.env missing (env stage creates it from .env.example)"; fi
}

free_gb() {  # free_gb PATH: free GB on the filesystem holding PATH (or its nearest existing parent)
  local p="$1"
  while [[ ! -e "${p}" && "${p}" != "/" && "${p}" != "." ]]; do p="$(dirname "${p}")"; done
  df -Pk "${p}" 2>/dev/null | awk 'NR == 2 { printf "%d", $4 / 1048576 }' || true
}

check_disk() {
  local scratch_free root_free docker_root
  if [[ "${MODE}" == "rehearsal" ]]; then
    root_free="$(free_gb "${ROOT}")"
    if [[ -n "${root_free}" ]] && (( root_free >= 5 )); then add "disk (repo)" PASS "${root_free} GB free"
    else add "disk (repo)" WARN "${root_free:-?} GB free (rehearsal needs ~2 GB)"; fi
    return
  fi
  # SCRATCH must exist: free_gb would walk up to / and report the root disk (a false PASS; the models would then
  # fill the boot disk)
  if [[ ! -d "${SCRATCH}" ]]; then
    add "disk (SCRATCH=${SCRATCH})" FAIL "does not exist: export SCRATCH=<the 5 TB mount from lsblk/df>, or mkdir -p it on that mount"
  else
    scratch_free="$(free_gb "${SCRATCH}")"
    if [[ -z "${scratch_free}" ]]; then add "disk (SCRATCH=${SCRATCH})" FAIL "cannot stat; export SCRATCH=<the 5 TB mount from lsblk/df>"
    elif (( scratch_free >= 400 )); then add "disk (SCRATCH=${SCRATCH})" PASS "${scratch_free} GB free"
    else add "disk (SCRATCH=${SCRATCH})" FAIL "${scratch_free} GB free; base + merged Gemma 4 31B + caches need >= 400 GB"; fi
    local scratch_mnt root_mnt
    scratch_mnt="$(df -P "${SCRATCH}" 2>/dev/null | awk 'NR == 2 { print $6 }' || true)"
    root_mnt="$(df -P / 2>/dev/null | awk 'NR == 2 { print $6 }' || true)"
    if [[ -n "${scratch_mnt}" && "${scratch_mnt}" == "${root_mnt}" && "${SCRATCH}" != "/" ]]; then
      add "SCRATCH mount" WARN "${SCRATCH} is on the root filesystem (${root_mnt}), not a separate scratch volume (lsblk)"
    fi
  fi
  docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
  root_free="$(free_gb "${docker_root}")"
  if [[ -n "${root_free}" ]] && (( root_free >= 80 )); then add "disk (docker ${docker_root})" PASS "${root_free} GB free"
  else add "disk (docker ${docker_root})" WARN "${root_free:-?} GB free; ROCm images are 20-40 GB each"; fi
}

hf_status() {  # hf_status URL: HTTP status of a HEAD request, the token sent from stdin (never on argv)
  local url="$1"
  if [[ -n "${HF_TOKEN:-}" ]]; then
    printf 'header = "Authorization: Bearer %s"\n' "${HF_TOKEN}" \
      | curl -K - -sI -o /dev/null -w '%{http_code}' --max-time 15 "${url}" 2>/dev/null || true
  else
    curl -sI -o /dev/null -w '%{http_code}' --max-time 15 "${url}" 2>/dev/null || true
  fi
}

check_hf() {
  if [[ "${MODE}" == "rehearsal" ]]; then
    add "HF_TOKEN" SKIP "CPU rehearsal uses a tiny random model (no download)"; return
  fi
  if [[ -n "${HF_TOKEN:-}" ]]; then add "HF_TOKEN" PASS "set in this shell (value not shown)"
  else add "HF_TOKEN" FAIL "not set: read -rs HF_TOKEN && export HF_TOKEN"; fi
  if [[ "${OFFLINE}" == "1" ]]; then add "network to huggingface.co" SKIP "--offline"; add "model access" SKIP "--offline"; return; fi
  if ! have curl; then add "network to huggingface.co" FAIL "curl missing"; return; fi
  local code; code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://huggingface.co/api/models/google/gemma-4-E4B-it 2>/dev/null || true)"
  if [[ "${code}" == "200" ]]; then add "network to huggingface.co" PASS "HTTP ${code}"
  else add "network to huggingface.co" FAIL "HTTP ${code:-none} (outbound firewall? DNS?)"; return; fi
  local model
  for model in "${BASE_MODEL}" "${VERIFIER_MODEL}"; do
    code="$(hf_status "https://huggingface.co/${model}/resolve/main/config.json")"
    case "${code}" in
      200|302|307) add "model access ${model}" PASS "HTTP ${code}" ;;
      401|403) add "model access ${model}" FAIL "HTTP ${code}: accept the licence on the model page with this token's account" ;;
      404) add "model access ${model}" FAIL "HTTP 404: check the repo id (configs/*.json, WAPSI_BASE_MODEL)" ;;
      *) add "model access ${model}" WARN "HTTP ${code:-none}" ;;
    esac
  done
}

check_inputs() {
  if [[ "${MODE}" == "rehearsal" ]]; then
    if [[ -n "${PY}" ]] && "${PY}" -c 'import torch, transformers, peft, trl' >/dev/null 2>&1; then
      add "python extras (amd)" PASS "torch, transformers, peft, trl import"
    else add "python extras (amd)" FAIL "uv sync --extra amd --extra amd-app (rehearsal trains a tiny LoRA and GNN)"; fi
    if [[ -n "${PY}" ]] && "${PY}" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
      add "python extras (amd-app)" PASS "fastapi, uvicorn import"
    else add "python extras (amd-app)" FAIL "uv sync --extra amd-app (GNN scoring service)"; fi
    if [[ -f "${ROOT}/data/golden/showcase/cases.jsonl" ]]; then add "golden cases" PASS "data/golden/showcase"
    else add "golden cases" FAIL "data/golden/showcase/cases.jsonl missing"; fi
    return
  fi
  if [[ -f "${ROOT}/data/docs/index/chunks.jsonl" ]]; then add "docs corpus" PASS "data/docs/index built"
  elif [[ -f "${ROOT}/data/docs/raw/_fetch.json" ]]; then add "docs corpus" PASS "data/docs/raw present (env stage builds the index)"
  else add "docs corpus" WARN "data/docs/raw missing (not in git): the env stage fetches it from the public sites; if that fails, e2e_bench runs without docs search. Faster: scp -r data/docs/raw from a laptop"; fi
  if [[ -f "${AMLWORLD_CSV}" ]]; then add "AMLworld HI-Small" PASS "${AMLWORLD_CSV}"
  else add "AMLworld HI-Small" WARN "${AMLWORLD_CSV} missing: GNN trains without AMLworld pre-training (Kaggle ealtman2019, CDLA-Sharing-1.0)"; fi
  if [[ -n "${WAPSI_INR_PER_USD:-}" ]]; then add "INR rate" PASS "WAPSI_INR_PER_USD=${WAPSI_INR_PER_USD}"
  else add "INR rate" WARN "WAPSI_INR_PER_USD unset: no rupee cost per case (pass the day's rate)"; fi
  if [[ -n "${WAPSI_DROPLET_STARTED_AT:-}" ]]; then add "droplet start" PASS "${WAPSI_DROPLET_STARTED_AT}"
  else add "droplet start" WARN "WAPSI_DROPLET_STARTED_AT unset: the budget guard uses the boot time"; fi
}

check_python
check_repo
check_gpu
check_docker
check_disk
check_hf
check_inputs

printf '\nWapsi AMD preflight (%s)\n' "$([[ "${MODE}" == "gpu" ]] && echo "MI300X droplet" || echo "CPU rehearsal")"
printf '%-34s %-6s %s\n' "CHECK" "STATUS" "DETAIL"
for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r check status detail <<< "${row}"
  case "${status}" in
    PASS) color="${C_PASS}" ;; WARN) color="${C_WARN}" ;; FAIL) color="${C_FAIL}" ;; *) color="${C_SKIP}" ;;
  esac
  printf '%-34s %s%-6s%s %s\n' "${check}" "${color}" "${status}" "${C_OFF}" "${detail}"
done
printf '\n%s FAIL, %s WARN -> %s\n' "${FAILS}" "${WARNS}" "$([[ ${FAILS} -eq 0 ]] && echo "${C_PASS}ready${C_OFF}" || echo "${C_FAIL}not ready${C_OFF}")"

if [[ -n "${JSON_OUT}" && -n "${PY}" ]]; then
  printf '%s\n' "${ROWS[@]}" | "${PY}" "${HERE}/stagelog.py" tsv-json "${JSON_OUT}" --mode "${MODE}"
fi
[[ ${FAILS} -eq 0 ]]
