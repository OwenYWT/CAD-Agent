#!/usr/bin/env bash
set -euo pipefail
STATE_DIR=${CAD_AGENT_FUSION_STATE_DIR:-"$HOME/.cad-agent/fusion360"}
if [ "${1:-}" = "--dry-run" ]; then
  printf '%s\n' "DRY-RUN: stop only the PID recorded in $STATE_DIR/runtime.pid"
  exit 0
fi
if [ ! -f "$STATE_DIR/runtime.pid" ]; then
  printf '%s\n' "Fusion Runtime is not running"
  exit 0
fi
PID=$(cat "$STATE_DIR/runtime.pid")
if kill -0 "$PID" 2>/dev/null; then kill "$PID"; fi
rm -f "$STATE_DIR/runtime.pid"
printf '%s\n' "Fusion Runtime stopped"
