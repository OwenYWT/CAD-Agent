import importlib.util
from pathlib import Path

import numpy as np
import trimesh

spec = importlib.util.spec_from_file_location("visual_render", Path(__file__).resolve().parents[1] / "sandbox/visual_render.py")
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def test_opaque_front_face_hides_back_faces_independent_of_triangle_order():
    mesh = trimesh.creation.box(extents=[60,40,8])
    first = np.asarray(renderer._rasterize(mesh, width=256, height=256, elevation=90, azimuth=0))
    reversed_mesh = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces[::-1], process=False)
    second = np.asarray(renderer._rasterize(reversed_mesh, width=256, height=256, elevation=90, azimuth=0))
    assert np.array_equal(first, second)
    # An opaque planar top has one shade, with no rear edges or diagonals.
    assert np.unique(first[110:146,90:166].reshape(-1,3),axis=0).shape[0] == 1
    assert not np.array_equal(first[128,128], first[0,0])


def test_four_views_export_real_png_evidence(tmp_path):
    source = tmp_path / "box.stl"
    trimesh.creation.box(extents=[60,40,8]).export(source)
    files, evidence = renderer.render_four_views(source,tmp_path,width=256,height=256)
    assert set(files) == {"front","right","top","isometric"}
    assert len({view["sha256"] for view in evidence["views"]}) >= 3
    assert all(path.read_bytes().startswith(b"\x89PNG") for path in files.values())
