"""Run in the real FreeCAD interpreter with the source projector on PYTHONPATH."""
import json
import sys
sys.path.insert(0,'/projector')
import FreeCAD as App
import Part
import Sketcher
from state_projector import project_document

doc=App.newDocument('InspectionAcceptance')
sphere=doc.addObject('PartDesign::Feature','Sphere');sphere.Shape=Part.makeSphere(10)
sketch=doc.addObject('Sketcher::SketchObject','Sketch')
sketch.addGeometry(Part.Circle(App.Vector(5,5,0),App.Vector(0,0,1),3),False)
sketch.addConstraint(Sketcher.Constraint('DistanceX',0,3,5))
sketch.addConstraint(Sketcher.Constraint('DistanceY',0,3,5))
sketch.addConstraint(Sketcher.Constraint('Radius',0,3))
doc.recompute()
state=project_document(doc)
by_name={o['name']:o for o in state['objects']}
assert by_name['Sphere']['shape']['volume']>4000
topology=by_name['Sphere']['inspection']['topology']
assert any(x.get('status')=='unavailable' and x['kind']=='edge' for x in topology['items'])
assert by_name['Sketch']['sketch']['fully_constrained']
detail=by_name['Sketch']['inspection']
assert detail['constraints']['total']==3 and detail['geometry']['total']==1
assert detail['geometry']['items'][0]['radius_mm']==3
assert detail['geometry']['items'][0]['center']==[5,5,0]
assert {c['type'] for c in detail['constraints']['items']}=={'DistanceX','DistanceY','Radius'}
print('CAD_INSPECTION_ACCEPTANCE='+json.dumps(state))
