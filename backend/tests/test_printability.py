"""Printability gate tests — verify GeometryValidator makes success HONEST for 3D printing.

These run locally without Docker: trimesh generates synthetic meshes in-memory.
Covers the branches added for the tester-readiness fix:
  - watertight is now an ERROR (non-manifold mesh must FAIL, not pass as before)
  - build-volume cap (default 256mm) rejects oversized parts
  - printable verdict = watertight AND fits_build_volume
  - min-wall is advisory (warning, never blocks)
"""
import tempfile
from pathlib import Path

import numpy as np
import pytest
import trimesh

from app.config import settings
from app.validation.geometry_validator import GeometryValidator


def _save(mesh: trimesh.Trimesh) -> Path:
    f = tempfile.NamedTemporaryFile(suffix=".stl", delete=False)
    mesh.export(f.name)
    return Path(f.name)


def _rule(result, name):
    return next(r for r in result.rules if r.name == name)


@pytest.fixture
def validator():
    return GeometryValidator()


@pytest.mark.asyncio
async def test_clean_cube_is_printable(validator):
    """A small watertight cube fits the bed and passes every error rule."""
    mesh = trimesh.creation.box(extents=(20, 30, 40))
    result = await validator.validate(_save(mesh))

    assert result.is_watertight is True
    assert result.passed is True
    assert result.printable is True
    assert result.fits_build_volume is True
    assert _rule(result, "watertight").severity == "info"


@pytest.mark.asyncio
async def test_non_watertight_mesh_fails(validator):
    """REGRESSION GUARD: before the fix this PASSED (watertight was only a warning).
    A mesh with a hole must now fail and be flagged not-printable."""
    mesh = trimesh.creation.box(extents=(20, 20, 20))
    # Delete a face to break watertightness
    mesh.update_faces(np.arange(len(mesh.faces)) != 0)
    assert mesh.is_watertight is False  # sanity

    result = await validator.validate(_save(mesh))

    assert result.is_watertight is False
    assert result.passed is False          # watertight is now severity=error
    assert result.printable is False
    assert _rule(result, "watertight").severity == "error"
    assert any("水密" in w for w in result.print_warnings)


@pytest.mark.asyncio
async def test_oversized_part_exceeds_build_volume(validator):
    """A part larger than the configured build volume must fail the build_volume rule."""
    over = settings.build_volume_mm + 50
    mesh = trimesh.creation.box(extents=(over, 10, 10))
    result = await validator.validate(_save(mesh))

    assert result.fits_build_volume is False
    assert result.printable is False
    assert result.passed is False
    bv = _rule(result, "build_volume")
    assert bv.passed is False and bv.severity == "error"


@pytest.mark.asyncio
async def test_within_build_volume_passes(validator):
    """A part exactly at the build-volume boundary still fits."""
    mesh = trimesh.creation.box(extents=(settings.build_volume_mm - 1, 10, 10))
    result = await validator.validate(_save(mesh))
    assert result.fits_build_volume is True
    assert _rule(result, "build_volume").passed is True


@pytest.mark.asyncio
async def test_min_wall_is_advisory_not_blocking(validator):
    """A thin wall produces a WARNING but never blocks success (ray-cast is noisy)."""
    # 0.4mm-thick thin plate: well under the 0.8mm default min wall
    mesh = trimesh.creation.box(extents=(40, 40, 0.4))
    result = await validator.validate(_save(mesh))

    wall = _rule(result, "min_wall")
    # whatever the estimate, the rule must be warning/info severity — never "error"
    assert wall.severity in ("warning", "info")
    # a thin watertight box that fits the bed is still "printable" (wall is advisory)
    assert result.printable is True


@pytest.mark.asyncio
async def test_degenerate_tiny_volume_fails(validator):
    """A near-zero-volume watertight mesh trips the volume error rule."""
    mesh = trimesh.creation.box(extents=(0.01, 0.01, 0.01))
    result = await validator.validate(_save(mesh))
    vol_rule = _rule(result, "volume")
    assert vol_rule.passed is False and vol_rule.severity == "error"
    assert result.passed is False


@pytest.mark.asyncio
async def test_semantic_dimensions_are_not_treated_as_bounding_box_axes(validator):
    """Diameters describe features, not three independent XYZ box extents.

    A frustum/ring plan commonly contains exactly three numeric values such as
    outer diameter, inner diameter and thickness.  Their count alone must not
    turn them into a bounding-box contract.
    """
    mesh = trimesh.creation.cylinder(radius=15, height=25)
    result = await validator.validate(
        _save(mesh),
        expected_dimensions={
            "top_diameter": 30,
            "bottom_diameter": 18,
            "height": 25,
        },
    )

    expected = _rule(result, "expected_dimensions")
    assert expected.passed is True
    assert expected.severity == "info"
    assert "包围盒" in expected.message
    assert result.passed is True


@pytest.mark.asyncio
async def test_explicit_bounding_box_dimensions_still_reject_mismatch(validator):
    """The semantic-dimension fix must not weaken an explicit XYZ-size gate."""
    mesh = trimesh.creation.box(extents=(30, 30, 25))
    result = await validator.validate(
        _save(mesh),
        expected_dimensions={"width": 30, "depth": 18, "height": 25},
    )

    expected = _rule(result, "expected_dimensions")
    assert expected.passed is False
    assert expected.severity == "error"
    assert result.passed is False


@pytest.mark.asyncio
async def test_plate_length_width_thickness_are_bounding_box_axes(validator):
    """Plate-style axis names remain a strict three-dimensional contract."""
    mesh = trimesh.creation.box(extents=(90, 20, 3))
    result = await validator.validate(
        _save(mesh),
        expected_dimensions={"length": 90, "width": 20, "thickness": 3},
    )

    expected = _rule(result, "expected_dimensions")
    assert expected.passed is True
    assert "尺寸匹配" in expected.message
