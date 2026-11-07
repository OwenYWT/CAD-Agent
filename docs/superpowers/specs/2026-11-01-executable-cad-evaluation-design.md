# Executable CAD Evaluation Design

> Status: Draft for review
>
> Date: 2026-11-01
>
> Scope: Developer-only evaluation infrastructure. This document does not authorize changes to the ordinary user generation, modification, or history-recovery flow.

## 1. Decision Summary

The project will evolve from a final-result benchmark into an executable CAD evaluation system. A user requirement will eventually be represented as a set of executable CADTests. Each test will produce structured evidence and a clear result, rather than being reduced to one opaque score.

The first implementation is intentionally small:

```text
existing benchmark result
    -> versioned CADTest definitions
    -> evidence-aware test executor
    -> developer-only JSON report
```

The first implementation will be an offline or explicitly invoked observe-mode tool. It will read existing generated artifacts and existing validation results. It will not add model calls, change candidate selection, change repair behavior, block a normal user task, or expose benchmark details in the ordinary frontend.

The long-term system may use tests during candidate ranking and repair, but that behavior is reserved for an explicit experimental mode and is outside the initial implementation.

## 2. Why This Change Is Needed

The current project already has valuable evaluation and validation pieces, but they are distributed across different layers:

- `backend/benchmark` evaluates generation cases, dimensions, printability, paths, artifacts, attempts, and regression deltas.
- `backend/app/validation/source_obligations.py` extracts engineering obligations from a requirement.
- `backend/app/validation/requirement_binding.py` binds generated evidence to those obligations.
- `backend/app/validation/acceptance_preflight.py` checks whether acceptance can be performed reliably before claiming success.
- Production workflows already perform source generation, execution, geometry validation, rendering, DFM validation, and artifact-related work.

These capabilities should not be replaced. They need a stable evaluation boundary so that the project can answer, for development and CI purposes:

1. Which explicit requirement was tested?
2. What evidence was used?
3. Did the evidence pass, fail, remain unknown, or become blocked?
4. Did a repair improve the result or merely change the output?
5. How did reliability change as text, geometry, topology, and operation complexity increased?
6. What resource cost was paid for the result?

The current user-facing product must remain focused on completing CAD work. Evaluation is a development instrument, not a new user workflow.

## 3. Product Boundaries

### 3.1 Developer-only visibility

Evaluation output is visible only through developer tools, local report files, CI artifacts, or a future developer-only report endpoint. It must not appear in:

- the normal chat or task card;
- the ordinary model preview;
- version history and restore dialogs;
- the normal change submission flow;
- user-facing error messages;
- user-facing document metadata.

The first implementation will write structured JSON reports under the existing benchmark report area or an equivalent developer-owned output directory. The report is not a product document and is not part of normal document history.

### 3.2 No ordinary-user behavior change

The following behavior is mandatory:

- Normal user generation continues to use the existing production workflow.
- Normal user modification and history recovery do not invoke CADTests.
- Evaluation failures never block a normal user task.
- Evaluation does not add LLM calls, FreeCAD calls, repair attempts, or candidate retries to ordinary requests.
- Existing production validation remains the source of user-facing acceptance decisions.
- Evaluation code must fail closed as a report-generation problem, not as a production task failure.

### 3.3 Explicit modes

The design uses three modes:

```text
CAD_EVALUATION_MODE=disabled
CAD_EVALUATION_MODE=observe
CAD_EVALUATION_MODE=experimental
```

`disabled` is the default for normal application startup. No evaluator is created and no evaluation report is produced.

`observe` reads an existing result or runs an explicitly requested benchmark evaluation. It may produce a report, but it cannot alter generation, repair, ranking, or acceptance behavior.

`experimental` is reserved for a future developer or CI configuration. It may enable candidate ranking or repair experiments only when the caller supplies an explicit evaluation suite, run identity, and budget. It must not be enabled by an ordinary user request.

## 4. Design Principles

