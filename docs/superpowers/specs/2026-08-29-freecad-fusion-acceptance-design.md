# FreeCAD Fusion Acceptance Remediation Design

## Status

Approved in conversation on 2026-08-29. This design addresses the nine CTO
acceptance blockers found after the durable Agent fusion. It extends the
existing fused architecture; it does not introduce a second orchestration or
artifact write path.

## Goal

Make browser-originated generation and modification, durable confirmation,
typed FreeCAD execution, projected parameters, Assembly BOM, and browser QA
operate as one truthful end-to-end product path. A passing result must come
from real FreeCAD execution and committed revision artifacts. Mock data,
placeholder UI, fixed outputs, and silent fallbacks are forbidden.

## Decisions

- Use one official FreeCAD 1.1.3 runtime for all FreeCAD generation,
  modification, inspection, validation, and BOM operations.
- Remove FreeCAD 0.20.2 from the production sandbox image after the 1.1.3
  compatibility gate passes. Do not maintain dual runtimes.
- Keep Temporal as the only production CAD workflow owner and preserve the
  existing candidate, staging-manifest, validation, Change Set, and revision
  fences.
- Put a new `BOM` tab in the existing right-side workspace inspector. It reads
  only persisted BOM evidence associated with the active committed or
  reviewable revision.
- Enable SwiftShader only in the headless QA launcher. Do not change the
  production 3D renderer or claim a canvas passed when WebGL is unavailable.

## Normative source changes

The following contracts and files are normative. Names in this section are
the names to implement; later prose explains behavior but does not override
these shapes.

### Submission and workflow contracts

`backend/app/models/schemas.py` changes the public REST request to:

```json
{
  "project_id": "uuid",
  "branch_id": "uuid",
  "expected_base_revision_id": "uuid",
  "idempotency_key": "1..500 characters",
  "prompt": "1..10000 characters",
  "code": "optional, 1..50000 characters when present",
  "modeling_backend": "auto | freecad | cadquery",
  "output_formats": ["step", "stl"]
}
```

This remains `ModifyRequest`; `modeling_backend` defaults to `auto` and
`code` defaults to `null`. Resolution is exact:

- `auto` + `code` selects `cadquery`, preserving existing code clients;
- `auto` + no `code` selects `freecad` only if the base revision owns exactly
  one integrity-valid `fcstd` artifact;
- explicit `cadquery` requires `code`;
- explicit `freecad` forbids `code` and requires the base FCStd;
- all other combinations fail before creating an execution attempt.

For a resolved FreeCAD request, public `output_formats` controls derivative
exports only; the existing FreeCAD compiler still requires and emits `fcstd`
and `state` for every successful execution.

The corresponding public errors are `modify_code_required`,
`modify_code_not_allowed`, `modify_base_fcstd_missing`, and
`modify_base_fcstd_ambiguous`. `/api/modify` retains `GenerateResponse`; these
codes occupy `error.type`, with bounded text in `error.message`. Pre-execution
input failures use HTTP 422, a stale base uses the existing HTTP 409, and
missing/forbidden project resources retain 404/403.

`frontend/src/hooks/useWebSocket.ts` sends the following current-browser
message for natural-language work:

```json
{
  "type": "user_message",
  "text": "string",
  "operation_intent": "generate | modify",
  "capability": "auto | cad | dxf",
  "panel_id": "string",
  "workflow_run_id": "optional uuid",
  "project_id": "uuid when the panel exists",
  "branch_id": "uuid when the panel exists",
  "expected_base_revision_id": "uuid when the panel exists",
  "idempotency_key": "string"
}
```

The new UI sets `generate` only for an empty/new conversation. Once the panel
has a successful editable result, it sets `modify`. `modify_part` remains a
supported explicit modify request and its browser `code` field becomes
optional. The browser never chooses `modeling_backend`; the server resolves it
from the authorized base revision after resolving `operation`:

1. Exactly one integrity-valid committed `fcstd` artifact selects `freecad`.
   This takes precedence over any browser-supplied code, which is discarded.
2. With no FCStd, exactly one source reachable from the base revision's sealed
   manifest selects `cadquery`. For an Agent revision, the resolver follows
   each committed modeling artifact back through the revision's
   `selected_manifests` entry, accepted staging manifest, and `source_id`, then
   deduplicates by verified `(source_id, source_hash)`; exactly one distinct
   terminal source is required. For an older `mcad-revision-manifest.v1`
   revision, it selects the last persisted `executions` entry that declares a
   committed output, verifies the inline code against `source_sha256`, and uses
   that code. The server loads this source and does not trust the browser copy.
3. More than one eligible FCStd or persisted source is an ambiguous-base
   failure. No eligible source is a missing-base failure.
4. A legacy `modify_part.code`, when present on the CadQuery branch, is only a
   compatibility assertion: its SHA-256 must equal the trusted persisted
   source hash. It is never the source of record.

The exact browser resolution errors are `modify_base_fcstd_ambiguous`,
`modify_base_source_ambiguous`, `modify_base_source_missing`, and
`modify_source_code_mismatch`. They fail before workflow creation. The resolved
backend and trusted source identity are passed explicitly to
`submit_durable_workflow`; that service copies them unchanged into
`McadAgentWorkflowV2Request` and must not resolve a modify backend again.

Generate is intentionally different because `model_kind` does not exist until
durable planning finishes. A 2D `dxf` submission sets request
`modeling_backend='cadquery'`; a 3D browser generation sets it to `auto`. The
planner first returns an unpersisted plan candidate. Before computing a plan
hash, appending `agent.plan.completed`, or allocating a candidate, the workflow
passes that candidate through the deterministic
`normalize_agent_plan_backend(request, plan_candidate)` function: `assembly`
and `profile_2d` resolve to CadQuery and every other current 3D model kind
resolves to FreeCAD. The copied, normalized `AgentPlan` contains the concrete
backend; only that copy is hashed and persisted. New-history execution reads
only the normalized plan value and never falls back to the other backend after
failure. A modify request is never `auto` inside Temporal: it is concrete
before workflow creation by the source rules above.

For `operation='modify'`, the same normalizer does not reclassify by model
kind. It requires the request backend to be concrete, fills a null
plan-candidate backend from that request value, and rejects a non-null different
value with `agent_plan_backend_mismatch`. Therefore every newly persisted
modify plan satisfies
`plan.modeling_backend == request.modeling_backend` before hashing, event
append, or candidate allocation.

A missing `operation_intent` remains compatible for older clients: the server
resolves the trusted base first, chooses `modify` for exactly one eligible
source, chooses `generate` for none, and returns the corresponding ambiguous
error for more than one. The server does not inspect Chinese or English
keywords. An explicit `generate` against a panel whose trusted head already
has an eligible source returns `new_conversation_required`; it cannot overwrite
the current design context.

Submission failures use one compatibility-preserving public envelope. REST
places it in `GenerateResponse.error`; the session WebSocket places the same
object in `generation_result.data.error`:

