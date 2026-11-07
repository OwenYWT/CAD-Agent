import pytest

from app.models.schemas import (
    CADPlan,
    BoundingBox,
    GenerateResponse,
    InspectCheck,
    InspectReport,
    ValidationResult,
)
from benchmark import metrics
from benchmark.cad_tests.evidence import normalize_benchmark_row
from benchmark.eval import sha256_file


pytestmark = pytest.mark.unit


def _bbox(dx, dy, dz):
    return BoundingBox(x_min=0, x_max=dx, y_min=0, y_max=dy, z_min=0, z_max=dz)


def _response(**kwargs):
    payload = {"request_id": "request-1", "success": True, "attempts": 1}
    payload.update(kwargs)
    return GenerateResponse(**payload)


def test_normalize_benchmark_row_preserves_measured_geometry_evidence():
    response = _response(
        validation=ValidationResult(
            is_watertight=True,
            printable=True,
            bounding_box=_bbox(10, 20, 30),
        ),
        inspect_report=InspectReport(
            verdict="pass",
            printable=True,
            is_watertight=True,
            bounding_box=_bbox(10, 20, 30),
            checks=[InspectCheck(name="dfm", status="pass", message="ok", source="dfm")],
        ),
    )
    case = {
        "id": "P01",
        "description": "make a 10 x 20 x 30 mm part",
        "difficulty": "simple",
        "path": "extrude_cut",
        "expected_dims": {"width": 10, "height": 20, "depth": 30},
    }
    row = metrics.extract_metrics(response, case)

    context = normalize_benchmark_row(case, row)

    assert context.observations["bbox_extents_mm"] == [10.0, 20.0, 30.0]
    assert context.observations["watertight"] is True
    assert context.observations["printable"] is True
    assert context.observations["solid_count"] is None
    assert context.observations["valid_solid"] is None
    bbox_evidence = next(item for item in context.evidence if item.source_kind == "geometry_bbox")
    assert bbox_evidence.authority == "independent_geometry"


def test_normalize_benchmark_row_does_not_infer_solid_count_from_planner_fields():
    response = _response(
        plan=CADPlan(
            description="one cube",
            part_type="box",
            dimensions={},
            features=["solid:count=1"],
        ),
        validation=ValidationResult(
            is_watertight=True,
            printable=True,
            bounding_box=_bbox(10, 10, 10),
        ),
    )
    case = {"id": "P01", "description": "one cube", "difficulty": "simple", "path": "extrude_cut"}
    row = metrics.extract_metrics(response, case)

    context = normalize_benchmark_row(case, row)

    assert context.observations["solid_count"] is None
    assert context.reasons["solid_count"] == "authoritative solid-count evidence unavailable"


def test_sha256_file_returns_digest_only_for_existing_file(tmp_path):
    artifact = tmp_path / "result.step"
    artifact.write_bytes(b"cad-artifact")

    digest = sha256_file(artifact)

    assert digest == "aca598682e36174dcdb321f2bd9ed119c9543062456eddcb1ce622cf28a75033"
    assert sha256_file(tmp_path / "missing.step") is None


def test_normalize_missing_bbox_has_no_zero_measurement():
    response = _response(validation=ValidationResult(is_watertight=False, printable=None))
    case = {"id": "P01", "description": "part", "difficulty": "simple", "path": "extrude_cut"}
    row = metrics.extract_metrics(response, case)

    context = normalize_benchmark_row(case, row)

    assert context.observations["bbox_extents_mm"] is None
    assert "bbox_extents_mm" in context.reasons
