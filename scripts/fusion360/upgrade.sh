#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DRY_RUN=0
WITH_LOCAL_RUNTIME=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --with-local-runtime) WITH_LOCAL_RUNTIME=1 ;;
    *) printf '%s\n' "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

INSTALL_ARGS=()
if [ "$DRY_RUN" -eq 1 ]; then INSTALL_ARGS+=(--dry-run); fi
if [ "$WITH_LOCAL_RUNTIME" -eq 1 ]; then INSTALL_ARGS+=(--with-local-runtime); fi

if [ "$WITH_LOCAL_RUNTIME" -eq 1 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then "$SCRIPT_DIR/stop-runtime.sh" --dry-run; else "$SCRIPT_DIR/stop-runtime.sh"; fi
fi
if ! "$SCRIPT_DIR/install.sh" "${INSTALL_ARGS[@]}"; then
  printf '%s\n' "Upgrade failed; install.sh restored the previous Add-in code" >&2
  exit 1
fi
if [ "$WITH_LOCAL_RUNTIME" -eq 1 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then "$SCRIPT_DIR/start-runtime.sh" --dry-run; else "$SCRIPT_DIR/start-runtime.sh"; fi
fi
