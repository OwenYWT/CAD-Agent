import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh

logger = logging.getLogger(__name__)


@dataclass
class CameraAngle:
    name: str
    elevation: float  # degrees
    azimuth: float    # degrees
    distance: float = 0  # 0 = auto-calculate


STANDARD_ANGLES = [
    CameraAngle("front",     elevation=0,   azimuth=0),
    CameraAngle("right",     elevation=0,   azimuth=90),
    CameraAngle("top",       elevation=90,  azimuth=0),
    CameraAngle("isometric", elevation=35,  azimuth=45),
]


class CADRenderer:
    def render_stl(
        self,
        stl_path: Path,
        output_dir: Path,
        angles: list[CameraAngle] = STANDARD_ANGLES,
    ) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)

        mesh = trimesh.load(stl_path)
        mesh.apply_translation(-mesh.centroid)

        auto_distance = float(max(mesh.bounding_box.extents) * 2.5)

        result_paths = []
        for angle in angles:
            distance = angle.distance if angle.distance > 0 else auto_distance

            # Compute camera position from spherical coordinates
            elev_rad = math.radians(angle.elevation)
            azim_rad = math.radians(angle.azimuth)

            cam_x = distance * math.cos(elev_rad) * math.sin(azim_rad)
            cam_y = distance * math.cos(elev_rad) * math.cos(azim_rad)
            cam_z = distance * math.sin(elev_rad)

            scene = trimesh.Scene(mesh)

            # Build camera transform: look from (cam_x, cam_y, cam_z) toward origin
            camera_pos = np.array([cam_x, cam_y, cam_z])
            target = np.array([0.0, 0.0, 0.0])
            up = np.array([0.0, 0.0, 1.0])

            # Compute look-at transform
            forward = target - camera_pos
            forward = forward / np.linalg.norm(forward)

            right = np.cross(forward, up)
            if np.linalg.norm(right) < 1e-6:
                # Camera is looking straight down or up
                up = np.array([0.0, 1.0, 0.0])
                right = np.cross(forward, up)
            right = right / np.linalg.norm(right)

            cam_up = np.cross(right, forward)
            cam_up = cam_up / np.linalg.norm(cam_up)

            # Camera transform matrix (4x4)
            transform = np.eye(4)
            transform[:3, 0] = right
            transform[:3, 1] = cam_up
            transform[:3, 2] = -forward
            transform[:3, 3] = camera_pos

            scene.camera_transform = transform

            out_path = output_dir / f"{angle.name}.png"
            rendered = False
            try:
                png_data = scene.save_image(resolution=(512, 512), visible=False)
                if png_data and len(png_data) > 100:
                    with open(out_path, "wb") as f:
                        f.write(png_data)
                    rendered = True
            except Exception:
                # GL render failed (no display / pyglet missing) — fall through to
                # the software renderer below instead of dropping the angle.
                pass

            if not rendered:
                rendered = self._render_matplotlib(mesh, angle, out_path)

            if rendered:
                result_paths.append(out_path)

        return result_paths

    def _render_matplotlib(self, mesh, angle: CameraAngle, out_path: Path) -> bool:
        """Software fallback: shade the mesh with matplotlib's 3D engine.

        The vision self-check is only as good as its input images; without this
        fallback a host with no OpenGL context produces zero renders and the whole
        visual verify-and-correct loop silently degrades to 'indeterminate'."""
        try:
            # Check the face COUNT before materializing the full (n,3,3) triangle
            # array — a multi-million-face mesh would otherwise allocate hundreds of
            # MB just to be rejected. matplotlib's 3D engine is pure-Python and slow,
            # so cap well below where per-render latency becomes painful.
            n_faces = len(mesh.faces)
            if n_faces == 0 or n_faces > 60_000:
                if n_faces > 60_000:
                    logger.warning(
                        "matplotlib fallback skipped for %s: %d faces exceeds cap (60k)",
                        angle.name, n_faces,
                    )
                return False
            faces = np.asarray(mesh.triangles)

            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from mpl_toolkits.mplot3d.art3d import Poly3DCollection

            # Simple lambert shading from face normals against a fixed light
            light = np.array([0.4, -0.5, 0.75])
            light /= np.linalg.norm(light)
            intensity = 0.35 + 0.6 * np.clip(mesh.face_normals @ light, 0.0, 1.0)
            base = np.array([0.62, 0.68, 0.80])
            colors = np.clip(intensity[:, None] * base[None, :], 0.0, 1.0)

            fig = plt.figure(figsize=(5.12, 5.12), dpi=100)
            try:
                ax = fig.add_subplot(111, projection="3d")
                ax.add_collection3d(
                    Poly3DCollection(faces, facecolors=colors, edgecolors="none")
                )
                # Mesh is centered at the origin; frame it by its bounding-sphere radius
                radius = float(np.linalg.norm(mesh.bounding_box.extents) / 2.0) or 1.0
                radius *= 1.05
                ax.set_xlim(-radius, radius)
                ax.set_ylim(-radius, radius)
                ax.set_zlim(-radius, radius)
                ax.set_box_aspect((1, 1, 1))
                # Match the GL camera convention (azimuth 0 = looking from +Y):
                # matplotlib measures azimuth from the +X axis.
                ax.view_init(elev=angle.elevation, azim=90 - angle.azimuth)
                ax.set_axis_off()
                fig.savefig(out_path, bbox_inches="tight", pad_inches=0.05, facecolor="white")
            finally:
                plt.close(fig)

            return out_path.exists() and out_path.stat().st_size > 100
        except Exception as e:
            logger.warning(f"matplotlib fallback render failed for {angle.name}: {e}")
            return False
