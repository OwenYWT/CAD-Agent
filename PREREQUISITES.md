# Prerequisites

This package can run three levels of checks.

## 1. Unit tests and dry-run

These checks validate Python code, data paths, and sample selection. They do not
require LLM, database, Temporal, or sandbox services.

```powershell
cd <eval_algorithms>
python -m unittest discover -s tests -p "test_*.py"
python evaluator.py --dataset both --mode generate --limit 1 --dry-run --output-dir results\handoff_smoke_eval
```

## 2. Reference execution

Reference mode executes dataset-provided CadQuery code. It requires the same
CAD execution/sandbox environment used by the CAD-Agent backend.

Check container runtime first:

```powershell
docker info
```

If the backend uses Podman instead, check the configured Podman machine and
connection.

## 3. Current-algorithm generation

Generation through `--runner direct` calls CAD-Agent durable workflow code
in-process. It requires:

- `CAD_AGENT_ROOT` points to the current CAD-Agent repo if it is not a sibling
  directory;
- backend `.env` is configured in `CAD-Agent\backend`;
- LLM provider credentials are valid;
- database is reachable;
- Temporal server and worker are available when required by the backend config;
- sandbox/container runtime is available;
- backend artifact storage is writable.

Typical environment override:

```powershell
$env:CAD_AGENT_ROOT="<CAD-Agent>"
```

Then run a small sample first:

```powershell
cd <eval_algorithms>
python evaluate_cadprompt.py --runner direct --limit 1 --output-dir results\smoke_cadprompt_current_1
```

## Common failure interpretation

- Path or sample count errors: check `eval_config.py` defaults and environment
  overrides.
- Provider errors: check LLM provider and credentials.
- Sandbox errors: check Docker/Podman and backend sandbox config.
- Workflow pending/timeout: check Temporal/worker and generation deadline.
- Missing STL in quality phase: inspect the generation row `files` field.