### 4.1 Evidence before score

Every aggregate metric must be traceable to individual tests and evidence. A score without evidence is diagnostic only and cannot be used to claim engineering acceptance.

### 4.2 Unknown is not pass

The evaluator must distinguish:

- `pass`: the required property was checked with authoritative evidence and passed;
- `fail`: the required property was checked and failed;
- `unknown`: the evaluator could not establish the property from available evidence;
- `blocked`: the test could not run because a prerequisite or infrastructure dependency was unavailable;
- `skipped`: the test was intentionally excluded by the case definition.

`unknown`, `blocked`, and `skipped` never count as `pass`. They must remain visible in the report. A report may be technically complete while its engineering verdict is `not_verified`.

### 4.3 Separate concerns

The evaluator reports separate dimensions for:

- source or program execution;
- geometry and topology;
- requirement satisfaction;
- artifact integrity and readability;
- printability and DFM;
- visual evidence;
- program or operation complexity;
- repair gain;
- resource cost.

No single weighted score may hide a hard requirement failure.

### 4.4 Reuse authoritative evidence

Planner labels, inferred feature names, and natural-language descriptions are diagnostic evidence only until independently bound to geometry or source. A test may use them to explain a result, but not to claim that a geometric requirement passed.

### 4.5 Version everything that affects a result

An evaluation report must record the evaluator version, test-suite version, case-set hash, verifier version, source revision, model/provider configuration, runtime image digest when available, and relevant execution settings.

### 4.6 Incremental and reversible adoption

Each phase must be useful and testable without requiring the next phase. Initial phases are additive and can be disabled without changing the production path.

## 5. Current Capability Map

The initial design maps onto the current repository as follows:

| Existing capability | Initial use | Initial change policy |
| --- | --- | --- |
| `backend/benchmark/eval_cases.py` | Case and expected-property source | Reuse; do not reinterpret existing cases silently |
| `backend/benchmark/metrics.py` | Existing metric extraction and aggregation | Reuse through an adapter |
| `backend/benchmark/eval.py` | Existing benchmark execution entry point | Preserve existing command behavior; add a separate report path only if needed |
| `backend/benchmark/compare.py` | Baseline/candidate regression comparison | Keep existing `FIXED`, `BROKE`, and hard-regression semantics |
| `backend/app/validation/source_obligations.py` | Future obligation-backed CADTests | Read through a versioned adapter; do not make planner text authoritative |
| `backend/app/validation/requirement_binding.py` | Future evidence-to-obligation binding | Reuse its authoritative binding rules |
| `backend/app/validation/acceptance_preflight.py` | Distinguish verifiable from unverified requirements | Map preflight outcomes to `unknown` or `blocked`, not `pass` |
| Existing geometry, artifact, DFM, and visual validators | Evidence producers | Reuse their evidence contracts where stable |
| Production `agent_v2` workflow | Ordinary user execution | No initial integration |

The design does not treat existing documentation claims or planner fields as proof that a feature is implemented. A test is considered available only when it has an executable implementation and a testable evidence contract.

## 6. Target Architecture

The long-term architecture is:

```text
user requirement
    -> requirement obligations
    -> executable CADTests
    -> candidate / intermediate operation states
    -> evidence producers
    -> test results
    -> candidate ranking and repair analysis
    -> developer report
```

The initial architecture is deliberately narrower:

```text
benchmark case or explicit evaluation input
    -> existing generated result and artifacts
    -> CADTest registry
    -> test executor
    -> structured report writer
```

The evaluator should be a separate module boundary with these responsibilities:

1. Load a versioned test suite.
2. Resolve an input result and its provenance.
3. Run tests against an evidence context.
4. Preserve per-test evidence and errors.
5. Derive aggregate metrics without losing hard failures.
6. Serialize a stable developer report.

The evaluator must not own generation, repair, persistence of user documents, or user-facing task state.

## 7. Versioned Contracts

