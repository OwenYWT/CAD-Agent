# Review memory: 2026-09-29 9ed20b1 codex-native-tools

- Reviewer: Codex `/root`; time: 2026-09-29 21:15 +08:00.
- Range: tracked-default merge-base `62084d5..9ed20b1`; focus `09c3ccb..9ed20b1`.
- Initial worktree clean; no business/application-test changes by reviewer. Only review reports/evidence and this memory were added.
- Conclusion: FAIL, 2 P1 + 2 P2. Report: `docs/qa/code-review/2026-09-29-9ed20b1-codex-native-tools.md`.
- Domains: 04/05/06/07/08/09/10/11/12/14/15/19/21/24; confirmed defects primarily 07/12/14/19.

## Reusable verified observations

1. A complete cylindrical parameter interval and radius difference do not prove continuous wall material. `feature_verification.measure_checks(radial)` passed both a hidden spherical cavity and a cross-drilled tube, including independent STEP round-trip and actual server evidence verification. Check material coverage and trimming, not just nominal radii.
2. `overall_dimension` uses conservative BRep bounding-box extents but compares with one OCCT confusion tolerance. A true Ø20 by 20 tube after cross drilling measured 20.0000002 and failed. Measurement uncertainty is distinct from user manufacturing tolerance; do not alter the requested dimension to satisfy the checker.
3. Every new evidence contract must trace to its public projection. `task_evidence.project_evidence` removes `acceptance` and `request_sha256` even for correctly sealed revision evidence. Existing integrity tests did not assert these new fields survived.
4. Internal verifier artifacts must not depend solely on requested download formats. STL-only FreeCAD workflow input is legal; `_required_formats` adds FCStd but not STEP, while new engineering checks require a parseable STEP.

## Fresh evidence and limits

- Backend: 1700 pass / 204 skip / 1 deselect; frontend: 155 pass; lint/types/build pass; architecture and feature-map pass.
- Fixed released Temporal histories: 12 replayed; existing real kernel contracts: feature measurement, curved surface, recorded checkpoint replay all passed.
- New review probes use real OCCT shapes, STEP/STL export and current production measurement code. Their success at reproducing defects is not product acceptance.
- Normalized AcceptanceContract -> actual kernel -> verify_geometry_evidence cross-validation confirms the radial false positive and STL-only indeterminate outcome.
- Source comparison: 666 files match prior recorded final source; 4 runtime production files match current repository SHA-256.
- No new model calls, HTTP/browser acceptance, cloud deployment, permission race or database recovery proof this review. Existing shared stack was not modified. Do not promote historical test evidence to current acceptance.
- Review helper initially used an incorrect root path, fixed only inside review evidence; no product repair was performed.
