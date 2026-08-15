from dataclasses import dataclass, field
from pathlib import Path

import trimesh

from app.config import settings
from app.validation.design_analyzer import DesignGeometryAnalyzer


@dataclass
class ValidationRule:
    name: str
    passed: bool
    message: str
    severity: str  # "error" | "warning" | "info"


@dataclass
class GeometryValidation:
    passed: bool
    rules: list[ValidationRule]
    bounding_box: dict[str, float]
    volume: float
    is_watertight: bool
    # 3D-printing feasibility
    printable: bool = False
    fits_build_volume: bool = False
    min_wall_thickness: float | None = None
    print_warnings: list[str] = field(default_factory=list)


class GeometryValidator:
    MIN_WALL_THICKNESS = 0.5
    MAX_DIMENSION = 2000
    MIN_DIMENSION = 0.1
    # Only these complete groups describe the model's three bounding-box axes.
    # Feature dimensions such as outer/inner diameter, hole diameter, radius,
    # wall thickness, and fillet radius must never be counted as XYZ extents.
    BOUNDING_BOX_DIMENSION_GROUPS = (
        ("width", "depth", "height"),
        ("length", "width", "height"),
        ("length", "depth", "height"),
        ("length", "width", "thickness"),
        ("width", "height", "thickness"),
        ("x", "y", "z"),
    )

    def __init__(self):
        # Reuse the existing ray-cast wall-thickness estimator (no LLM cost)
        self._wall_analyzer = DesignGeometryAnalyzer()

    async def validate(
        self, stl_path: Path, expected_dimensions: dict | None = None
    ) -> GeometryValidation:
        mesh = trimesh.load(stl_path)
        extents = mesh.bounding_box.extents

        build_volume = settings.build_volume_mm
        min_wall = settings.min_wall_mm

        rules = []
        rules.append(self._check_watertight(mesh))
        rules.append(self._check_dimension_range(mesh, extents))
        rules.append(self._check_build_volume(extents, build_volume))
        rules.append(self._check_expected_dimensions(mesh, extents, expected_dimensions))
        rules.append(self._check_volume(mesh))
        rules.append(self._check_face_count(mesh))

        # Wall thickness estimate (advisory — the ray-cast is noisy, so it warns, never blocks)
        min_wall_thickness = self._wall_analyzer._estimate_min_wall_thickness(mesh)
        wall_rule = self._check_min_wall(min_wall_thickness, min_wall)
        rules.append(wall_rule)

        is_watertight = mesh.is_watertight
        # success gate: a model passes only if every ERROR-severity rule passes
        passed = all(r.passed for r in rules if r.severity == "error")

        bounds = mesh.bounding_box.bounds  # [[x_min,y_min,z_min],[x_max,y_max,z_max]]
        bbox = {
            "x_min": round(float(bounds[0][0]), 2),
            "x_max": round(float(bounds[1][0]), 2),
            "y_min": round(float(bounds[0][1]), 2),
            "y_max": round(float(bounds[1][1]), 2),
            "z_min": round(float(bounds[0][2]), 2),
            "z_max": round(float(bounds[1][2]), 2),
        }
        volume = float(mesh.volume) if is_watertight else 0.0

        # printability: the two HARD gates a slicer cares about
        fits_build_volume = float(max(extents)) <= build_volume
        printable = bool(is_watertight and fits_build_volume)

        # Surface every non-info rule that isn't passing as a human-readable print warning
        print_warnings = [
            r.message for r in rules if not r.passed and r.severity in ("error", "warning")
        ]

        return GeometryValidation(
            passed=passed,
            rules=rules,
            bounding_box=bbox,
            volume=volume,
            is_watertight=is_watertight,
            printable=printable,
            fits_build_volume=fits_build_volume,
            min_wall_thickness=min_wall_thickness if min_wall_thickness > 0 else None,
            print_warnings=print_warnings,
        )

    def _check_watertight(self, mesh: trimesh.Trimesh) -> ValidationRule:
        # A non-watertight mesh cannot be sliced/printed — this is a hard failure for 3D printing.
        if mesh.is_watertight:
            return ValidationRule("watertight", True, "模型是水密的，可切片打印", "info")
        return ValidationRule(
            "watertight", False,
            "模型不是水密的（存在间隙/未闭合面），切片软件无法打印", "error"
        )

    def _check_dimension_range(self, mesh: trimesh.Trimesh, extents) -> ValidationRule:
        max_dim = float(max(extents))
        min_dim = float(min(extents))
        if max_dim > self.MAX_DIMENSION:
            return ValidationRule(
                "dimension_range", False,
                f"最大尺寸 {max_dim:.1f}mm 超过限制 {self.MAX_DIMENSION}mm", "error"
            )
        if min_dim < self.MIN_DIMENSION:
            return ValidationRule(
                "dimension_range", False,
                f"最小尺寸 {min_dim:.3f}mm 小于最低限制 {self.MIN_DIMENSION}mm", "warning"
            )
        return ValidationRule(
            "dimension_range", True,
            f"尺寸范围正常: {min_dim:.1f} ~ {max_dim:.1f}mm", "info"
        )

    def _check_build_volume(self, extents, build_volume: float) -> ValidationRule:
        """Reject parts that won't fit a typical FDM build volume (default 256mm cube)."""
        max_dim = float(max(extents))
        if max_dim > build_volume:
            return ValidationRule(
                "build_volume", False,
                f"最大尺寸 {max_dim:.0f}mm 超过打印机成型空间 {build_volume:.0f}mm，无法打印", "error"
            )
        return ValidationRule(
            "build_volume", True,
            f"可放入 {build_volume:.0f}mm 成型空间", "info"
        )

    def _check_min_wall(self, est_wall: float, min_wall: float) -> ValidationRule:
        """Advisory wall-thickness check. The ray-cast estimate is noisy, so this warns
        (surfaces to the user) but does not block success / trigger retries."""
        if est_wall <= 0:
            return ValidationRule(
                "min_wall", True, "无法估算壁厚（实心或非水密），跳过", "info"
            )
        if est_wall < min_wall:
            return ValidationRule(
                "min_wall", False,
                f"最薄处约 {est_wall:.1f}mm，低于推荐打印壁厚 {min_wall:.1f}mm，可能打印失败或易碎",
                "warning",
            )
        return ValidationRule(
            "min_wall", True, f"最薄壁约 {est_wall:.1f}mm，满足打印要求", "info"
        )

    def _check_expected_dimensions(
        self, mesh: trimesh.Trimesh, extents, expected: dict | None
    ) -> ValidationRule:
        if expected is None:
            return ValidationRule("expected_dimensions", True, "未指定期望尺寸", "info")

        normalized = {str(key).strip().lower(): value for key, value in expected.items()}
        axis_group = next(
            (
                group
                for group in self.BOUNDING_BOX_DIMENSION_GROUPS
                if all(axis in normalized for axis in group)
            ),
            None,
        )
        if axis_group is None:
            return ValidationRule(
                "expected_dimensions",
                True,
                "未提供完整包围盒轴尺寸，跳过检查",
                "info",
            )

        actual_sorted = sorted(float(e) for e in extents)
        try:
            expected_values = sorted(float(normalized[axis]) for axis in axis_group)
        except (TypeError, ValueError):
            return ValidationRule(
                "expected_dimensions", False, "包围盒期望尺寸不是有效数值", "error"
            )

        max_error = 0.0
        for a, e in zip(actual_sorted, expected_values):
            if e > 0:
                error = abs(a - e) / e
                max_error = max(max_error, error)

        if max_error > 0.05:
            return ValidationRule(
                "expected_dimensions", False,
                f"实际尺寸与期望偏差 {max_error:.0%}，超过 5% 阈值", "error"
            )
        return ValidationRule(
            "expected_dimensions", True,
            f"尺寸匹配，最大偏差 {max_error:.1%}", "info"
        )

    def _check_volume(self, mesh: trimesh.Trimesh) -> ValidationRule:
        if not mesh.is_watertight:
            return ValidationRule("volume", True, "非水密模型，跳过体积检查", "info")
        vol = float(mesh.volume)
        if vol < 0.001:
            return ValidationRule(
                "volume", False,
                f"体积 {vol:.6f} mm³ 过小，可能是退化几何", "error"
            )
        return ValidationRule("volume", True, f"体积 {vol:.0f} mm³", "info")

    def _check_face_count(self, mesh: trimesh.Trimesh) -> ValidationRule:
        n_faces = len(mesh.faces)
        if n_faces > 500000:
            return ValidationRule(
                "face_count", True,
                f"面数 {n_faces} 较多，可能影响性能", "warning"
            )
        return ValidationRule("face_count", True, f"面数 {n_faces}", "info")