The following contracts are logical contracts for the initial design. They are not production code yet. The implementation should use the project's established Python typing and serialization conventions rather than introducing a second schema system without need.

### 7.1 `EvaluationRun`

```json
{
  "schema_version": "cad-evaluation-run.v1",
  "run_id": "uuid",
  "mode": "observe",
  "started_at": "UTC timestamp",
  "finished_at": "UTC timestamp",
  "source": {
    "kind": "benchmark_case",
    "case_set_hash": "sha256",
    "case_ids": ["P01"]
  },
  "provenance": {
    "git_sha": "commit or unknown",
    "model": "provider/model or unknown",
    "temperature": "number or unknown",
    "rag_enabled": "boolean or unknown",
    "runtime_image": "digest or unknown",
    "verifier_version": "version",
    "test_suite_version": "cad-tests.v1"
  },
  "budget": {
    "candidate_limit": 1,
    "repair_limit": 0,
    "time_limit_seconds": 0
  },
  "cases": []
}
```

`budget` is recorded even when the initial evaluator does not spend a budget. This prevents later reports from losing the distinction between a single observation and a search experiment.

### 7.2 `EvaluationCase`

```json
{
  "case_id": "P01",
  "input_text": "original requirement",
  "difficulty": "simple",
  "modeling_path": "extrude_cut",
  "tests": [],
  "result": "passed|failed|not_verified|blocked|error",
  "artifacts": [],
  "metrics": {}
}
```

The case result is derived from test results using explicit policy. It is not copied from a model-generated success flag.

### 7.3 `ExecutableCADTest`

Each test definition must contain:

```json
{
  "test_id": "dimension.bbox.v1",
  "test_version": "1",
  "kind": "dimension",
  "severity": "required|diagnostic",
  "obligation_ids": ["o-objective-..."],
  "parameters": {
    "axis": "width",
    "expected": 10.0,
    "tolerance": 0.1,
    "unit": "mm"
  }
}
```

Test definitions are declarative. The executable implementation is registered by `kind` and version. An unknown test kind must produce `blocked` or `error` with an explicit reason; it must never silently pass.

### 7.4 `CADTestEvidence`

Evidence must identify its source and authority:

```json
{
  "evidence_id": "uuid",
  "source_kind": "geometry_validator",
  "source_ref": "artifact or validation result id",
  "authority": "independent_geometry|source_bound|planner_diagnostic|runtime",
  "artifact_sha256": "sha256 or null",
  "observed": {
    "width": 10.02,
    "unit": "mm"
  },
  "created_at": "UTC timestamp"
}
```

Evidence without a source reference, artifact identity, or authoritative observation can support diagnostics but cannot satisfy a required CADTest.

### 7.5 `CADTestResult`

```json
{
  "test_id": "dimension.bbox.v1",
  "status": "pass",
  "severity": "required",
  "message": "Bounding-box width is within tolerance",
  "evidence_ids": ["uuid"],
  "observed": {"value": 10.02, "unit": "mm"},
  "expected": {"value": 10.0, "tolerance": 0.1, "unit": "mm"},
  "failure_code": null,
  "duration_ms": 4
}
```

Failure codes must be stable machine-readable values. The human message may evolve, but the failure code is used by comparisons and CI.

### 7.6 `EvaluationReport`

The report contains the run metadata, cases, tests, evidence, metrics, and an explicit overall verdict:

```json
{
  "schema_version": "cad-evaluation-report.v1",
  "run": {},
  "summary": {
    "required_pass_rate": 0.0,
    "obligation_coverage": 0.0,
    "unknown_count": 0,
    "blocked_count": 0,
    "hard_regression_count": 0
  },
  "verdict": "verified|failed|not_verified|blocked|error",
  "cases": []
}
```

The report is append-friendly at the case level, but a final report must not be presented as complete until all declared cases have a terminal result.

## 8. Initial CADTest Set: Evaluation v0

Evaluation v0 uses existing result data and stable evidence sources. It does not attempt to parse every natural-language requirement or verify every CAD property.

