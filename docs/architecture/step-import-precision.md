# STEP import precision normalization

The STEP reader can return a valid solid whose BOP check reports
`BOPAlgo_InvalidCurveOnSurface`. OCCT's transfer/healing estimates edge precision
with samples; the BOP analyzer computes the maximum distance between the 3D edge
curve and the curve on its supporting surface. On curved intersection edges,
the sampled estimate can be too small. Re-running general healing does not
necessarily fix that discrepancy. Refinement/reprojection can change geometry
and is deliberately excluded.

`step_import_normalization.py` applies only at the explicit STEP import boundary.
It measures the serialized native BREP using the existing isolated OCP runtime,
binds the measurement to its SHA-256 and edge indices, then corrects the affected
edge and incident vertex precision metadata in a FreeCAD copy. It never changes
3D curves, surfaces, topology, source files, or design acceptance tolerances.
Normal imports and all ordinary modeling operations retain their existing checks.

The automatic policy is intentionally conservative: each corrected edge must
remain within 0.001 mm, and within twice its original precision or 0.00001 mm,
whichever is larger. An additional 0.00000001 mm serialization margin is included
inside those bounds. This is a limit on repairing representation metadata, not a
claim about the manufacturing accuracy of an imported model. Larger discrepancies,
other BOP errors, diagnostic failures, or changed volume/area/bounds/topology fail
the import with an actionable error. They must be repaired in the source CAD.

Full FreeCAD BOP checks run after correction and again on an independently opened
saved FCStd. The source digest, per-edge measurements and limits, geometry facts,
and readback result are persisted in the execution receipt/staging manifest and
existing artifact store. Candidate review and commit remain separate; failure
cannot advance the saved document head. No model-provider call is involved.

STEP transfer reconstructs precision metadata; a plain external STEP reader may
reproduce the defect when reopening a derivative. The native FCStd preserves the
corrected records. Re-importing the derivative through CAD-Agent repeats the same
bounded checks; the exporter does not inflate global STEP precision to conceal
the underlying reader behavior.

Regression evidence uses a real torus/box boolean and STEP roundtrip, not a
customer file. It checks FCStd readback, repeated STEP import, excessive precision
rejection, immutable source bytes, preserved ordinary modeling validation, and
the authenticated API → Temporal → Model Job → FreeCAD → independent geometry
validation → review/commit chain with PostgreSQL/MinIO evidence persistence.
The original customer model is used only in private deployment verification.

Kernel references: [OCCT curve-on-surface diagnostic](https://github.com/Open-Cascade-SAS/OCCT/blob/V7_8_1/src/BOPAlgo/BOPAlgo_ArgumentAnalyzer.cxx)
and [sample-based inner tolerance calculation](https://github.com/Open-Cascade-SAS/OCCT/blob/V7_8_1/src/BRepLib/BRepLib.cxx).
