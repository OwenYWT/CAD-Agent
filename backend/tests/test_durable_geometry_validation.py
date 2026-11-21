"""Geometry gate contracts and deterministic sandbox analysis tests."""
from __future__ import annotations

import json

import ezdxf
import numpy as np
import pytest
import trimesh

from app.contracts.geometry_request import is_fused_assembly_preview
from app.validation.durable_geometry import (
    DurableGeometryReport,
    geometry_failure,
    indeterminate_geometry_report,
)
from sandbox.geometry_validation import (
    _acceptance_issue_strings,
    validate_geometry_files,
)


def _write_box(tmp_path, dimensions=(20, 10, 4)):
    stl = tmp_path / "box.stl"
    trimesh.creation.box(extents=dimensions).export(stl)
    return stl


def test_valid_stl_solid_passes_with_measured_dimensions(tmp_path):
    from sandbox.geometry_validation import _dimension_error
    assert _dimension_error((20, 10, 4), {'x': 20, 'z': 4}, world_axes=True) == 0
    assert _dimension_error((20, 10, 4), {'x': 10, 'y': 20}, world_axes=True) == 1
    stl = _write_box(tmp_path)
    report = validate_geometry_files(
        [{"role": "stl", "format": "stl", "path": str(stl)}],
        expected_dimensions={"length": 20, "width": 10, "height": 4},
    )
    validated = DurableGeometryReport.model_validate(report)
    assert validated.outcome == "passed"
    assert validated.artifact_kind == "solid"
    assert all(item.valid for item in validated.artifacts)
    assert validated.artifacts[0].solid_count == 1
    assert validated.artifacts[0].is_watertight is True


def test_touching_assembly_stl_preview_can_be_fused_when_step_keeps_two_solids():
    acceptance = {
        "checks": [
            {
                "kind": "assembly_predicate",
                "scope": {"assembly_measurement": "body_lid_fit"},
            }
        ]
    }

    assert is_fused_assembly_preview('stl', 2, 2, acceptance, {'modeling_backend': 'cadquery'})
    assert not is_fused_assembly_preview('stl', 2, 2, acceptance, {'modeling_backend': 'freecad'})


def test_fused_stl_preview_does_not_hide_missing_step_component():
    acceptance = {
        "checks": [
            {
                "kind": "assembly_predicate",
                "scope": {"assembly_measurement": "body_lid_fit"},
            }
        ]
    }

    assert not is_fused_assembly_preview('stl', 2, 1, acceptance, {'modeling_backend': 'cadquery'})


def test_acceptance_issue_strings_preserve_measurement_root_cause():
    acceptance = {
        "checks": [
            {"check_id": "assembly-envelope-length", "required": True},
            {"check_id": "body-lid-fit", "required": True},
        ]
    }
    evidence = [
        {
            "check_id": "assembly-envelope-length",
            "outcome": "indeterminate",
            "issues": [
                "measurement_error:ValueError:unclassified oblique planar boundary in rectangular assembly"
            ],
        },
        {"check_id": "body-lid-fit", "outcome": "passed", "issues": []},
    ]

    assert _acceptance_issue_strings(acceptance, evidence) == [
        "acceptance:assembly-envelope-length:measurement_error:ValueError:"
        "unclassified oblique planar boundary in rectangular assembly"
    ]


def test_stl_duplicate_triangle_is_normalized_without_filling_holes(tmp_path):
    original = trimesh.creation.box(extents=(20, 10, 4))
    mesh = trimesh.Trimesh(
        vertices=np.array(original.vertices),
        faces=np.vstack((original.faces, original.faces[0])),
        process=False,
    )
    path = tmp_path / "duplicate-face.stl"
    mesh.export(path)

    report = DurableGeometryReport.model_validate(
        validate_geometry_files(
            [{"role": "stl", "format": "stl", "path": str(path)}],
            expected_dimensions={"length": 20, "width": 10, "height": 4},
        )
    )

    assert report.outcome == "passed"
    assert report.artifacts[0].is_watertight is True


def test_valid_dxf_profile_passes_without_fake_solid_evidence(tmp_path):
    path = tmp_path / "profile.dxf"
    document = ezdxf.new(units=ezdxf.units.MM)
    document.modelspace().add_lwpolyline(
        [(0, 0), (20, 0), (20, 10), (0, 10)],
        close=True,
    )
    document.saveas(path)
    report = DurableGeometryReport.model_validate(
        validate_geometry_files(
            [{"role": "dxf", "format": "dxf", "path": str(path)}],
            expected_dimensions={"length": 20, "width": 10},
        )
    )
    assert report.outcome == "passed"
    assert report.artifact_kind == "profile"
    assert report.artifacts[0].solid_count is None
    assert report.artifacts[0].dimensions_mm == (20.0, 10.0)


@pytest.mark.parametrize("units", [0, 1, 5, 6])
def test_dxf_geometry_rejects_non_millimetre_coordinates(tmp_path, units):
    path = tmp_path / "wrong-units.dxf"
    document = ezdxf.new(units=units)
    document.modelspace().add_lwpolyline([(0, 0), (20, 0), (20, 10), (0, 10)], close=True)
    document.saveas(path)
    report = validate_geometry_files([{"role": "dxf", "format": "dxf", "path": str(path)}])
    assert report["outcome"] == "failed"
    assert any("dxf_units_not_millimetres" in issue for issue in report["issues"])


def test_non_watertight_and_dimension_mismatch_fail(tmp_path):
    mesh = trimesh.creation.box(extents=(20, 10, 4))
    mesh.update_faces(range(len(mesh.faces) - 1))
    broken = tmp_path / "broken.stl"
    mesh.export(broken)
    report = DurableGeometryReport.model_validate(
        validate_geometry_files(
            [{"role": "stl", "format": "stl", "path": str(broken)}],
            expected_dimensions={"length": 40, "width": 10, "height": 4},
        )
    )
    assert report.outcome == "failed"
    assert report.artifacts[0].is_watertight is False
    assert "stl:not_watertight" in report.issues
    assert "stl:dimension_mismatch" in report.issues


@pytest.mark.parametrize("artifact_format", ["step", "stl"])
def test_corrupt_geometry_is_indeterminate(artifact_format, tmp_path):
    path = tmp_path / f"corrupt.{artifact_format}"
    path.write_bytes(b"this is not a CAD artifact")
    report = DurableGeometryReport.model_validate(
        validate_geometry_files(
            [
                {
                    "role": artifact_format,
                    "format": artifact_format,
                    "path": str(path),
                }
            ]
        )
    )
    assert report.outcome == "indeterminate"
    assert report.artifacts[0].parseable is False
    assert any("parse_failed" in issue for issue in report.issues)


def test_required_evidence_preserves_runtime_and_maps_failed_gate_to_repair():
    report = indeterminate_geometry_report(
        outputs=(
            {
                "filename": "part.step",
                "format": "step",
                "sha256": "a" * 64,
                "size_bytes": 42,
            },
        ),
        expected_dimensions={"length": 20},
        dimension_tolerance=0.05,
        issue="object_integrity_mismatch",
    )
    evidence = report.durable_evidence(
        runtime_provenance={"image_digest": "sha256:" + "b" * 64}
    )
    assert report.outcome == "indeterminate"
    assert evidence["runtime_provenance"]["image_digest"].startswith("sha256:")

    failed = DurableGeometryReport.model_validate(
        {
            **json.loads(report.model_dump_json()),
            "outcome": "failed",
        }
    )
    failure = geometry_failure(failed)
    assert failure["category"] == "validation"
    assert failure["runtime_error_type"] == "GeometryError"
