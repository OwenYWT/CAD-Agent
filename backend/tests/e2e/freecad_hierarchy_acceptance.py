"""Inspect native container membership using the actual packaged projector."""
import json
import os
import sys
sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
from freecad_state_projector import project_document

try:
    doc = App.openDocument(os.environ['CAD_HIERARCHY_SOURCE'])
    state = project_document(doc)
    by_name = {obj['name']:obj for obj in state['objects']}
    body = next(obj for obj in doc.Objects if obj.TypeId == 'PartDesign::Body')
    structure = by_name[body.Name]['structure']
    assert structure['status'] == 'measured'
    assert structure['category'] == 'body'
    assert structure['members'] == [member.Name for member in body.Group]
    assert structure['body_tip'] == body.Tip.Name == 'Hole'
    assert structure['members'].index('Pad') < structure['members'].index('Hole')
    assert all(obj['structure']['status'] == 'measured' for obj in state['objects'])
    assert by_name['Pad']['structure']['members'] == []
    assert by_name['Pad']['out'], 'the dependency graph is recorded separately'
    print('CAD_NATIVE_HIERARCHY='+json.dumps({'structure':structure,'pad_dependencies':by_name['Pad']['out'],'objects':len(by_name)}),flush=True)
    App.closeDocument(doc.Name)
except BaseException:
    import traceback
    traceback.print_exc()
    os._exit(1)
