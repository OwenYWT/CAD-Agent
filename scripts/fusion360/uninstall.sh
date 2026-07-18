#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STATE_DIR=${CAD_AGENT_FUSION_STATE_DIR:-"$HOME/.cad-agent/fusion360"}
if [ "$(uname -s)" = "Darwin" ]; then
  CURRENT_TARGET="$HOME/Library/Application Support/Autodesk/Autodesk Fusion/API/AddIns/CADAgentFusionConnector"
  LEGACY_TARGET="$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/CADAgentFusionConnector"
else
  CURRENT_TARGET="${APPDATA:-$HOME/AppData/Roaming}/Autodesk/Autodesk Fusion/API/AddIns/CADAgentFusionConnector"
  LEGACY_TARGET="${APPDATA:-$HOME/AppData/Roaming}/Autodesk/Autodesk Fusion 360/API/AddIns/CADAgentFusionConnector"
fi
if [ "${1:-}" = "--dry-run" ]; then
  "$SCRIPT_DIR/stop-runtime.sh" --dry-run
  printf '%s\n' "DRY-RUN: remove only $CURRENT_TARGET and any legacy-path copy; preserve $STATE_DIR"
  exit 0
fi
if [ "${1:-}" != "" ]; then printf '%s\n' "Unknown option: $1" >&2; exit 2; fi
"$SCRIPT_DIR/stop-runtime.sh"
rm -rf "$CURRENT_TARGET" "$LEGACY_TARGET"
printf '%s\n' "Fusion Add-in removed. Connector configuration, audit journal, and credentials remain in $STATE_DIR."
