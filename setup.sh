#!/usr/bin/env bash
# CAD Agent Web one-shot local setup.
# Defaults to Podman + Azure OpenAI. Copy backend/.env.example to backend/.env first.
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
  die "Created backend/.env from template. Edit Azure OpenAI values, then run this script again."
fi
if grep -q "replace-with-your-azure-openai-key" backend/.env; then
  die "Edit backend/.env and set AZURE_OPENAI_API_KEY first."
fi

say "Building sandbox image with $RUNTIME"
if ! "$RUNTIME" image exists cad-agent-sandbox:latest >/dev/null 2>&1; then
  ( cd backend/sandbox && "$RUNTIME" build -t cad-agent-sandbox:latest . )
else
  echo "Sandbox image already exists."
fi

say "Installing backend dependencies"
( cd backend && "$PYTHON_BIN" -m pip install -r requirements.txt )

say "Installing frontend dependencies and building Web UI"
( cd frontend && npm ci && npm run build )

say "Starting backend on http://localhost:8000"
export SANDBOX_RUNTIME="$RUNTIME"
( cd backend && "$PYTHON_BIN" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 )
