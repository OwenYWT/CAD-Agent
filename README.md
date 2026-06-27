# CAD Agent Web

Natural language to CAD file web application. The product is browser-first: users generate, preview, adjust, analyze, and download CAD outputs directly from the Web UI.

## Architecture

```
React Web App -> REST + WebSocket -> CAD Agent FastAPI -> Podman/Docker Sandbox
```

## Stack

- **Backend**: Python 3.11, FastAPI, CadQuery, ezdxf, trimesh
- **Frontend**: React, TypeScript, Three.js, TailwindCSS, Vite
- **LLM**: Azure OpenAI (`LLM_PROVIDER=azure`) or OpenAI-compatible APIs
- **Sandbox**: Docker or Podman image `cad-agent-sandbox:latest` for isolated CAD code execution
- **Output files**: STEP, STL, DXF, SVG, PNG where supported by the generation pipeline

## Quick Start

### Backend

```bash
cd backend
pip install -r requirements.txt
export LLM_PROVIDER=azure
export AZURE_OPENAI_ENDPOINT=https://<your-azure-openai-resource>.openai.azure.com/
export AZURE_OPENAI_API_KEY=<AZURE_OPENAI_API_KEY>
export AZURE_OPENAI_API_VERSION=2025-03-01-preview
export LLM_MODEL=gpt-5
export LLM_REASONING_EFFORT=minimal
export SANDBOX_RUNTIME=podman
uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

### Sandbox

Use Podman when Docker is unavailable:

```bash
cd backend/sandbox
podman build -t cad-agent-sandbox:latest .
export SANDBOX_RUNTIME=podman
```

Docker remains supported as the default runtime:

```bash
cd backend/sandbox
docker build -t cad-agent-sandbox:latest .
```

## Single-Service Web Deployment

Build the frontend and serve it from FastAPI:

```bash
cd frontend
npm run build

cd ../backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

After `frontend/dist` exists, FastAPI serves the Web app at `/` while API routes continue to use `/api`, `/health`, and `/ws/{session_id}`.


