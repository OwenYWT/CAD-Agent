#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
DRY_RUN=0
WITH_LOCAL_RUNTIME=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --with-local-runtime) WITH_LOCAL_RUNTIME=1 ;;
    *) printf '%s\n' "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

STATE_DIR=${CAD_AGENT_FUSION_STATE_DIR:-"$HOME/.cad-agent/fusion360"}
if [ "$(uname -s)" = "Darwin" ]; then
  ADDINS_DIR="$HOME/Library/Application Support/Autodesk/Autodesk Fusion/API/AddIns"
else
  ADDINS_DIR="${APPDATA:-$HOME/AppData/Roaming}/Autodesk/Autodesk Fusion/API/AddIns"
fi
ADDIN_TARGET="$ADDINS_DIR/CADAgentFusionConnector"
VENV="$STATE_DIR/runtime-venv"

plan() { printf '%s\n' "$*"; }
run() { if [ "$DRY_RUN" -eq 1 ]; then plan "DRY-RUN: $*"; else "$@"; fi; }

plan "Fusion connector user install (direct HTTPS Cloud Agent mode)"
plan "  Add-in: $ADDIN_TARGET"
plan "  State:  $STATE_DIR"
run mkdir -p "$ADDINS_DIR" "$STATE_DIR/artifacts"

if [ "$WITH_LOCAL_RUNTIME" -eq 1 ]; then
  plan "  Optional loopback Runtime: enabled"
  run python3 -m venv "$VENV"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$VENV/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/scripts/fusion360/runtime-requirements.txt"
    if [ ! -f "$STATE_DIR/backend.secret" ]; then
      python3 -c 'import secrets,sys; open(sys.argv[1],"w").write(secrets.token_urlsafe(48))' "$STATE_DIR/backend.secret"
    fi
    if [ ! -f "$STATE_DIR/connector.secret" ]; then
      python3 -c 'import secrets,sys; open(sys.argv[1],"w").write(secrets.token_urlsafe(48))' "$STATE_DIR/connector.secret"
    fi
    chmod 600 "$STATE_DIR/backend.secret" "$STATE_DIR/connector.secret"
  else
    plan "DRY-RUN: provision independent Backend/Connector Runtime secrets with mode 0600"
  fi
fi

if [ "$DRY_RUN" -eq 0 ]; then
  if [ ! -f "$STATE_DIR/agent.token" ]; then
    install -m 600 /dev/null "$STATE_DIR/agent.token"
  fi
  if [ ! -f "$STATE_DIR/connector.json" ]; then
    CONNECTOR_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
    TEMP_CONFIG="$STATE_DIR/connector.json.tmp"
    sed \
      -e "s#/replace/with/user-only/config/path/agent.token#$STATE_DIR/agent.token#" \
      -e "s#/replace/with/user-only/config/path/connector.secret#$STATE_DIR/connector.secret#" \
      -e "s#replace-with-a-stable-uuid#$CONNECTOR_ID#" \
      -e "s#/replace/with/user/data/path/artifacts#$STATE_DIR/artifacts#" \
      -e "s#/replace/with/user/data/path/execution-journal.json#$STATE_DIR/execution-journal.json#" \
      "$ROOT/fusion_addin/config.example.json" > "$TEMP_CONFIG"
    chmod 600 "$TEMP_CONFIG"
    mv "$TEMP_CONFIG" "$STATE_DIR/connector.json"
  fi
  BACKUP="$STATE_DIR/addin-backup"
  if [ -d "$ADDIN_TARGET" ]; then
    rm -rf "$BACKUP"
    mv "$ADDIN_TARGET" "$BACKUP"
  fi
  if ! cp -R "$ROOT/fusion_addin/CADAgentFusionConnector" "$ADDIN_TARGET"; then
    [ ! -d "$BACKUP" ] || mv "$BACKUP" "$ADDIN_TARGET"
    exit 1
  fi
  rm -rf "$BACKUP"
else
  plan "DRY-RUN: create an empty mode-0600 Agent token file for operator provisioning"
  plan "DRY-RUN: preserve connector.json and atomically replace Add-in code"
fi

plan "Installed. Configure agent_url and place the bearer token in $STATE_DIR/agent.token."
plan "Restart Fusion, open Utilities > Add-Ins, and run CADAgentFusionConnector."
if [ "$WITH_LOCAL_RUNTIME" -eq 1 ]; then
  plan "Local Runtime was installed but is not selected; set mode=local_runtime before starting it."
fi
