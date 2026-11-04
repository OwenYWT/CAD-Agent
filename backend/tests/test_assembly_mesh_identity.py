"""STL solid blocks must retain touching components and real open boundaries."""
import pytest
import trimesh
from sandbox.geometry_validation import _validate_stl
from sandbox.mesh_normalization import load_normalized_mesh


@pytest.mark.parametrize('translation',[[0,0,10],[10,0,10],[10,10,10]])
@pytest.mark.parametrize('missing_face',[False,True])
def test_ascii_stl_keeps_component_identity_at_touching_boundaries(tmp_path,translation,missing_face):
    parts=[trimesh.creation.box([10,10,10]),trimesh.creation.box([10,10,10])]
    parts[1].apply_translation(translation)
    if missing_face:parts[1].update_faces(list(range(len(parts[1].faces)-1)))
    blocks=[]
    for i,part in enumerate(parts):
        lines=trimesh.exchange.stl.export_stl_ascii(part).splitlines()
        lines[0]=f'solid component_{i}';lines[-1]=f'endsolid component_{i}'
        blocks.append('\n'.join(lines))
    path=tmp_path/'assembly.stl';path.write_text('\n'.join(blocks)+'\n')
    measured=_validate_stl(path,role='model')
    assert measured['solid_count']==2
    assert measured['is_watertight'] is (not missing_face)
    assert measured['valid'] is (not missing_face)
    if not missing_face:
        assert load_normalized_mesh(path).volume==pytest.approx(2000)
