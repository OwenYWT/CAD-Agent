# Executable CAD Evaluation Implementation Plan

> **For agentic workers:** This plan is intended for task-by-task execution with a verification checkpoint after each task. Do not modify `agent_v2` or ordinary user workflows while implementing Evaluation v0.

**Goal:** Add a developer-only, observe-mode executable CADTest evaluator that reads existing benchmark results, preserves authoritative evidence, and emits a versioned JSON report without changing ordinary generation behavior.

**Architecture:** Add an isolated `backend/benchmark/cad_tests` package containing versioned contracts, evidence normalization, test registration, execution policy, and report serialization. Add a separate benchmark CLI that consumes an existing benchmark report or deterministic fixture; keep the production application unaware of the evaluator. Extend existing benchmark rows only with additive evidence fields needed to make the report traceable.

**Tech Stack:** Python 3.11, standard-library dataclasses/enums/hashlib/json, existing benchmark dictionaries, existing Pydantic validation models as read-only evidence sources, pytest unit tests, and the existing `python -m benchmark...` entry-point convention.

---

## Scope Guard

The implementation covers Evaluation v0 only:

- required tests for artifact readability, non-empty geometry, valid geometry, explicit solid count when authoritative evidence exists, bounding-box dimensions, watertightness, printability, and artifact hash/provenance;
- `disabled` and explicit `observe` mode handling;
- a deterministic evaluator over existing report rows;
- developer-only JSON output;
- contract, unit, and regression tests.

The implementation does not change `backend/app/workflows/agent_v2.py`, candidate selection, repair behavior, user-facing APIs, frontend components, document revisions, or ordinary task state. Requirement binding, intermediate operation checks, candidate ranking, repair gain, and complexity matrices remain later phases from the approved design.

## File Map

Files to create:

- `backend/benchmark/cad_tests/__init__.py`: public v0 evaluator exports and package version.
- `backend/benchmark/cad_tests/contracts.py`: status enums, test definitions, evidence, results, run metadata, and report dataclasses.
- `backend/benchmark/cad_tests/mode.py`: `disabled`, `observe`, and `experimental` mode parsing with a safe default.
- `backend/benchmark/cad_tests/evidence.py`: conversion from existing benchmark rows into normalized evidence without inventing measurements.
- `backend/benchmark/cad_tests/definitions.py`: v0 test definitions and the test-kind registry.
- `backend/benchmark/cad_tests/executor.py`: test execution, required-test verdict policy, and case-level error isolation.
- `backend/benchmark/cad_tests/report.py`: JSON serialization and report summary calculation.
- `backend/benchmark/cad_eval.py`: explicit developer CLI for evaluating an existing JSON benchmark report.
- `backend/tests/test_cad_evaluation_contracts.py`: contract and serialization tests.
- `backend/tests/test_cad_evaluation_modes.py`: mode isolation and default behavior tests.
- `backend/tests/test_cad_evaluation_evidence.py`: evidence normalization tests.
- `backend/tests/test_cad_evaluation_executor.py`: v0 test and verdict tests.
- `backend/tests/test_cad_evaluation_report.py`: report and CLI-level pure-function tests.

Files to modify additively:

- `backend/benchmark/metrics.py`: expose measured bounding-box evidence and stable metric metadata in existing rows while preserving all existing keys and pass/fail behavior.
- `backend/benchmark/eval.py`: include artifact paths and SHA-256 evidence in the existing artifact evidence object; do not change the existing artifact gate decision.
- `backend/benchmark/README.md`: document the developer-only evaluator command and its no-production-side-effects guarantee.

Files that must not be modified for v0:

- `backend/app/workflows/agent_v2.py`;
- `backend/app/api/**`;
- `frontend/**`;
- document, revision, change-set, and history persistence modules;
- Podman, FreeCAD, Temporal, or model-provider configuration.

## Task 1: Add Versioned Contracts and Safe Modes

**Files:**
- Create: `backend/benchmark/cad_tests/__init__.py`
- Create: `backend/benchmark/cad_tests/contracts.py`
- Create: `backend/benchmark/cad_tests/mode.py`
- Test: `backend/tests/test_cad_evaluation_contracts.py`
- Test: `backend/tests/test_cad_evaluation_modes.py`

