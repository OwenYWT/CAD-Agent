"""Final geometry, not feature names, decides whether one-piece intent holds."""
import pytest
import trimesh

from app.validation.durable_geometry import DurableGeometryReport
from sandbox.geometry_validation import validate_geometry_files


def exported_solids(tmp_path, separated):
    first = trimesh.creation.box(extents=(24, 16, 3))
    shapes = [first]
    if separated:
        second = first.copy()
        second.apply_translation((0, 0, 4))
        shapes.append(second)
    path = tmp_path / "candidate.stl"
    trimesh.util.concatenate(shapes).export(path)
    return [{"role": "model", "format": "stl", "path": str(path)}]


def test_closed_disconnected_solids_cannot_pass_one_piece_acceptance(tmp_path):
    artifacts = exported_solids(tmp_path, separated=True)
    old = validate_geometry_files(artifacts)
    assert old["outcome"] == "passed"  # Reproduce the existing false positive.
    report = DurableGeometryReport.model_validate(
        validate_geometry_files(artifacts, expected_solid_count=1)
    )
    assert report.outcome == "failed"
    assert report.artifacts[0].solid_count == 2
    assert "model:solid_count_mismatch" in report.issues


@pytest.mark.parametrize("separated,count", [(False, 1), (True, 2)])
def test_declared_part_or_assembly_structure_passes(tmp_path, separated, count):
    report = validate_geometry_files(
        exported_solids(tmp_path, separated), expected_solid_count=count
    )
    assert DurableGeometryReport.model_validate(report).outcome == "passed"


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_invalid_acceptance_count_rejected_before_measurement(tmp_path, invalid):
    with pytest.raises(ValueError, match="positive integer"):
        validate_geometry_files(exported_solids(tmp_path, False), expected_solid_count=invalid)


def test_old_or_different_measurement_cache_cannot_authorize_new_gate(tmp_path):
    from app.contracts.geometry_request import geometry_request_digest
    from app.validation.durable_geometry import verify_geometry_evidence
    artifacts = exported_solids(tmp_path, False)
    digest = geometry_request_digest(expected_dimensions_mm={}, dimension_tolerance=0.05,
                                      expected_solid_count=1)
    old = DurableGeometryReport.model_validate(validate_geometry_files(artifacts))
    with pytest.raises(ValueError, match="different measurement"):
        verify_geometry_evidence(old, request_sha256=digest, acceptance=None, expected_solid_count=1)
    new = DurableGeometryReport.model_validate(validate_geometry_files(artifacts, expected_solid_count=1))
    verify_geometry_evidence(new, request_sha256=digest, acceptance=None, expected_solid_count=1)
    with pytest.raises(ValueError, match="solid count"):
        verify_geometry_evidence(new, request_sha256=digest, acceptance=None, expected_solid_count=2)


def test_legacy_wire_does_not_gain_new_fields(tmp_path):
    import json
    report=validate_geometry_files(exported_solids(tmp_path,False))
    assert DurableGeometryReport.model_validate(report).model_dump(mode="json")==json.loads(json.dumps(report))


def test_displayed_criteria_are_bound_to_the_measured_contract(tmp_path):
    from app.contracts.acceptance import AcceptanceContract
    from app.validation.durable_geometry import verify_geometry_evidence
    contract=AcceptanceContract.model_validate({'objective':'one solid','checks':[
        {'check_id':'one','kind':'solid_count','nominal':1,'description':'one','source_quote':'one solid'}]})
    # STL alone is not sufficient; even an indeterminate report must faithfully
    # expose the exact criteria, not unrelated or later edited requirements.
    raw=validate_geometry_files(exported_solids(tmp_path,False),acceptance=contract.model_dump(mode='json'))
    report=DurableGeometryReport.model_validate(raw)
    verify_geometry_evidence(report,request_sha256=raw['request_sha256'],acceptance=contract,expected_solid_count=None)
    raw['acceptance_contract']['checks'][0]['nominal']=2
    altered=DurableGeometryReport.model_validate(raw)
    with pytest.raises(ValueError,match='displayed measurement criteria'):
        verify_geometry_evidence(altered,request_sha256=raw['request_sha256'],acceptance=contract,expected_solid_count=None)
