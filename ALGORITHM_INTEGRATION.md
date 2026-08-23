# Algorithm Integration Handoff

## Legacy integration

The original direct runner called the old algorithm shortcut directly:

```python
app.agent.orchestrator.Orchestrator.generate(...)
app.agent.orchestrator.Orchestrator.execute_code(...)
```

That path bypasses FastAPI, sessions, durable workflow, revision identity,
change sets, inspection, repair history, and artifact persistence. It is useful
only for old-flow comparisons.

## Current default integration

`--runner direct` now uses the current CAD-Agent durable workflow path
in-process:

1. Discover the CAD-Agent backend directory through `eval_config.py`.
2. Bind an evaluation principal.
3. Create an isolated session and panel per sample.
4. Call `ensure_workspace_identity(...)` to create project, branch, and initial
   head revision.
5. Call `submit_durable_workflow(...)` with operation `generate` or `execute`.
6. Call `wait_for_compatibility_response(...)`.
7. Store workflow, revision, change set, files, inspection, repair, and error
   metadata in result JSONL.

Important files:

- Evaluation package: `direct_algorithm.py`, `eval_config.py`, `evaluator.py`.
- CAD-Agent backend: `backend/app/services/durable_submission.py`.

## Why the old Orchestrator shortcut is not the default

Current CAD-Agent behavior includes:

- durable workflow scheduling;
- revision optimistic concurrency;
- change sets;
- engineering inspection and repair history;
- geometry/topology/verification/evidence layers;
- artifact persistence under `/api/files/...`.

The old shortcut skips those layers, so it does not measure the current product
algorithm path.

## HTTP runner

`--runner http` is kept for advanced REST testing. Current REST write APIs
require durable identity fields:

- `project_id`
- `branch_id`
- `expected_base_revision_id`
- `idempotency_key`

A batch evaluator cannot safely reuse one branch identity because each success
moves the branch head. The handoff default is therefore `--runner direct` unless
a dedicated HTTP workspace bootstrap flow is added later.

## Path discovery

Default expected layout:

```text
<workspace-parent>\
  CAD-Agent\
  eval_algorithms\
```

Overrides:

```powershell
$env:CAD_AGENT_ROOT="<path-to-CAD-Agent>"
$env:EVAL_ALGORITHMS_DATA_ROOT="<path-to-eval-data>"
$env:CAD_AGENT_STORAGE_ROOT="<path-to-CAD-Agent>\backend\data\files"
```

## Runtime dependencies

Real generation requires a working CAD-Agent backend environment:

- LLM credentials;
- database;
- Temporal and workers;
- sandbox/container runtime;
- artifact storage directory.

Unit tests and dry-run commands can pass without those runtime services.
