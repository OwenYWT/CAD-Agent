"""Reopen the committed FCStd in a fresh kernel and check frozen criteria."""
import json
import FreeCAD as App
import Part

criteria = json.load(open('/sandbox/input/acceptance.json'))
document = App.openDocument('/sandbox/input/model.FCStd')
try:
    document.recompute()
    bodies = [obj for obj in document.Objects if obj.TypeId == 'PartDesign::Body']
    assert len(bodies) == 1
    shape = bodies[0].Shape
    assert shape.isValid() and not shape.isNull()
    measured = []
    for check in criteria['checks']:
        if check['kind'] == 'solid_count':
            value = len(shape.Solids)
        elif check['kind'] == 'surface_clearance':
            points = [Part.Vertex(App.Vector(*p)) for p in check['scope']['centers_mm']]
            faces = [next(face for face in shape.Faces if face.distToShape(point)[0] < 1e-7) for point in points]
            value = faces[0].distToShape(faces[1])[0]
        else:
            raise AssertionError('unsupported saved-model check: ' + check['kind'])
        assert abs(value - check['nominal']) < 1e-6, (check['check_id'], value, check['nominal'])
        measured.append({'check_id': check['check_id'], 'value': value})
    sketches = [obj for obj in document.Objects if obj.TypeId == 'Sketcher::SketchObject']
    assert sketches and all(sketch.solve() == 0 for sketch in sketches)
    assert all(not obj.State or 'Invalid' not in obj.State for obj in document.Objects)
    print('CAD_CONSTRAINT_SAVED_MEASUREMENTS=' + json.dumps({'passed': True, 'checks': measured,
        'fresh_document_reopened': True, 'recomputed': True, 'sketch_count': len(sketches)}))
finally:
    App.closeDocument(document.Name)
