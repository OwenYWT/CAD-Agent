#!/usr/bin/env bash
# CAD Agent Web one-shot local setup.
# Defaults to Podman + the provider configured in backend/.env.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf "\n\033[1;36m==> %s\033[0m\n" "$1"; }
die() { printf "\n\033[1;31mERROR: %s\033[0m\n" "$1" >&2; exit 1; }

RUNTIME="${SANDBOX_RUNTIME:-podman}"

say "Checking prerequisites"
command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1 || die "Python 3.11+ not found."
command -v node >/dev/null 2>&1 || die "Node.js not found."
command -v npm >/dev/null 2>&1 || die "npm not found."
command -v "$RUNTIME" >/dev/null 2>&1 || die "$RUNTIME not found. Install Podman Desktop or Docker Desktop."

PYTHON_BIN="python"
if ! command -v python >/dev/null 2>&1; then PYTHON_BIN="python3"; fi

say "Preparing backend .env"
if [ ! -f backend/.env ]; then
  cp backend/.env.example backend/.env
  die "Created backend/.env from template. Fill the LLM and auth values, then run this script again."
fi

say "Installing backend dependencies"
( cd backend && "$PYTHON_BIN" -m pip install -r requirements.txt )

SANDBOX_IMAGE=$(cd backend && "$PYTHON_BIN" -c 'from app.config import settings; print(settings.sandbox_image)')
export SANDBOX_IMAGE SANDBOX_RUNTIME="$RUNTIME"
check_sandbox() {
  ( cd backend && "$PYTHON_BIN" -c 'from app.execution.composition import get_execution_backend; get_execution_backend().runtime_snapshot()' )
}

say "Checking sandbox image $SANDBOX_IMAGE with $RUNTIME"
if ! check_sandbox >/dev/null 2>&1; then
  say "Building missing or incompatible sandbox image $SANDBOX_IMAGE"
  "$RUNTIME" build -f backend/sandbox/Dockerfile -t "$SANDBOX_IMAGE" .
  check_sandbox
fi

say "Installing frontend dependencies and building Web UI"
( cd frontend && npm ci && npm run build )

say "Starting backend on http://localhost:8000"
( cd backend && "$PYTHON_BIN" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 )
