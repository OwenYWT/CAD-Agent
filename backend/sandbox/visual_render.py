"""Deterministic four-view STL rendering for the isolated MCAD worker."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/.cache")


_VIEWS = (
    ("front", 0.0, 0.0),
    ("right", 0.0, 90.0),
    ("top", 90.0, 0.0),
    ("isometric", 35.0, 45.0),
)


def render_four_views(
    source: Path,
    output_root: Path,
    *,
    width: int = 512,
    height: int = 512,
) -> tuple[dict[str, Path], dict[str, Any]]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import trimesh
    from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection

    if width < 128 or height < 128 or width > 2048 or height > 2048:
        raise ValueError("render dimensions must be between 128 and 2048")
    mesh_source = source
    if source.suffix.lower() in {".step", ".stp"}:
        import cadquery as cq

        mesh_source = Path("/tmp/visual-render-source.stl")
        model = cq.importers.importStep(str(source))
        cq.exporters.export(model, str(mesh_source), exportType="STL")
    mesh = trimesh.load_mesh(mesh_source, force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty or not len(mesh.faces):
        raise ValueError("STL contains no renderable mesh")
    mesh.apply_translation(-mesh.centroid)
    outputs: dict[str, Path] = {}
    evidence: list[dict[str, Any]] = []
    for view, elevation, azimuth in _VIEWS:
        path = output_root / f"{view}.png"
        figure = plt.figure(
            figsize=(width / 100, height / 100),
            dpi=100,
            facecolor="#f8fafc",
        )
        try:
            axis = figure.add_subplot(111, projection="3d")
            triangles = mesh.vertices[mesh.faces]
            light = np.array([0.35, -0.45, 0.82], dtype=float)
            light /= np.linalg.norm(light)
            intensity = 0.66 + 0.28 * np.clip(
                mesh.face_normals @ light,
                0.0,
                1.0,
            )
            base = np.array([0.58, 0.67, 0.73])
            face_colors = np.column_stack(
                [base[None, :] * intensity[:, None], np.ones(len(triangles))]
            )
            axis.add_collection3d(
                Poly3DCollection(
                    triangles,
                    facecolors=face_colors,
                    edgecolor="none",
                    alpha=1.0,
                )
            )
            # STL tessellation splits planar faces into triangles. Drawing every
            # triangle edge invents diagonal "features" for the vision model, so
            # only true crease edges are rendered.
            adjacency = mesh.face_adjacency
            if len(adjacency):
                adjacent_dot = np.einsum(
                    "ij,ij->i",
                    mesh.face_normals[adjacency[:, 0]],
                    mesh.face_normals[adjacency[:, 1]],
                )
                crease_edges = mesh.face_adjacency_edges[adjacent_dot < 0.999]
                if len(crease_edges):
                    axis.add_collection3d(
                        Line3DCollection(
                            mesh.vertices[crease_edges],
                            colors="#52606d",
                            linewidths=0.8,
                        )
                    )
            bounds = mesh.bounds
            center = bounds.mean(axis=0)
            extents = np.maximum(bounds[1] - bounds[0], 1e-3)
            radius = float(max(extents) * 0.58)
            axis.set_xlim(center[0] - radius, center[0] + radius)
            axis.set_ylim(center[1] - radius, center[1] + radius)
            axis.set_zlim(center[2] - radius, center[2] + radius)
            axis.set_box_aspect((1, 1, 1))
            axis.set_proj_type("ortho")
            axis.view_init(elev=elevation, azim=azimuth)
            axis.set_axis_off()
            figure.subplots_adjust(left=0, right=1, bottom=0, top=1)
            figure.savefig(path, format="png", dpi=100, facecolor="#f8fafc")
        finally:
            plt.close(figure)
        payload = path.read_bytes()
        if len(payload) <= 100 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError(f"render output is not a valid PNG: {view}")
        outputs[view] = path
        evidence.append(
            {
                "view": view,
                "filename": path.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "width": width,
                "height": height,
            }
        )
    return outputs, {
        "schema_version": "durable-render-report.v1",
        "views": evidence,
    }
