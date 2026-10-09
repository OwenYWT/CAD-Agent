"""Reopen actual C10 FCStd/STEP exports and measure both requirement versions."""
import json
import math
from pathlib import Path

import FreeCAD as App
import Part

root = Path('/measurements')
rows = []
for name, thickness in [('original', 9), ('revised', 10)]:
    document = App.openDocument(str(root / (name + '.FCStd')))
    try:
        document.recompute()
        bodies = [obj for obj in document.Objects if obj.TypeId == 'PartDesign::Body']
        assert len(bodies) == 1 and bodies[0].Tip is not None
        assert not [obj.Name for obj in document.Objects if 'Invalid' in obj.State]
        shapes = {'fcstd': bodies[0].Shape, 'step': Part.read(str(root / (name + '.step')))}
        for kind, shape in shapes.items():
            bounds = shape.BoundBox
            assert shape.isValid() and len(shape.Solids) == 1, (name, kind)
            actual = [bounds.XLength, bounds.YLength, bounds.ZLength]
            assert all(abs(a-b) < 1e-6 for a,b in zip(actual, [60,40,thickness])), actual
            expected = (60*40-math.pi*6**2)*thickness
            assert abs(shape.Volume-expected) < 1e-5, shape.Volume
            holes = [face for face in shape.Faces if face.Surface.TypeId == 'Part::GeomCylinder']
            assert len(holes) == 1, (name, kind, len(holes))
            hole = holes[0]
            assert abs(hole.Surface.Radius-6) < 1e-7
            assert abs(abs(hole.Surface.Axis.z)-1) < 1e-7
            assert abs(hole.Surface.Center.x-(bounds.XMin+30)) < 1e-7
            assert abs(hole.Surface.Center.y-(bounds.YMin+20)) < 1e-7
            assert abs(hole.BoundBox.ZLength-thickness) < 1e-6
            for fraction in [.001,.5,.999]:
                assert not shape.isInside(App.Vector(bounds.XMin+30,bounds.YMin+20,bounds.ZMin+thickness*fraction),1e-7,False)
            assert shape.isInside(App.Vector(bounds.XMin+2,bounds.YMin+2,bounds.ZMin+thickness/2),1e-7,False)
            rows.append({'version':name,'format':kind,'dimensions_mm':actual,'volume_mm3':shape.Volume,
                'solid_count':len(shape.Solids),'hole_count':len(holes),'hole_diameter_mm':hole.Surface.Radius*2,
                'hole_depth_mm':hole.BoundBox.ZLength,'centered':True,'through':True})
    finally:
        App.closeDocument(document.Name)
print('CAD_REQUIREMENT_REVISION_MEASUREMENTS='+json.dumps({'passed':True,'measurements':rows}))