### 8.1 Required v0 tests

1. `artifact.readable.v1`
   - Checks that the expected artifact exists and can be opened by the applicable reader.
   - Applies to STEP, STL, and DXF where the case declares the format.

2. `geometry.non_empty.v1`
   - Checks that the artifact contains non-empty geometry when geometry is required.

3. `geometry.valid_solid.v1`
   - Uses existing geometry validation evidence for a valid solid.
   - A missing validator result is `unknown` or `blocked`, never pass.

4. `geometry.solid_count.v1`
   - Runs only when the case explicitly declares a solid-count requirement.
   - Does not infer count from words such as “one” unless a binding layer has produced an explicit obligation.

5. `geometry.bounding_box.v1`
   - Compares declared dimensions with measured bounding-box dimensions using the case tolerance and unit normalization.
   - Axis ordering and unit conversion must be recorded in evidence.

6. `geometry.watertight.v1`
   - Uses an authoritative topology or inspect result when the case requires it.

7. `manufacturing.printable.v1`
   - Maps the existing printable result into a test result.
   - Printability is separate from geometric validity and dimension matching.

8. `provenance.artifact_hash.v1`
   - Records the artifact digest used by the test.
   - A digest mismatch between evidence and artifact is a report error or blocked result, not a pass.

### 8.2 Deliberately excluded from v0

The following are not v0 acceptance tests:

- visual semantic similarity;
- inferred part-type labels;
- planner-only feature names or hole counts;
- full DFM rule coverage;
- operation-by-operation geometry checks;
- automatic candidate search;
- automatic repair strategy changes;
- training reward integration;
- multimodal input expansion;
- human evaluation replacement.

They may be reported as existing diagnostics where available, but they must not be represented as implemented CADTests until they have executable evidence contracts.

## 9. Requirement Binding Roadmap

### V0: Existing case expectations

Test parameters come from explicit benchmark case fields such as `expected_dims`, `tol`, `should_be_printable`, and explicit artifact expectations. This keeps the first phase deterministic.

### V1: Obligation-backed tests

The evaluator consumes `source_obligations`, `requirement_binding`, and `acceptance_preflight` outputs. A requirement such as “one cube with side 10 cm” must result in independently testable obligations for:

- product count, when count is explicitly bound;
- equal axes, when cube semantics are recognized;
- the requested length and normalized unit;
- valid solid evidence.

If an obligation is not bound to executable evidence, the report must say `not_verified`. It must not downgrade the requirement to a passing label because the generated source happens to contain a plausible number.

### V2 and later: Requirement-to-test compilation

The long-term compiler may convert supported obligation types into CADTests. Unsupported or ambiguous requirements remain visible as `unknown` with a reason. The compiler must be versioned so that a change in extraction or binding can be compared against previous evaluation runs.

## 10. Intermediate-State Evaluation

After v0, each modeling operation or repair step can produce an intermediate evaluation record:

```json
{
  "operation_id": "op-03",
  "candidate_id": "candidate-01",
  "artifact_sha256": "sha256",
  "geometry_fingerprint": "fingerprint",
  "passed_obligation_ids": [],
  "failed_obligation_ids": [],
  "newly_passed": [],
  "newly_failed": [],
  "repair_delta": {
    "before": "failed",
    "after": "pass"
  }
}
```

This is designed to support the CADENA-style principle that early checks are cheaper and more useful than a single final check. It is not part of v0 because the current production workflow must first expose stable operation-level artifact identities and evidence references.

## 11. Candidate Ranking and Repair Gain

Candidate ranking is a future experimental capability. When enabled explicitly, the ranking policy must be lexicographic for hard requirements and only then use soft quality metrics:

1. Required CADTests passed.
2. Critical obligation coverage.
3. Geometry and topology quality.
4. Artifact integrity.
5. DFM and visual quality.
6. Program or operation complexity.
7. Resource cost.

