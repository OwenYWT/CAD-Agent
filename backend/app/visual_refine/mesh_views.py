"""Deterministic, depth-buffered multi-view mesh rendering with measurement overlays.

Why this module exists
----------------------
A vision model can only judge what the render actually shows. The previous
matplotlib ``Poly3DCollection`` renderer had two defects that put a hard ceiling
on visual-gate accuracy:

1. No depth buffer. Matplotlib sorts whole collections, not fragments, so solids
   came out semi-transparent: back faces bled through, through-holes read as
   solid pillars and pockets read as floating frames.
2. No scale. The subject was framed inside a cubic radius, so a 100x60x40 part
   filled a fraction of the canvas with nothing in the image tying pixels to
   millimetres. "Is this 35 mm across?" was unanswerable.

This module renders with a real per-fragment z-buffer, frames each view on its
own projected extents, and burns dimensions, a millimetre grid and a scale bar
into the image. The judgement stops being a vibe check and becomes a reading.

Boundaries
----------
Dependency floor is numpy + trimesh + Pillow and nothing else. There are no
``app.*`` imports on purpose: the identical module runs inside the isolated MCAD
worker (where no application package exists) and in the control plane, so both
sides of the durable visual gate see byte-identical renders.

Rendering is deterministic: fixed camera basis, fixed light, fixed palette,
integer pixel math and Pillow's bundled bitmap font. The same mesh always
produces the same PNG bytes, which is what lets the durable gate hash renders
as evidence.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

# Palette. Front faces read as a machined neutral; back faces (interior walls
# seen through a section cut, or a non-watertight defect) read warm so the two
# are never confused with each other.
_BACKGROUND = (248, 250, 252)
_FRONT_BASE = np.array([0.44, 0.53, 0.62])
_BACK_BASE = np.array([0.78, 0.55, 0.36])
_EDGE_COLOR = (34, 45, 58)
_ANNOTATION_COLOR = (17, 24, 39)
_GRID_COLOR = (214, 221, 230)
_GRID_MAJOR_COLOR = (178, 190, 204)

_AMBIENT = 0.42
_DIFFUSE = 0.58

# A view is framed on its own projected extents plus this fraction of margin, so
# the subject fills the canvas instead of floating in it.
_FRAME_MARGIN = 0.14
# Space reserved around the drawing area. The bottom carries the scale bar, the
# bounding box and the objective footer, so it is deeper than the rest.
_GUTTER_TOP = 62
_GUTTER_SIDE = 76
_GUTTER_BOTTOM = 132


@dataclass(frozen=True)
class ViewSpec:
    """One camera. ``elevation``/``azimuth`` are degrees on the unit sphere.

    ``section`` cuts the model with a plane through its centre and discards
    everything between the plane and the camera, exposing internal cavities and
    blind holes that no external view can show.
    """

    name: str
    elevation: float
    azimuth: float
    section: bool = False
    section_normal: tuple[float, float, float] = (0.0, -1.0, 0.0)
    label: str = ""

    def display_label(self) -> str:
        return self.label or self.name.replace("_", " ").upper()


# The four names below are a durable contract: VisualRenderEvidence pins them and
# stored evidence is replayed against them. Add views, never rename these.
STANDARD_VIEWS: tuple[ViewSpec, ...] = (
    ViewSpec("front", elevation=0.0, azimuth=-90.0, label="FRONT (from -Y)"),
    ViewSpec("right", elevation=0.0, azimuth=0.0, label="RIGHT (from +X)"),
    ViewSpec("top", elevation=90.0, azimuth=0.0, label="TOP (from +Z)"),
    ViewSpec("isometric", elevation=35.264, azimuth=45.0, label="ISOMETRIC"),
)

# Optional fifth view. It is not part of the four-render durable contract; it is
# extra evidence for shells, pockets and blind holes, which are invisible from
# outside and are exactly where visual gates historically passed bad models.
SECTION_VIEW = ViewSpec(
    "section",
    elevation=18.0,
    azimuth=-60.0,
    section=True,
    section_normal=(0.0, -1.0, 0.0),
    label="SECTION (cut through centre, Y=0)",
)

ALL_VIEWS: tuple[ViewSpec, ...] = STANDARD_VIEWS + (SECTION_VIEW,)


@dataclass(frozen=True)
class RenderedView:
    """One written PNG plus the facts needed to read it as a measurement."""

    name: str
    path: Path
    width: int
    height: int
    sha256: str
    size_bytes: int
    # Millimetres per pixel in the drawing area. Shared across the orthographic
    # views so their sizes are directly comparable by eye.
    scale_mm_per_px: float
    horizontal_axis: str
    vertical_axis: str
    horizontal_mm: float
    vertical_mm: float
    is_section: bool

    def evidence(self) -> dict[str, Any]:
        """Project to the durable ``VisualRenderEvidence`` field set."""
        return {
            "view": self.name,
            "filename": self.path.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class _Camera:
    """Orthographic camera basis in world space."""

    forward: np.ndarray  # unit vector from the model toward the camera
    right: np.ndarray  # screen +x
    up: np.ndarray  # screen +y

    @classmethod
    def from_view(cls, view: ViewSpec) -> "_Camera":
        elevation = math.radians(view.elevation)
        azimuth = math.radians(view.azimuth)
        forward = np.array(
            [
                math.cos(elevation) * math.cos(azimuth),
                math.cos(elevation) * math.sin(azimuth),
                math.sin(elevation),
            ],
            dtype=float,
        )
        forward /= np.linalg.norm(forward)
        # Looking straight down +Z leaves world-up degenerate; fall back to +Y so
        # the top view keeps screen-x = +X and screen-y = +Y.
        world_up = np.array([0.0, 0.0, 1.0])
        if abs(float(forward[2])) > 0.999:
            world_up = np.array([0.0, 1.0, 0.0])
        right = np.cross(-forward, world_up)
        right /= np.linalg.norm(right)
        up = np.cross(right, -forward)
        up /= np.linalg.norm(up)
        return cls(forward=forward, right=right, up=up)

    def axis_names(self) -> tuple[str, str]:
        return _axis_name(self.right), _axis_name(self.up)


def _axis_name(vector: np.ndarray) -> str:
    """Name a screen axis when it is aligned with a world axis, else 'mixed'."""
    labels = ("X", "Y", "Z")
    for index in range(3):
        component = float(vector[index])
        if abs(component) > 0.999:
            return f"{'+' if component > 0 else '-'}{labels[index]}"
    return "mixed"


def _nice_step(span_mm: float, target_divisions: int = 10) -> float:
    """Round a grid interval to 1/2/5 x 10^n so labels stay readable."""
    if span_mm <= 0:
        return 1.0
    raw = span_mm / max(target_divisions, 1)
    exponent = math.floor(math.log10(raw)) if raw > 0 else 0
    magnitude = 10.0**exponent
    for multiple in (1.0, 2.0, 5.0):
        if raw <= multiple * magnitude:
            return multiple * magnitude
    return 10.0 * magnitude


def load_mesh(source: Any):
    """Accept a path or an in-memory mesh/scene and return one concrete Trimesh."""
    import trimesh

    mesh = source
    if isinstance(source, (str, Path)):
        path = Path(source)
        mesh_path = path
        if path.suffix.lower() in {".step", ".stp"}:
            import cadquery as cq

            mesh_path = path.with_suffix(".render.stl")
            cq.exporters.export(
                cq.importers.importStep(str(path)),
                str(mesh_path),
                exportType="STL",
            )
        mesh = trimesh.load_mesh(mesh_path, force="mesh")
    if hasattr(mesh, "dump"):  # a Scene, e.g. a CadQuery assembly export
        mesh = mesh.dump(concatenate=True)
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty or not len(mesh.faces):
        raise ValueError("mesh contains no renderable triangles")
    return mesh


def _rasterize(
    triangles: np.ndarray,
    depths: np.ndarray,
    colors: np.ndarray,
    normals: np.ndarray,
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Scanline-fill triangles into a colour buffer under a per-pixel z-test.

    ``triangles`` is (n, 3, 2) in pixel coordinates, ``depths`` is (n, 3) with
    larger values nearer the camera, ``colors`` and ``normals`` are (n, 3).

    Returns the colour buffer, the depth buffer (-inf where nothing was drawn)
    and a per-pixel normal buffer. Those last two are what image-space edge
    detection reads, which is why hidden-line removal comes out exact and free.
    """
    color_buffer = np.zeros((height, width, 3), dtype=np.float32)
    depth_buffer = np.full((height, width), -np.inf, dtype=np.float32)
    normal_buffer = np.zeros((height, width, 3), dtype=np.float32)
    if not len(triangles):
        return color_buffer, depth_buffer, normal_buffer

    xs = triangles[:, :, 0]
    ys = triangles[:, :, 1]
    min_x = np.clip(np.floor(xs.min(axis=1)), 0, width - 1).astype(np.int32)
    max_x = np.clip(np.ceil(xs.max(axis=1)), 0, width - 1).astype(np.int32)
    min_y = np.clip(np.floor(ys.min(axis=1)), 0, height - 1).astype(np.int32)
    max_y = np.clip(np.ceil(ys.max(axis=1)), 0, height - 1).astype(np.int32)

    x0, y0 = xs[:, 0], ys[:, 0]
    x1, y1 = xs[:, 1], ys[:, 1]
    x2, y2 = xs[:, 2], ys[:, 2]
    area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    # A degenerate triangle has no interior to fill and would divide by zero.
    # Anything fully off-canvas is skipped before the per-triangle loop.
    drawable = (np.abs(area) > 1e-12) & (max_x >= min_x) & (max_y >= min_y)

    for index in np.nonzero(drawable)[0]:
        px_lo, px_hi = int(min_x[index]), int(max_x[index])
        py_lo, py_hi = int(min_y[index]), int(max_y[index])
        pixel_x = (np.arange(px_lo, px_hi + 1, dtype=np.float32) + 0.5)[None, :]
        pixel_y = (np.arange(py_lo, py_hi + 1, dtype=np.float32) + 0.5)[:, None]

        inv_area = 1.0 / area[index]
        weight_1 = (
            (pixel_x - x0[index]) * (y2[index] - y0[index])
            - (pixel_y - y0[index]) * (x2[index] - x0[index])
        ) * inv_area
        weight_2 = (
            (pixel_y - y0[index]) * (x1[index] - x0[index])
            - (pixel_x - x0[index]) * (y1[index] - y0[index])
        ) * inv_area
        weight_0 = 1.0 - weight_1 - weight_2
        inside = (weight_0 >= -1e-6) & (weight_1 >= -1e-6) & (weight_2 >= -1e-6)
        if not inside.any():
            continue

        fragment_depth = (
            weight_0 * depths[index, 0]
            + weight_1 * depths[index, 1]
            + weight_2 * depths[index, 2]
        ).astype(np.float32)
        window_depth = depth_buffer[py_lo : py_hi + 1, px_lo : px_hi + 1]
        winners = inside & (fragment_depth > window_depth)
        if not winners.any():
            continue
        window_depth[winners] = fragment_depth[winners]
        color_buffer[py_lo : py_hi + 1, px_lo : px_hi + 1][winners] = colors[index]
        normal_buffer[py_lo : py_hi + 1, px_lo : px_hi + 1][winners] = normals[index]

    return color_buffer, depth_buffer, normal_buffer


