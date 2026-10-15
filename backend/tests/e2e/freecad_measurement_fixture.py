"""Actual non-origin Body and rotated App::Link for read-only binding tests."""
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,'/opt/cad-agent')
import FreeCAD as App
import Part
from freecad_scene import component_shapes

root=Path('/sandbox/input')
doc=App.newDocument('MeasurementBinding')
body=doc.addObject('PartDesign::Body','Bracket')
feature=body.newObject('PartDesign::Feature','BracketGeometry')
feature.Shape=Part.makeBox(50,35,20,App.Vector(-25,-17.5,0)).cut(
    Part.makeBox(40,25,20,App.Vector(-15,-12.5,0))).removeSplitter()
body.Tip=feature
body.Placement=App.Placement(App.Vector(13,-7,11),App.Rotation(App.Vector(0,0,1),20))
link=doc.addObject('App::Link','BracketInstance');link.setLink(body)
link.LinkPlacement=App.Placement(App.Vector(100,50,20),App.Rotation(App.Vector(1,0,0),37))
doc.recompute()
doc.saveAs(str(root/'selected.FCStd'))
components=component_shapes(doc)
assert len(components)==2 and all(s.isValid() for _,s in components)
Part.makeCompound([s for _,s in components]).exportStep(str(root/'final.step'))
next(s for obj,s in components if obj==link).exportStep(str(root/'missing-body.step'))
(root/'native-reference.json').write_text(json.dumps({
    'sha256':hashlib.sha256((root/'selected.FCStd').read_bytes()).hexdigest(),
    'size_bytes':(root/'selected.FCStd').stat().st_size}))
App.closeDocument(doc.Name)
print('CAD_MEASUREMENT_FIXTURE=1')