```json
{
  "type": "modify_base_source_missing",
  "message": "bounded localized display text",
  "details": {"base_revision_id": "uuid"},
  "retryable": false
}
```

`type` remains the stable machine code because existing REST and frontend
clients already read that field. `message` is presentation only; control flow
must not parse it. `details` contains only bounded JSON primitives and arrays,
never paths, generated code, object-store keys, or tracebacks. Resolution
failures and `new_conversation_required` are non-retryable and use HTTP 422 for
REST. The WebSocket remains open after returning the error.

State transport is fixed rather than inferred. The session WebSocket returns
only the immediate durable acknowledgement:

```json
{
  "type": "task_submitted",
  "data": {
    "workflow_run_id": "uuid",
    "project_id": "uuid",
    "branch_id": "uuid",
    "expected_base_revision_id": "uuid",
    "panel_id": "string",
    "status": "pending"
  }
}
```

After that acknowledgement, `GET /api/tasks/{workflow_run_id}/snapshot` and
the existing durable task event WebSocket are the only UI authorities. The
persisted workflow status vocabulary remains exactly `pending`, `planning`,
`running`, `waiting_confirmation`, `cancelling`, `succeeded`, `failed`,
`cancelled`, and `timed_out`; the frontend may map these to visual labels but
must retain the raw status. Reconnect first replaces panel durable state from
the snapshot, then applies events whose sequence is greater than
`last_event_sequence`. The session WebSocket does not emit synthetic progress,
confirmation, parameter, BOM, or terminal success state.

`backend/app/workflows/temporal.py` adds these frozen contracts:

```text
OperationContextV1
  schema_version = "mcad-operation-context.v1"
  rule = explicit_rest_operation | explicit_modify_part |
         explicit_parameter_edit | explicit_ui_intent |
         legacy_editable_base_present | legacy_empty_panel
  source_channel: rest | session_websocket
  panel_id: str | null
  requested_operation: generate | modify | null
  resolved_operation: generate | modify
  requested_modeling_backend: auto | freecad | cadquery | null
  submission_modeling_backend: auto | freecad | cadquery
  base_revision_id: UUID
  base_source_kind: fcstd_artifact | agent_generated_source |
                    revision_manifest_source | request_code | none
  base_source_id: UUID | null
  base_source_sha256: lowercase SHA-256 | null

FreeCADParameterUpdateV1
  parameter_id: "<ObjectName>.<PropertyName>"
  value: finite number

FreeCADStructuredModificationV1
  schema_version = "freecad-structured-modification.v1"
  expected_state_sha256: lowercase SHA-256
  parameter_updates: non-empty tuple[FreeCADParameterUpdateV1]
```

`McadAgentWorkflowV2Request.modeling_backend` expands to
`auto | freecad | cadquery`, retaining historical default `cadquery`.
`AgentPlan` gains optional `modeling_backend: freecad | cadquery`; it is
required on every newly generated plan and defaults to null only so historical
plan/event payloads replay. `McadAgentWorkflowV2Request` also gains optional
`operation_context` and optional
`structured_modification`, both defaulting to `null` so historical Temporal
payloads remain valid. A structured modification is valid only for
`operation=modify`, `modeling_backend=freecad`, and `existing_code=null`.
For every new request, top-level `operation` and `modeling_backend` must equal
the submission values in `operation_context`; source kind `fcstd_artifact`
requires concrete FreeCAD, `agent_generated_source` and
`revision_manifest_source` require concrete CadQuery, `request_code` requires
REST plus concrete CadQuery, and `none` is valid only
for generate with `auto` or CadQuery. A new plan must contain a concrete
backend before candidate allocation. `base_source_id` is the artifact/source
UUID; it is null for inline legacy revision-manifest source because the owning
`base_revision_id` plus verified hash is its identity, and null for
`request_code` because its verified hash plus the request payload hash is its
identity.
`panel_id` is required for `session_websocket` and must be null for `rest`.
These values are persisted unchanged in `workflow_runs.request_payload`, whose
existing canonical hash/idempotency behavior remains authoritative.

The Agent V2 workflow guards plan-owned backend selection with Temporal patch
ID `agent-v2-backend-policy-v1`. Inside the patched branch, normalization above
is mandatory and null is an error before the plan event. Histories that did not
record that patch keep the exact existing rule during replay:
`freecad` only when request backend is FreeCAD and model kind is neither
`assembly` nor `profile_2d`, otherwise `cadquery`. This unpatched branch accepts
historical null plan backends; the patched branch never does. The later BOM
branch uses its separate `agent-v2-native-bom-v1` patch ID. These IDs are
literals and may not be renamed after deployment.

The parameter drawer sends one batch message rather than one request per
field:

```json
{
  "type": "modify_parameters",
  "updates": [{"parameter_id": "Pad.Length", "value": 12.0}],
  "expected_state_sha256": "64 lowercase hex characters",
  "panel_id": "string",
  "project_id": "uuid",
  "branch_id": "uuid",
  "expected_base_revision_id": "uuid",
  "idempotency_key": "string"
}
```

The backend loads and verifies the base `state` artifact, validates every ID,
rejects duplicate parameter IDs, validates every type, editability, bound, and
finite value, sorts updates by parameter ID, and
compiles one `property.set` operation per update followed by the existing
`document.export`. It does not call an LLM and does not patch source code.

For this path, `backend/app/freecad/contracts.py::PropertySetArgs` adds optional
backward-compatible fields
`expected_property_type: str | null = null` and
`unit: 'mm' | 'deg' | null = null`. The structured compiler always fills both:
Length/Distance use `mm`, Angle uses `deg`, and Float/Integer use null. The
runner rechecks the live property type against `expected_property_type`, then
assigns an explicit FreeCAD quantity string for mm/deg or a typed numeric value
for Float/Integer. Existing non-structured `property.set` callers may omit both
fields and retain current behavior.

### State and frontend parameter contracts

`backend/app/freecad/state_projector.py` upgrades its artifact to
`freecad-state.v2`; readers accept v1 for existing revisions but only v2 can
drive structured parameter editing. The exact v2 addition is:

```json
{
  "schema_version": "freecad-state.v2",
  "document": "Model",
  "object_count": 4,
  "root_objects": ["Body"],
  "objects": [],
  "parameters": [
    {
      "id": "Pad.Length",
      "object_name": "Pad",
      "property_name": "Length",
      "label": "Pad · Length",
      "group": "Data",
      "property_type": "App::PropertyLength",
      "value": 10.0,
      "unit": "mm",
      "editable": true,
      "minimum": null,
      "maximum": null,
      "step": null
    }
  ]
}
```

Only finite scalar properties of `App::PropertyLength`,
`App::PropertyDistance`, `App::PropertyAngle`, `App::PropertyFloat`, and
`App::PropertyInteger` with editor mode `0` are included. Shape, placement,
link, expression-controlled, transient, output/read-only, and unsupported
properties are excluded. Bounds and step are `null` unless FreeCAD exposes a
real constraint; the projector does not invent slider ranges.

