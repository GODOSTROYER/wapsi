#!/usr/bin/env bash
# Pull the images the AMD edition runs and pin each one by digest in editions/amd/.env.
#
#   editions/amd/serving/pull_and_pin.sh                 # pull defaults, write VAR=repo@sha256:... into .env
#   editions/amd/serving/pull_and_pin.sh --dry-run       # show what would be pulled; touches nothing
#   editions/amd/serving/pull_and_pin.sh ROCM_PYTORCH_IMAGE=rocm/pytorch:<tag>   # pin extra images too
#
# Tags to pull can be overridden with VLLM_IMAGE_TAG, VLLM_GEMMA4_IMAGE_TAG, PYTHON_IMAGE_TAG, UV_IMAGE_TAG.
# `:latest` is refused: every image Wapsi runs must be reproducible. The digest is read with
#   docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' <image:tag>
# and the matching repository's entry is written as VAR=<repo>@sha256:<digest> (plus VAR_TAG=<image:tag>).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AMD_DIR="$(dirname "${HERE}")"
ENV_FILE="${WAPSI_ENV_FILE:-${AMD_DIR}/.env}"
DRY=0

PAIRS=(
  "VLLM_IMAGE=${VLLM_IMAGE_TAG:-rocm/vllm:rocm10.0.0_ubuntu24.04_py3.14_pytorch_2.12.0_vllm_0.27.0}"
  "VLLM_GEMMA4_IMAGE=${VLLM_GEMMA4_IMAGE_TAG:-vllm/vllm-openai-rocm:gemma4}"
  "PYTHON_IMAGE=${PYTHON_IMAGE_TAG:-python:3.12-slim}"
  "UV_IMAGE=${UV_IMAGE_TAG:-ghcr.io/astral-sh/uv:0.8}"
)

usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY=1 ;;
    -h|--help) usage; exit 0 ;;
    [A-Z_]*=*) PAIRS+=("${arg}") ;;
    *) echo "unexpected argument: ${arg}" >&2; usage >&2; exit 64 ;;
  esac
done

set_env() {  # set_env VAR VALUE: replace VAR=... in ENV_FILE or append it
  local var="$1" value="$2" tmp
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  awk -v var="${var}" -v value="${value}" '
    BEGIN { done = 0 }
    $0 ~ "^" var "=" { print var "=" value; done = 1; next }
    { print }
    END { if (!done) print var "=" value }
  ' "${ENV_FILE}" > "${tmp}"
  mv "${tmp}" "${ENV_FILE}"
}

if [[ ! -f "${ENV_FILE}" ]]; then
  if [[ "${DRY}" == "1" ]]; then
    echo "would create ${ENV_FILE} from ${AMD_DIR}/.env.example"
  else
    cp "${AMD_DIR}/.env.example" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    echo "created ${ENV_FILE} from .env.example"
  fi
fi

for pair in "${PAIRS[@]}"; do
  var="${pair%%=*}"
  ref="${pair#*=}"
  if [[ "${ref}" == *@sha256:* ]]; then
    echo "${var}: ${ref} is already a digest; nothing to pull"
    continue
  fi
  last="${ref##*/}"
  if [[ "${last}" != *:* || "${ref}" == *:latest ]]; then
    echo "${var}: refusing '${ref}' (no tag or :latest); give an explicit version tag" >&2
    exit 65
  fi
  repo="${ref%:*}"
  if [[ "${DRY}" == "1" ]]; then
    echo "would pull ${ref} and set ${var}=${repo}@sha256:<digest>"
    continue
  fi
  docker pull "${ref}"
  pinned="$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${ref}" \
            | awk -v repo="${repo}" 'index($0, repo "@sha256:") == 1 || index($0, "docker.io/" repo "@sha256:") == 1 { print; exit }')"
  if [[ -z "${pinned}" ]]; then
    echo "${var}: no RepoDigest for ${ref} (locally built image?)" >&2
    exit 66
  fi
  pinned="${pinned#docker.io/}"
  set_env "${var}" "${pinned}"
  set_env "${var}_TAG" "${ref}"
  echo "${var}=${pinned}   (from ${ref})"
done

if [[ "${DRY}" != "1" ]]; then
  echo "pinned digests written to ${ENV_FILE}; commit the digests (not the .env) in RUNBOOK notes / AMD_RESULTS.md"
fi