def _detect_edges(
    depth_buffer: np.ndarray,
    normal_buffer: np.ndarray,
    depth_tolerance: float,
) -> np.ndarray:
    """Find silhouettes and creases in image space.

    Working from the resolved buffers instead of from mesh topology means hidden
    line removal is free and exact: a line can only be drawn where its own
    fragment survived the depth test. Tessellation diagonals inside a flat face
    share a normal and lie on one depth plane, so they never become lines --
    important, because invented diagonals read to a vision model as real
    chamfers, which is a defect the previous renderer had.
    """
    filled = depth_buffer > -np.inf
    edges = np.zeros(depth_buffer.shape, dtype=bool)

    # Empty pixels hold -inf; differencing them yields NaN, so compare on a
    # finite copy and let the coverage masks decide what counts.
    finite_depth = np.where(filled, depth_buffer, 0.0)

    for shift_y, shift_x in ((0, 1), (1, 0), (1, 1), (1, -1)):
        shifted_filled = np.roll(np.roll(filled, shift_y, 0), shift_x, 1)
        both = filled & shifted_filled
        silhouette = filled & ~shifted_filled

        shifted_depth = np.roll(np.roll(finite_depth, shift_y, 0), shift_x, 1)
        depth_jump = both & (np.abs(finite_depth - shifted_depth) > depth_tolerance)

        shifted_normal = np.roll(np.roll(normal_buffer, shift_y, 0), shift_x, 1)
        alignment = np.sum(normal_buffer * shifted_normal, axis=2)
        crease = both & (alignment < 0.985)  # about 10 degrees

        edges |= silhouette | depth_jump | crease

    # np.roll wraps around; the wrapped border is not a real edge.
    edges[0, :] = edges[-1, :] = edges[:, 0] = edges[:, -1] = False
    return edges