The system must never select a candidate that fails a required test merely because it has a higher visual score or lower cost, unless the experiment explicitly declares that requirement as diagnostic.

Repair gain is measured as a delta between a named before-result and after-result. It records which tests changed, which obligations changed, the number of repair attempts, and the resource cost. A repair that changes only a diagnostic score is not reported as satisfying a hard requirement.

## 12. Complexity and Cost Matrix

The long-term benchmark must stratify results instead of publishing only one overall success rate.

### 12.1 Complexity dimensions

- text complexity: short geometric prompt through detailed procedural description;
- geometry complexity: primitive, feature-driven, multi-feature, curved, or organic;
- B-Rep complexity: measured faces, edges, vertices, solids, and topology indicators;
- operation complexity: operation count, dependency depth, and repair count;
- engineering complexity: tolerances, interfaces, manufacturing constraints, and assembly relationships;
- input modality: text, image-assisted, CAD-assisted, or other explicitly supported modality.

### 12.2 Resource dimensions

- model calls and token counts;
- FreeCAD or sandbox execution time;
- repair count;
- candidate count;
- artifact size;
- CPU and memory where the runtime can provide reliable measurements.

Each metric must identify whether it is measured, estimated, or unavailable. An unavailable cost must not be reported as zero.

## 13. Error and Failure Semantics

Evaluation errors are classified separately from CAD failures:

- `test_failed`: the test ran and the artifact did not satisfy the property;
- `not_verified`: the property is required or relevant but evidence is insufficient;
- `blocked`: infrastructure, runtime, artifact, or dependency prevented the test from running;
- `evaluator_error`: the evaluator itself violated a contract or could not serialize a valid result;
- `input_error`: the evaluation input or test definition is invalid.

An evaluator error must be prominent in the developer report and must not be converted into a model failure. A blocked test must preserve the blocking dependency, such as unavailable FreeCAD, missing artifact, missing validator result, or unavailable provider.

The evaluator should be resilient at case level: one case error should produce an error result for that case and allow independent cases to finish, unless the test suite or report contract itself is invalid.

## 14. Testing Strategy

The implementation will be introduced with tests before production integration.

### 14.1 Contract tests

- Serialize and deserialize each v1 contract.
- Reject unknown required fields or preserve forward-compatible extension fields according to existing project conventions.
- Verify stable schema and test-suite version values.
- Verify that missing evidence cannot produce a required pass.

### 14.2 Unit tests

- Dimension equality with unit normalization and tolerance.
- Bounding-box axis ordering.
- Explicit solid-count checks.
- Non-empty and valid-solid mapping.
- Artifact readability and hash mismatch handling.
- `unknown`, `blocked`, and `error` propagation.
- Case verdict derivation with hard and diagnostic tests.

### 14.3 Regression tests

- Existing `backend/benchmark` commands retain their current behavior.
- Existing `compare.py` results retain `FIXED`, `BROKE`, and hard-regression semantics.
- Production generation and modification tests do not invoke the evaluator when mode is `disabled`.
- Observe-mode report creation does not modify a document, revision, task state, or candidate.

### 14.4 Fixture requirements

Fixtures should use small deterministic geometry artifacts and synthetic validator outputs where possible. A live Podman/FreeCAD environment is reserved for integration tests that already require it. The v0 contract and evaluator tests must not require a model provider or network access.

## 15. Rollout Plan

### Phase 0: Design and contract review

- Review this document.
- Confirm developer-only visibility and no ordinary-user behavior change.
- Confirm the initial test list and status semantics.
- Freeze the v0 schema before implementation.

### Phase 1: Offline evaluator

- Add the versioned contract types.
- Add the test registry and v0 test implementations.
- Add an adapter from existing benchmark results.
- Produce developer-only JSON reports.
- Add deterministic unit and contract tests.

### Phase 2: Requirement binding

- Add adapters for source obligations and requirement binding.
- Emit `not_verified` for unbound or unsupported obligations.
- Add obligation coverage and binding diagnostics to reports.

