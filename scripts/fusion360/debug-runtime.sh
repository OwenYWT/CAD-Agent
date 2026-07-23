#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
STATE_DIR=${CAD_AGENT_FUSION_STATE_DIR:-"$HOME/.cad-agent/fusion360"}
if [ "${1:-}" = "--dry-run" ]; then
  printf '%s\n' "DRY-RUN: run Fusion Runtime in foreground with debug logs and loopback binding"
  exit 0
fi
export FUSION_RUNTIME_BACKEND_SECRET="$(cat "$STATE_DIR/backend.secret")"
export FUSION_RUNTIME_CONNECTOR_SECRET="$(cat "$STATE_DIR/connector.secret")"
export FUSION_RUNTIME_DB="$STATE_DIR/runtime.db"
export FUSION_ARTIFACT_ROOT="$STATE_DIR/artifacts"
export PYTHONPATH="$ROOT/backend"
exec "$STATE_DIR/runtime-venv/bin/python" -m uvicorn app.fusion360.runtime_app:app_from_env --factory --host 127.0.0.1 --port 8765 --log-level debug
