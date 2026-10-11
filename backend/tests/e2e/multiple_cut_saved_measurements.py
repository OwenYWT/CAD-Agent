"""Independent measurements after reading the committed artifact from object storage."""
import json
import math
import FreeCAD as App

document = App.openDocument('/sandbox/input/model.FCStd')
try:
    document.recompute()
    body = document.getObject('HeadphoneHook')
    assert body.Shape.isValid() and len(body.Shape.Solids) == 1
    sizes = []
    for name in ('BasePad', 'ThroatPocket', 'ClampPocket', 'ScrewPocket', 'TipPocket'):
        feature = document.getObject(name)
        assert feature.Shape.isValid() and len(feature.Shape.Solids) == 1
        sizes.append(feature.Shape.Volume)
    assert all(previous > current for previous, current in zip(sizes, sizes[1:])), sizes
    # The screw cuts the 5 mm outer wall, with one 6 mm shaft.
    holes = [face for face in body.Shape.Faces if face.Surface.TypeId == 'Part::GeomCylinder']
    assert len(holes) == 1 and abs(holes[0].Surface.Radius - 3) < 1e-7
    assert abs(holes[0].Area / (6 * math.pi) - 5) < 1e-7
    assert abs((sizes[2] - sizes[3]) - math.pi * 9 * 5) < 1e-6
    assert document.TipUpturn.FullyConstrained
    assert document.ScrewHole.Shape.BoundBox.ZMax < 0
    assert document.TipUpturn.Shape.BoundBox.ZMax < 0
    print('CAD_MULTIPLE_CUT_SAVED=' + json.dumps({'saved_reopened': True, 'solid_count': 1,
        'hole_count': 1, 'hole_diameter_mm': 6, 'hole_depth_mm': 5, 'feature_volumes': sizes}))
finally:
    App.closeDocument(document.Name)