### Phase 3: Intermediate evidence

- Add operation and repair evidence identities.
- Record newly passed and newly failed obligations.
- Report repair gain without changing repair behavior.

### Phase 4: Experimental ranking

- Add an explicitly configured evaluation worker or task queue.
- Add candidate ranking and bounded repair experiments.
- Keep experimental execution identity and report storage separate from ordinary user tasks.

### Phase 5: Complexity and quality benchmark

- Add complexity and resource matrices.
- Freeze reproducibility metadata.
- Add baseline comparison and CI/nightly reporting.

Each phase must pass its own tests and preserve the previous phase. No phase is a prerequisite for ordinary user operation.

## 16. Security and Data Handling

Evaluation reports may contain prompts, generated source, artifact hashes, provider metadata, runtime information, and failure details. They must therefore:

- remain outside ordinary user document history;
- follow existing developer/CI artifact access controls;
- avoid storing provider secrets, API keys, or full sensitive configuration;
- avoid exposing internal runtime paths or infrastructure details in user-facing APIs;
- preserve artifact hashes and provenance without copying artifacts unnecessarily.

The evaluator must not read or serialize `.env` contents or other secret configuration as part of provenance.

## 17. Acceptance Criteria for the Initial Implementation

The initial implementation is complete only when all of the following are true:

1. `CAD_EVALUATION_MODE=disabled` produces no evaluator side effects and preserves current ordinary behavior.
2. An explicitly invoked observe-mode run can evaluate at least one deterministic benchmark case without a model call.
3. The report contains versioned run metadata, per-case results, per-test results, evidence references, and an explicit verdict.
4. Required tests distinguish `pass`, `fail`, `unknown`, `blocked`, and `error`.
5. Missing or non-authoritative evidence cannot become a required pass.
6. Existing benchmark and comparison entry points continue to work.
7. Evaluation failures do not change document, revision, task, candidate, or user-visible state.
8. The implementation has no fake test results, dummy pass branches, or unimplemented test kinds presented as available.
9. The report and tests are UTF-8 clean and introduce no mojibake.
10. Tests demonstrate the above behavior without requiring the live model provider or network.

## 18. Non-Goals

This design does not authorize:

- changing the production agent workflow;
- changing user-facing acceptance rules;
- adding an evaluation panel to the normal frontend;
- making visual similarity the primary acceptance criterion;
- treating planner output as geometry proof;
- implementing all CAD features or all DFM rules;
- adding a new model provider;
- rebuilding or replacing the existing Podman/FreeCAD environment;
- adding training-time rewards;
- claiming that future phases already exist.

## 19. Research Alignment

The direction is aligned with the supplied research references, with explicit separation between research inspiration and implemented capability:

- CADTestBench motivates executable tests instead of only final similarity.
- CADBench motivates complexity stratification and multiple metrics rather than one global pass rate.
- CADMorph motivates candidate queues, ranking, repair gain, and multiple quality signals.
- CAD-Coder motivates separating execution/format validity from geometry validity and requirement validity.
- CADENA and related intermediate-checking work motivate operation-level evidence.
- Text2CAD motivates varying text description depth and tracking visual, parameter, and geometric accuracy separately.

These references inform the roadmap. They do not justify marking a capability as implemented before the repository contains an executable test, authoritative evidence source, and passing regression coverage.

## 20. Review Questions

Before implementation begins, review these decisions:

1. Is developer-only visibility correctly enforced, with no ordinary user exposure?
2. Is `disabled` the correct default, with `observe` as the first usable mode?
3. Is the v0 test set small enough to implement and verify without changing production behavior?
4. Are `unknown` and `blocked` semantics strict enough for engineering evaluation?
5. Is the versioned contract boundary suitable for future intermediate checks, candidate ranking, repair gain, and complexity reporting?

After these decisions are approved, the next artifact should be a separate implementation plan for Evaluation v0. Code changes should begin only from that approved plan.