Numeric wire units are fixed: Length/Distance values are millimetres, Angle is
degrees, Float is dimensionless, and Integer must be a JSON number whose
mathematical value is integral and within FreeCAD's accepted integer range.
The projector obtains quantity values in those units rather than serializing a
locale-dependent `UserString`. The structured modifier converts the wire
number back to the corresponding FreeCAD quantity before `property.set`; it
does not rely on the runtime's display-unit preference.

The state bytes remain an immutable `artifacts` row with
`artifact_kind='state'`; the object-store bytes and `artifacts.sha256` are the
source of truth. No parameter table is added. `get_task_snapshot` and
`get_revision_detail` load at most the one selected state artifact, verify its
size/SHA-256 and schema, and project:

```text
parameters: list[CADParameter]
parameter_state_sha256: SHA-256 | null
```

`CADParameter` keeps all current code-parameter fields for compatibility and
adds optional `source='code'|'freecad'`, `object_name`, `property_name`, and
`property_type`; `line` becomes optional. A FreeCAD state entry maps `id` to
`CADParameter.name`, uses the current revision value as `default_value`, and
sets `source='freecad'`. `GenerationResult` and the TypeScript equivalent add
`parameter_state_sha256`. `projectAdapter.ts` uses the stable `name` unchanged
as the UI parameter ID.

### Confirmation projection and request

`backend/app/models/schemas.py` adds this projection to
`DurableTaskSnapshot.confirmation`:

```text
DurableConfirmationProjection
  status = waiting
  workflow_run_id: UUID
  reason: str
  plan_hash: lowercase SHA-256
  affected_objects: list[AffectedObject]
```

It exists only while `workflow_runs.status='waiting_confirmation'`. Its data is
derived from the persisted `agent.plan.completed` event and the canonical
plan hash; no browser-only confirmation state is copied into it. Missing or
malformed persisted plan data while the workflow claims to be waiting is a
`confirmation_projection_invalid` server error, not an empty card.

The frontend calls the already-existing endpoint with exactly:

```http
POST /api/tasks/{workflow_run_id}/confirmation
Content-Type: application/json

{"accepted": true|false, "note": "0..4000 characters"}
```

A successful signal delivery returns
`{"workflow_run_id":"uuid","accepted":bool,"status":"signal_delivered"}`.
Errors use FastAPI's nested shape
`{"detail":{"code":"stable_code","message":"bounded text","retryable":bool}}`:
HTTP 409 `confirmation_not_waiting`, HTTP 403 `task_forbidden`, HTTP 404
`task_not_found`, and HTTP 503 `confirmation_signal_unavailable` with
`retryable=true`. `confirmation_projection_invalid` is HTTP 500 and
non-retryable to the browser. The card remains visible with disabled buttons
while the request is in flight and is cleared only by a newer task
snapshot/event. A rejection ends the current workflow as `cancelled`; it does
not allocate a candidate.

### Structured execution error contract

`backend/app/execution/contracts.py` extends `ExecutionError` without changing
`execution-result.v1`:

```text
category: existing ExecutionErrorCategory
code: stable string
message: bounded display text
operation_id: string | null = null
action: string | null = null
details: map[string, primitive | list[string] | list[int]] = {}
retryable: bool = false
evidence: existing primitive map
```

The defaults are normative: existing persisted `execution-result.v1` objects
without the four new fields must continue to validate and project. No v1 field
is made newly required.

`backend/sandbox/freecad_entry.py` continues to emit
`freecad-operation-result.v1`. `backend/sandbox/capability_entry.py` preserves
its error in the outer `result.json` instead of converting it to a string. The
inner v1 key remains `op_id` for compatibility and is renamed exactly once to
outer `operation_id`:

```json
{
  "status": "error",
  "error": {
    "schema_version": "mcad-error.v1",
    "code": "sketch_conflicting_constraints",
    "message": "bounded text",
    "operation_id": "constraint-04",
    "action": "sketch.add_constraint",
    "details": {"object": "Sketch", "solver_status": -3}
  }
}
```

`backend/app/sandbox/executor.py::SandboxResult` gains
`error_code: str | null` and `error_details: dict = {}` while retaining its
existing `error_message`. `capability_entry.py` copies a valid `mcad-error.v1`
object unchanged into its outer `result.json.error`. The host executor maps
that object as follows:

```text
SandboxResult.error_code              <- error.code
SandboxResult.error_message           <- error.message
SandboxResult.error_details           <- entire mcad-error.v1 object
ExecutionError.code                   <- SandboxResult.error_code
ExecutionError.message                <- SandboxResult.error_message
ExecutionError.operation_id           <- error_details.operation_id
ExecutionError.action                 <- error_details.action
ExecutionError.details                <- error_details.details
ExecutionError.category / retryable   <- the stable code table below
ExecutionError.evidence               <- existing bounded runtime evidence
```

`PodmanExecutionBackend` uses the generic `sandbox_protocol_error` only when
the sandbox returned no valid structured envelope. It does not flatten a valid
envelope into `cad_execution_failed`.
Sandbox tracebacks remain bounded internal diagnostic output and are not copied
into task snapshots, REST responses, WebSocket events, or frontend text.

`backend/alembic/versions/0011_freecad_fusion_acceptance.py`, directly after
`0010_agent_candidate_seal_links`, adds non-null JSONB
`error_details DEFAULT '{}'` to `execution_attempts` and `step_runs`.
`transition_attempt` and `transition_step` keep their existing scalar
`error_code`/`error_message` columns and additionally write
`ExecutionError.model_dump(mode='json')` verbatim to `error_details`; their
state-change event payloads copy that same object under `error`. An empty JSONB
object means no structured error for old rows. `DurableAttemptSnapshot` and
`DurableStepSnapshot` add `error: ExecutionError | null`, decoded from JSONB,
while retaining the two scalar compatibility fields.
`DurableTaskSnapshot.error` is non-null only when the current workflow status
is `failed` or `timed_out`. It selects the error from the most recently updated
currently-failed/timed-out step and then that step's latest failed/timed-out
attempt; earlier failures followed by a successful repair are never selected.
For every other workflow status, including `succeeded` and `cancelled`, the
task-level error is null, while historical step/attempt snapshots retain their
own errors for the timeline. If a terminal legacy row has only scalar
code/message, synthesis uses category `timeout` for `timed_out` and `internal`
for `failed`, with null operation/action, empty details/evidence, and
`retryable=false`. Thus reload/reconnect does not lose a terminal structured
error, does not resurrect a repaired error, and old rows remain readable.

The stable codes and categories for this remediation are:

