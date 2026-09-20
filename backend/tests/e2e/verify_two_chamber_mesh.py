"""Independently measure a saved rectangular two-chamber enclosure STL.

Usage: python backend/tests/e2e/verify_two_chamber_mesh.py saved.stl \
    --size 60 40 25 --wall 2 --floor 2
This verifies actual geometry, not the generating plan's declared parameters.
"""
import argparse
import json
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon
import trimesh


def verify(path: Path, size: list[float], wall: float, floor: float) -> dict:
    mesh = trimesh.load(path, file_type='stl', force='mesh')
    assert mesh.is_watertight and mesh.volume > 0, 'Invalid solid boundary'
    assert np.allclose(mesh.extents, size, atol=1e-4, rtol=0), 'Envelope mismatch'
    z0 = float(mesh.bounds[0, 2])

    def rings(z):
        section = mesh.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
        if section is None:
            return []
        polygons = [Polygon(line[:, :2]) for line in section.discrete]
        assert all(p.is_valid and p.area > 0 for p in polygons), 'Invalid section'
        return sorted(polygons, key=lambda p: p.area, reverse=True)

    middle = rings(z0 + (floor + size[2]) / 2)
    assert len(middle) == 3, 'Expected one exterior contour and two cavities'
    outer, a, b = middle
    assert outer.contains(a) and outer.contains(b) and a.disjoint(b)
    assert abs(a.area - b.area) < 1e-4, 'Cavities are not equally divided'
    xmin, ymin, xmax, ymax = outer.bounds
    # Each cavity must be rectangular: no unverified extra cutouts or islands.
    for cavity in (a, b):
        x0, y0, x1, y1 = cavity.bounds
        assert abs(cavity.area - (x1-x0)*(y1-y0)) < 1e-4
    clear_x, clear_y = xmax-xmin-2*wall, ymax-ymin-2*wall
    expected_y = [
        [xmin+wall, ymin+wall, xmax-wall, (ymin+ymax-wall)/2],
        [xmin+wall, (ymin+ymax+wall)/2, xmax-wall, ymax-wall],
    ]
    expected_x = [
        [xmin+wall, ymin+wall, (xmin+xmax-wall)/2, ymax-wall],
        [(xmin+xmax+wall)/2, ymin+wall, xmax-wall, ymax-wall],
    ]
    assert clear_x > wall and clear_y > wall
    bounds = sorted([a.bounds, b.bounds])
    assert any(np.allclose(bounds, sorted(expected), atol=1e-4, rtol=0)
               for expected in (expected_x, expected_y)), 'Wall or divider placement mismatch'
    assert len(rings(z0+floor-0.001)) == 1
    assert len(rings(z0+floor+0.001)) == 3, 'Floor thickness mismatch'
    assert len(rings(z0+size[2]-0.001)) == 3, 'Unexpected closed lid'
    assert not rings(z0+size[2]+0.001)
    return {'extents_mm': mesh.extents.tolist(), 'cavity_count': 2,
            'equal_chambers': True, 'wall_mm': wall, 'floor_mm': floor,
            'partition_mm': a.distance(b), 'open_top': True,
            'volume_mm3': float(mesh.volume), 'watertight': True,
            'cavity_sections_mm2': [a.area, b.area],
            'cavity_bounds_mm': bounds}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mesh', type=Path)
    parser.add_argument('--size', type=float, nargs=3, required=True)
    parser.add_argument('--wall', type=float, required=True)
    parser.add_argument('--floor', type=float, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.mesh, args.size, args.wall, args.floor), ensure_ascii=False))
