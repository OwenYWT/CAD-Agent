# CAD-Agent Feature Logic Baseline

This file is the review-oriented interpretation of `docs/functional-inventory-2026-09-14.md`. It does not replace the inventory. When source code and this baseline disagree, inspect the current source and update this file only when the behavior is supported by code and tests.

Each domain lists the invariant that must remain true when a related change is reviewed. The matching implementation and test paths are maintained in `feature-test-map.json`.

## 01 — Account and login

Password, invitation, session refresh, logout, account deletion, administrator invitation management, optional SMS verification, and deployment-specific authentication must share one server-side identity boundary. A browser token or local auth state is not proof of authorization; expired or revoked sessions must fail closed.

## 02 — Projects, sessions, and threads

Projects, conversations, model snapshots, task context, and history restore must preserve their relationships. Closing or deleting a visible thread must not silently claim physical deletion of every stored artifact, and restoring history must not replace newer durable state with stale browser state.

## 03 — Workspace and layout

The four-area workspace, collapsible panels, responsive intermediate layout, inspector, tabs, and empty states must preserve loaded model/view content. Layout changes must not detach the selected document, task stream, or active conversation from its source of truth.

## 04 — Requirements and engineering basis

Requirement intake, clarification, design brief, manufacturing profile, and confirmation gates must retain the engineering basis used by downstream planning and execution. A UI confirmation is not sufficient if the durable submission omits the confirmed basis or its revision identity.

## 05 — Agent modeling collaboration

Planner, executor, repair loop, progress stream, candidate generation, and human-in-the-loop collaboration must be durable and resumable. Plans and repairs must operate on the expected document/revision context, and a successful workflow must produce inspectable evidence rather than only a success message.

## 06 — Task state and recovery

Persisted workflow state is authoritative over local UI state. Replay, snapshots, retries, cancellation, timeout, terminal events, and continuation recovery must be monotonic for the current task and must reject stale task/revision/ABA events.

## 07 — CAD generation and native modeling

Natural-language or structured operations must resolve to native FreeCAD/CadQuery-compatible operations, update the projected model state, and preserve checkpoint/artifact provenance. A contract, operation list, or mock runtime is not equivalent to real native geometry execution.

## 08 — Features, properties, and manual edits

Feature-tree inspection, parameter validation, manual edits, leases, annotations, and operation resolution must target the viewed revision and respect ownership/concurrency rules. Invalid or stale edits must not mutate the current model or report a false success.

## 09 — Sketch and dimension editing

Sketch geometry, constraints, dimensions, preview, and edit submission must preserve units, constraint semantics, native selection identity, and source revision. Preview-only geometry must not be presented as committed model state.

## 10 — Model view, selection, and scenes

3D/2D scene generation, camera/view state, selection, visual depth, and document geometry must be bound to the requested revision and tenant. Scene or selection data from another revision/document must not be reused merely because the browser has the same object IDs.

## 11 — Parts and assembly

Part boundaries, instances, transforms, mates, assembly hierarchy, and assembly-level selection must remain consistent between native model state, scene output, and persisted document metadata. Partial or placeholder assembly responses must be labeled as such and must not look like a committed assembly.

## 12 — Inspection, validation, and evidence

Geometry checks, measurements, visual inspection, validation stages, and evidence summaries must identify the exact source revision and artifact. A green check is meaningful only when the underlying calculation or native/runtime evidence was actually produced and is retrievable.

## 13 — DFM knowledge and standard parts

DFM rules, thresholds, knowledge sets, materials, process recommendations, suppliers, and standard-part lookup must distinguish built-in knowledge from tenant customization. Recommendations and summaries are advisory unless the inventory explicitly identifies a real manufacturing or procurement integration.

## 14 — Candidate review and commit

Generated candidates, local drafts, review decisions, requested changes, acceptance, rejection, and commit are distinct states. Only an authorized commit of the expected candidate/revision may advance the document Head; rejection or failed commit must preserve the last valid model.

## 15 — History, rollback, branches, and merge

History views, diffs, rollback, branch creation, and merge must retain immutable revision identity and generation. Rollback creates a new current state or candidate according to the contract; it must not rewrite history or allow an old branch to overwrite a newer Head without conflict checks.

## 16 — Multi-user collaboration and permissions

Tenant isolation, project membership, document sharing, co-editing, leases, revocation, and role checks must be enforced server-side at every read and mutation boundary. Revocation and stale leases must stop writes even when a client still holds an old session or document view.

## 17 — FEA static analysis

FEA inputs, mesh/material/constraint settings, solver execution, result fields, and engineering evidence must be tied to one source revision and preserve failure/timeout semantics. A contract or mocked field viewer is not real solver acceptance.

## 18 — CAM contour milling

CAM setup, stock/tool/parameter validation, contour toolpath generation, preview, and export must preserve source revision and manufacturing parameters. A displayed path or downloadable file is not proof of executable toolpath generation unless the relevant runtime evidence exists.

## 19 — Files, export, BOM, and releases

Downloads, native/source files, BOMs, exports, release records, and release history must bind to the selected revision and artifact digest. Export success must correspond to a real file with expected ownership, size, checksum, and failure behavior.

## 20 — Local Bridge delivery

Bridge registration, capability negotiation, delivery, progress, retry, cancellation, local path handling, and failure recovery must preserve delivery identity and prevent unauthorized or stale artifacts from being written locally. A cloud-side queued record is not proof of local delivery.

## 21 — CAD Skills capability catalog and runner

Capability discovery, input schema, authorization, dispatch, execution backend selection, artifact publication, and result reporting must share one capability contract. Catalog metadata or a test facade must not be reported as successful execution.

## 22 — External CAD connectors

Onshape, Fusion 360, add-in, OAuth/token, webhook, protocol, and runtime integrations must clearly separate contract tests from real provider acceptance. External identifiers, tenant ownership, retries, idempotency, and unavailable-provider behavior must be explicit.

## 23 — Interfaces and tool extensions

REST endpoints, WebSocket events, batch APIs, feedback, agent tools, capability actions, and compatibility shims must remain registered, authorized, version-compatible, and consumed by clients. A backend route without persistence/consumer coverage or a client type without a matching server contract is incomplete.

## 24 — Reliability, deployment, and operations

Startup/readiness, configuration, workflow dispatch, Temporal, execution backend wiring, persistence, deployment packaging, WebSocket health, and operational recovery must agree across local and deployed environments. Passing unit tests alone does not establish deployment or external-service readiness.

## Review use

When a change touches multiple domains, review the direct domain first and then every domain that consumes its contract, revision identity, task state, artifact, permission, or event. Record the selected domain IDs and the exact implementation/test paths in the dated review report.