| Code | Category | Retryable | Meaning |
|---|---|---:|---|
| `agent_plan_backend_mismatch` | `validation` | false | modify plan backend contradicts the trusted request backend |
| `invalid_edge_selection_mode` | `user_input` | false | all-edges/selector combination is incomplete or contradictory |
| `topology_resolution_failed` | `validation` | false | semantic selector did not resolve under the expected revision |
| `topology_target_mismatch` | `validation` | false | selector resolved a different object |
| `sketch_malformed_constraint` | `user_input` | false | constraint signature/index is invalid |
| `sketch_redundant_constraints` | `validation` | false | FreeCAD solver status is `-2` |
| `sketch_conflicting_constraints` | `validation` | false | FreeCAD solver status is `-3` |
| `sketch_under_constrained` | `validation` | false | solver succeeded but a required sketch is not fully constrained |
| `sketch_solver_failed` | `cad_kernel` | false | another negative/failed solver status |
| `parameter_state_stale` | `validation` | false | client state SHA differs from committed state artifact |
| `parameter_state_missing` | `validation` | false | base revision has no unique editable v2 state artifact |
| `parameter_duplicate_update` | `user_input` | false | a batch contains the same stable parameter ID more than once |
| `parameter_not_editable` | `user_input` | false | parameter ID is absent or excluded/read-only |
| `parameter_value_type_invalid` | `user_input` | false | value cannot be represented by the declared FreeCAD property type |
| `parameter_property_type_changed` | `validation` | false | live FCStd property type differs from the verified state artifact |
| `parameter_value_out_of_range` | `user_input` | false | typed value violates a real FreeCAD bound/type |
| `bom_input_missing` | `artifact` | false | an accepted combine/component STEP cannot be resolved |
| `bom_input_ambiguous` | `artifact` | false | a required step has more than one eligible accepted STEP |
| `bom_input_integrity_failed` | `artifact` | false | a declared BOM input fails size/SHA verification |
| `bom_runtime_unsupported` | `infrastructure` | false | required native Assembly/BOM type is absent |
| `bom_source_not_assembly` | `validation` | false | source contains no native BOM-eligible rows |
| `bom_source_hierarchy_lost` | `validation` | false | native rows do not match the persisted assembly component set |
| `bom_source_geometry_mismatch` | `validation` | false | rebuilt component tree does not match combine STEP measurements |
| `bom_generation_failed` | `cad_kernel` | false | native BOM recompute/read failed |
| `bom_empty` | `validation` | false | native BOM returned headers but zero rows |
| `bom_seal_failed` | `artifact` | false | BOM passed but the candidate seal failed before revision allocation |
| `bom_seal_timed_out` | `timeout` | false | BOM passed but the candidate seal timed out before revision allocation |
| `sandbox_protocol_error` | `infrastructure` | false | result envelope is missing or malformed |
| `freecad_internal_error` | `internal` | false | unexpected runner exception |

`retryable` means that Temporal may repeat the identical execution input. The
workflow retry policy reads this field directly; it must not derive
retryability again from `category`. A solver, selection, or parameter failure
is therefore non-retryable for the identical input. An Agent repair, when
allowed by the existing bounded repair policy, is a new persisted source/plan
and a new attempt—not a retry of the failed input.
Existing codes outside this remediation keep their current explicit mapping;
an unknown structured code maps to category `internal` and `retryable=false`.

For sketch state, the projector emits `constraint_status` using deterministic
precedence `invalid` > `conflicting` > `redundant` > `under_constrained` >
`fully_constrained`. Negative solver results fail execution with the matching
code; an under-constrained sketch is retained as state only until an operation
that requires a fully constrained profile consumes it.

### Native BOM execution, persistence, and read contract

`backend/app/agent/durable_plan.py` adds a `bom` validation policy. It defaults
to `disabled` when absent so historical plan payloads validate; every newly
created assembly plan sets it to `required`. The workflow addition is guarded
by Temporal patch ID `agent-v2-native-bom-v1` so existing V2 histories replay
without scheduling a new activity.

After the assembly-combine staging manifest passes geometry validation, the
workflow creates step kind `agent_bom` and a real `ExecutionAttempt`. It runs
`ExecutionSpec(capability='mcad.freecad', operation='bom', mode='analysis')`
and adds `('freecad','bom')` to
`backend/sandbox/capability_entry.py::SUPPORTED`.

The BOM activity resolves inputs only from accepted manifests already recorded
for this candidate. It declares the combine manifest's STEP as artifact ID
`assembly:<combine-step-key>` and each `assembly_part` dependency's STEP as
`component:<part-step-key>`. Missing, duplicate, non-STEP, hash-invalid, or
cross-candidate artifacts return `bom_input_missing`, `bom_input_ambiguous`,
or `bom_input_integrity_failed` before sandbox execution. The canonical JSON
`ExecutionSource` is exactly:

```json
{
  "schema_version": "mcad-capability-task.v1",
  "capability": "freecad",
  "operation": "bom",
  "params": {
    "schema_version": "freecad-bom-request.v1",
    "candidate_build_id": "uuid",
    "base_revision_id": "uuid",
    "plan_hash": "64 lowercase hex characters",
    "runtime_image_digest": "string matching ^sha256:[0-9a-f]{64}$",
    "combine_step_key": "combine",
    "components": [
      {
        "step_key": "part-01",
        "label": "Bracket",
        "position_mm": [0.0, 0.0, 0.0],
        "artifact_id": "component:part-01",
        "quantity": 1
      }
    ],
    "property_columns": []
  },
  "inputs": {
    "assembly": "combine.step",
    "component:part-01": "part-01.step"
  }
}
```

`backend/app/freecad/bom_contracts.py` owns frozen, `extra='forbid'` Pydantic
models `FreeCADBOMRequestV1`, `FreeCADBOMComponentV1`, and
`FreeCADBOMDocumentV1` for the host-side shapes above. The sandbox does not
import the web application model layer: `backend/sandbox/freecad_bom.py`
implements an independent fail-closed validator for the same wire contract and
the native runner. `backend/sandbox/freecad_entry.py` dispatches `operation`
`execute` to its existing path and `bom` to that module; no second container or
FreeCAD executable is introduced.

`components` is sorted by persisted `AgentPlan.steps` order. Its label and
placement come from that immutable plan, and its artifact ID/hash come from the
accepted part manifest; the browser cannot supply this descriptor. The
activity recomputes and verifies `plan_hash` from the persisted plan before
creating the spec. It obtains `runtime_image_digest` from one backend runtime
snapshot and uses the same value in the task source and
`ExecutionSpec.runtime.image_digest`; a mismatch is rejected before execution.
Current assembly planning models every component occurrence
as one `assembly_part`, so `quantity` is fixed at `1`; repeated occurrences are
separate entries. A future grouped-quantity contract requires a schema version
change and is not inferred from matching labels.

The runner first imports every declared component STEP into one new FreeCAD
document, puts its imported `Part::Feature` objects beneath an `App::Part`
whose label and placement are taken from the trusted descriptor, and saves the
document as `assembly.FCStd`. The combine STEP is independently imported and
checked for at least one solid, but is not substituted for the component tree.
Before BOM generation, both representations must have valid shapes and equal
solid count. For each bounding-box dimension in millimetres and for total
volume in cubic millimetres, values `a` and `b` must satisfy
`abs(a-b) <= max(1e-6, 1e-6 * max(abs(a), abs(b)))`; otherwise the attempt
fails `bom_source_geometry_mismatch`.
This preserves the per-part boundary even if a STEP importer flattens the
CadQuery assembly hierarchy. The runner sets `bom.detailParts = false` and
`bom.onlyParts = true`, so the native BOM contains one top-level row per
persisted component occurrence and no duplicate child-shape rows.

