"""Geometry-based DFM analysis using trimesh (no LLM cost)."""

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import trimesh


@dataclass
class DFMGeometryResult:
    min_wall_thickness: float = 0.0
    has_thin_walls: bool = False
    overhang_ratio: float = 0.0
    has_sharp_edges: bool = False
    sharp_edge_count: int = 0
    surface_area: float = 0.0
    volume: float = 0.0
    material_ratio: float = 0.0
    face_count: int = 0
    is_watertight: bool = False
    bounding_box: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


class DesignGeometryAnalyzer:
    """Analyze STL geometry for DFM concerns without LLM."""

    THIN_WALL_THRESHOLD = 1.0  # mm
    SHARP_EDGE_ANGLE = math.radians(30)  # 30 degrees
    OVERHANG_ANGLE_THRESHOLD = -0.5  # face normal z component

    def analyze(self, stl_path: Path) -> DFMGeometryResult:
        mesh = trimesh.load(stl_path)
        result = DFMGeometryResult()

        # Basic metrics
        result.face_count = len(mesh.faces)
        result.is_watertight = bool(mesh.is_watertight)
        result.surface_area = round(float(mesh.area), 2)

        if mesh.is_watertight:
            result.volume = round(float(mesh.volume), 2)
        else:
            result.volume = 0.0

        # Bounding box
        bounds = mesh.bounding_box.bounds
        result.bounding_box = {
            "x_min": round(float(bounds[0][0]), 2),
            "x_max": round(float(bounds[1][0]), 2),
            "y_min": round(float(bounds[0][1]), 2),
            "y_max": round(float(bounds[1][1]), 2),
            "z_min": round(float(bounds[0][2]), 2),
            "z_max": round(float(bounds[1][2]), 2),
        }

        # Material ratio (solidity)
        bbox_vol = float(np.prod(mesh.bounding_box.extents))
        if bbox_vol > 0 and mesh.is_watertight:
            result.material_ratio = round(float(mesh.volume) / bbox_vol, 3)

        # Wall thickness estimation via ray sampling
        result.min_wall_thickness = self._estimate_min_wall_thickness(mesh)
        result.has_thin_walls = result.min_wall_thickness < self.THIN_WALL_THRESHOLD

        # Overhang detection
        result.overhang_ratio = self._compute_overhang_ratio(mesh)

        # Sharp edge detection
        sharp_count = self._count_sharp_edges(mesh)
        result.sharp_edge_count = sharp_count
        result.has_sharp_edges = sharp_count > 0

        # Collect issues
        result.issues = self._collect_issues(result)

        return result

    def _estimate_min_wall_thickness(self, mesh: trimesh.Trimesh) -> float:
        """Sample interior rays from face centroids to estimate minimum wall thickness."""
        if not mesh.is_watertight or len(mesh.faces) == 0:
            return 0.0

        # Sample a subset of faces for performance
        n_samples = min(200, len(mesh.faces))
        rng = np.random.RandomState(42)
        indices = rng.choice(len(mesh.faces), size=n_samples, replace=False)

        centroids = mesh.triangles_center[indices]
        normals = mesh.face_normals[indices]

        # Shoot rays inward (opposite to face normal)
        ray_origins = centroids + normals * 0.01  # slight offset outward
        ray_directions = -normals

        try:
            locations, ray_indices, _ = mesh.ray.intersects_location(
                ray_origins=ray_origins,
                ray_directions=ray_directions,
            )
        except Exception:
            return 0.0

        if len(locations) == 0:
            return 0.0

        # For each ray, find the closest hit (skip the origin face)
        thicknesses = []
        for i in range(n_samples):
            hits = locations[ray_indices == i]
            if len(hits) == 0:
                continue
            distances = np.linalg.norm(hits - ray_origins[i], axis=1)
            # Filter out near-zero hits (self-intersection)
            valid = distances[distances > 0.05]
            if len(valid) > 0:
                thicknesses.append(float(np.min(valid)))

        if not thicknesses:
            return 0.0

        # Return the 5th percentile as the "minimum" (robust to outliers)
        return round(float(np.percentile(thicknesses, 5)), 2)

    def _compute_overhang_ratio(self, mesh: trimesh.Trimesh) -> float:
        """Compute ratio of faces that are overhangs (normal points downward)."""
        if len(mesh.faces) == 0:
            return 0.0

        normals_z = mesh.face_normals[:, 2]
        areas = mesh.area_faces

        overhang_mask = normals_z < self.OVERHANG_ANGLE_THRESHOLD
        overhang_area = float(np.sum(areas[overhang_mask]))
        total_area = float(np.sum(areas))

        if total_area == 0:
            return 0.0
        return round(overhang_area / total_area, 3)

    def _count_sharp_edges(self, mesh: trimesh.Trimesh) -> int:
        """Count edges with dihedral angle below threshold."""
        try:
            angles = mesh.face_adjacency_angles
        except Exception:
            return 0

        sharp_mask = angles < self.SHARP_EDGE_ANGLE
        return int(np.sum(sharp_mask))

    def _collect_issues(self, result: DFMGeometryResult) -> list[str]:
        issues = []

        if not result.is_watertight:
            issues.append("模型非水密，可能存在间隙或未闭合的面")

        if result.has_thin_walls:
            issues.append(
                f"检测到薄壁区域 (最薄约 {result.min_wall_thickness:.1f}mm)，"
                "可能导致加工困难或结构脆弱"
            )

        if result.overhang_ratio > 0.3:
            issues.append(
                f"悬臂面积占比 {result.overhang_ratio:.0%}，"
                "3D 打印可能需要大量支撑"
            )
        elif result.overhang_ratio > 0.1:
            issues.append(
                f"存在悬臂区域 (占比 {result.overhang_ratio:.0%})，"
                "建议检查打印方向"
            )

        if result.sharp_edge_count > 20:
            issues.append(
                f"检测到 {result.sharp_edge_count} 处锐利边缘 (<30°)，"
                "可能导致应力集中"
            )

        if result.material_ratio > 0.9:
            issues.append("实心率极高 (>90%)，考虑添加减重孔或壳化以节省材料")
        elif result.material_ratio < 0.05 and result.material_ratio > 0:
            issues.append("实心率很低 (<5%)，结构可能较薄弱")

        if result.face_count > 200000:
            issues.append(
                f"面片数 {result.face_count:,} 较多，可能影响切片和加工路径生成"
            )

        return issues
