"""Rendering and measurement: the properties the visual gate is built on.

Two of these are load-bearing for the durable gate rather than cosmetic:

* renders must be **deterministic**, because stored evidence hashes them;
* the model must be **opaque**, because the previous renderer's lack of a depth
  buffer is what made through-holes read as solid pillars.

No LLM and no Docker: this is trimesh, numpy and Pillow only.
"""
from __future__ import annotations

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from app.visual_refine.facts import measure
from app.visual_refine.mesh_views import ALL_VIEWS, STANDARD_VIEWS, render_views


def _plate_with_holes(tmp_path):
    """100x60x40 plate, four Ø6 through holes on Z, 60x30x12 pocket in the top."""
    box = trimesh.creation.box(extents=(100.0, 60.0, 40.0))
    box.apply_translation((0, 0, 20.0))
    cutters = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            bore = trimesh.creation.cylinder(radius=3.0, height=60.0, sections=48)
            bore.apply_translation((sx * 40.0, sy * 22.0, 20.0))
            cutters.append(bore)
    pocket = trimesh.creation.box(extents=(60.0, 30.0, 12.0))
    pocket.apply_translation((0, 0, 34.0))
    cutters.append(pocket)
    try:
        solid = box.difference(trimesh.util.concatenate(cutters))
    except Exception:  # no boolean backend installed
        pytest.skip("trimesh has no boolean backend (install manifold3d)")
    path = tmp_path / "plate.stl"
    solid.export(path)
    return path


# --- rendering ----------------------------------------------------------------


def test_renders_are_deterministic(tmp_path):
    """Stored visual evidence hashes these bytes; the same mesh must hash alike."""
    stl = _plate_with_holes(tmp_path)
    first = render_views(stl, tmp_path / "a", views=STANDARD_VIEWS)
    second = render_views(stl, tmp_path / "b", views=STANDARD_VIEWS)

    assert [view.sha256 for view in first] == [view.sha256 for view in second]
    assert all(view.size_bytes > 100 for view in first)


def test_orthographic_views_share_one_scale(tmp_path):
    """Per-view autoscaling makes every part look cubic and hides proportion errors."""
    stl = _plate_with_holes(tmp_path)
    rendered = {view.name: view for view in render_views(stl, tmp_path / "r")}

    scales = {rendered[name].scale_mm_per_px for name in ("front", "right", "top")}
    assert len(scales) == 1


def test_view_axes_are_the_conventional_ones(tmp_path):
    stl = _plate_with_holes(tmp_path)
    rendered = {view.name: view for view in render_views(stl, tmp_path / "r")}

    assert (rendered["front"].horizontal_axis, rendered["front"].vertical_axis) == (
        "+X",
        "+Z",
    )
    assert (rendered["right"].horizontal_axis, rendered["right"].vertical_axis) == (
        "+Y",
        "+Z",
    )
    assert (rendered["top"].horizontal_axis, rendered["top"].vertical_axis) == (
        "+X",
        "+Y",
    )
    # The isometric camera is oblique, so it reports no world-aligned axis and
    # never gets dimension labels a model could misread as true lengths.
    assert rendered["isometric"].horizontal_axis == "mixed"


def test_true_extents_are_reported_per_orthographic_view(tmp_path):
    stl = _plate_with_holes(tmp_path)
    rendered = {view.name: view for view in render_views(stl, tmp_path / "r")}

    assert rendered["front"].horizontal_mm == pytest.approx(100.0, abs=0.05)
    assert rendered["front"].vertical_mm == pytest.approx(40.0, abs=0.05)
    assert rendered["top"].vertical_mm == pytest.approx(60.0, abs=0.05)


