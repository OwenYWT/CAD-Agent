"""Independently measure the STEP exports produced by the real HTTP edit chain."""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner

root = Path('/measurements')
cases = json.loads((root/'measurements.json').read_text())
assert {case['kind'] for case in cases} == {
    'pattern','revolve','counterbore','counterbore_batch','counterbore_batch_reverse'}
for case in cases:
    shape = runner.Part.read(str(root/Path(case['step']).name))
    assert shape.isValid() and len(shape.Solids) == 1, case['kind']
    assert abs(shape.Volume-case['expected_volume']) < 1e-4, (case['kind'], shape.Volume)
    case['measured_volume'] = shape.Volume
    case['measured_solids'] = len(shape.Solids)
    if case['kind'] == 'pattern':
        holes = [f for f in shape.Faces if f.Surface.TypeId == 'Part::GeomCylinder'
                 and abs(f.Surface.Radius-2) < 1e-7]
        assert len(holes) == case['after']
    if case['kind'].startswith('counterbore_batch'):
        cylinders = {round(f.Surface.Radius, 7): f for f in shape.Faces
                     if f.Surface.TypeId == 'Part::GeomCylinder'}
        assert abs(cylinders[6].Area / (12 * math.pi) - 3) < 1e-6
        assert abs(cylinders[5].Area / (10 * math.pi) - 9) < 1e-6
print('CAD_NATIVE_PARAMETER_MEASUREMENTS='+json.dumps({'passed':True,'measured_cases':cases}))
