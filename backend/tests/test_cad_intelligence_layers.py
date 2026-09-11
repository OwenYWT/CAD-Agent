from __future__ import annotations

from app.agent.design_brief import ensure_design_brief
from app.geometry_ir.planner import build_geometry_plan, render_geometry_plan
from app.geometry_ir.contracts import FeatureType
from app.models.schemas import CADPlan, CriticalDimension, DesignBrief
from app.topology.resolver import TopologyResolver
from app.validation.durable_geometry import (
    DurableGeometryReport,
    GeometryArtifactEvidence,
    GeometryBounds,
)
from app.validation.feature_evidence import (
    build_feature_evidence,
    build_feature_repair_context,
    summarize_feature_evidence,
)
from app.validation.verification.evaluator import (
    build_verification_targets,
    evaluate_verification_targets,
    summarize_verification_evidence,
    summarize_verification_targets,
)


def _sample_plan() -> CADPlan:
    plan = CADPlan(
        description="Bracket with two mounting holes",
        part_type="bracket",
        dimensions={"length": 80.0, "width": 20.0, "hole_diameter": 6.0},
        features=["base extrusion", "left mounting hole"],
        constraints=["hole diameter must be 6 mm"],
        design_brief=DesignBrief(
            intent_summary="Mounting bracket",
            artifact_type="bracket",
            critical_dimensions=[
                CriticalDimension(name="hole_diameter", value=6.0, unit="mm", reason="fit screw"),
                CriticalDimension(name="length", value=80.0, unit="mm", reason="overall size"),
            ],
            functional_requirements=["left mounting hole"],
            printability_targets=["封闭无破面"],
            acceptance_criteria=["模型应封闭无破面"],
        ),
    )
    ensure_design_brief(plan)
    return plan


def _sample_report() -> DurableGeometryReport:
    return DurableGeometryReport(
        schema_version="durable-geometry-report.v1",
        outcome="passed",
        artifact_kind="solid",
        expected_dimensions_mm={"hole_diameter": 6.0, "length": 80.0},
        dimension_tolerance=0.05,
        artifacts=(
            GeometryArtifactEvidence(
                role="artifact-00",
                filename="model.step",
                format="step",
                sha256="0" * 64,
                size_bytes=128,
                parseable=True,
                valid=True,
                solid_count=1,
                face_count=12,
                is_watertight=True,
                volume_mm3=2560.0,
                bounds_mm=GeometryBounds(
                    x_min=0.0,
                    x_max=80.0,
                    y_min=0.0,
                    y_max=20.0,
                    z_min=0.0,
                    z_max=8.0,
                ),
                dimensions_mm=(80.0, 20.0, 8.0),
                max_dimension_error=0.02,
                issues=(),
            ),
        ),
        issues=(),
    )


def test_geometry_ir_and_verification_targets_are_derived_from_plan():
    plan = _sample_plan()
    geometry_plan = build_geometry_plan(plan)
    targets = build_verification_targets(plan)

    assert geometry_plan.artifact_type == "bracket"
    assert geometry_plan.features[0].feature_type is FeatureType.EXTRUDE
    assert any(target.target_type.value == "hole_diameter" for target in targets)
    assert "Verification targets:" in render_geometry_plan(plan)


def test_verification_and_topology_layers_remain_conservative():
    plan = _sample_plan()
    targets = build_verification_targets(plan)
    report = _sample_report()

    evidence = evaluate_verification_targets(targets, report, evidence_ref="evidence-1")
    hole_target = next(item for item in targets if item.target_type.value == "hole_diameter")
    resolver = TopologyResolver()
    topology = resolver.resolve_target(hole_target, feature_id=hole_target.feature_id)

    assert any(item.outcome == "passed" for item in evidence)
    assert topology.selector is not None
    assert topology.selector.entity_type == "face"
    assert topology.resolution_state == "hinted"


def test_feature_evidence_and_repair_context_include_structured_data():
    plan = _sample_plan()
    geometry_plan = build_geometry_plan(plan)
    targets = build_verification_targets(plan)
    report = _sample_report()
    evidence = evaluate_verification_targets(targets, report, evidence_ref="evidence-1")

    feature_result = build_feature_evidence(
        step={"step_key": "model-main", "kind": "model"},
        generated={"source_hash": "a" * 64, "generator_kind": "generate"},
        executed={
            "staging_manifest_id": "manifest-1",
            "outputs": [{"object_key": "step-00", "format": "step", "sha256": "b" * 64}],
        },
        geometry_report=report,
        verification_evidence=evidence,
        topology_resolutions=(),
        geometry_plan=geometry_plan,
    )
    repair_context = build_feature_repair_context(
        geometry_plan=geometry_plan,
        verification_targets=targets,
        verification_evidence=evidence,
        topology_resolutions=(),
        feature_evidence=feature_result.evidence,
        geometry_report=report,
        expected_dimensions_mm={"hole_diameter": 6.0},
    )

    assert feature_result.evidence.valid is True
    assert "feature=model-main" in summarize_feature_evidence(feature_result.evidence)
    assert repair_context["feature_evidence"]["feature_id"] == "model-main"
    assert repair_context["geometry_plan"]["schema_version"] == "geometry-plan.v1"
    assert "hole_diameter" in summarize_verification_targets(targets)
    assert "passed" in summarize_verification_evidence(evidence)
