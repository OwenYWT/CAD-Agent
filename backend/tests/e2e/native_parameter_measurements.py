"""Independently measure the STEP exports produced by the real HTTP edit chain."""
import json
import sys
from pathlib import Path

sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner

root = Path('/measurements')
cases = json.loads((root/'measurements.json').read_text())
assert {case['kind'] for case in cases} == {'pattern','revolve','counterbore'}
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
print('CAD_NATIVE_PARAMETER_MEASUREMENTS='+json.dumps({'passed':True,'measured_cases':cases}))