def _shade(normals: np.ndarray, facing: np.ndarray, camera: _Camera) -> np.ndarray:
    """Lambert shading with the key light fixed to the camera basis.

    Anchoring the light to the camera rather than to the world guarantees every
    view is lit the same way, so a vision model cannot mistake a dark view for a
    dark feature.
    """
    light = 0.32 * camera.right + 0.38 * camera.up + 0.87 * camera.forward
    light /= np.linalg.norm(light)
    # Back faces are lit by their reversed normal so interior walls stay legible.
    oriented = np.where(facing[:, None], normals, -normals)
    intensity = _AMBIENT + _DIFFUSE * np.clip(oriented @ light, 0.0, 1.0)
    base = np.where(facing[:, None], _FRONT_BASE[None, :], _BACK_BASE[None, :])
    return np.clip(base * intensity[:, None], 0.0, 1.0)


def _font(size: int):
    from PIL import ImageFont

    # Pillow's bundled bitmap font is version-stable and always present, which
    # keeps renders reproducible across hosts. A system TrueType face would make
    # the same mesh hash differently on a different machine.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 takes no size argument
        return ImageFont.load_default()


def _text_size(draw: Any, text: str, font: Any) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _format_mm(value: float) -> str:
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _draw_grid(
    draw: Any,
    *,
    origin_px: tuple[float, float],
    plane_min: tuple[float, float],
    plane_max: tuple[float, float],
    scale: float,
    area: tuple[int, int, int, int],
    step_mm: float,
) -> None:
    """Draw a millimetre grid so distances can be counted rather than guessed."""
    left, top, right, bottom = area
    origin_x, origin_y = origin_px

    for axis in (0, 1):
        first = math.ceil(plane_min[axis] / step_mm)
        last = math.floor(plane_max[axis] / step_mm)
        # A pathological scale could ask for millions of lines; the grid is a
        # reading aid, not a reason to hang the render.
        if last - first > 400:
            continue
        for tick in range(first, last + 1):
            color = _GRID_MAJOR_COLOR if tick % 5 == 0 else _GRID_COLOR
            if axis == 0:
                x_px = origin_x + tick * step_mm * scale
                if left <= x_px <= right:
                    draw.line([(x_px, top), (x_px, bottom)], fill=color, width=1)
            else:
                y_px = origin_y - tick * step_mm * scale
                if top <= y_px <= bottom:
                    draw.line([(left, y_px), (right, y_px)], fill=color, width=1)


