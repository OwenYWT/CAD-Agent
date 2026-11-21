# Durable CadQuery Static Analysis Design

## Problem

Durable Agent V2 executes generated CadQuery source without applying the static
analysis used by the legacy state machine. In task
`f7e4c53f-51fc-4d21-830f-d5c8d900c774`, a workplane derived from the enclosure
body performed `cutBlind()`, then the returned body was subtracted from the
original body a second time. The code executed without a kernel exception but
reduced the body to a thin difference, so final assembly validation received a
`90 x 83.5 x 3 mm` plate instead of a `90 x 60 x 35 mm` enclosure.

## Approved Design

Add an AST-based analyzer rule that tracks assignments derived from an existing
body. If a body-derived value has already applied an in-place subtractive
CadQuery operation such as `cutBlind`, `cutThruAll`, or `hole`, reject any later
attempt to subtract that value from the same body with `-` or `.cut()`.

Run the analyzer in the Durable `agent_execute_model` handler after source hash
verification and before starting the Podman backend. A violation becomes a
structured `static_analysis_failed` validation failure, allowing the existing
bounded source-repair loop to correct the source. The failure must be persisted
through the existing execution-attempt error path.

Add a focused generation/repair instruction explaining that subtractive
operations on a body-derived workplane already return the updated body and must
be assigned directly, rather than subtracted from the old body again.

## Boundaries

- Do not weaken final assembly or engineering-acceptance validation.
- Do not rewrite the deterministic assembly combiner.
- Do not reject independent cutter solids created from a fresh `cq.Workplane`.
- Do not rebuild or download the FreeCAD sandbox image; this is host-side code.
- Preserve all unrelated worktree changes.

## Verification

Tests must first demonstrate that the known bad source passes the current
analyzer. After implementation, the bad pattern must produce
`static_analysis_failed`, while direct assignment of `cutBlind()` and subtraction
of an independent cutter remain accepted. Durable workflow tests must prove that
the structured failure enters the existing repair path.
