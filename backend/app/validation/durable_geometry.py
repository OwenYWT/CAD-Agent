"""Strict wire contract for geometry evidence returned by isolated MCAD workers."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer
from app.contracts.acceptance import AcceptanceContract, AcceptanceMeasurements, acceptance_outcome
from app.contracts.acceptance_v2 import VersionedAcceptance


class GeometryBounds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float


class GeometryArtifactEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=255)
    format: Literal["step", "stl", "dxf"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    parseable: bool
    valid: bool
    solid_count: int | None = Field(default=None, ge=0)
    face_count: int | None = Field(default=None, ge=0)
    is_watertight: bool | None = None
    volume_mm3: float | None = Field(default=None, ge=0)
    bounds_mm: GeometryBounds | None = None
    dimensions_mm: tuple[float, ...] = ()
    max_dimension_error: float | None = Field(default=None, ge=0)
    issues: tuple[str, ...] = ()

    @model_validator(mode="after")
    def successful_artifact_has_measured_geometry(self):
        if self.valid and (not self.parseable or self.bounds_mm is None):
            raise ValueError("valid geometry requires parseable measured bounds")
        return self


class DurableGeometryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["durable-geometry-report.v1"]
    outcome: Literal["passed", "failed", "indeterminate"]
    artifact_kind: Literal["solid", "profile"]
    expected_dimensions_mm: dict[str, float] = Field(default_factory=dict)
    dimension_tolerance: float = Field(ge=0, le=1)
    artifacts: tuple[GeometryArtifactEvidence, ...]
    issues: tuple[str, ...] = ()
    acceptance: AcceptanceMeasurements | None = None
    acceptance_contract: VersionedAcceptance | None = None
    request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_serializer(mode="wrap")
    def preserve_legacy_wire(self, handler):
        result = handler(self)
        if self.acceptance is None:
            result.pop("acceptance", None)
        if self.acceptance_contract is None:
            result.pop("acceptance_contract", None)
        if self.request_sha256 is None:
            result.pop("request_sha256", None)
        return result

    @model_validator(mode="after")
    def outcome_matches_artifact_evidence(self):
        if not self.artifacts:
            raise ValueError("geometry report requires artifact evidence")
        if self.outcome == "passed" and (
            self.issues or any(not item.valid for item in self.artifacts)
        ):
            raise ValueError("passed geometry cannot contain failed evidence")
        if self.outcome == "indeterminate" and all(
            item.parseable for item in self.artifacts
        ) and self.acceptance is None:
            raise ValueError("indeterminate geometry requires an unparseable artifact")
        return self

    def durable_evidence(self, *, runtime_provenance: dict | None) -> dict:
        return {
            **self.model_dump(mode="json"),
            "runtime_provenance": runtime_provenance,
        }


def verify_geometry_evidence(report, *, request_sha256, acceptance, expected_solid_count, measurement_context=None):
    """Reject stale workers and stale cached evidence before authorizing a gate."""
    if report.request_sha256 != request_sha256:
        raise ValueError("geometry evidence belongs to a different measurement request")
    if measurement_context is not None:
        if sorted(a.sha256 for a in report.artifacts) != sorted(measurement_context['artifact_sha256s']):
            raise ValueError('geometry evidence belongs to a different manifest artifact')
    if acceptance is not None:
        if report.acceptance_contract is not None and report.acceptance_contract.digest() != acceptance.digest():
            raise ValueError("displayed measurement criteria differ from the acceptance contract")
        if report.acceptance is None or report.acceptance.contract_sha256 != acceptance.digest():
            raise ValueError("geometry evidence is not bound to the acceptance contract")
        if report.outcome == "passed" and acceptance_outcome(acceptance, report.acceptance.evidence) != "passed":
            raise ValueError("required acceptance evidence did not pass")
        checks = {c.check_id: c for c in acceptance.checks}
        # Historical contracts remain readable, but neither a legacy contract
        # nor an old worker may authorize an incompletely enumerated inventory.
        for row in report.acceptance.evidence:
            check = checks[row.check_id].model_dump(mode='json')
            if row.outcome == 'indeterminate' or not check['kind'].startswith('hole_') or check.get('hole_group'):
                continue
            from app.contracts.hole_populations import verify_inventory_coverage, inventory_values, digest
            inventory = row.details.get('hole_inventory') or {}
            # Counts have zero comparison tolerance; geometric coverage still
            # propagates the kernel's independent linear precision.
            verify_inventory_coverage(inventory, row.details.get('numerical_tolerance_mm'), check=check)
            if (row.details.get('hole_inventory_sha256') != digest(inventory)
                    or (check['kind'] != 'hole_position' and list(row.measured) != inventory_values(check, inventory))
                    or (check['kind'] != 'hole_count' and row.details.get('feature_count') != len(inventory['passages']))):
                raise ValueError('hole measurement differs from its independently certified inventory')
            if measurement_context is not None:
                selection = row.details['measurement_binding']['proof']['selection']
                if selection.get('hole_inventory_sha256') != digest(inventory):
                    raise ValueError('hole coverage differs from its bound measurement evidence')
        if acceptance.schema_version == 'engineering-acceptance.v2':
            from app.contracts.acceptance_comparison import compare_feature_measurement
            grouped = [c.model_dump(mode='json') for c in acceptance.checks if c.hole_group]
            if grouped:
                from app.contracts.hole_populations import population_pools, compare_hole_populations, inventory_values, digest
                pools = population_pools(grouped)
                seen_inventories = {}
            for row in report.acceptance.evidence:
                if measurement_context is not None and (row.outcome != 'indeterminate' or row.details.get('measurement_binding')):
                    from app.contracts.measurement_binding import verify_measurement_binding
                    verify_measurement_binding(row.details.get('measurement_binding'),
                        check=checks[row.check_id].model_dump(mode='json'), acceptance_digest=acceptance.digest(),
                        context=measurement_context, artifact_sha256s=[a.sha256 for a in report.artifacts if a.format=='step'])
                    if row.outcome in {'passed', 'failed'} and row.details['measurement_binding']['status'] != 'bound':
                        raise ValueError('a definite measurement requires a bound scope')
                    if row.details.get('feature_count') is not None:
                        selection = row.details['measurement_binding']['proof']['selection']
                        if any(selection.get(key) != row.details.get(key) for key in ('feature_count','feature_failure')):
                            raise ValueError('feature cardinality differs from its bound scope evidence')
                if row.outcome == 'indeterminate':
                    continue
                comparison = checks[row.check_id].comparison.model_dump(mode='json')
                if (row.details.get('comparison_policy_id') != acceptance.comparison_policy_id
                        or row.details.get('comparison') != comparison):
                    raise ValueError('v2 geometry evidence omitted or changed its frozen comparison')
                check = checks[row.check_id].model_dump(mode='json')
                if check.get('hole_group'):
                    pool_id = digest(check.get('scope') or {})
                    inventory = row.details.get('hole_inventory')
                    result = compare_hole_populations(pools[pool_id], inventory or {},
                        row.details['numerical_tolerance'], policy_id=acceptance.comparison_policy_id)
                    if (result != row.details.get('population_evaluation') or result['outcome'] != row.outcome
                            or list(row.measured) != inventory_values(check, inventory)
                            or row.details.get('feature_count') != result['feature_count']
                            or row.details.get('hole_inventory_sha256') != result['inventory_sha256']
                            or row.details.get('population_evaluation_sha256') != digest(result)):
                        raise ValueError('hole population evidence differs from the complete measured inventory')
                    if pool_id in seen_inventories and seen_inventories[pool_id] != result:
                        raise ValueError('one population scope must have one complete measured inventory')
                    seen_inventories[pool_id] = result
                    if measurement_context is not None:
                        selection = row.details['measurement_binding']['proof']['selection']
                        if any(selection.get(key) != row.details[key] for key in
                               ('hole_inventory_sha256', 'population_evaluation_sha256')):
                            raise ValueError('hole population inventory differs from its bound measurement evidence')
                    continue
                result = compare_feature_measurement(checks[row.check_id].model_dump(mode='json'),
                    row.measured, row.details['numerical_tolerance'], feature_count=row.details.get('feature_count'))
                if result['outcome'] != row.outcome:
                    raise ValueError('v2 geometry outcome differs from measured values')
                if result.get('feature_failure') != row.details.get('feature_failure'):
                    raise ValueError('v2 feature failure differs from measured cardinality')
    if expected_solid_count is not None and report.outcome == "passed":
        solids = [a for a in report.artifacts if a.format in {"step", "stl"}]
        if not solids or any(a.solid_count != expected_solid_count for a in solids):
            raise ValueError("final solid count did not pass")


def geometry_failure(report: DurableGeometryReport) -> dict[str, str | None]:
    """Map a failed gate into the existing bounded source-repair taxonomy."""
    return {
        "category": "validation",
        "error_code": "geometry_validation_failed",
        "error_message": "; ".join(report.issues)
        or "Geometry validation did not pass.",
        "runtime_error_type": "GeometryError",
    }


def indeterminate_geometry_report(
    *,
    outputs: tuple[dict, ...],
    expected_dimensions: dict[str, float],
    dimension_tolerance: float,
    issue: str,
) -> DurableGeometryReport:
    return DurableGeometryReport(
        schema_version="durable-geometry-report.v1",
        outcome="indeterminate",
        artifact_kind=(
            "profile"
            if outputs and all(item.get("format") == "dxf" for item in outputs)
            else "solid"
        ),
        expected_dimensions_mm=expected_dimensions,
        dimension_tolerance=dimension_tolerance,
        artifacts=tuple(
            GeometryArtifactEvidence(
                role=f"artifact-{index:02d}",
                filename=str(item["filename"]),
                format=str(item["format"]),
                sha256=str(item["sha256"]),
                size_bytes=int(item["size_bytes"]),
                parseable=False,
                valid=False,
                issues=(issue,),
            )
            for index, item in enumerate(outputs)
        ),
        issues=(issue,),
    )
