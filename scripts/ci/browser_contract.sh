#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
export PYTHONPATH="$root/backend"
api_port=${CAD_BROWSER_API_PORT:-18041}
web_port=${CAD_BROWSER_WEB_PORT:-18111}
export CAD_NATIVE_E2E_URL=http://127.0.0.1:$api_port
export CAD_NATIVE_E2E_WEB=http://127.0.0.1:$web_port
export CAD_API_TARGET="$CAD_NATIVE_E2E_URL"
export CAD_NATIVE_E2E_COMMAND='["python"]'
export CAD_NATIVE_E2E_PRIVATE="${RUNNER_TEMP:?}/cad-browser-private.json"
report_root=${CAD_BROWSER_REPORT_ROOT:-$root/reports}
export CAD_BROWSER_CONTRACT_REPORT="$report_root/browser-contract"
export CAD_TASK_STATE_REPORT_DIR="$report_root/browser-parameters"
export CAD_PARAMETER_REJECTION_REPORT="$report_root/browser-rejection"
export CAD_LAYOUT_REPORT_DIR="$report_root/browser-layout"
export CAD_LAYOUT_FIXTURE="$CAD_TASK_STATE_REPORT_DIR/fixture.json"
export CAD_BROWSER_FAULT_CONTRACTS=1
export CAD_MONITOR_E2E_REPORT="$report_root/browser-monitor"
export CAD_CONSTRAINT_BROWSER_REPORT="$report_root/browser-constraint-repair"
export CAD_NATIVE_PARAMETER_REPORT="$report_root/native-parameters"
export CAD_MONITOR_E2E_URL="http://127.0.0.1:${CAD_BROWSER_MONITOR_PORT:-18092}/"
export TEMPORAL_TASK_QUEUE="browser-contract-${GITHUB_RUN_ID:-local}"
export TEMPORAL_AGENT_V2_TASK_QUEUE="browser-contract-v2-${GITHUB_RUN_ID:-local}"
export APP_ENVIRONMENT=test DURABLE_CONTROL_PLANE_ENABLED=true AUTH_REQUIRED=true
export AUTH_TOKEN_SECRET="$(python -c 'import secrets;print(secrets.token_hex(32))')"
export ADMIN_PASSWORD="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
mkdir -p "$report_root"
cd "$root/backend"
python tests/e2e/create_ci_account.py
python -m uvicorn app.main:app --host 127.0.0.1 --port "$api_port" > "$report_root/browser-api.log" 2>&1 &
api_pid=$!
python -m app.workers.workflow_worker > "$report_root/browser-worker.log" 2>&1 &
worker_pid=$!
python -m uvicorn app.monitoring:app --host 127.0.0.1 --port "${CAD_BROWSER_MONITOR_PORT:-18092}" > "$report_root/browser-monitor.log" 2>&1 &
monitor_pid=$!
(cd "$root/frontend" && exec node node_modules/vite/bin/vite.js --host 127.0.0.1 --port "$web_port") > "$report_root/browser-web.log" 2>&1 &
web_pid=$!
cleanup() {
  kill "$api_pid" "$worker_pid" "$web_pid" "$monitor_pid" 2>/dev/null || true
  wait "$api_pid" "$worker_pid" "$web_pid" "$monitor_pid" 2>/dev/null || true
  rm -f "$CAD_NATIVE_E2E_PRIVATE"
}
trap cleanup EXIT
for attempt in {1..90}; do
  # This contract executes a real compiled native plan, without a Provider key.
  # Require the durable dependencies, not the unrelated LLM readiness claim.
  if python - "$CAD_NATIVE_E2E_URL/ready" <<'PYREADY'
import json, sys, urllib.request, urllib.error
try:
    try:
        response = urllib.request.urlopen(sys.argv[1])
    except urllib.error.HTTPError as response_error:
        response = response_error
    value = json.load(response)
    sys.exit(0 if value.get('durable_control_plane', {}).get('status') == 'ready' else 1)
except (OSError, ValueError):
    sys.exit(1)
PYREADY
  then
    if curl --fail --silent "$CAD_NATIVE_E2E_WEB" >/dev/null; then break; fi
  fi
  kill -0 "$api_pid" "$worker_pid" "$web_pid"
  test "$attempt" -lt 90
  sleep 1
done
python tests/e2e/browser_contract.py
python tests/e2e/task_state_browser.py candidate
python tests/e2e/parameter_rejection_browser.py
python tests/e2e/workspace_layout_browser.py
python tests/e2e/monitoring_permissions_browser.py
# The incident fixture owns a worker with controlled provider proposals. Model
# jobs are claimed from this database, so no second provider runner may compete.
kill "$worker_pid"
wait "$worker_pid" || true
python tests/e2e/constraint_failure_browser.py
python -m app.workers.workflow_worker >> "$report_root/browser-worker.log" 2>&1 &
worker_pid=$!
python tests/e2e/native_parameter_contract.py
"${SANDBOX_COMMAND:-docker}" run --rm --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,size=2g \
  -v "$CAD_NATIVE_PARAMETER_REPORT:/measurements:ro" \
  -v "$root/backend/tests/e2e:/tests:ro" --entrypoint /opt/freecad/bin/FreeCADCmd \
  "${SANDBOX_IMAGE:?SANDBOX_IMAGE required}" \
  -c "exec(compile(open('/tests/native_parameter_measurements.py').read(), '/tests/native_parameter_measurements.py', 'exec'))" \
  > "$CAD_NATIVE_PARAMETER_REPORT/kernel.log" 2>&1
grep -F 'CAD_NATIVE_PARAMETER_MEASUREMENTS=' "$CAD_NATIVE_PARAMETER_REPORT/kernel.log"
if grep -Fq 'Traceback (most recent call last)' "$CAD_NATIVE_PARAMETER_REPORT/kernel.log"; then exit 1; fi
