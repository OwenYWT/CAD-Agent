"""Curves and polygons have real editable Sketcher constraints, not fixed meshes."""
import json
import math
import sys
sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner
App = runner.App

checks = []
for geometry in [
    {'kind': 'arc', 'center': {'x': 0, 'y': 45}, 'radius_mm': 45, 'start_angle_deg': 270, 'end_angle_deg': 360},
    {'kind': 'arc', 'center': {'x': -3, 'y': 0}, 'radius_mm': 9, 'start_angle_deg': 0, 'end_angle_deg': 65},
    {'kind': 'regular_polygon', 'center': {'x': 0, 'y': 0}, 'radius_mm': 6, 'sides': 6, 'rotation_deg': 0},
    {'kind': 'regular_polygon', 'center': {'x': -8, 'y': 3}, 'radius_mm': 11, 'sides': 5, 'rotation_deg': 23},
]:
    d = App.newDocument('Profile')
    try:
        runner._sketch_create(d, {'name': 'Profile'})
        result = runner._sketch_add_profile(d, {'sketch': 'Profile', 'geometry': geometry})
        d.recompute()
        sketch = d.getObject('Profile')
        assert sketch.solve() == 0 and sketch.FullyConstrained
        assert not sketch.RedundantConstraints and not sketch.ConflictingConstraints
        radii = [i for i,c in enumerate(sketch.Constraints) if c.Type=='Radius']
        assert len(radii)==1
        sketch.setDatum(radii[0], App.Units.Quantity(f"{geometry['radius_mm']*1.2} mm"))
        d.recompute()
        assert sketch.solve()==0 and sketch.FullyConstrained
        if geometry['kind']=='arc':
            arc = sketch.Geometry[result['geometry_indexes'][0]]
            assert abs(arc.Radius-geometry['radius_mm']*1.2)<1e-6
        else:
            edges = [sketch.Geometry[i] for i in result['geometry_indexes']]
            expected = 2*geometry['radius_mm']*1.2*math.sin(math.pi/geometry['sides'])
            assert all(abs((e.EndPoint-e.StartPoint).Length-expected)<1e-6 for e in edges)
        angles=[i for i,c in enumerate(sketch.Constraints) if c.Type=='Angle']
        assert angles
        i=angles[-1];wanted=math.degrees(sketch.Constraints[i].Value)+5
        runner._sketch_set_constraint(d,{'sketch':'Profile','constraint_index':i,
            'expected_type':'Angle','value_deg':wanted})
        d.recompute()
        assert sketch.solve()==0 and sketch.FullyConstrained
        assert abs(math.degrees(sketch.Constraints[i].Value)-wanted)<1e-6
        checks.append(geometry['kind'])
    finally:
        App.closeDocument(d.Name)
print('CAD_CURVE_PROFILE_CONTRACT='+json.dumps({'constrained_editable_profiles':checks, 'angle_edits':len(checks)}))
