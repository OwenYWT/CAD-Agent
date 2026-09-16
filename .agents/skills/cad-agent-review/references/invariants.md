# CAD-Agent Review Invariants

Treat these as review gates. If a change appears to violate one, report it as a high-severity concern unless the current code and tests prove an intentional, documented replacement.

## Identity and state

1. Candidate, local draft, and committed revision are different identities.
2. Accepting a candidate is not the same operation as committing it.
3. Rejecting the first candidate must not create a saved model.
4. Durable task snapshots and persisted events are authoritative; local UI state is a projection.
5. A late event, old task, stale revision, or old workflow response cannot replace the current Head.
6. An ABA return to the same revision does not make an old generation valid again.

## Mutation safety

7. Mutating requests validate project, branch, expected base revision, expected state version, permissions, and operation context.
8. Mutating requests are idempotent and do not duplicate work after an acknowledgement loss.
9. Feature edits use the correct feature identity and edit lease; stale leases are not silently reused.
10. Retry preserves the original persisted objective, selected object, evidence basis, and baseline; it does not invent a new objective.
11. Cancellation, timeout, provider failure, or validation failure preserves the last valid model and records a readable terminal result.

## Evidence and provenance

12. An artifact is usable only when its ownership, source revision, size, and SHA-256 are verified.
13. Engineering evidence is bound to the exact source revision and cannot be reused from another revision.
14. Intermediate evidence cannot be presented as final candidate evidence.
15. A release package does not move the design Head and contains an auditable manifest.
16. A frontend success state must correspond to a real server result, persisted record, or verified artifact.

## Access control

17. Knowing a document ID does not grant access.
18. Revocation affects read, execute, review, commit, download, and delivery paths as applicable.
19. Tenant and project boundaries are enforced server-side, not only by the UI.
20. Reviewer/editor/owner/admin roles are not interchangeable.

## Reality claims

21. An API route, capability manifest, adapter, facade, fixture, or mock is not proof of a product feature.
22. Contract-only and simulated Fusion tests cannot be reported as real Fusion desktop acceptance.
23. Optional provider/device capabilities remain optional or blocked until dependencies and real evidence exist.
24. Geometry generation, geometry checks, file save, physical fit, strength, and manufacturing validation are distinct claims.
25. A test marked skipped, conditional, mocked, or fixture-only is not a passing real-environment test.