The runner loads `Assembly`, then uses the 1.1.3 native type and recompute path:

```python
bom = document.addObject("Assembly::BomObject", "CADAgentBOM")
bom.columnsNames = ["Index", "Name", "Quantity", "File Name", *property_columns]
bom.detailParts = False
bom.onlyParts = True
document.recompute()  # BomObject::execute() calls generateBOM()
```

Property columns are accepted only as safe FreeCAD property identifiers and
are prefixed with `.` for the native BOM object, as required by FreeCAD 1.1.3.
The runner reads the generated spreadsheet with its native cell accessor,
allows at most 64 columns and 10,000 rows, requires positive integer Quantity,
and rejects a header-only result. It does not traverse the document to create
its own component rows. Before success it compares native row count, ordered
labels, and total quantity with the trusted `components` descriptor. No
eligible rows is `bom_source_not_assembly`; any count/label/quantity mismatch
is `bom_source_hierarchy_lost`. There is no one-row compound fallback and no
Python-generated substitute row.

The native `File Name` cell may contain the sandbox's absolute FCStd path. The
serializer exposes only `Path(cell).name` after safe-basename validation, so
the persisted value is `assembly.FCStd`; all other row cells remain the values
read from the native sheet. Sandbox paths and object-store keys never enter BOM
JSON/CSV.

The required deterministic JSON artifact is:

```json
{
  "schema_version": "freecad-bom.v1",
  "source": {
    "candidate_build_id": "uuid",
    "base_revision_id": "uuid",
    "plan_hash": "64 lowercase hex characters",
    "combine": {
      "staging_manifest_id": "uuid",
      "artifact_role": "step",
      "filename": "combine.step",
      "sha256": "64 lowercase hex characters"
    },
    "components": [
      {
        "step_key": "part-01",
        "staging_manifest_id": "uuid",
        "artifact_role": "step",
        "filename": "part-01.step",
        "sha256": "64 lowercase hex characters"
      }
    ]
  },
  "generator": {
    "freecad_version": "1.1.3",
    "runtime_image_digest": "string matching ^sha256:[0-9a-f]{64}$",
    "native_type": "Assembly::BomObject"
  },
  "columns": ["Index", "Name", "Quantity", "File Name"],
  "rows": [
    {
      "index": "1",
      "name": "Bracket",
      "quantity": 1,
      "file_name": "assembly.FCStd",
      "properties": {}
    }
  ]
}
```

No clock value is embedded, so identical inputs produce identical BOM bytes.
`runtime_image_digest` is copied from
the verified request field and must equal both
`ExecutionSpec.runtime.image_digest` and the digest recorded in the successful
execution result's runtime provenance. Host-side output validation rejects any
runner output that changes it.
CSV is serialized from the same ordered columns/rows with RFC 4180 quoting.
The required `ExecutionSpec.outputs` roles are exactly `bom-json` with
`application/json`, `bom-csv` with `text/csv; charset=utf-8`, and
`capability-result` with `application/json`. Their physical basenames are
respectively `bom.json`, `bom.csv`, and `capability-result.json`; every
declaration has `required=true`. BOM JSON/CSV each have a 16 MiB declared cap;
capability metadata has 512 KiB. The overall execution retains the existing
180-second timeout, 1536 MiB memory, 2000 millicores, 512 PID, and 384 MiB
aggregate-output limits used by FreeCAD execution. The
execution stages the two BOM files under the existing candidate validation
prefix. The same `0011_freecad_fusion_acceptance.py` migration replaces the
`agent_validation_evidence.gate` check constraint so it also accepts `bom`; its
immutable evidence points at the source modeling manifest and contains the two
artifact declarations plus runtime provenance. For an assembly, `outcome`
must be `passed` before sealing.

`artifact_commit.py` materializes BOM evidence exactly like existing DFM and
visual evidence, producing immutable `artifacts` rows with kinds `bom_json`
and `bom_csv`. Their size/SHA/object keys remain in the existing tables and
object store. The candidate revision manifest adds:

```json
{
  "bom": {
    "status": "succeeded | not_applicable",
    "evidence_id": "uuid or null",
    "json_artifact_kind": "bom_json or null",
    "csv_artifact_kind": "bom_csv or null"
  }
}
```

New assembly revisions can only be `succeeded`; non-assembly revisions are
`not_applicable`. Failed/unsupported assembly BOM attempts fail the workflow
and remain visible through structured task errors; they cannot be sealed as a
successful empty BOM.

`GET /api/projects/{project_id}/revisions/{revision_id}/bom` is added to
`backend/app/api/revisions.py`. It uses the existing `VIEW_PROJECT`
authorization, resolves exactly one `bom_json` artifact, verifies stored size
and SHA-256, validates `freecad-bom.v1`, and returns the JSON above. It returns
404 `bom_not_found`, 409 `bom_not_applicable`, 409
`bom_artifact_ambiguous`, or 503 `bom_artifact_integrity_failed`. Error bodies
use `{"detail":{"code":"...","message":"...","retryable":false}}`.
The existing authenticated artifact URL supplies the CSV download.

`DurableTaskSnapshot.agent.bom` and revision detail expose metadata only:

```text
status: pending | running | succeeded | not_applicable | missing | failed |
        unsupported | cancelled
revision_id: UUID | null
evidence_id: UUID | null
json_download_url: str | null
csv_download_url: str | null
error: ExecutionError | null
```

The inspector fetches rows from the authorized revision BOM endpoint only
when `status='succeeded'`; it never receives rows embedded in a task event or
manufactures rows from `assembly_parts`.

The task projection does not publish `succeeded` immediately when the BOM
attempt finishes. Before candidate seal it remains `running`; only the
successful seal transaction can project `succeeded`, at which point
`revision_id` is the allocated reviewable candidate revision and both artifact
URLs resolve committed artifact rows. If BOM execution fails before seal, the
projection is `failed` or `unsupported` with `revision_id=null` and its
persisted error. This avoids a revision-only read path pointing at unsealed
candidate objects.

If BOM execution passed but the later seal does not complete, terminal
workflow state overrides the pre-seal `running` projection: workflow `failed`
maps to BOM `failed` using the terminal seal-step error or synthesized
`bom_seal_failed`; workflow `timed_out` maps to BOM `failed` with category
`timeout` and code `bom_seal_timed_out`; workflow `cancelled` maps to BOM
`cancelled` with `error=null`. All three keep `revision_id`, evidence ID, and
URLs null. A successful sealed candidate is the only transition to BOM
`succeeded`.

For a legacy revision whose manifest has no `bom` member, revision detail emits
`status='missing'`, its own `revision_id`, null evidence/URLs, and no error. A
new non-assembly revision emits `not_applicable`. An assembly attempt that
fails emits `failed` with its persisted error; missing Assembly support emits
`unsupported` with `bom_runtime_unsupported`. `pending` and `running` are task
states only and always carry the candidate revision ID when allocated.