def _draw_dimension(
    draw: Any,
    font: Any,
    *,
    start: tuple[float, float],
    end: tuple[float, float],
    text: str,
    horizontal: bool,
    canvas_width: int,
) -> None:
    """Draw one witness-line dimension with arrowheads and a millimetre label."""
    draw.line([start, end], fill=_ANNOTATION_COLOR, width=1)
    arrow = 4
    if horizontal:
        for point, direction in ((start, 1), (end, -1)):
            draw.polygon(
                [
                    point,
                    (point[0] + direction * arrow * 2, point[1] - arrow),
                    (point[0] + direction * arrow * 2, point[1] + arrow),
                ],
                fill=_ANNOTATION_COLOR,
            )
    else:
        for point, direction in ((start, 1), (end, -1)):
            draw.polygon(
                [
                    point,
                    (point[0] - arrow, point[1] + direction * arrow * 2),
                    (point[0] + arrow, point[1] + direction * arrow * 2),
                ],
                fill=_ANNOTATION_COLOR,
            )

    text_width, text_height = _text_size(draw, text, font)
    if horizontal:
        anchor = (
            (start[0] + end[0]) / 2 - text_width / 2,
            start[1] - text_height - 5,
        )
    else:
        # Prefer the outside of the witness line, but flip inward rather than
        # letting the label run off the canvas and lose its digits.
        anchor_x = start[0] + 7
        if anchor_x + text_width > canvas_width - 4:
            anchor_x = start[0] - text_width - 7
        anchor = (anchor_x, (start[1] + end[1]) / 2 - text_height / 2)
    draw.rectangle(
        [
            anchor[0] - 2,
            anchor[1] - 2,
            anchor[0] + text_width + 2,
            anchor[1] + text_height + 2,
        ],
        fill=_BACKGROUND,
    )
    draw.text(anchor, text, fill=_ANNOTATION_COLOR, font=font)