- [ ] **Step 1: Write failing contract tests**

Add tests that construct a run containing one required passing test, serialize it to JSON-compatible data, and assert that the following values round-trip exactly: `schema_version`, `test_id`, `status`, `severity`, `evidence_ids`, and `verdict`. Add tests that construct results for `pass`, `fail`, `unknown`, `blocked`, `skipped`, and `error` without coercing any status into `pass`.

Add mode tests with `monkeypatch`:

```python
def test_missing_mode_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("CAD_EVALUATION_MODE", raising=False)
    assert resolve_mode() is EvaluationMode.DISABLED


def test_invalid_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "enabled")
    with pytest.raises(ValueError, match="CAD_EVALUATION_MODE"):
        resolve_mode()


def test_observe_mode_is_explicit(monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    assert resolve_mode() is EvaluationMode.OBSERVE
```

Mark these tests `unit`; they must not import application startup code or require environment services.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run from `backend`:

```powershell
python -m pytest tests/test_cad_evaluation_contracts.py tests/test_cad_evaluation_modes.py -m unit -q
```

Expected result: collection fails because the new package and contract functions do not exist.

- [ ] **Step 3: Implement the contract types**

Use standard-library dataclasses and enums so the benchmark evaluator remains independent from application startup. Define these exact concepts:

```python
class TestStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    ERROR = "error"


class TestSeverity(StrEnum):
    REQUIRED = "required"
    DIAGNOSTIC = "diagnostic"


class CaseVerdict(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    NOT_VERIFIED = "not_verified"
    BLOCKED = "blocked"
    ERROR = "error"
```

Add frozen dataclasses for `TestDefinition`, `Evidence`, `TestResult`, `EvaluationCase`, `EvaluationRun`, and `EvaluationReport`. Each dataclass must expose a `to_dict()` method that emits only JSON-compatible values, including enum `.value` strings. Required fields are:

- `TestDefinition`: `test_id`, `test_version`, `kind`, `severity`, `obligation_ids`, `parameters`;
- `Evidence`: `evidence_id`, `source_kind`, `source_ref`, `authority`, `artifact_sha256`, `observed`;
- `TestResult`: `test_id`, `status`, `severity`, `message`, `evidence_ids`, `observed`, `expected`, `failure_code`, `duration_ms`;
- `EvaluationCase`: `case_id`, `input_text`, `difficulty`, `modeling_path`, `tests`, `result`, `artifacts`, `metrics`;
- `EvaluationRun`: `schema_version`, `run_id`, `mode`, `provenance`, `budget`, `cases`;
- `EvaluationReport`: `schema_version`, `run`, `summary`, `verdict`, `cases`.

Reject invalid status/severity/mode values during construction. Do not add a permissive fallback that turns an unknown value into `pass`.

Implement `EvaluationMode` in `mode.py` with `DISABLED`, `OBSERVE`, and `EXPERIMENTAL`, plus `resolve_mode(value: str | None = None)`. An omitted argument reads `CAD_EVALUATION_MODE`; an omitted environment variable resolves to `DISABLED`; unknown values raise `ValueError`.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run the same pytest command. Expected result: all contract and mode tests pass. Also run:

```powershell
python -c "from benchmark.cad_tests.mode import resolve_mode; print(resolve_mode().value)"
```

Expected output with no environment override: `disabled`.

## Task 2: Normalize Existing Benchmark Evidence

**Files:**
- Create: `backend/benchmark/cad_tests/evidence.py`
- Modify: `backend/benchmark/metrics.py`
- Modify: `backend/benchmark/eval.py`
- Test: `backend/tests/test_cad_evaluation_evidence.py`
- Test: `backend/tests/test_eval_metrics.py` only where additive evidence is asserted

- [ ] **Step 1: Write failing evidence tests**

Build deterministic `GenerateResponse` fixtures using the existing `BoundingBox`, `ValidationResult`, and `InspectReport` models. Assert that normalization produces:

- measured `bbox_extents_mm` in descending-independent axis form;
- `watertight=True` and `printable=True` only when supplied by existing validation evidence;
- `unknown`-capable absence for solid count rather than a guessed count;
- artifact gate values copied from the existing row;
- artifact hash only when a real file was hashed;
- no planner-only `part_type` or `feature_match` value is classified as independent geometry evidence.

Add a test with a missing bounding box and assert that the dimension observation is absent, not zero. Add a test with an artifact path that does not exist and assert `sha256=None` and an explicit missing-artifact reason.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_cad_evaluation_evidence.py -m unit -q
```

Expected result: import or assertion failures because the evidence normalizer and additive fields do not exist.

- [ ] **Step 3: Add additive measured evidence to benchmark rows**

In `backend/benchmark/metrics.py`, preserve all current return keys and pass/fail logic. Add a separate `evidence` object containing only facts available from the response:

```python
{
    "bbox_extents_mm": [dx, dy, dz] or None,
    "watertight": True | False | None,
    "printable": True | False | None,
    "solid_count": int | None,
    "authority": {
        "bbox_extents_mm": "independent_geometry" | "unavailable",
        "watertight": "independent_geometry" | "unavailable",
        "printable": "independent_geometry" | "unavailable",
        "solid_count": "independent_geometry" | "unavailable",
    },
}
```

Only expose `solid_count` when it already exists in a stable authoritative validation object. Do not infer it from the words “one”, `part_type`, planner features, or the number of files. Preserve the existing diagnostic `part_type_match` and `feature_match` fields separately.

In `backend/benchmark/eval.py`, add a small SHA-256 helper using streamed file reads. Extend `_verify_stl`, `_verify_dxf`, and `_verify_step` evidence with the artifact path and hash when the file exists and can be hashed. Keep existing readability booleans and `_artifact_evidence` behavior unchanged. A missing or unreadable file must return an explicit false/readability error and no fabricated hash.

Implement `normalize_benchmark_row(case, row) -> EvidenceContext` in `evidence.py`. It must copy authoritative values, preserve source references, attach artifact hashes, and expose missing values as `None` with a reason. The function must not execute FreeCAD, call an LLM, access the network, or inspect planner text as geometry proof.

- [ ] **Step 4: Run evidence and existing metric tests**

Run:

```powershell
python -m pytest tests/test_cad_evaluation_evidence.py tests/test_eval_metrics.py -m unit -q
```

Expected result: all new tests pass and the existing metric suite remains green with the same `passed` outcomes.

## Task 3: Implement v0 Test Definitions and Executor

**Files:**
- Create: `backend/benchmark/cad_tests/definitions.py`
- Create: `backend/benchmark/cad_tests/executor.py`
- Test: `backend/tests/test_cad_evaluation_executor.py`

- [ ] **Step 1: Write failing executor tests**

Use a fixture context containing measured dimensions, valid geometry, solid count, watertightness, printability, artifact gate values, and hashes. Cover these cases:

1. All required tests pass and the case verdict is `verified`.
2. A dimension mismatch returns `fail` and the case verdict is `failed`.
3. Missing dimension evidence returns `unknown` and the case verdict is `not_verified`.
4. Missing runtime/artifact dependency returns `blocked` and the case verdict is `blocked`.
5. A required test with an unsupported kind returns `error` or `blocked`, never `pass`.
6. A diagnostic failure does not make a case fail when all required tests pass.
7. A required `solid_count` test runs only with explicit authoritative solid-count evidence.

Pin failure codes such as `dimension_mismatch`, `missing_dimension_evidence`, `missing_artifact`, `solid_count_unavailable`, `artifact_not_readable`, and `unsupported_test_kind` in assertions.

- [ ] **Step 2: Run the focused executor tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_cad_evaluation_executor.py -m unit -q
```

Expected result: import or assertion failures because the registry and executor are not implemented.

- [ ] **Step 3: Implement the v0 registry**

Register these exact versioned test kinds in `definitions.py`:

```text
artifact.readable.v1
geometry.non_empty.v1
geometry.valid_solid.v1
geometry.solid_count.v1
geometry.bounding_box.v1
geometry.watertight.v1
manufacturing.printable.v1
provenance.artifact_hash.v1
```