### Frontend service and component state

`frontend/src/types/index.ts` adds `StructuredExecutionError`,
`DurableConfirmationProjection`, `DurableBOMProjection`, `FreeCADBOMDocument`,
and the fields defined above to `DurableTaskSnapshot`/`GenerationResult`.
`frontend/src/services/engineeringService.ts` adds only these network calls:

```text
confirmDurableTask(workflowRunId, accepted, note)
getRevisionBOM(projectId, revisionId)
```

Both use `authFetch`. `readJson` is extended to throw
`EngineeringServiceError extends Error` with exact fields
`status: number`, `code: string | null`, `retryable: boolean`, and
`details: Record<string, unknown>` while preserving its current localized
message. It decodes both FastAPI's nested `detail` envelope and the legacy
string detail. This is how `bom_not_found`, confirmation 409, and integrity
errors reach component state without message parsing.
`sessionStore.applyDurableSnapshot` copies the server confirmation, structured
error, parameters/state hash, and BOM metadata into the active durable panel.
It does not infer confirmation or BOM success from local messages.

`AgentPanel.tsx` keeps the local pre-submit review card and adds a visually
separate server-plan confirmation card keyed by workflow ID. Its only local
state is `idle | submitting_accept | submitting_reject | error`; the displayed
reason/objects always come from `panel.durable.confirmation`. A successful HTTP
response does not optimistically change the durable task status.

The same panel renders structured execution failures from the snapshot. It
maps `sketch_redundant_constraints`, `sketch_conflicting_constraints`,
`sketch_under_constrained`, and `sketch_solver_failed` to localized labels and
shows operation/action plus available solver status/index details. Selection,
parameter, and BOM errors use their code-specific labels. Display logic never
regex-parses `message`.

`WorkspaceInspector.tsx` extends `InspectorTab` to include `bom` and adds the
`["bom", "BOM"]` tab. It receives project ID, revision ID, and
`DurableBOMProjection`; on those identities changing it cancels the prior read,
clears prior rows, and fetches `getRevisionBOM` only for succeeded metadata.
Its render states are exactly `loading`, `succeeded`, `not_applicable`,
`missing`, `unsupported`, `failed`, `cancelled`, and `stale_revision`. Projection
`pending|running` maps to `loading`; projection `missing` maps to `missing`.
`stale_revision` applies only when both IDs are non-null and
`bom.revision_id !== inspectedRevisionId`; a null pre-seal revision ID never
becomes stale. `EngineeringWorkspace` computes `inspectedRevisionId` as the
reviewable `result.revision_id` when present, otherwise the panel's committed
`currentRevisionId`, and passes it explicitly with `projectId`. Every request
carries a monotonically increasing local read token; an
aborted or late response whose token/identity is no longer current is dropped
without mutating rows or status. `succeeded` requires `rows.length > 0`;
otherwise it renders `bom_empty` as `failed`. JSON/CSV download actions use the
persisted URLs and `downloadEngineeringArtifact`.

`frontend/qa/engineering-workspace.tsx` remains a direct mount of the same
production `EngineeringWorkspace`, store, styles, i18n provider, and error
boundary. No fixtures, alternate BOM rows, alternate confirmation state, or
QA-only production import is added.

### Persistence and ownership matrix

No frontend store or process-local cache becomes authoritative. The exact
write/read ownership is:

| Data | Authoritative storage | Writer | Reader/projection |
|---|---|---|---|
| operation/backend-policy/source resolution | `workflow_runs.request_payload.operation_context` JSONB, covered by existing `request_payload_hash` | durable submission boundary before Temporal start | workflow request and task snapshot |
| concrete generate backend | `AgentPlan.modeling_backend` in `agent.plan.completed` and the candidate's canonical plan/hash | durable planner before candidate allocation | Agent V2 execution branches and task projection |
| structured parameter update request | `workflow_runs.request_payload.structured_modification` JSONB | durable submission boundary | Agent V2 workflow and FreeCAD compiler |
| generated CadQuery source | existing `agent_generated_sources.source_code/source_hash`, linked through accepted/sealed manifests | existing source-generation activity | trusted browser modify resolver |
| FreeCAD state and parameters | staging declaration in `agent_staging_manifests.manifest.outputs`; after seal, immutable object-store object plus `artifacts` row with `artifact_kind='state'` | FreeCAD execution and existing candidate seal | revision/task projector after size/hash/schema verification |
| server confirmation plan | existing `workflow_events` `agent.plan.completed` payload plus `workflow_runs.status`; no new confirmation table | planning activity and workflow transition | `DurableTaskSnapshot.confirmation` |
| structured execution error | scalar compatibility columns plus new `execution_attempts.error_details` and `step_runs.error_details` JSONB | transition helpers at the trusted backend boundary | task snapshot/event projector |
| BOM execution output before seal | candidate validation object keys declared in `agent_validation_evidence.evidence`, with `gate='bom'` and immutable `evidence_hash` | BOM activity | candidate seal verifier |
| committed BOM | immutable object-store objects plus `artifacts` rows `bom_json`/`bom_csv`; status/evidence linkage in `project_revisions.manifest.bom` | existing candidate seal transaction | revision detail and authorized BOM endpoint |
| runtime identity | repository `backend/sandbox/runtime-lock.json`, immutable image digest in `ExecutionSpec`, and result runtime provenance | image build and execution backend | probe, attempt result, BOM provenance |
| frontend in-flight state | React/Zustand memory only | component/service | presentation only; discarded on snapshot/revision identity change |

The migration adds only the two error JSONB columns and the existing evidence
gate constraint change. It does not create parameter, confirmation, BOM-row,
or runtime tables. Object bytes are never stored in task events or frontend
state, and committed objects are never mutated in place.

## Runtime packaging and provenance

`backend/sandbox/Dockerfile` replaces the Debian
`freecad-python3=0.20.2+dfsg1-4` installation with a `freecad_runtime` build
stage. `TARGETARCH=amd64|arm64` selects the matching official FreeCAD 1.1.3
Linux AppImage and its exact official SHA-256 from an in-file architecture
map. The build downloads, verifies, and extracts the AppImage into
`/opt/freecad` without FUSE. Unsupported architecture, absent checksum,
checksum mismatch, missing command, or missing `Assembly` import fails the
image build.

`backend/sandbox/runtime-lock.json.freecad` becomes:

```json
{
  "version": "1.1.3",
  "distribution": "official-appimage",
  "asset_by_platform": {
    "linux/amd64": {
      "filename": "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage",
      "sha256": "3a853eb69ee595f779f2255dbf80a765926981d8ff68903cefee4dfb03a8f5ef"
    },
    "linux/arm64": {
      "filename": "FreeCAD_1.1.3-Linux-aarch64-py311.AppImage",
      "sha256": "9a8f9f7f2802bb856f2bb70f53d536e2ae06569f4e6d718407803076104ff55e"
    }
  },
  "command": "/opt/freecad/bin/FreeCADCmd",
  "required_modules": ["Part", "PartDesign", "Sketcher", "Import", "Assembly", "Spreadsheet"]
}
```

