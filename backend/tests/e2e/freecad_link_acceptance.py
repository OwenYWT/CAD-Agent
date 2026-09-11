"""Measure real App::Link transforms used by component rendering."""
import json
import FreeCAD as App
import Part

document = App.newDocument('Links')
body = document.addObject('PartDesign::Body', 'Body')
solid = body.newObject('PartDesign::Feature', 'Solid')
solid.Shape = Part.makeBox(10, 10, 10)
document.recompute()
body.Placement.Base.x = 10
link = document.addObject('App::Link', 'Instance')
link.setLink(body)
link.Placement.Base.x = 40
document.recompute()
facts = []
for obj in [body, solid, link]:
    facts.append({'name': obj.Name, 'shape_position': list(obj.Shape.Placement.toMatrix().A),
        'global': list(obj.getGlobalPlacement().toMatrix().A) if hasattr(obj, 'getGlobalPlacement') else None, 'bound_x': obj.Shape.BoundBox.XMin,
        'visibility': str(getattr(obj, 'Visibility', None)), 'parents': [p.Name for p in obj.InList]})
print('CAD_LINK_FACTS=' + json.dumps(facts), flush=True)
App.closeDocument(document.Name)