Each definition carries severity, parameters, and optional obligation IDs. The handler receives one `EvidenceContext` and returns one `TestResult`. Handlers must not silently substitute a different evidence source.

Implement exact v0 policies:

- `geometry.bounding_box.v1` compares sorted measured extents against sorted expected positive dimensions using the case tolerance; absent measurement is `unknown`;
- `geometry.valid_solid.v1` requires an authoritative valid geometry result; absent result is `unknown`;
- `geometry.solid_count.v1` requires an explicit integer observation and exact equality; absent observation is `unknown`;
- `geometry.watertight.v1` requires an explicit boolean; absent observation is `unknown`;
- `manufacturing.printable.v1` requires an explicit boolean and compares it with the case expectation;
- `geometry.non_empty.v1` and `artifact.readable.v1` use artifact evidence booleans and return `blocked` when the artifact dependency was unavailable;
- `provenance.artifact_hash.v1` passes only when the referenced artifact exists and has a 64-character lower-case SHA-256 digest;
- any unknown kind returns `error` with `unsupported_test_kind`.

Implement `execute_case(case, context, definitions=None)` in `executor.py`. Run each declared test independently, catch handler exceptions into an `error` result with the exception type in diagnostic details, and derive the case verdict using this order:

1. any required `error` -> `error`;
2. any required `fail` -> `failed`;
3. any required `blocked` -> `blocked`;
4. any required `unknown` -> `not_verified`;
5. otherwise -> `verified`.

Diagnostic tests never change the case verdict. Do not compute a weighted score in v0.

- [ ] **Step 4: Run executor tests and verify they pass**

Run the focused executor command again. Expected result: all tests pass, including the assertion that unknown and blocked never count as required passes.

## Task 4: Add Developer-only Report Serialization and CLI

**Files:**
- Create: `backend/benchmark/cad_tests/report.py`
- Create: `backend/benchmark/cad_eval.py`
- Test: `backend/tests/test_cad_evaluation_report.py`
- Modify: `backend/benchmark/README.md`

- [ ] **Step 1: Write failing report tests**

Test pure report functions with two synthetic cases and assert:

- output contains `schema_version="cad-evaluation-report.v1"`;
- run metadata contains evaluator version, test-suite version, git SHA, case-set hash, and mode;
- summary contains required pass rate, obligation coverage only when obligations are present and `null` otherwise, unknown count, blocked count, and error count;
- a failed required test is visible at case and test level;
- evidence IDs resolve to evidence entries;
- JSON serialization uses UTF-8 and never serializes environment secrets.

Test CLI argument parsing with `--mode disabled` and `--mode observe`. `disabled` must exit without reading a report or writing an output file. `observe` must require an input report and write exactly one developer report when the input is valid.