def _draw_scale_bar(
    draw: Any,
    font: Any,
    *,
    scale: float,
    area: tuple[int, int, int, int],
    step_mm: float,
) -> None:
    left, _, right, bottom = area
    length_px = step_mm * 5 * scale
    while length_px > (right - left) * 0.45 and step_mm > 1e-6:
        step_mm /= 2.0
        length_px = step_mm * 5 * scale
    if length_px < 12:
        return
    bar_y = bottom + 20
    bar_x = left
    draw.line(
        [(bar_x, bar_y), (bar_x + length_px, bar_y)],
        fill=_ANNOTATION_COLOR,
        width=2,
    )
    for offset in (0.0, length_px):
        draw.line(
            [(bar_x + offset, bar_y - 4), (bar_x + offset, bar_y + 4)],
            fill=_ANNOTATION_COLOR,
            width=2,
        )
    draw.text(
        (bar_x + length_px + 7, bar_y - 6),
        f"{_format_mm(step_mm * 5)} mm",
        fill=_ANNOTATION_COLOR,
        font=font,
    )


def _annotate(
    image: Any,
    *,
    view: ViewSpec,
    camera: _Camera,
    scale: float,
    plane_min: np.ndarray,
    plane_max: np.ndarray,
    offset: tuple[float, float],
    area: tuple[int, int, int, int],
    extents: np.ndarray,
    grid_step: float,
    footer: Sequence[str],
) -> None:
    """Burn view name, dimensions, grid origin and scale bar into the render."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    title_font = _font(15)
    label_font = _font(13)
    small_font = _font(11)
    left, top, right, bottom = area

    horizontal_axis, vertical_axis = camera.axis_names()
    # Only a world-aligned camera measures true lengths. On an oblique view the
    # projected span is foreshortened, so labelling it "100 mm" would teach the
    # vision model to read a wrong number off the picture.
    measurable = horizontal_axis != "mixed" and vertical_axis != "mixed"

    draw.text((left, 8), view.display_label(), fill=_ANNOTATION_COLOR, font=title_font)
    axis_note = (
        f"screen right = {horizontal_axis}   screen up = {vertical_axis}"
        f"   |   1 px = {_format_mm(1.0 / scale)} mm"
        if measurable
        else "oblique projection - lengths are foreshortened, read the bbox below"
    )
    draw.text((left, 28), axis_note, fill=(71, 85, 105), font=small_font)

    if measurable:
        model_left = offset[0]
        model_top = offset[1]
        span_x = float(plane_max[0] - plane_min[0])
        span_y = float(plane_max[1] - plane_min[1])
        if span_x > 0:
            _draw_dimension(
                draw,
                label_font,
                start=(model_left, top - 14),
                end=(model_left + span_x * scale, top - 14),
                text=f"{horizontal_axis[1]} = {_format_mm(span_x)} mm",
                horizontal=True,
                canvas_width=image.size[0],
            )
        if span_y > 0:
            _draw_dimension(
                draw,
                label_font,
                start=(right + 14, model_top),
                end=(right + 14, model_top + span_y * scale),
                text=f"{vertical_axis[1]} = {_format_mm(span_y)} mm",
                horizontal=False,
                canvas_width=image.size[0],
            )
        _draw_scale_bar(draw, small_font, scale=scale, area=area, step_mm=grid_step)

    cursor = bottom + (40 if measurable else 14)
    box_text = (
        f"overall bounding box:  X {_format_mm(float(extents[0]))} x "
        f"Y {_format_mm(float(extents[1]))} x "
        f"Z {_format_mm(float(extents[2]))} mm"
    )
    draw.text((left, cursor), box_text, fill=_ANNOTATION_COLOR, font=small_font)
    cursor += 15
    if measurable:
        draw.text(
            (left, cursor),
            f"background grid = {_format_mm(grid_step)} mm squares",
            fill=(71, 85, 105),
            font=small_font,
        )
        cursor += 15
    for line in footer:
        if cursor > image.size[1] - 14:
            break
        draw.text((left, cursor), line, fill=(71, 85, 105), font=small_font)
        cursor += 14


def render_views(
    source: Any,
    output_dir: Path,
    *,
    views: Sequence[ViewSpec] = STANDARD_VIEWS,
    width: int = 768,
    height: int = 768,
    supersample: int = 2,
    annotate: bool = True,
    footer: Sequence[str] = (),
) -> list[RenderedView]:
    """Render ``views`` of ``source`` into ``output_dir`` as annotated PNGs.

    All orthographic views share one millimetre-per-pixel scale, so a part that
    is twice as long as it is tall looks twice as long as it is tall across the
    view set. Without that, per-view autoscaling makes every part look cubic and
    proportion errors become invisible.
    """
    from PIL import Image

    if width < 128 or height < 128 or width > 2048 or height > 2048:
        raise ValueError("render dimensions must be between 128 and 2048")
    if supersample < 1 or supersample > 4:
        raise ValueError("supersample must be between 1 and 4")

    mesh = load_mesh(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    centred = mesh.copy()
    centred.apply_translation(-centred.bounding_box.centroid)
    extents = np.asarray(centred.bounding_box.extents, dtype=float)

    area_width = width - 2 * _GUTTER_SIDE
    area_height = height - _GUTTER_TOP - _GUTTER_BOTTOM
    if area_width < 64 or area_height < 64:
        raise ValueError("render dimensions leave no room for annotations")
    drawing_area = (
        _GUTTER_SIDE,
        _GUTTER_TOP,
        width - _GUTTER_SIDE,
        height - _GUTTER_BOTTOM,
    )

    # One shared scale for the orthographic views. The isometric and section
    # cameras see a longer diagonal, so they get their own fit; they are read for
    # shape, while the orthographic trio carries the measurements.
    orthographic = [view for view in views if not view.section and view.name != "isometric"]
    shared_scale: float | None = None
    if orthographic:
        worst_x = 0.0
        worst_y = 0.0
        for view in orthographic:
            camera = _Camera.from_view(view)
            plane, _, _, _ = _project(centred, camera)
            flat = plane.reshape(-1, 2)
            worst_x = max(worst_x, float(flat[:, 0].max() - flat[:, 0].min()))
            worst_y = max(worst_y, float(flat[:, 1].max() - flat[:, 1].min()))
        span_x = worst_x * (1.0 + 2 * _FRAME_MARGIN) or 1.0
        span_y = worst_y * (1.0 + 2 * _FRAME_MARGIN) or 1.0
        shared_scale = min(area_width / span_x, area_height / span_y)

    results: list[RenderedView] = []
    for view in views:
        camera = _Camera.from_view(view)
        buffer_scale = supersample
        raster_width = width * buffer_scale
        raster_height = height * buffer_scale

        plane, depth, corners, face_normals = _project(centred, camera)
        facing = (face_normals @ camera.forward) > 0.0

        keep = np.ones(len(plane), dtype=bool)
        if view.section:
            normal = np.asarray(view.section_normal, dtype=float)
            normal /= np.linalg.norm(normal)
            # Drop every triangle on the camera side of the cut plane. What is
            # left is the far half plus the interior walls now facing the lens.
            keep = (corners @ normal).max(axis=1) <= 1e-9
            if not keep.any():
                keep = np.ones(len(plane), dtype=bool)

        plane = plane[keep]
        depth = depth[keep]
        face_normals = face_normals[keep]
        facing = facing[keep]

        flat = plane.reshape(-1, 2)
        plane_min = flat.min(axis=0)
        plane_max = flat.max(axis=0)
        span = np.maximum(plane_max - plane_min, 1e-6)
        if shared_scale is not None and not view.section and view.name != "isometric":
            scale = shared_scale
        else:
            padded = span * (1.0 + 2 * _FRAME_MARGIN)
            scale = min(area_width / padded[0], area_height / padded[1])

        # Centre the subject inside the drawing area.
        offset_x = _GUTTER_SIDE + (area_width - span[0] * scale) / 2.0
        offset_y = _GUTTER_TOP + (area_height - span[1] * scale) / 2.0

        pixel_x = (plane[:, :, 0] - plane_min[0]) * scale + offset_x
        # Screen y grows downward; the world plane grows upward.
        pixel_y = (plane_max[1] - plane[:, :, 1]) * scale + offset_y
        triangles = np.stack([pixel_x, pixel_y], axis=2) * buffer_scale

        colors = _shade(face_normals, facing, camera).astype(np.float32)
        color_buffer, depth_buffer, normal_buffer = _rasterize(
            triangles,
            depth.astype(np.float32),
            colors,
            face_normals.astype(np.float32),
            raster_width,
            raster_height,
        )

        model_depth = float(depth.max() - depth.min()) or 1.0
        edges = _detect_edges(depth_buffer, normal_buffer, model_depth * 0.012)

        rgb = np.empty((raster_height, raster_width, 3), dtype=np.uint8)
        rgb[:] = np.array(_BACKGROUND, dtype=np.uint8)
        filled = depth_buffer > -np.inf
        rgb[filled] = np.clip(color_buffer[filled] * 255.0, 0, 255).astype(np.uint8)
        rgb[edges] = np.array(_EDGE_COLOR, dtype=np.uint8)

        image = Image.fromarray(rgb, mode="RGB")
        if buffer_scale > 1:
            image = image.resize((width, height), Image.LANCZOS)

        grid_step = _nice_step(max(float(span[0]), float(span[1])))
        horizontal_axis, vertical_axis = camera.axis_names()
        measurable = horizontal_axis != "mixed" and vertical_axis != "mixed"
        if annotate:
            # A millimetre grid under an oblique projection measures nothing, so
            # it is drawn only where screen axes are world axes.
            if measurable:
                from PIL import ImageDraw

                grid_layer = Image.new("RGB", image.size, _BACKGROUND)
                _draw_grid(
                    ImageDraw.Draw(grid_layer),
                    origin_px=(
                        offset_x + (0.0 - plane_min[0]) * scale,
                        offset_y + (plane_max[1] - 0.0) * scale,
                    ),
                    plane_min=(float(plane_min[0]), float(plane_min[1])),
                    plane_max=(float(plane_max[0]), float(plane_max[1])),
                    scale=scale,
                    area=drawing_area,
                    step_mm=grid_step,
                )
                # The grid belongs behind the model, never over it.
                background_mask = Image.fromarray(
                    (
                        ~_downsample_mask(filled, buffer_scale, width, height)
                    ).astype(np.uint8)
                    * 255,
                    mode="L",
                )
                image = Image.composite(grid_layer, image, background_mask)
            _annotate(
                image,
                view=view,
                camera=camera,
                scale=scale,
                plane_min=plane_min,
                plane_max=plane_max,
                offset=(offset_x, offset_y),
                area=drawing_area,
                extents=extents,
                grid_step=grid_step,
                footer=footer,
            )

        path = output_dir / f"{view.name}.png"
        image.save(path, format="PNG", optimize=True)
        payload = path.read_bytes()
        if len(payload) <= 100 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError(f"render output is not a valid PNG: {view.name}")

        horizontal_axis, vertical_axis = camera.axis_names()
        results.append(
            RenderedView(
                name=view.name,
                path=path,
                width=width,
                height=height,
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                scale_mm_per_px=1.0 / scale if scale else 0.0,
                horizontal_axis=horizontal_axis,
                vertical_axis=vertical_axis,
                horizontal_mm=float(span[0]),
                vertical_mm=float(span[1]),
                is_section=view.section,
            )
        )
    return results


def _downsample_mask(
    mask: np.ndarray, factor: int, width: int, height: int
) -> np.ndarray:
    """Reduce a supersampled coverage mask to output resolution."""
    if factor <= 1:
        return mask
    reduced = mask.reshape(height, factor, width, factor)
    return reduced.any(axis=(1, 3))


def _project(
    mesh: Any, camera: _Camera
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return per-triangle screen coordinates, depths, world corners and normals."""
    vertices = np.asarray(mesh.vertices, dtype=float)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    corners = vertices[faces]  # (n, 3, 3)
    plane_x = corners @ camera.right
    plane_y = corners @ camera.up
    depth = corners @ camera.forward
    normals = np.asarray(mesh.face_normals, dtype=float)
    return np.stack([plane_x, plane_y], axis=2), depth, corners, normals