The image extraction step resolves the included `FreeCADCmd` binary and creates
the fixed symlink `/opt/freecad/bin/FreeCADCmd`. The build executes the symlink
before finalizing the layer. `backend/tests/test_runtime_manifest.py`
rejects missing values, non-SHA placeholders, and any remaining Debian 0.20.2
dependency.

`capability_entry.py`, `runtime_probe.py`, and
`PodmanExecutionBackend.inspect_container_runtime` read the one absolute
command from the installed lock and use the extracted runtime's own Python
environment. They do not combine AppImage modules with product Python or
Debian FreeCAD bindings. Existing read-only root-filesystem, unprivileged-user,
declared-input, declared-output, timeout, and execution-attempt controls remain
in force.

Before cutover, the full existing real-FreeCAD suite must pass against 1.1.3,
including FCStd create/save/reopen/modify, transaction rollback, idempotent
operation replay, topology resolution, revision fencing, STEP/STL export, and
the previously verified hole/chamfer modification.

## Request classification and API compatibility

### Browser natural-language modification

The WebSocket submission boundary determines the operation from the explicit
message action and durable panel context, not from localized keyword matching.
Precedence is deterministic: explicit `modify_part` is `modify`; an explicit
new-conversation/generate action is `generate`; a generic conversational
message in a panel whose active identity resolves to an editable committed or
reviewable CAD revision is `modify`; and a generic message in an empty/new
panel is `generate`. Creating another part therefore uses the existing new
conversation action rather than overloading a modification thread.

The selected operation, classifier rule ID, panel ID, backend policy, base
revision ID, and resolved source kind/ID/hash are persisted in the workflow
request. A modify request must fail before execution when it cannot resolve an
eligible base revision and unique trusted FCStd or persisted CadQuery source;
it must never silently regenerate a replacement part.

### REST `/api/modify`

`code` becomes optional at the public request model. It remains required only
for the legacy-compatible CadQuery/code modification strategy. The FreeCAD
strategy resolves the base committed revision and FCStd through the existing
artifact loader and accepts structured instructions/typed operations without
source code. Requests that supply neither a usable FreeCAD base nor required
code return a specific validation error rather than a generic execution
failure.

Existing clients that still submit `code` remain compatible and preserve
their current behavior.

## Durable server confirmation in the UI

The task snapshot is the source of truth for `waiting_confirmation`. It
contains the workflow run ID, confirmation reason, planned affected objects,
and canonical plan hash. Whether a response took effect is represented only by
the newer persisted workflow status; there is no separate browser or snapshot
boolean.

When that state is active, the Agent panel renders a server-confirmation card
distinct from the existing local pre-submit confirmation. Accept and reject
call `POST /api/tasks/{workflow_run_id}/confirmation` with the existing
contract. Buttons enter a pending state, prevent duplicate submission, surface
real API errors, and disappear only after a newer durable snapshot/event shows
that the workflow moved on. Reconnect and page reload reconstruct the card
from the snapshot; local state cannot manufacture or clear server
confirmation.

## Fail-closed typed FreeCAD boundary

`FeatureFilletArgs.use_all_edges` and
`FeatureChamferArgs.use_all_edges` in `backend/app/freecad/contracts.py` change
from default `true` to optional with default `null`. The model validator and a
separate runner validator in `backend/sandbox/freecad_entry.py::_validate_plan`
apply the same matrix before any document is opened:

- `use_all_edges: true` forbids a selector and intentionally selects all
  eligible edges;
- `use_all_edges: false` requires a valid selector;
- omitting `use_all_edges` is permitted only when a valid selector is present;
- contradictory or incomplete combinations fail with a structured validation
  error `invalid_edge_selection_mode` before a document transaction starts.

`_feature_fillet` and `_feature_chamfer` set `UseAllEdges` from the validated
boolean, never from `selector is None`. No invalid or missing selector is
converted into all-edge behavior. The runner continues to abort the document
transaction on any failed operation and never publishes a partially modified
FCStd.

## Stable structured sketch status and error transport

Runner errors retain a versioned envelope across `freecad_entry.py`, the
capability dispatcher, `ExecutionBackend`, workflow events, API snapshots, and
frontend presentation. The envelope contains at least:

- stable error `code`;
- human-readable `message`;
- `operation_id` and `action` when applicable;
- structured `details`;
- retryability/classification assigned by the trusted backend boundary.

Sketch recompute/solve results additionally contain a stable constraint
status: `fully_constrained`, `under_constrained`, `redundant`, `conflicting`,
or `invalid`. Redundant and conflicting constraints use dedicated error codes
and include solver counts/indexes when FreeCAD exposes them. Text may vary by
locale or FreeCAD version; control flow and frontend state never depend on
parsing that text. When multiple solver conditions are reported, precedence is
`invalid` > `conflicting` > `redundant` > `under_constrained` >
`fully_constrained`; all raw structured flags remain in `details`.

Unknown, malformed, or non-JSON sandbox failures are still represented as a
generic sandbox error, without overwriting a valid structured FreeCAD error.

## Real runtime probe

`backend/sandbox/runtime_probe.py` emits `mcad-runtime-probe.v2`:

```json
{
  "schema_version": "mcad-runtime-probe.v2",
  "runtime_lock_sha256": "64 lowercase hex characters",
  "versions": {"freecad": "1.1.3"},
  "checks": [
    {"id": "freecad.create_save_reopen_modify", "status": "passed", "evidence": "typed object defined below"},
    {"id": "freecad.transaction_abort", "status": "passed", "evidence": "typed object defined below"},
    {"id": "freecad.sketch_solve", "status": "passed", "evidence": "typed object defined below"},
    {"id": "freecad.shape_check", "status": "passed", "evidence": "typed object defined below"},
    {"id": "freecad.export_step_stl", "status": "passed", "evidence": "typed object defined below"},
    {"id": "freecad.assembly_bom", "status": "passed", "evidence": "typed object defined below"}
  ],
  "verified_operations": [
    "freecad.create_save_reopen_modify",
    "freecad.transaction_abort",
    "freecad.sketch_solve",
    "freecad.shape_check",
    "freecad.export_step_stl",
    "freecad.assembly_bom"
  ]
}
```

In the real JSON, every `evidence` is an object, never the explanatory strings
shown in the shape above. Required fields are exact:

```text
freecad.create_save_reopen_modify:
  object_count_before, object_count_after: int >= 1
  property: str
  before_value, after_value: finite number and unequal
  fcstd_size_bytes: int > 0
  fcstd_sha256: lowercase SHA-256
freecad.transaction_abort:
  object_count_before, object_count_after: equal int >= 1
  aborted_object_name: str absent from the document after abort
freecad.sketch_solve:
  solver_status: 0
  fully_constrained: true
  constraint_count: int > 0
freecad.shape_check:
  shape_valid: true
  shape_error_count: 0
  solid_count: int > 0
freecad.export_step_stl:
  step_size_bytes, stl_size_bytes: int > 0
  step_sha256, stl_sha256: lowercase SHA-256
  reopened_step_solid_count: int > 0
freecad.assembly_bom:
  native_type: "Assembly::BomObject"
  columns: ["Index", "Name", "Quantity", "File Name"]
  row_count: int > 0
  total_quantity: int > 0
```

The probe invokes `/opt/freecad/bin/FreeCADCmd` for one isolated probe script.
That script creates a document, adds a fully constrained sketch and solid,
saves/reopens it, changes a property and verifies the value, aborts a deliberate
failed transaction and verifies no leaked object, calls `Shape.check(True)`,
exports and reopens STEP/STL, imports `Assembly`, creates
`Assembly::BomObject`, recomputes it, and verifies at least one native row.

`verified_operations` is constructed in memory only from check records whose
status is `passed`; it is not copied from `runtime-lock.json`. A failed or
missing required check makes the process non-zero and omits the final success
document. Each evidence object contains measured values such as object count,
shape error count, modified property value, output byte sizes/hashes, and BOM
row count—not a boolean copied from configuration.

## FreeCAD parameter projection and editing

The existing state projector produces a normalized parameter collection from
editable FreeCAD properties. Each parameter has a stable object/property ID,
label, current value, value type, unit where applicable, editability, and
bounds/enumeration when reliably available. Internal, read-only, transient,
and unsupported properties are excluded.

The projected state and parameter collection are stored as accepted output
from the same execution attempt as the FCStd, selected during candidate seal,
and associated with the resulting revision. Task snapshots and compatibility
responses expose those persisted parameters. The frontend adapter consumes
that contract, so a restored committed revision shows the same parameters
without rerunning or inferring geometry.

Editing a projected parameter submits its stable ID and typed value through
the real FreeCAD modify workflow. The backend verifies the base revision,
property type, allowed range, revision fence, and editable FCStd before
compiling a `property.set` operation. Successful edits produce a new candidate,
execution attempt, FCStd/state artifacts, validation evidence, and Change Set.
The frontend never patches generated source code for a FreeCAD parameter.

## Real Assembly BOM and frontend display

### Capability

The normative BOM execution and storage contract above applies. New assembly
workflows treat native BOM as a required gate; non-assembly revisions record
`not_applicable`. Existing revisions with no BOM artifact report `bom_not_found`
rather than being upgraded or populated by the API. No row is synthesized in
FastAPI or React, and an empty success is forbidden.

### Inspector UI

The existing workspace inspector gains a `BOM` tab without changing the
overall workspace layout. The tab is revision-aware and displays:

- source/revision identity and BOM generation status;
- real native column names and rows;
- component label/path, quantity, and available properties;
- JSON/CSV artifact download actions using existing authenticated artifact
  URLs;
- explicit loading, not-applicable, legacy-missing, unsupported, failed,
  cancelled, and stale-revision states.

The tab does not show sample rows or a successful zero-row table. Switching
history revisions reloads the matching persisted BOM. Mobile behavior follows
the current inspector/preview overlay rules and does not create another
navigation system.

## Browser WebGL QA boundary

The QA invocation for Chromium revision 1223 uses exactly
`--headless --use-angle=swiftshader --enable-unsafe-swiftshader`; it does not
use `--disable-gpu`. Before testing the product flow, browser JavaScript creates
a canvas, obtains `webgl2 || webgl`, and records `VERSION`, `RENDERER`, and
`UNMASKED_RENDERER_WEBGL` when available. A null context is a failed QA gate.

The same session then loads the production workspace harness, restores a real
committed STL, asserts the React Three Fiber canvas has non-zero dimensions and
no “无法创建 3D 画布” state, and performs pointer drag plus wheel input without a
console/page error. The QA evidence records Chromium revision, launch flags,
renderer strings, page URL, model revision/artifact hash, console errors, and
screenshots. `frontend/qa/engineering-workspace.tsx` stays a direct production
mount; production browser flags and `Viewer3D` behavior are unchanged.

## Ordering and rollback

Implementation proceeds in dependency order:

1. package and prove FreeCAD 1.1.3 while retaining a rebuildable reference to
   the old image tag outside production routing;
2. harden runner validation, structured errors, and the real runtime probe;
3. fix modify classification/API and server confirmation;
4. persist/project/edit FreeCAD parameters;
5. add native BOM artifacts and the inspector tab;
6. fix the QA-only WebGL launcher and run complete regression.

The production image/configuration changes only after the new immutable image
passes the release gates. Rollback means selecting the previously known image,
not dynamically routing individual jobs across two FreeCAD versions.

## Compatibility constraints

- Do not change existing authentication, tenancy, object-store ownership,
  revision comparison-and-swap, Change Set review, or Temporal replay rules.
- Do not bypass `ExecutionBackend` for FreeCAD, BOM, validation, or exports.
- Do not mutate committed artifacts in place.
- Preserve existing CadQuery/code requests and non-FreeCAD capabilities.
- Do not alter production UI layout beyond the confirmation state, real
  parameter data/actions, structured errors, and the new inspector BOM tab.
- QA harness changes must not be imported into or bundled with production
  frontend code.

## Acceptance criteria

All of the following must be demonstrated with actual execution:

1. Browser natural-language modification downloads the committed FCStd and
   reaches the FreeCAD modify step, not generate.
2. A workflow paused in `waiting_confirmation` can be accepted and rejected
   from the browser, including after reload/reconnect.
3. `/api/modify` performs a real FreeCAD modification without `code`, while
   code-based clients remain compatible.
4. Invalid fillet/chamfer selection inputs fail closed and leave the FCStd
   unchanged.
5. Redundant/conflicting sketch constraints arrive at the frontend as stable
   structured statuses/codes.
6. The runtime probe proves every operation it declares and fails when a
   required FreeCAD operation or Assembly module is unavailable.
7. Generated and restored FreeCAD revisions show non-zero real editable
   parameters, and a browser parameter edit creates a valid new revision.
8. FreeCAD 1.1.3 produces a non-empty native BOM for a real assembly; the
   matching browser inspector tab shows those persisted rows and downloads the
   real JSON/CSV artifacts.
9. Headless browser QA creates a real WebGL context through SwiftShader and
   exercises the 3D canvas without production-code changes.

The full backend, frontend, real FreeCAD, Fusion Connector, browser responsive
and core interaction suites pass, followed by ESLint, TypeScript, production
build, Python compile checks, and `git diff --check`. Any missing external
module, unavailable real dependency, skipped required scenario, or
environment-only limitation is reported explicitly and is not counted as a
pass.

## Out of scope

- Maintaining two production FreeCAD versions.
- Replacing Temporal, the artifact/revision model, or the current workspace
  shell.
- A new general-purpose assembly authoring UI.
- Cloud deployment, GitHub operations, or changes to
  `isearch-ai-pr-platform`.
