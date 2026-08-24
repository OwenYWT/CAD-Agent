"""Measured geometry facts extracted from a mesh, with no model in the loop.

A vision model asked "does this have four 6 mm holes?" will guess. The same
model asked "the mesh reports four Ø6.00 mm through holes on the Z axis at
(±40, ±22); does the picture agree, and does the request?" will check. This
module produces that second half: deterministic, auditable numbers the critic
cross-examines the render against.

Everything here is pure geometry over numpy + trimesh. No ``app.*`` imports and
no network, so the same facts can be computed inside the isolated worker or in
the control plane and compared byte for byte.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# This module is also copied flat into the isolated MCAD worker, where the
# application package does not exist (and pydantic is deliberately not in the
# pinned runtime, so the package's __init__ cannot be imported there either).
# Both sides therefore run this one file, and only the import line differs.
try:  # pragma: no cover - exercised by whichever side is running
    from app.visual_refine.mesh_views import load_mesh
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mesh_views import load_mesh  # type: ignore

# A face belongs to a cylinder about an axis when its normal is perpendicular to
# that axis. Tessellation leaves a little slack, hence the tolerance.
_AXIS_PERPENDICULAR_TOLERANCE = 0.09
# A fitted circle is accepted when residuals stay inside this fraction of the
# radius; anything looser is a fillet, a cone or a freeform blend, not a hole.
_CIRCLE_FIT_TOLERANCE = 0.06
# Below this angular coverage the surface is a rounded corner, not a bore.
_MIN_ANGULAR_COVERAGE_DEG = 290.0
_MIN_FACES_PER_CYLINDER = 6

_AXES: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("X", (1.0, 0.0, 0.0)),
    ("Y", (0.0, 1.0, 0.0)),
    ("Z", (0.0, 0.0, 1.0)),
)


@dataclass(frozen=True)
class HoleFact:
    """One detected cylindrical bore or boss."""

    axis: str
    diameter: float
    depth: float
    through: bool
    kind: str  # "hole" | "boss"
    center: tuple[float, float, float]

    def describe(self) -> str:
        span = "through" if self.through else f"{self.depth:.2f} mm deep blind"
        position = ", ".join(f"{value:.1f}" for value in self.center)
        return (
            f"{self.kind} D{self.diameter:.2f} mm, {span}, "
            f"axis {self.axis}, centre ({position})"
        )


@dataclass(frozen=True)
class GeometryFacts:
    """Everything measurable about the produced solid, in millimetres."""

    bounding_box: tuple[float, float, float]
    volume: float
    surface_area: float
    is_watertight: bool
    body_count: int
    largest_body_volume_fraction: float
    fill_ratio: float
    holes: tuple[HoleFact, ...] = ()
    triangle_count: int = 0
    center_of_mass: tuple[float, float, float] = (0.0, 0.0, 0.0)
    notes: tuple[str, ...] = ()

    @property
    def sorted_extents(self) -> tuple[float, float, float]:
        values = sorted(self.bounding_box, reverse=True)
        return (values[0], values[1], values[2])

    def hole_diameters(self) -> tuple[float, ...]:
        return tuple(
            sorted(hole.diameter for hole in self.holes if hole.kind == "hole")
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "bounding_box_mm": {
                "x": round(self.bounding_box[0], 3),
                "y": round(self.bounding_box[1], 3),
                "z": round(self.bounding_box[2], 3),
            },
            "volume_mm3": round(self.volume, 2),
            "surface_area_mm2": round(self.surface_area, 2),
            "is_watertight": self.is_watertight,
            "body_count": self.body_count,
            "largest_body_volume_fraction": round(
                self.largest_body_volume_fraction, 4
            ),
            "bounding_box_fill_ratio": round(self.fill_ratio, 4),
            "triangle_count": self.triangle_count,
            "center_of_mass_mm": [round(value, 3) for value in self.center_of_mass],
            "holes": [
                {
                    "axis": hole.axis,
                    "diameter_mm": round(hole.diameter, 3),
                    "depth_mm": round(hole.depth, 3),
                    "through": hole.through,
                    "kind": hole.kind,
                    "center_mm": [round(value, 3) for value in hole.center],
                }
                for hole in self.holes
            ],
            "notes": list(self.notes),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GeometryFacts | None":
        """Rebuild facts measured elsewhere, e.g. inside the isolated worker.

        Returns ``None`` for anything that is not a well-formed payload: the
        caller then simply proceeds without measured facts rather than failing a
        gate over a malformed dictionary.
        """
        try:
            box = payload["bounding_box_mm"]
            holes = tuple(
                HoleFact(
                    axis=str(item["axis"]),
                    diameter=float(item["diameter_mm"]),
                    depth=float(item["depth_mm"]),
                    through=bool(item["through"]),
                    kind=str(item["kind"]),
                    center=tuple(float(value) for value in item["center_mm"]),
                )
                for item in payload.get("holes") or ()
            )
            return cls(
                bounding_box=(
                    float(box["x"]),
                    float(box["y"]),
                    float(box["z"]),
                ),
                volume=float(payload["volume_mm3"]),
                surface_area=float(payload.get("surface_area_mm2") or 0.0),
                is_watertight=bool(payload.get("is_watertight")),
                body_count=int(payload.get("body_count") or 1),
                largest_body_volume_fraction=float(
                    payload.get("largest_body_volume_fraction") or 1.0
                ),
                fill_ratio=float(payload.get("bounding_box_fill_ratio") or 0.0),
                holes=holes,
                triangle_count=int(payload.get("triangle_count") or 0),
                center_of_mass=tuple(
                    float(value) for value in payload.get("center_of_mass_mm") or (0, 0, 0)
                ),
                notes=tuple(str(item) for item in payload.get("notes") or ()),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def render_briefing(self) -> str:
        """A compact human/model-readable block to sit beside the renders."""
        extents = self.bounding_box
        lines = [
            "MEASURED GEOMETRY (from the mesh itself, not from the picture):",
            f"- bounding box: X {extents[0]:.2f} x Y {extents[1]:.2f} x "
            f"Z {extents[2]:.2f} mm",
            f"- volume: {self.volume:.1f} mm^3   surface area: "
            f"{self.surface_area:.1f} mm^2",
            f"- bounding-box fill ratio: {self.fill_ratio:.3f} "
            "(1.0 = a plain solid block)",
            f"- watertight: {self.is_watertight}",
            f"- separate bodies: {self.body_count}"
            + (
                "  <-- MORE THAN ONE BODY: parts are not fused"
                if self.body_count > 1
                else ""
            ),
        ]
        bores = [hole for hole in self.holes if hole.kind == "hole"]
        bosses = [hole for hole in self.holes if hole.kind == "boss"]
        if bores:
            lines.append(f"- cylindrical holes detected: {len(bores)}")
            for hole in bores:
                lines.append(f"    * {hole.describe()}")
        else:
            lines.append("- cylindrical holes detected: 0")
        if bosses:
            lines.append(f"- cylindrical bosses/pins detected: {len(bosses)}")
            for boss in bosses:
                lines.append(f"    * {boss.describe()}")
        for note in self.notes:
            lines.append(f"- note: {note}")
        return "\n".join(lines)


def _fit_circle(points: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Algebraic (Kasa) circle fit. Returns centre, radius and max residual."""
    x = points[:, 0]
    y = points[:, 1]
    design = np.column_stack([x, y, np.ones(len(points))])
    target = x**2 + y**2
    solution, *_ = np.linalg.lstsq(design, target, rcond=None)
    center = np.array([solution[0] / 2.0, solution[1] / 2.0])
    radius_squared = solution[2] + center[0] ** 2 + center[1] ** 2
    if radius_squared <= 0:
        return center, 0.0, math.inf
    radius = math.sqrt(radius_squared)
    residual = float(np.abs(np.linalg.norm(points - center, axis=1) - radius).max())
    return center, radius, residual


