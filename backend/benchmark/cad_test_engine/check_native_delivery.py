"""Runs with FreeCADCmd in an offline container; no generation or repair."""
import json
import math
from pathlib import Path
import FreeCAD as App
import Part
import Mesh
from native_delivery_selection import select_final_objects

result={'passed':False,'freecad_version':App.Version()}
doc=None
try:
    inputs=json.loads(Path('/out/delivery-inputs.json').read_text(encoding='utf-8'))
    doc=App.openDocument('/input/'+inputs['fcstd']);doc.recompute()
    objects=select_final_objects(doc.Objects)
    if not objects:raise ValueError('no final native solids')
    native=Part.makeCompound([o.Shape for o in objects]);step=Part.read('/input/'+inputs['step'])
    mesh=Mesh.Mesh('/input/'+inputs['stl'])
    if not native.isValid() or not step.isValid() or not native.Solids or not step.Solids:raise ValueError('invalid native or STEP solids')
    if any('Invalid' in str(o.State) for o in doc.Objects):raise ValueError('document has invalid objects after recompute')
    difference=native.cut(step).Volume+step.cut(native).Volume
    tolerance=max(1e-6,step.Volume*1e-5)
    sb=step.BoundBox;mb=mesh.BoundBox
    sd=[sb.XLength,sb.YLength,sb.ZLength];md=[mb.XLength,mb.YLength,mb.ZLength]
    mesh_bounds_ok=all(abs(a-b)<=max(1e-4,a*.005) for a,b in zip(sd,md))
    passed=difference<=tolerance and len(native.Solids)==len(step.Solids) and mesh.CountFacets>0 and mesh_bounds_ok
    result.update(passed=passed,native_solids=len(native.Solids),step_solids=len(step.Solids),
        symmetric_difference_volume_mm3=difference,tolerance_mm3=tolerance,
        stl_facets=mesh.CountFacets,stl_bounds_match=mesh_bounds_ok,
        native_objects=[o.Name for o in objects],
        limitation='STL checked for readability and bounds; full mesh equivalence and native parameter editability are not certified.')
except Exception as exc:result['error']=type(exc).__name__+': '+str(exc)
finally:
    if doc:App.closeDocument(doc.Name)
Path('/out/native-delivery.json').write_text(json.dumps(result,indent=2))
print('CAD_EVAL_NATIVE_DELIVERY='+str(result['passed']))
raise SystemExit(0 if result['passed'] else 1)
