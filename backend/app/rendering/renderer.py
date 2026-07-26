import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

_RENDER_CACHE_DIR = Path(tempfile.gettempdir()) / "cad-agent-render-cache"
os.environ.setdefault("MPLCONFIGDIR", str(_RENDER_CACHE_DIR / "matplotlib"))
os.environ.setdefault("XDG_CACHE_HOME", str(_RENDER_CACHE_DIR))

import matplotlib
import numpy as np
import trimesh

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

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

        mesh = trimesh.load(stl_path, force="mesh")
        if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
            raise ValueError(f"STL contains no renderable mesh: {stl_path}")
        mesh.apply_translation(-mesh.centroid)

        result_paths = []
        for angle in angles:
            out_path = output_dir / f"{angle.name}.png"
            figure = plt.figure(figsize=(5.12, 5.12), dpi=100, facecolor="#f8fafc")
            try:
                axis = figure.add_subplot(111, projection="3d")
                triangles = mesh.vertices[mesh.faces]
                collection = Poly3DCollection(
                    triangles,
                    facecolor="#b8c6d1",
                    edgecolor="#52606d",
                    linewidth=0.18,
                    alpha=1.0,
                )
                axis.add_collection3d(collection)

                bounds = mesh.bounds
                center = bounds.mean(axis=0)
                extents = np.maximum(bounds[1] - bounds[0], 1e-3)
                radius = float(max(extents) * 0.58)
                axis.set_xlim(center[0] - radius, center[0] + radius)
                axis.set_ylim(center[1] - radius, center[1] + radius)
                axis.set_zlim(center[2] - radius, center[2] + radius)
                axis.set_box_aspect((1, 1, 1))
                axis.set_proj_type("ortho")
                axis.view_init(elev=angle.elevation, azim=angle.azimuth)
                axis.set_axis_off()
                figure.subplots_adjust(left=0, right=1, bottom=0, top=1)
                figure.savefig(
                    out_path,
                    format="png",
                    dpi=100,
                    facecolor=figure.get_facecolor(),
                )
                if out_path.stat().st_size > 100:
                    result_paths.append(out_path)
                else:
                    out_path.unlink(missing_ok=True)
            except Exception as exc:
                out_path.unlink(missing_ok=True)
                logger.warning(
                    "CAD render failed for %s view of %s: %s",
                    angle.name,
                    stl_path,
                    exc,
                )
            finally:
                plt.close(figure)

        return result_paths