def test_the_solid_renders_opaque(tmp_path):
    """The old renderer had no z-buffer, so back faces bled through the front.

    Looking at the front of a solid plate, every pixel of the body must come from
    the near face. If far geometry showed through, the body would contain the
    darker shades of back-facing surfaces.
    """
    from PIL import Image

    box = trimesh.creation.box(extents=(60.0, 40.0, 20.0))
    stl = tmp_path / "block.stl"
    box.export(stl)

    front = next(
        view
        for view in render_views(stl, tmp_path / "r", views=STANDARD_VIEWS)
        if view.name == "front"
    )
    with Image.open(front.path) as image:
        pixels = np.asarray(image.convert("RGB"))

    height, width = pixels.shape[:2]
    patch = pixels[height // 2 - 20 : height // 2 + 20, width // 2 - 20 : width // 2 + 20]
    # A flat lit face under one fixed light is a single colour. Any bleed-through
    # or transparency would introduce a second one.
    assert len(np.unique(patch.reshape(-1, 3), axis=0)) == 1


def test_section_view_exposes_interior(tmp_path):
    """Blind holes and cavities are invisible from outside; the cut shows them."""
    from PIL import Image

    stl = _plate_with_holes(tmp_path)
    rendered = {view.name: view for view in render_views(stl, tmp_path / "r", views=ALL_VIEWS)}
    assert rendered["section"].is_section is True

    with Image.open(rendered["section"].path) as image:
        pixels = np.asarray(image.convert("RGB")).reshape(-1, 3).astype(int)

    # Interior (back-facing) surfaces are rendered warm: red clearly above blue.
    warm = (pixels[:, 0] - pixels[:, 2]) > 30
    assert warm.sum() > 500


def test_a_mesh_with_no_triangles_is_rejected(tmp_path):
    empty = trimesh.Trimesh(vertices=np.zeros((0, 3)), faces=np.zeros((0, 3), dtype=int))
    with pytest.raises(ValueError, match="renderable"):
        render_views(empty, tmp_path / "r")


# --- measurement --------------------------------------------------------------


def test_bores_are_measured_not_guessed(tmp_path):
    stl = _plate_with_holes(tmp_path)
    facts = measure(stl)

    bores = [hole for hole in facts.holes if hole.kind == "hole"]
    assert len(bores) == 4
    assert all(hole.diameter == pytest.approx(6.0, abs=0.05) for hole in bores)
    assert all(hole.through for hole in bores)
    assert all(hole.axis == "Z" for hole in bores)
    assert sorted(
        (round(hole.center[0]), round(hole.center[1])) for hole in bores
    ) == [(-40, -22), (-40, 22), (40, -22), (40, 22)]


def test_bounding_box_and_topology_are_measured(tmp_path):
    stl = _plate_with_holes(tmp_path)
    facts = measure(stl)

    assert facts.bounding_box == pytest.approx((100.0, 60.0, 40.0), abs=0.01)
    assert facts.body_count == 1
    assert facts.is_watertight is True
    assert 0.0 < facts.fill_ratio < 1.0


def test_detached_geometry_is_counted_as_separate_bodies(tmp_path):
    """The failure mode the prompts worry about, settled by measurement."""
    box = trimesh.creation.box(extents=(40.0, 40.0, 40.0))
    floater = trimesh.creation.box(extents=(8.0, 8.0, 8.0))
    floater.apply_translation((60.0, 0.0, 0.0))
    stl = tmp_path / "detached.stl"
    trimesh.util.concatenate([box, floater]).export(stl)

    facts = measure(stl)
    assert facts.body_count == 2
    assert any("disconnected" in note for note in facts.notes)


def test_a_plain_block_reports_no_holes(tmp_path):
    """Fillets and tessellation must not be mistaken for bores."""
    stl = tmp_path / "plain.stl"
    trimesh.creation.box(extents=(30.0, 20.0, 10.0)).export(stl)

    assert measure(stl).holes == ()


def test_facts_survive_a_round_trip_through_the_worker_payload(tmp_path):
    """The worker measures and the control plane rebuilds; both must agree."""
    from app.visual_refine.facts import GeometryFacts

    stl = _plate_with_holes(tmp_path)
    original = measure(stl)
    restored = GeometryFacts.from_payload(original.as_dict())

    assert restored is not None
    assert restored.bounding_box == pytest.approx(original.bounding_box, abs=0.01)
    assert len(restored.holes) == len(original.holes)
    assert restored.body_count == original.body_count


def test_a_malformed_payload_yields_no_facts_rather_than_raising():
    from app.visual_refine.facts import GeometryFacts

    assert GeometryFacts.from_payload({"error": "render failed"}) is None
