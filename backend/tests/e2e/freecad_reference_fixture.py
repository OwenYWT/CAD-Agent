"""Native-only CI fixture for the saved-reference compatibility contract."""
import os
import FreeCAD as App
import Part
path = '/sandbox/output/reference-source.FCStd'
doc = App.newDocument('ReferenceContract')
body = doc.addObject('PartDesign::Body', 'Body')
solid = body.newObject('PartDesign::Feature', 'TestSolid')
solid.Shape = Part.makeBox(60, 40, 8)
doc.recompute(); doc.saveAs(path); App.closeDocument(doc.Name)
os.environ['CAD_REFERENCE_TEST_FCSTD'] = path
exec(compile(open('/tests/freecad_reference_geometry.py').read(), '/tests/freecad_reference_geometry.py', 'exec'))
