"""The advisory analyzer must consume real named STL shells without merging them."""
import numpy as np
import pytest
import trimesh

from app.validation.design_analyzer import DesignGeometryAnalyzer


@pytest.mark.parametrize('count,missing_face', [(1, False), (2, False), (2, True)])
def test_dfm_reads_named_ascii_stl_and_keeps_touching_shell_boundaries(tmp_path, count, missing_face):
    blocks = []
    for index in range(count):
        part = trimesh.creation.box([10, 10, 10])
        part.apply_translation([0, 0, 10 * index])
        if missing_face and index == 0:
            part.update_faces(np.arange(11))
        lines = trimesh.exchange.stl.export_stl_ascii(part).splitlines()
        lines[0], lines[-1] = f'solid body_{index}', f'endsolid body_{index}'
        blocks.append('\n'.join(lines))
    path = tmp_path / 'named.stl'
    path.write_text('\n'.join(blocks) + '\n')
    if count > 1:
        assert isinstance(trimesh.load(path), trimesh.Scene)
    result = DesignGeometryAnalyzer().analyze(path)
    assert result.face_count == 12 * count - int(missing_face)
    assert result.is_watertight is (not missing_face)
    assert result.volume == (0 if missing_face else 1000 * count)
    assert result.bounding_box['z_min'] == -5
    assert result.bounding_box['z_max'] == 10 * count - 5
    if missing_face:
        assert any('非水密' in issue for issue in result.issues)


def test_dfm_preserves_each_scene_instance_and_its_transform(tmp_path):
    scene = trimesh.Scene(trimesh.creation.box([2, 3, 4]))
    geometry_name = next(iter(scene.geometry))
    transform = np.eye(4)
    transform[0, 3] = 10
    scene.graph.update(frame_to='second-instance', matrix=transform, geometry=geometry_name)
    path = tmp_path / 'instances.glb'
    path.write_bytes(scene.export(file_type='glb'))
    result = DesignGeometryAnalyzer().analyze(path)
    assert result.face_count == 24
    assert result.volume == 48
    assert result.bounding_box['x_min'] == -1
    assert result.bounding_box['x_max'] == 11


def test_dfm_keeps_the_existing_binary_single_mesh_analysis(tmp_path):
    path = tmp_path / 'single.stl'
    trimesh.creation.box([10, 8, 7]).export(path)
    result = DesignGeometryAnalyzer().analyze(path)
    assert result.face_count == 12 and result.is_watertight
    assert result.volume == 560


def test_dfm_rejects_an_empty_scene_instead_of_reporting_success(tmp_path):
    path = tmp_path / 'empty.stl'
    path.write_text('solid empty\nendsolid empty\n')
    with pytest.raises(ValueError, match='non-empty triangle mesh'):
        DesignGeometryAnalyzer().analyze(path)
