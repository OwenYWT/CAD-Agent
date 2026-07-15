# CAD Agent Web

Natural language to CAD file web application. The product is browser-first: users generate, preview, adjust, analyze, and download CAD outputs directly from the Web UI.

## Architecture

```
React Web App -> REST + WebSocket -> CAD Agent FastAPI -> Podman/Docker Sandbox
```

## Stack

- **Backend**: Python 3.11, FastAPI, CadQuery, ezdxf, trimesh
- **Frontend**: React, TypeScript, Three.js, TailwindCSS, Vite
- **LLM**: Azure OpenAI (`LLM_PROVIDER=azure`) or any OpenAI-compatible API (e.g. Moonshot `kimi-k2.5`, DashScope `qwen-plus`)
- **Sandbox**: Docker or Podman image `cad-agent-sandbox:latest`, or `SANDBOX_RUNTIME=local` (host subprocess, dev only — no container isolation) for isolated CAD code execution
- **Output files**: STEP, STL, DXF, SVG, PNG where supported by the generation pipeline

## Vision verify-and-correct

Generated geometry is rendered to images (trimesh with a matplotlib software fallback
for GL-less hosts) and shown to a multimodal model that checks the shape against the
prompt. On a mismatch the model's critique drives a targeted code fix, retried up to
`VISION_MAX_RETRIES` times; a mismatch that survives the budget is surfaced honestly as
a `fail` check on the inspect report (and the code is not cached), never a silent pass.

Set `VISION_MODEL` to a model that accepts image input (defaults to `LLM_MODEL`). With a
text-only `LLM_MODEL`, leave `VISION_MODEL` pointing at a multimodal model — otherwise the
visual check degrades to "indeterminate" and no correction happens.

### Experiment results

Measured on this build with `LLM_MODEL=VISION_MODEL=kimi-k2.5` (Moonshot, OpenAI-compatible),
CadQuery via `SANDBOX_RUNTIME=local`, and the matplotlib software renderer (no GPU on the
test host). Because the earlier pipeline only checked that code *executed* and that the mesh
was *watertight* — never whether the shape matched the request — every geometry below would
previously have been returned to the user as `success` regardless of correctness. The
geometries are built deterministically so the numbers isolate the vision check itself, not
LLM code-generation variance. (`backend/benchmark/`; full log in the PR.)

**Experiment 1 — detection** (does the vision judge flag wrong geometry?). 8 controlled
`(description, geometry)` cases, 4 correct and 4 deliberately wrong:

| Failure mode injected                        | Judge verdict | Correct? |
|----------------------------------------------|---------------|----------|
| Box described as a cylinder (wrong shape)    | mismatch      | ✅       |
| 40 mm cube described with a Ø12 through-hole, hole omitted | mismatch | ✅ |
| Box described as an L-bracket (wrong shape)  | mismatch      | ✅       |
| Boss floating 30 mm above the body (detached)| mismatch      | ✅       |
| 4 correct shapes (box, cylinder, hole, shell)| 3 match / 1 false alarm | 3/4 |

- **Wrong-geometry detection rate: 4/4 = 100%** — each flagged with a specific, correct
  critique (e.g. *"凸台与盒体完全分离，存在明显间隙"* / *"顶视图和前视图显示为矩形而非圆形，确认生成的是棱柱而非圆柱"*).
- Specificity 3/4 (75%): the one false alarm was a *correct* box-with-hole that the crude
  flat-shaded fallback render made look chamfered — a rendering-quality limit, not a logic
  error, and it fails safe (surfaced as a `fail`/`warn`, never a wrong silent pass).

**Experiment 2 — correction** (does the loop fix what it flags?). Start from flawed CadQuery,
run the real render → judge → `fix_visual_issues` → re-execute → judge loop:

| Flawed input                                  | Before | After (1 fix round) |
|-----------------------------------------------|--------|---------------------|
| Cube with the Ø12 through-hole missing        | mismatch | **match** ✅       |
| Boss floating 15 mm above the box (detached)  | mismatch | **match** ✅       |

- **Correction rate: 2/2 = 100%**, each in a single fix round driven by the model's own critique.

**Net improvement.** Of 6 flawed models that the old pipeline would have shipped silently as
`success`, the vision loop flagged **6/6** and automatically repaired **2/2** of the ones it
was allowed to fix. Cost is latency: on this host one judged attempt adds ~8 s of rendering
plus one multimodal call (kimi ~15–40 s); rendering is off-loaded to a worker thread so it
does not block the server.

*Caveats: the evaluator is the same multimodal model that drives the correction, and N is
small, so this measures shape-match reliability (catching/repairing the wrong-shape,
missing-feature, and detached-part failure modes), not absolute ground truth. A real GPU
render would raise specificity on fine features like small holes.*

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


