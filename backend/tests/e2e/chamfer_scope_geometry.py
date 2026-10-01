"""Independent final-solid oracle; executed inside the real FreeCAD runtime.

The reference selects box perimeter by coordinates, independently of production
topology classification. Both the saved Body Tip and reimported STEP are checked.
"""
import json
import os
import sys
from pathlib import Path

import FreeCAD as App
import Import
import Part

sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner
from freecad_edge_scope import EdgeScopeError, resolve_edge_scope, verify_protected_faces


def measure(shape, expected, *, radius, height, center, chamfer_hole):
    assert shape.isValid() and len(shape.Solids) == 1
    delta = shape.cut(expected).Volume + expected.cut(shape).Volume
    assert delta < 1e-5, f'final solid differs from requested region: {delta} mm3'
    cylinders = [f for f in shape.Faces if f.Surface.TypeId == 'Part::GeomCylinder']
    assert len(cylinders) == 1
    cylinder = cylinders[0]
    assert abs(cylinder.Surface.Radius - radius) < 1e-6
    expected_depth = height - (2 if chamfer_hole else 0)
    assert abs(cylinder.BoundBox.ZLength - expected_depth) < 1e-5
    mouths = [e.Curve.Radius * 2 for e in shape.Edges
              if e.Curve.TypeId == 'Part::GeomCircle'
              and abs(e.Curve.Center.x - center.x) < 1e-6
              and abs(e.Curve.Center.y - center.y) < 1e-6
              and (abs(e.Curve.Center.z) < 1e-6 or abs(e.Curve.Center.z-height) < 1e-6)]
    assert len(mouths) == 2
    assert all(abs(d - 2*(radius + (1 if chamfer_hole else 0))) < 1e-5 for d in mouths)
    return {'symmetric_difference_mm3': delta, 'mouth_diameters_mm': mouths,
            'straight_hole_depth_mm': cylinder.BoundBox.ZLength}


def run_case(args, scope, dimensions, radius, *, target_body=False):
    width, depth, height = dimensions
    center = App.Vector(width/2, depth/2, 0)
    original = Part.makeBox(width, depth, height).cut(Part.makeCylinder(radius,height,center))
    edges = []
    for edge in original.Edges:
        points = [v.Point for v in edge.Vertexes]
        perimeter = (edge.Curve.TypeId == 'Part::GeomLine' and len(points) == 2
            and sum(all(abs(getattr(p, axis)-bound) < 1e-6 for p in points)
                    for axis, bounds in [('x',(0,width)),('y',(0,depth)),('z',(0,height))]
                    for bound in bounds) >= 2)
        mouth = edge.Curve.TypeId == 'Part::GeomCircle'
        if (scope in {'outer','all'} and perimeter) or (scope in {'hole_mouths','all'} and mouth):
            edges.append(edge)
    assert len(edges) == {'outer':12,'hole_mouths':2,'all':14}[scope]
    expected = original.makeChamfer(1, edges)
    doc = App.newDocument('ScopeContract')
    body = doc.addObject('PartDesign::Body','Body')
    source = body.newObject('PartDesign::Feature','UnrelatedLabel')
    source.Shape = original
    doc.recompute()
    args = {**args, 'target':body.Name if target_body else source.Name, 'name':'EdgeTreatment', 'size_mm':1}
    result = runner._feature_chamfer(doc, runner._keys('feature.chamfer', args))
    runner._validate_document(doc, op_id='scope-contract', action='feature.chamfer')
    assert body.Tip.Name == result['object']
    stem = Path('/sandbox/output') / f'{scope}-{radius}'
    doc.saveAs(str(stem.with_suffix('.FCStd')))
    Import.export([body],str(stem.with_suffix('.step')))
    App.closeDocument(doc.Name)
    saved = App.openDocument(str(stem.with_suffix('.FCStd')))
    tip = saved.getObject('Body').Tip.Shape
    facts = {kind:measure(shape,expected,radius=radius,height=height,center=center,
                         chamfer_hole=scope in {'hole_mouths','all'})
             for kind,shape in [('body_tip',tip),('step',Part.read(str(stem.with_suffix('.step'))))]}
    App.closeDocument(saved.Name)
    return {'scope':scope,'radius':radius,'target_body':target_body,'measurements':facts}