def _angular_coverage(points: np.ndarray, center: np.ndarray) -> float:
    """Degrees of arc the sampled points span around ``center``."""
    offsets = points - center
    angles = np.sort(np.arctan2(offsets[:, 1], offsets[:, 0]))
    if len(angles) < 3:
        return 0.0
    gaps = np.diff(np.concatenate([angles, angles[:1] + 2 * math.pi]))
    return float(math.degrees(2 * math.pi - gaps.max()))


def _components(mesh: Any, face_mask: np.ndarray) -> list[np.ndarray]:
    """Connected components of the selected faces, via shared-edge adjacency."""
    from trimesh import graph

    selected = np.nonzero(face_mask)[0]
    if len(selected) < _MIN_FACES_PER_CYLINDER:
        return []
    adjacency = np.asarray(mesh.face_adjacency)
    if not len(adjacency):
        return []
    keep = face_mask[adjacency[:, 0]] & face_mask[adjacency[:, 1]]
    edges = adjacency[keep]
    if not len(edges):
        return []
    return [
        component
        for component in graph.connected_components(
            edges=edges, nodes=selected, min_len=_MIN_FACES_PER_CYLINDER
        )
    ]


def _detect_cylinders(mesh: Any) -> list[HoleFact]:
    """Find cylindrical bores and bosses aligned with a principal axis.

    CadQuery output tessellates a bore into a fan of quadrilateral strips whose
    normals are perpendicular to the bore axis. Grouping those faces and fitting
    a circle recovers the diameter exactly, which is what turns "I think I see
    four holes" into "four Ø6.00 mm holes".
    """
    results: list[HoleFact] = []
    vertices = np.asarray(mesh.vertices, dtype=float)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    normals = np.asarray(mesh.face_normals, dtype=float)
    bounds = np.asarray(mesh.bounds, dtype=float)

    for axis_name, axis_vector in _AXES:
        axis = np.asarray(axis_vector, dtype=float)
        axis_index = int(np.argmax(np.abs(axis)))
        plane_indices = [index for index in range(3) if index != axis_index]

        perpendicular = np.abs(normals @ axis) < _AXIS_PERPENDICULAR_TOLERANCE
        for component in _components(mesh, perpendicular):
            face_vertices = np.unique(faces[component].reshape(-1))
            points = vertices[face_vertices]
            planar = points[:, plane_indices]
            center_2d, radius, residual = _fit_circle(planar)
            if radius <= 1e-6 or not math.isfinite(residual):
                continue
            if residual > max(_CIRCLE_FIT_TOLERANCE * radius, 0.05):
                continue
            if _angular_coverage(planar, center_2d) < _MIN_ANGULAR_COVERAGE_DEG:
                continue

            axial = points[:, axis_index]
            depth = float(axial.max() - axial.min())
            if depth <= 1e-6:
                continue

            # Concave (normals pointing at the axis) is a bore; convex is a boss.
            face_centers = vertices[faces[component]].mean(axis=1)
            radial = face_centers[:, plane_indices] - center_2d
            radial_norm = np.linalg.norm(radial, axis=1, keepdims=True)
            radial = np.divide(
                radial,
                np.where(radial_norm > 1e-9, radial_norm, 1.0),
            )
            outward = np.einsum("ij,ij->i", normals[component][:, plane_indices], radial)
            kind = "hole" if float(outward.mean()) < 0 else "boss"

            model_span = float(bounds[1][axis_index] - bounds[0][axis_index])
            through = depth >= model_span - max(1e-3, 0.01 * model_span)

            center = [0.0, 0.0, 0.0]
            center[plane_indices[0]] = float(center_2d[0])
            center[plane_indices[1]] = float(center_2d[1])
            center[axis_index] = float((axial.max() + axial.min()) / 2.0)
            results.append(
                HoleFact(
                    axis=axis_name,
                    diameter=float(radius * 2.0),
                    depth=depth,
                    through=bool(through),
                    kind=kind,
                    center=(center[0], center[1], center[2]),
                )
            )

    # One physical bore can be picked up on two axes when it is short and wide.
    # Keep the reading whose axis matches the deepest extent.
    deduplicated: list[HoleFact] = []
    for hole in sorted(results, key=lambda item: -item.depth):
        duplicate = any(
            abs(hole.diameter - kept.diameter) < 0.2
            and math.dist(hole.center, kept.center) < max(1.0, hole.diameter * 0.5)
            for kept in deduplicated
        )
        if not duplicate:
            deduplicated.append(hole)
    return sorted(
        deduplicated,
        key=lambda item: (item.kind, round(item.diameter, 2), item.center),
    )


