"""Real geometry/LOD/cache/instance/export checks in the pinned FreeCAD runtime."""
import json
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, '/scene-source')
import FreeCAD as App
import Part
from freecad_scene import component_shapes, tessellate_scene

with tempfile.TemporaryDirectory() as root:
    output = Path(root)
    doc = App.newDocument('SceneAcceptance')
    for name, x in [('A', 0), ('B', 30)]:
        body = doc.addObject('PartDesign::Body', 'Body' + name)
        solid = body.newObject('PartDesign::Feature', 'Solid' + name)
        solid.Shape = Part.makeCylinder(5 if name == 'A' else 6, 10)
        body.Placement.Base.x = x
    doc.recompute()
    link = doc.addObject('App::Link', 'InstanceA')
    link.setLink(doc.BodyA)
    link.Placement.Base.x = 60
    doc.recompute()
    path = output / 'model.FCStd'
    doc.saveAs(str(path))
    App.closeDocument(doc.Name)
    doc = App.openDocument(str(path))
    files = tessellate_scene(doc, output)
    first = json.loads(Path(files['scene']).read_text())
    assert len(first['instances']) == 3 and len(first['definitions']) == 2, first
    by_name = {i['kernel_name']: i for i in first['instances']}
    assert by_name['BodyA']['geometry_sha256'] == by_name['InstanceA']['geometry_sha256']
    assert by_name['InstanceA']['matrix'][3] == 60 and by_name['BodyB']['matrix'][3] == 30
    for definition in first['definitions'].values():
        counts = [definition['lods'][lod]['triangles'] for lod in ['coarse', 'medium', 'fine']]
        assert counts[0] < counts[1] < counts[2], counts
    files = tessellate_scene(doc, output, list(first['definitions']))
    reused = json.loads(Path(files['scene']).read_text())
    assert reused['generated_definitions'] == 0 and reused['reused_definitions'] == 2
    with zipfile.ZipFile(files['meshes']) as archive: assert not archive.namelist()
    doc.SolidB.Shape = Part.makeCylinder(6, 12)
    doc.InstanceA.Placement.Base.x = 75
    doc.recompute(); doc.saveAs(str(path)); App.closeDocument(doc.Name)
    doc = App.openDocument(str(path))
    files = tessellate_scene(doc, output, list(first['definitions']))
    edited = json.loads(Path(files['scene']).read_text())
    assert edited['generated_definitions'] == 1 and edited['reused_definitions'] == 1
    assert next(i for i in edited['instances'] if i['kernel_name'] == 'InstanceA')['matrix'][3] == 75
    final = Part.makeCompound([shape for _, shape in component_shapes(doc)])
    assert len(final.Solids) == 3
    expected_volume = 2 * 3.141592653589793 * 25 * 10 + 3.141592653589793 * 36 * 12
    assert abs(final.Volume - expected_volume) < 1e-6
    final.exportStep(str(output / 'assembly.step'))
    restored = Part.read(str(output / 'assembly.step'))
    assert len(restored.Solids) == 3 and abs(restored.Volume - expected_volume) < 1e-6
    App.closeDocument(doc.Name)
    print('CAD_SCENE_ACCEPTANCE=' + json.dumps({'three_instances_two_definitions':True,
        'all_lods_measured':True,'unchanged_geometry_reused':True,'only_changed_component_tessellated':True,
        'instance_translation_only':True,'step_preserves_all_solids':True}), flush=True)
