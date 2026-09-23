#!/usr/bin/env bash
# Wapsi AMD edition app container: FastAPI (8080) + Streamlit UI (8501) in one container.
#   WAPSI_APP_MODE=all (default) | api | ui
# If either process exits, the container exits (Docker's restart policy brings it back).
set -euo pipefail

MODE="${WAPSI_APP_MODE:-all}"
API_PORT="${WAPSI_API_PORT:-8080}"
UI_PORT="${WAPSI_UI_PORT:-8501}"

api() {
  exec uvicorn editions.amd.app.api:app --host 0.0.0.0 --port "${API_PORT}" \
    --timeout-graceful-shutdown 8 --no-server-header
}

ui() {
  exec streamlit run editions/amd/app/ui_app.py --server.address 0.0.0.0 --server.port "${UI_PORT}" \
    --server.headless true --browser.gatherUsageStats false
}

case "${MODE}" in
  api) api ;;
  ui) ui ;;
  all)
    api &
    api_pid=$!
    ui &
    ui_pid=$!
    trap 'kill -TERM "${api_pid}" "${ui_pid}" 2>/dev/null || true' TERM INT
    set +e
    wait -n "${api_pid}" "${ui_pid}"
    status=$?
    set -e
    kill -TERM "${api_pid}" "${ui_pid}" 2>/dev/null || true
    wait || true
    exit "${status}"
    ;;
  *)
    echo "unknown WAPSI_APP_MODE=${MODE} (expected all, api or ui)" >&2
    exit 64
    ;;
esac
