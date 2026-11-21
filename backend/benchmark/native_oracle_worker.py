"""Independent file readback for native_oracle, inside the pinned FreeCAD sandbox."""
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0,'/opt/cad-agent')
sys.path.insert(0,'/oracle')
import FreeCAD as App
import Part
import freecad_entry as runner
from freecad_scene import component_shapes
from native_oracle import compare, read_frozen, write_json


def measure(shape, axis):
    checked = shape.check(True)
    bounds = shape.BoundBox
    levels = [sum(a*b for a,b in zip(vertex.Point,axis)) for vertex in shape.Vertexes]
    holes=[]
    for face in shape.Faces:
        surface = face.Surface
        if surface.TypeId != 'Part::GeomCylinder':continue
        ends = [list(edge.Curve.Center) for edge in face.Edges if edge.Curve.TypeId == 'Part::GeomCircle']
        end_levels = [sum(a*b for a,b in zip(point,axis)) for point in ends]
        through = all(any(abs(level-edge) < 1e-6 for edge in end_levels) for level in (min(levels),max(levels)))
        holes.append({'center_mm':list(surface.Center),'axis':list(surface.Axis),'radius_mm':surface.Radius,
                      'depth_mm':face.Area/(2*math.pi*surface.Radius),'ends_mm':ends,'through':through})
    solids = list(shape.Solids)
    volume = sum(solid.Volume for solid in solids)
    centroid = [sum(solid.Volume*list(solid.CenterOfMass)[i] for solid in solids)/volume for i in range(3)] if volume else None
    return {'valid':shape.isValid() and not checked,'solid_count':len(solids),
            'volume_mm3':shape.Volume,'area_mm2':shape.Area,'centroid_mm':centroid,
            'bounds_min':[bounds.XMin,bounds.YMin,bounds.ZMin],
            'bounds_max':[bounds.XMax,bounds.YMax,bounds.ZMax],'holes':holes}


case,plan,oracle,identity = read_frozen(Path('/case'))
report={'input':identity,'invocation':os.environ['CAD_ORACLE_INVOCATION'],
        'runtime':{'freecad':'.'.join(App.Version()[:3]),'occt':Part.OCC_VERSION},
        'execution_path':'typed_freecad_runner','model_calls':0,
        'runtime_source_sha256':{name:hashlib.sha256((Path('/opt/cad-agent')/name).read_bytes()).hexdigest()
                                for name in ('freecad_entry.py','freecad_native_references.py','freecad_native_reference_contracts.py','freecad_state_projector.py')}}
stage = 'execute'
try:
    result = runner.run_task({'schema_version':'mcad-capability-task.v1','capability':'freecad',
        'operation':'execute','inputs':{},'params':{'plan':plan}})
    stage = 'readback'
    document = App.openDocument(result['files']['fcstd'])
    try:
        components = component_shapes(document)
        native = Part.makeCompound([shape for _,shape in components])
        step = Part.read(result['files']['step'])
        measurements={'fcstd':measure(native,case['frame']['normal']), 'step':measure(step,case['frame']['normal'])}
        failures=[kind+'.'+failure for kind,facts in measurements.items() for failure in compare(oracle,facts)]
        difference = native.cut(step).Volume + step.cut(native).Volume
        if not math.isfinite(difference) or difference > max(1e-6,oracle['volume_mm3']*1e-8):
            failures.append('roundtrip.material_difference')
        report.update(status='failed' if failures else 'passed',failed_assertions=failures,
                      measurements=measurements,symmetric_difference_mm3=difference)
    finally:
        App.closeDocument(document.Name)
    report['artifacts']=[{'role':role,'filename':Path(path).name,
                         'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                        for role,path in result['files'].items()]
except Exception as exc:
    details = getattr(exc,'details',{}) or {}
    diagnostic = json.loads(details['cut_diagnostic_json']) if details.get('cut_diagnostic_json') else {}
    report.update(status='failed' if stage == 'execute' and isinstance(exc,runner.FreeCADRunnerError) else 'infrastructure_error',
                  stage=stage,error_code=getattr(exc,'code',type(exc).__name__),
                  diagnosis=diagnostic.get('classification'),
                  failed_action=getattr(exc,'action',None),failed_op_id=getattr(exc,'op_id',None),message=str(exc))
    traceback.print_exc()
write_json(Path('/sandbox/output/report.json'),report)
print('CAD_NATIVE_ORACLE_CASE='+json.dumps(report,allow_nan=False))