def body_target_contracts():
    doc = App.newDocument('BodyTargets')
    # The target's identity, not a conventional name, determines its body.
    unrelated = doc.addObject('PartDesign::Body', 'Body')
    body = doc.addObject('PartDesign::Body', 'ActualPart')
    source = body.newObject('PartDesign::Feature', 'ActualTip')
    source.Shape = Part.makeBox(30, 20, 10)
    doc.recompute()
    expected = source.Shape.makeFillet(1, source.Shape.Edges)
    result = runner._feature_fillet(doc, {'target':body.Name, 'name':'Rounded', 'radius_mm':1, 'use_all_edges':True})
    runner._validate_document(doc, op_id='fillet-body', action='feature.fillet')
    assert body.Tip.Name == result['object'] and unrelated.Tip is None
    assert body.Tip.Base[0] == source
    assert body.Tip.Shape.isValid()
    assert body.Tip.Shape.cut(expected).Volume + expected.cut(body.Tip.Shape).Volume < 1e-5
    for args, code in [({'target':body.Name,'selector':{}}, 'topology_target_mismatch'),
                       ({'target':unrelated.Name}, 'invalid_dressup_target')]:
        try:
            runner._dressup_target(doc, args, 'test')
        except runner.FreeCADRunnerError as error:
            assert error.code == code
        else:
            raise AssertionError('ambiguous/empty Body target was silently accepted')
    App.closeDocument(doc.Name)
    return {'body_fillet_tip_reference':True,'non_default_body':True,'ambiguous_body_selector_rejected':True,'empty_body_rejected':True}


def adversarial_topologies():
    # Coordinate-independent production selection must still work after rotation
    # and with multiple off-center bores; the reference is classified beforehand.
    original = Part.makeBox(90, 50, 12)
    for x, y, radius in [(21, 17, 3), (63, 32, 5)]:
        original = original.cut(Part.makeCylinder(radius, 12, App.Vector(x, y, 0)))
    perimeter = [e for e in original.Edges if e.Curve.TypeId == 'Part::GeomLine'
                 and len(e.Vertexes) == 2 and sum(
                     all(abs(getattr(v.Point, axis)-bound) < 1e-6 for v in e.Vertexes)
                     for axis, bounds in [('x', (0,90)), ('y', (0,50)), ('z', (0,12))]
                     for bound in bounds) >= 2]
    assert len(perimeter) == 12
    expected = original.makeChamfer(1, perimeter)
    for shape in (original, expected):
        shape.rotate(App.Vector(0,0,0), App.Vector(1,2,3), 37)
        shape.translate(App.Vector(17,-29,41))
    names, protected = resolve_edge_scope(original, 'outer')
    assert len(names) == 12 and len(protected) == 2
    actual = original.makeChamfer(1, [original.Edges[int(name[4:])-1] for name in names])
    assert actual.cut(expected).Volume + expected.cut(actual).Volume < 1e-5
    verify_protected_faces(protected, actual)
    unsupported = [Part.makeSphere(10), Part.makeCylinder(10,20),
                   Part.makeBox(30,30,10).cut(Part.makeBox(10,10,10,App.Vector(20,20,0)))]
    for shape in unsupported:
        try:
            resolve_edge_scope(shape, 'outer')
        except EdgeScopeError:
            pass
        else:
            raise AssertionError('ambiguous topology must not silently become all edges')
    return {'rotated_multi_bore': True, 'unsupported_shapes_rejected': len(unsupported)}


if __name__ == '__main__':
    if os.environ.get('CAD_SCOPE_VERIFY_ARTIFACTS'):
        doc = App.openDocument('/sandbox/input/model.FCStd')
        bodies = [o for o in doc.Objects if o.TypeId == 'PartDesign::Body']
        assert len(bodies) == 1
        reference = Part.makeBox(100,60,10).cut(Part.makeCylinder(4,10,App.Vector(50,30,0)))
        outer = [e for e in reference.Edges if e.Curve.TypeId == 'Part::GeomLine'
                 and len(e.Vertexes) == 2 and sum(
                     all(abs(getattr(v.Point,axis)-bound)<1e-6 for v in e.Vertexes)
                     for axis,bounds in [('x',(0,100)),('y',(0,60)),('z',(0,10))]
                     for bound in bounds) >= 2]
        assert len(outer) == 12
        reference = reference.makeChamfer(1,outer)
        facts = {}
        for kind,shape in [('body_tip',bodies[0].Tip.Shape.copy()),
                           ('step',Part.read('/sandbox/input/model.step'))]:
            b = shape.BoundBox
            assert all(abs(actual-wanted)<1e-6 for actual,wanted in
                       zip((b.XLength,b.YLength,b.ZLength),(100,60,10)))
            shape.translate(App.Vector(-b.XMin,-b.YMin,-b.ZMin))
            facts[kind] = measure(shape,reference,radius=4,height=10,
                                  center=App.Vector(50,30,0),chamfer_hole=False)
        print('CAD_CHAMFER_SCOPE='+json.dumps(facts),flush=True)
        App.closeDocument(doc.Name)
        sys.exit(0)
    supplied = Path('/sandbox/input/chamfer-args.json')
    if supplied.exists():
        cases = [(json.loads(supplied.read_text()),'outer')]
    else:
        cases = [({'edge_scope':scope,'use_all_edges':False},scope)
                 for scope in ('outer','hole_mouths','all')]
    results = [run_case(args,scope,dims,radius) for args,scope in cases
               for dims,radius in [((100,60,10),4),((72,48,14),5.5)]]
    results.extend(run_case(args,scope,(100,60,10),4,target_body=True) for args,scope in cases)
    results.append(body_target_contracts())
    results.append(adversarial_topologies())
    print('CAD_CHAMFER_SCOPE='+json.dumps(results),flush=True)
