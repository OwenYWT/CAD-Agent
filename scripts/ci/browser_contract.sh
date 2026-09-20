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
(cd "$root/frontend" && npm run dev -- --host 127.0.0.1 --port "$web_port") > "$report_root/browser-web.log" 2>&1 &
web_pid=$!
trap 'kill "$api_pid" "$worker_pid" "$web_pid" 2>/dev/null || true; rm -f "$CAD_NATIVE_E2E_PRIVATE"' EXIT
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
