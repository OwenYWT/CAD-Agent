#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
STATE_DIR=${CAD_AGENT_FUSION_STATE_DIR:-"$HOME/.cad-agent/fusion360"}
DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=1; fi
if [ "$DRY_RUN" -eq 1 ]; then
  printf '%s\n' "DRY-RUN: start loopback Runtime on 127.0.0.1:8765 using $STATE_DIR/runtime-venv"
  exit 0
fi
if [ -f "$STATE_DIR/runtime.pid" ] && kill -0 "$(cat "$STATE_DIR/runtime.pid")" 2>/dev/null; then
  printf '%s\n' "Fusion Runtime is already running"
  exit 0
fi
export FUSION_RUNTIME_BACKEND_SECRET="$(cat "$STATE_DIR/backend.secret")"
export FUSION_RUNTIME_CONNECTOR_SECRET="$(cat "$STATE_DIR/connector.secret")"
export FUSION_RUNTIME_DB="$STATE_DIR/runtime.db"
export FUSION_ARTIFACT_ROOT="$STATE_DIR/artifacts"
export PYTHONPATH="$ROOT/backend"
nohup "$STATE_DIR/runtime-venv/bin/python" -m uvicorn app.fusion360.runtime_app:app_from_env --factory --host 127.0.0.1 --port 8765 >"$STATE_DIR/runtime.log" 2>&1 &
printf '%s' "$!" > "$STATE_DIR/runtime.pid"
sleep 1
curl --fail --silent http://127.0.0.1:8765/health >/dev/null
printf '%s\n' "Fusion Runtime started (PID $(cat "$STATE_DIR/runtime.pid"))"