- [ ] **Step 2: Run the focused report tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_cad_evaluation_report.py -m unit -q
```

Expected result: import or assertion failures because the report writer and CLI do not exist.

- [ ] **Step 3: Implement report building**

Implement `build_report(run, cases, evidence)` in `report.py`. It must:

- include `schema_version="cad-evaluation-report.v1"`;
- include a stable evaluator version and test-suite version;
- preserve the input benchmark report reference and case-set hash;
- count required test statuses without treating `unknown`, `blocked`, or `error` as pass;
- calculate required pass rate as passed required tests divided by all declared required tests, while also reporting the denominator and non-verifiable counts;
- report `obligation_coverage` as a measured ratio when obligation-backed tests are supplied and `null` when none are supplied, never as a fabricated positive rate;
- preserve per-test evidence references and failure codes;
- produce an overall verdict using the same hard-result policy as case verdicts.

Implement JSON writing with `encoding="utf-8"`, `ensure_ascii=False`, and an atomic temporary-file replacement within the chosen developer report directory. Do not include environment variable values, API keys, `.env` content, or full runtime configuration in provenance.

- [ ] **Step 4: Implement the explicit CLI**

`backend/benchmark/cad_eval.py` must accept:

```text
--input REPORT.json
--output REPORT.json
--mode disabled|observe
--case CASE_ID[,CASE_ID...]
```

The CLI must default to `disabled`. In disabled mode it prints a short disabled message, returns exit code 0, and does not read or write report files. In observe mode it loads the existing benchmark JSON, selects all or requested cases, converts rows with `normalize_benchmark_row`, executes only declared v0 tests, and writes the versioned developer report. It must not instantiate `Orchestrator`, call a provider, start a sandbox, or modify application state.

Use a deterministic default test selection derived from explicit case fields and available evidence. Do not add a test just because a free-form planner field contains a matching word.

- [ ] **Step 5: Document and test the command**

Add to `backend/benchmark/README.md`:

```powershell
python -m benchmark.cad_eval --mode observe --input reports/eval_<id>.json --output reports/cad_eval_<id>.json
```

State that this command is developer-only, offline over an existing report, and cannot affect ordinary user requests. State that `--mode disabled` is the default and produces no report.

Run the report tests and one deterministic CLI smoke test using a temporary JSON fixture:

```powershell
python -m pytest tests/test_cad_evaluation_report.py -m unit -q
python -m benchmark.cad_eval --mode disabled --output "$env:TEMP\cad-eval-disabled.json"
```

Expected result: the test suite passes and the disabled command does not create the output file.

## Task 5: Regression, Isolation, and Encoding Verification

**Files:**
- Modify only the files listed in Tasks 1-4 if a test exposes a defect.
- Test: existing benchmark and relevant validation suites.

- [ ] **Step 1: Run all new unit tests together**

Run:

```powershell
python -m pytest tests/test_cad_evaluation_contracts.py tests/test_cad_evaluation_modes.py tests/test_cad_evaluation_evidence.py tests/test_cad_evaluation_executor.py tests/test_cad_evaluation_report.py -m unit -q
```

Expected result: all new tests pass without Podman, FreeCAD, Temporal, network access, or model credentials.

- [ ] **Step 2: Run existing benchmark regression tests**

Run:

```powershell
python -m pytest tests/test_eval_cases.py tests/test_eval_metrics.py -m unit -q
```

Expected result: existing metric outcomes remain unchanged. In particular, planner-only `part_type_match` and `feature_match` remain diagnostic and do not gate the existing benchmark pass result.

- [ ] **Step 3: Verify production isolation statically**

Run:

```powershell
rg -n "benchmark\.cad_tests|benchmark\.cad_eval|CAD_EVALUATION_MODE" backend/app frontend
```

Expected result: no ordinary application import or frontend usage. The evaluator may be imported only by benchmark tests/CLI, not by application startup, API routes, Temporal workflows, or frontend code.

- [ ] **Step 4: Verify report encoding and secret exclusion**

Create a report with Chinese input text in a temporary fixture, read it back as UTF-8, and assert there is no U+FFFD replacement character or common mojibake marker. Assert that strings matching known secret variable names and values are absent from serialized provenance.

Run:

```powershell
git diff --check
```

Expected result: no whitespace errors. No `.env`, temporary reports, generated artifacts, or secret configuration files are added to the Git patch.

- [ ] **Step 5: Verify ordinary behavior is untouched**

Run the smallest existing production contract test that exercises the generation workflow without live provider dependencies, plus the existing test selection used by CI:

```powershell
python -m pytest -m "not docker and not llm" -q
```

If the full suite exposes an unrelated pre-existing failure, record the exact test and failure without changing unrelated code. The new evaluator must not be used as a workaround.

## Final Handoff Criteria

The implementation plan is complete only when:

- all new unit and contract tests pass;
- existing benchmark tests pass unchanged;
- `CAD_EVALUATION_MODE` is absent from production startup behavior;
- disabled mode has no file or application side effects;
- observe mode reads existing reports and writes only developer output;
- required evidence is authoritative or the result is `unknown`/`blocked`;
- no fake pass path, dummy handler, or unimplemented test kind is registered as available;
- no frontend, user task, revision, history, Podman, FreeCAD, Temporal, or provider behavior changes;
- the final Git diff contains only the planned source, tests, and documentation changes, with no `.env` or generated artifacts.