def measure(source: Any, *, detect_holes: bool = True) -> GeometryFacts:
    """Measure a mesh, a scene or an STL/STEP path.

    Never raises for a merely awkward mesh: a non-watertight or multi-body
    result is a *finding*, not an error, and the critic must be told about it.
    """
    mesh = load_mesh(source)
    extents = np.asarray(mesh.bounding_box.extents, dtype=float)
    bounding_volume = float(np.prod(np.maximum(extents, 1e-9)))
    volume = float(abs(mesh.volume)) if mesh.is_volume else 0.0

    notes: list[str] = []
    bodies = mesh.split(only_watertight=False)
    body_count = max(1, len(bodies))
    if body_count > 1:
        body_volumes = sorted(
            (float(abs(body.volume)) if body.is_volume else 0.0) for body in bodies
        )
        total = sum(body_volumes) or 1.0
        largest_fraction = body_volumes[-1] / total
        notes.append(
            f"the result is {body_count} disconnected bodies; the largest holds "
            f"{largest_fraction:.1%} of the volume"
        )
    else:
        largest_fraction = 1.0

    if not mesh.is_watertight:
        notes.append(
            "the mesh is not watertight, so volume and wall readings are estimates"
        )

    holes: tuple[HoleFact, ...] = ()
    if detect_holes:
        try:
            holes = tuple(_detect_cylinders(mesh))
        except Exception as exc:  # measurement is best-effort, never fatal
            notes.append(f"hole detection did not complete ({type(exc).__name__})")

    center_of_mass = (0.0, 0.0, 0.0)
    try:
        if mesh.is_volume:
            center_of_mass = tuple(float(value) for value in mesh.center_mass)
    except Exception:
        pass

    return GeometryFacts(
        bounding_box=(float(extents[0]), float(extents[1]), float(extents[2])),
        volume=volume,
        surface_area=float(mesh.area),
        is_watertight=bool(mesh.is_watertight),
        body_count=body_count,
        largest_body_volume_fraction=float(largest_fraction),
        fill_ratio=float(volume / bounding_volume) if bounding_volume else 0.0,
        holes=holes,
        triangle_count=int(len(mesh.faces)),
        center_of_mass=center_of_mass,
        notes=tuple(notes),
    )
