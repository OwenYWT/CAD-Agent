"""Executable v0 CADTest definitions and their evidence-only handlers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import hashlib
from pathlib import Path
from typing import Any

from .contracts import TestDefinition, TestResult, TestSeverity, TestStatus
from .evidence import EvidenceContext

TestHandler = Callable[[TestDefinition, EvidenceContext], TestResult]


def _evidence_ids(context: EvidenceContext, *source_kinds: str) -> tuple[str, ...]:
    return tuple(item.evidence_id for item in context.evidence if item.source_kind in source_kinds)


def _result(
    definition: TestDefinition,
    status: TestStatus,
    message: str,
    *,
    context: EvidenceContext,
    evidence_ids: tuple[str, ...] = (),
    observed: dict[str, Any] | None = None,
    expected: dict[str, Any] | None = None,
    failure_code: str | None = None,
) -> TestResult:
    return TestResult(
        test_id=definition.test_id,
        status=status,
        severity=definition.severity,
        message=message,
        evidence_ids=evidence_ids,
        observed=observed,
        expected=expected,
        failure_code=failure_code,
    )


def _artifact_readable(definition: TestDefinition, context: EvidenceContext) -> TestResult:
    gate = context.observations.get("artifact_gate") or {}
    formats = tuple(definition.parameters.get("formats") or ())
    keys = tuple(f"{item}_readable" for item in formats)
    if not keys or not any(key in gate for key in keys):
        return _result(
            definition,
            TestStatus.BLOCKED,
            "artifact evidence is unavailable",
            context=context,
            failure_code="missing_artifact",
        )
    if all(gate.get(key) is True for key in keys):
        return _result(
            definition,
            TestStatus.PASS,
            "declared artifacts are readable",
            context=context,
            evidence_ids=_evidence_ids(context, *(f"artifact_{item}" for item in formats)),
            observed={key: gate.get(key) for key in keys},
            expected={key: True for key in keys},
        )
    return _result(
        definition,
        TestStatus.FAIL,
        "one or more declared artifacts are not readable",
        context=context,
        evidence_ids=_evidence_ids(context, *(f"artifact_{item}" for item in formats)),
        observed={key: gate.get(key) for key in keys},
        expected={key: True for key in keys},
        failure_code="artifact_not_readable",
    )


def _non_empty(definition: TestDefinition, context: EvidenceContext) -> TestResult:
    gate = context.observations.get("artifact_gate") or {}
    if "geometry_nonempty" not in gate:
        return _result(
            definition,
            TestStatus.BLOCKED,
            "non-empty geometry evidence is unavailable",
            context=context,
            failure_code="missing_artifact",
        )
    status = TestStatus.PASS if gate["geometry_nonempty"] is True else TestStatus.FAIL
    return _result(
        definition,
        status,
        "geometry is non-empty" if status is TestStatus.PASS else "geometry is empty",
        context=context,
        observed={"geometry_nonempty": gate["geometry_nonempty"]},
        expected={"geometry_nonempty": True},
        failure_code=None if status is TestStatus.PASS else "empty_geometry",
    )


def _valid_solid(definition: TestDefinition, context: EvidenceContext) -> TestResult:
    observed = context.observations.get("valid_solid")
    if observed is None:
        return _result(
            definition,
            TestStatus.UNKNOWN,
            context.reasons.get("valid_solid", "valid-solid evidence is unavailable"),
            context=context,
            failure_code="valid_solid_unavailable",
        )
    status = TestStatus.PASS if observed is True else TestStatus.FAIL
    return _result(
        definition,
        status,
        "geometry is a valid solid" if status is TestStatus.PASS else "geometry is not a valid solid",
        context=context,
        evidence_ids=_evidence_ids(context, "geometry_valid_solid"),
        observed={"valid_solid": observed},
        expected={"valid_solid": True},
        failure_code=None if status is TestStatus.PASS else "invalid_solid",
    )


def _solid_count(definition: TestDefinition, context: EvidenceContext) -> TestResult:
    observed = context.observations.get("solid_count")
    expected = definition.parameters.get("expected")
    if observed is None:
        return _result(
            definition,
            TestStatus.UNKNOWN,
            context.reasons.get("solid_count", "solid-count evidence is unavailable"),
            context=context,
            failure_code="solid_count_unavailable",
        )
    status = TestStatus.PASS if observed == expected else TestStatus.FAIL
    return _result(
        definition,
        status,
        "solid count matches" if status is TestStatus.PASS else "solid count does not match",
        context=context,
        evidence_ids=_evidence_ids(context, "geometry_solid_count"),
        observed={"solid_count": observed},
        expected={"solid_count": expected},
        failure_code=None if status is TestStatus.PASS else "solid_count_mismatch",
    )


def _bounding_box(definition: TestDefinition, context: EvidenceContext) -> TestResult:
    observed = context.observations.get("bbox_extents_mm")
    expected_map = definition.parameters.get("expected") or {}
    expected = sorted(
        [float(expected_map.get(key, 0)) for key in ("width", "height", "depth") if expected_map.get(key, 0) > 0],
        reverse=True,
    )
    if observed is None:
        return _result(
            definition,
            TestStatus.UNKNOWN,
            context.reasons.get("bbox_extents_mm", "bounding-box evidence is unavailable"),
            context=context,
            expected={"dimensions_mm": expected},
            failure_code="missing_dimension_evidence",
        )
    actual = sorted((float(value) for value in observed), reverse=True)
    tolerance = float(definition.parameters.get("tolerance", 0.1))
    matches = len(actual) == len(expected) and all(
        abs(measured - target) / max(target, 1.0) <= tolerance
        for measured, target in zip(actual, expected)
    )
    status = TestStatus.PASS if matches else TestStatus.FAIL
    return _result(
        definition,
        status,
        "bounding-box dimensions match" if matches else "bounding-box dimensions do not match",
        context=context,
        evidence_ids=_evidence_ids(context, "geometry_bbox"),
        observed={"dimensions_mm": actual},
        expected={"dimensions_mm": expected, "tolerance": tolerance},
        failure_code=None if matches else "dimension_mismatch",
    )


def _boolean_observation(
    definition: TestDefinition,
    context: EvidenceContext,
    *,
    name: str,
    expected: bool,
    missing_code: str,
) -> TestResult:
    observed = context.observations.get(name)
    if observed is None:
        return _result(
            definition,
            TestStatus.UNKNOWN,
            f"{name} evidence is unavailable",
            context=context,
            failure_code=missing_code,
        )
    status = TestStatus.PASS if observed is expected else TestStatus.FAIL
    return _result(
        definition,
        status,
        f"{name} matches" if status is TestStatus.PASS else f"{name} does not match",
        context=context,
        evidence_ids=_evidence_ids(context, f"geometry_{name}"),
        observed={name: observed},
        expected={name: expected},
        failure_code=None if status is TestStatus.PASS else f"{name}_mismatch",
    )


def _watertight(definition: TestDefinition, context: EvidenceContext) -> TestResult:
    return _boolean_observation(
        definition,
        context,
        name="watertight",
        expected=True,
        missing_code="watertight_unavailable",
    )


def _printable(definition: TestDefinition, context: EvidenceContext) -> TestResult:
    return _boolean_observation(
        definition,
        context,
        name="printable",
        expected=bool(definition.parameters.get("expected", True)),
        missing_code="printable_unavailable",
    )


def _artifact_hash(definition: TestDefinition, context: EvidenceContext) -> TestResult:
    gate = context.observations.get("artifact_gate") or {}
    formats = tuple(definition.parameters.get("formats") or ())
    artifact_rows = {
        item: next(
            (evidence for evidence in context.evidence if evidence.source_kind == f"artifact_{item}"),
            None,
        )
        for item in formats
    }
    if any(artifact_rows[item] is None for item in formats):
        return _result(
            definition,
            TestStatus.UNKNOWN,
            "referenced artifact evidence is unavailable",
            context=context,
            failure_code="artifact_source_unavailable",
        )
    digests = {
        item: artifact_rows[item].artifact_sha256
        for item in formats
    }
    paths = {
        item: (artifact_rows[item].details or {}).get("path")
        for item in formats
    }
    if any(not isinstance(paths[item], str) or not Path(paths[item]).is_file() for item in formats):
        return _result(
            definition,
            TestStatus.UNKNOWN,
            "referenced artifact file is unavailable",
            context=context,
            observed={"paths": paths, "digests": digests},
            failure_code="artifact_source_unavailable",
        )
    actual = {}
    for item in formats:
        digest = hashlib.sha256()
        with Path(paths[item]).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        actual[item] = digest.hexdigest()
    valid = all(
        isinstance(digests[item], str)
        and len(digests[item]) == 64
        and all(char in "0123456789abcdef" for char in digests[item])
        and digests[item] == actual[item]
        for item in formats
    )
    status = TestStatus.PASS if valid else TestStatus.FAIL
    return _result(
        definition,
        status,
        "artifact hashes match referenced files" if valid else "artifact hash does not match referenced file",
        context=context,
        observed={"paths": paths, "digests": digests, "actual": actual},
        expected={item: "sha256" for item in formats},
        failure_code=None if valid else "artifact_hash_mismatch",
    )


HANDLERS: dict[str, TestHandler] = {
    "artifact.readable": _artifact_readable,
    "geometry.non_empty": _non_empty,
    "geometry.valid_solid": _valid_solid,
    "geometry.solid_count": _solid_count,
    "geometry.bounding_box": _bounding_box,
    "geometry.watertight": _watertight,
    "manufacturing.printable": _printable,
    "provenance.artifact_hash": _artifact_hash,
}


def v0_definitions(case: dict[str, Any]) -> tuple[TestDefinition, ...]:
    path = case.get("path", "extrude_cut")
    definitions: list[TestDefinition] = []
    if path == "2d":
        formats = ("dxf",)
    else:
        formats = ("step", "stl")
    definitions.extend(
        [
            TestDefinition(
                test_id="artifact.readable.v1",
                test_version="1",
                kind="artifact.readable",
                severity=TestSeverity.REQUIRED,
                parameters={"formats": formats},
            ),
            TestDefinition(
                test_id="provenance.artifact_hash.v1",
                test_version="1",
                kind="provenance.artifact_hash",
                severity=TestSeverity.DIAGNOSTIC,
                parameters={"formats": formats},
            ),
        ]
    )
    if path != "2d":
        definitions.extend(
            [
                TestDefinition(
                    test_id="geometry.non_empty.v1",
                    test_version="1",
                    kind="geometry.non_empty",
                    severity=TestSeverity.REQUIRED,
                ),
                TestDefinition(
                    test_id="geometry.valid_solid.v1",
                    test_version="1",
                    kind="geometry.valid_solid",
                    severity=TestSeverity.REQUIRED,
                ),
                TestDefinition(
                    test_id="geometry.watertight.v1",
                    test_version="1",
                    kind="geometry.watertight",
                    severity=TestSeverity.REQUIRED,
                ),
            ]
        )
        if case.get("should_be_printable", True):
            definitions.append(
                TestDefinition(
                    test_id="manufacturing.printable.v1",
                    test_version="1",
                    kind="manufacturing.printable",
                    severity=TestSeverity.REQUIRED,
                    parameters={"expected": True},
                )
            )
    expected_dims = case.get("expected_dims") or {}
    if expected_dims:
        definitions.append(
            TestDefinition(
                test_id="geometry.bounding_box.v1",
                test_version="1",
                kind="geometry.bounding_box",
                severity=TestSeverity.REQUIRED,
                parameters={"expected": expected_dims, "tolerance": case.get("tol", 0.1)},
            )
        )
    if case.get("expected_solid_count") is not None:
        definitions.append(
            TestDefinition(
                test_id="geometry.solid_count.v1",
                test_version="1",
                kind="geometry.solid_count",
                severity=TestSeverity.REQUIRED,
                parameters={"expected": case["expected_solid_count"]},
            )
        )
    return tuple(definitions)
