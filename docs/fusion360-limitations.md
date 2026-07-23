# Fusion 360 Connector capabilities, limitations, and next phase

## Implemented

- Stable `CadAdapter` and strict JSON/Pydantic schemas with generated, versioned JSON Schema.
- Default Fusion Desktop Add-in/Palette -> authenticated HTTPS Cloud Agent path with bounded context,
  one typed Action per plan, server-owned identifiers/risk, native no-write preview, stale-context
  protection, one-time approval, structured execution report, and optional consent-bound artifact upload.
- Local Palette assets use a restrictive CSP and text-only rendering; bearer credentials and `adsk`
  objects never enter JavaScript. The network worker never calls Fusion APIs other than signaling a
  main-thread `CustomEvent` through its injected callback.
- Cloud Agent plan/result audit is SQLite-backed and idempotent without persisting prompt, full context,
  complete result, internal snapshots, local paths, or bearer tokens. F3D upload requires separate consent.
- Direct Connector heartbeats provide owner-scoped online/Fusion-positive status; verified mutation reports
  are durably queued in the local journal and replayed by identical report ID after restart, never by
  replaying the CAD mutation. Uploaded exports have authenticated Web download URLs.
- Authenticated Local Runtime with separate Backend/Connector credentials, owner isolation, SQLite tasks, phase-aware idempotency, one-time approval, lease fencing, cancellation, timeout, heartbeat, reconnect, artifact confinement, and structured errors.
- Fusion Add-in lifecycle, stdlib polling transport, main-thread CustomEvent dispatcher, crash-safe mutation journal, no arbitrary code/reflection execution.
- Context for application/document/design, component/occurrence/selection, parameter/unit/material/mass, sketch/feature/body/assembly, and DataFile cloud state.
- Direct Agent turns redact the local Fusion user name before HTTPS upload while preserving the locally
  available application context and the deterministic stale-context fingerprint.
- Real Fusion mappings for parameter/feature parameter; basic Sketch; Extrude/Hole/Fillet/Chamfer; name/part number/description/material; save/saveAs; STEP/STL/DXF/F3D/PNG.
- `computeAll`, Feature health comparison, target re-resolution, value/property checks, file signature/size/hash, before/after Diff, semantic compensation, and honest indeterminate state.
- Optional APS OAuth v2/PKCE, encrypted owner tokens, and read-only Hub/Project/Folder/Item/Version traversal.

## Known limitations

- Fusion's Python API is in-process and main-thread constrained. Long operations can make Fusion temporarily unresponsive; cancellation is cooperative between safe stages, not thread termination.
- As of 2026-07-17, the delivery evidence contains no live Fusion Desktop run. Pure-Python contract/integration/facade tests pass, but the manual/current-version E2E checklist remains required before production rollout.
- Direct Agent authentication currently uses the existing CAD Agent login/API credential supplied through
  an owner-only file or environment variable. Automated device enrollment, OS Keychain/Credential Manager
  integration, short-lived token rotation, signed Add-in packaging, and certificate pinning are not yet
  implemented; system TLS and normal CA validation are used.
- `ExportManager.createDXFSketchExportOptions` is still officially Preview and Autodesk says distributed programs must not depend on preview capability. The connector defaults to retired-but-still-callable `Sketch.saveAsDXF` and reports this limitation. It will switch once Autodesk releases the replacement.
- Fusion configuration inspection APIs are still officially Preview. The production Connector does not call `DataFile.isConfiguration`; it only enforces the released `DataFile.isReadOnly` signal, so configuration-specific diagnosis is not declared as a capability.
- Fusion save returns when cloud processing starts. `local_save_accepted=true` is distinct from `cloud_version_processing=pending|complete`; callers needing a completed cloud version must poll/verify.
- Entity tokens are stable lookup handles but their literal strings can change and must not be compared for identity. The connector always re-resolves them.
- Only one connector is selected implicitly. With multiple online desktop sessions, callers must supply `connector_instance_id`.
- `fusion_running=true` is positive evidence from an online in-process Add-in. When the Connector is offline, the status is `fusion_running=null`, not `false`, because released Fusion APIs cannot distinguish a stopped process from a running Fusion session whose Add-in is disabled.
- Mutations provide at-most-once safety, not distributed exactly-once. A durable `started` entry without a completed result becomes `indeterminate` and is never automatically replayed.
- Save/saveAs cannot be rolled back. Parameter/property changes and newly created Sketch/Feature objects have best-effort compensation, whose success is recomputed and verified.
- Optional Runtime is intentionally same-host/loopback. Remote Runtime deployment and public Runtime
  exposure are unsupported; remote operation uses the direct HTTPS Agent contract instead.
- APS phase one is read-only `data:read`; there is no CAD upload, Data Management write, or Automation submission endpoint. Fusion Automation (GA since May 2025) is not a substitute for the live desktop/UI session and is deferred.
- APS collection endpoints currently return one official Data Management page per call; automatic traversal of pagination links is deferred, so large Hubs/Projects/Folders must be paged by a future contract revision.
- Material assignment depends on libraries/material IDs available in the signed-in Fusion installation.
- Context traversal is bounded (5,000 entities, requested depth); responses set `truncated` and warnings rather than silently returning an unbounded object graph.

## Next phase

1. Run and record the Windows/macOS real-Fusion acceptance matrix on current production builds, then add certified version ranges to capability negotiation.
2. Replace legacy DXF export when Autodesk releases the DXF Sketch ExportManager API.
3. Add a Fusion-controlled E2E harness/palette for fixtures, screenshots, and journal fault injection without weakening production policy.
4. Add richer safe feature edit adapters (pattern, shell, draft, joint) as new discriminated actions without changing business code.
5. Evaluate APS Automation only for explicit unattended cloud jobs, with separate schemas, cost/license controls, appbundle versioning, and user authorization.
6. Add signed connector packages, Windows code-signing/notarization guidance, secret rotation, and operational metrics for larger deployments.
7. Add managed device enrollment and OS-native secret storage so operators do not need a long-lived token file.

Official facts and citations are in [fusion360-research.md](fusion360-research.md); system boundary and failure semantics are in [fusion360-architecture.md](fusion360-architecture.md).
