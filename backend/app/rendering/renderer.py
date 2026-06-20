import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh


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
            try:
                png_data = scene.save_image(resolution=(512, 512), visible=False)
                if png_data and len(png_data) > 100:
                    with open(out_path, "wb") as f:
                        f.write(png_data)
                    result_paths.append(out_path)
                else:
                    # Empty or trivially small render — treat as failure, skip this angle
                    pass
            except Exception:
                # Render failed for this angle (e.g., no display available)
                # Do NOT create a placeholder — skip this angle entirely
                pass

        return result_paths
