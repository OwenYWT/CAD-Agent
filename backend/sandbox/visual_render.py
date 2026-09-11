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


def _rasterize(mesh, *, width: int, height: int, elevation: float, azimuth: float):
    """Orthographic software depth buffer. Hidden surfaces never become lines.

    mplot3d's painter sorting is unsuitable for concave CAD tessellations: it
    can paint rear triangles/edges over a nearer face. This rasterizer resolves
    visibility per pixel and requires no display server or GPU in the worker.
    """
    import numpy as np
    from PIL import Image

    if len(mesh.faces) > 250_000:
        raise ValueError("visual mesh exceeds the 250000 triangle rendering limit")
    scale_factor = 2
    w, h = width * scale_factor, height * scale_factor
    el, az = np.radians([elevation, azimuth])
    eye = np.array([np.cos(el)*np.cos(az), np.cos(el)*np.sin(az), np.sin(el)])
    up_hint = np.array([0.0, 1.0, 0.0]) if abs(eye[2]) > 0.99 else np.array([0.0, 0.0, 1.0])
    right = np.cross(up_hint, eye); right /= np.linalg.norm(right)
    up = np.cross(eye, right)
    points = np.asarray(mesh.vertices) - mesh.bounds.mean(axis=0)
    scale = 0.84 * min(w, h) / max(float(np.linalg.norm(mesh.extents)), 1e-6)
    projected = np.column_stack((w/2 + points @ right * scale,
                                 h/2 - points @ up * scale, points @ eye))
    pixels = np.full((h, w, 3), [248, 250, 252], dtype=np.uint8)
    depth = np.full((h, w), -np.inf, dtype=np.float32)
    light = eye + up * 0.6 - right * 0.3
    light /= np.linalg.norm(light)
    intensity = 0.55 + 0.4 * np.maximum(0.0, mesh.face_normals @ light)
    colors = np.clip(np.array([166, 183, 194])[None, :] * intensity[:, None], 0, 255).astype(np.uint8)
    for index, face in enumerate(mesh.faces):
        a, b, c = projected[face]
        denominator = (b[1]-c[1])*(a[0]-c[0]) + (c[0]-b[0])*(a[1]-c[1])
        if abs(denominator) < 1e-10:
            continue
        x0 = max(0, int(np.floor(min(a[0], b[0], c[0]))))
        x1 = min(w-1, int(np.ceil(max(a[0], b[0], c[0]))))
        y0 = max(0, int(np.floor(min(a[1], b[1], c[1]))))
        y1 = min(h-1, int(np.ceil(max(a[1], b[1], c[1]))))
        if x1 < x0 or y1 < y0:
            continue
        xs = np.arange(x0, x1+1, dtype=np.float32)[None, :] + 0.5
        ys = np.arange(y0, y1+1, dtype=np.float32)[:, None] + 0.5
        u = ((b[1]-c[1])*(xs-c[0]) + (c[0]-b[0])*(ys-c[1])) / denominator
        v = ((c[1]-a[1])*(xs-c[0]) + (a[0]-c[0])*(ys-c[1])) / denominator
        t = 1-u-v
        z = u*a[2] + v*b[2] + t*c[2]
        target = depth[y0:y1+1, x0:x1+1]
        visible = (u >= -1e-6) & (v >= -1e-6) & (t >= -1e-6) & (z > target)
        target[visible] = z[visible]
        pixels[y0:y1+1, x0:x1+1][visible] = colors[index]
    return Image.fromarray(pixels).resize((width, height), Image.Resampling.LANCZOS)


def render_four_views(
    source: Path,
    output_root: Path,
    *,
    width: int = 512,
    height: int = 512,
) -> tuple[dict[str, Path], dict[str, Any]]:
    import trimesh

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
    outputs: dict[str, Path] = {}
    evidence: list[dict[str, Any]] = []
    for view, elevation, azimuth in _VIEWS:
        path = output_root / f"{view}.png"
        image = _rasterize(mesh, width=width, height=height, elevation=elevation, azimuth=azimuth)
        image.save(path, format="PNG")
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
