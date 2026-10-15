"""Independent BRep selection: never choose the faces by the requested result."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import hashlib

sys.path.insert(0, '/opt/cad-agent')
import cadquery as cq
from feature_verification import measure_checks


def check(nominal=25, axis=None):
    return {'check_id': 'gap', 'kind': 'planar_gap', 'nominal': nominal,
            'scope': {'axis': axis} if axis else {}}


def bracket(gap=25):
    outer = cq.Solid.makeBox(50, gap+10, 20, cq.Vector(-25, -gap/2-5, 0))
    opening = cq.Solid.makeBox(40, gap, 20, cq.Vector(-15, -gap/2, 0))
    return outer.cut(opening).clean()


part = bracket()
def shape_digest(shape):
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory)/'source.brep')
        shape.exportBrep(path)
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

original_shape_digest = shape_digest(part)
report = {}
for nominal in (25, 24):
    row = measure_checks(part, [check(nominal)])[0]
    assert row['outcome'] == ('passed' if nominal == 25 else 'failed'), row
    assert abs(row['measured'][0]-25) < 1e-7 and not row['details']['selection_uses_nominal']
report['nominal_does_not_select_faces'] = True
rotated = part.rotate((0, 0, 0), (1, 1, 1), 37).translate((77, -93, 42))
row = measure_checks(rotated, [check()])[0]
assert row['outcome'] == 'passed', row
report['translation_rotation_invariant'] = True
multiple = cq.Compound.makeCompound([part.copy(), bracket(30).translate((80, 0, 0))])
row = measure_checks(multiple, [check(axis=[0, 1, 0])])[0]
assert row['outcome'] == 'indeterminate', row
assert row['details']['error_code'] == 'acceptance_scope_invalid'
report['multiple_gaps_are_not_resolved_by_nominal'] = True
obstruction = cq.Solid.makeBox(10, 2, 10, cq.Vector(-5, -1, 5))
row = measure_checks(cq.Compound.makeCompound([part.copy(), obstruction]), [check(axis=[0, 1, 0])])[0]
assert row['outcome'] == 'indeterminate', row
report['obstructed_gap_not_certified'] = True
boundary = {'check_id': 'legacy', 'kind': 'surface_clearance', 'nominal': 25,
            'scope': {'centers_mm': [[25, -12.5, 10], [25, 12.5, 10]]}}
row = measure_checks(part, [boundary])[0]
assert row['outcome'] == 'indeterminate' and row['details']['error_code'] == 'acceptance_scope_invalid', row
report['ambiguous_legacy_selection_is_not_rewritten'] = True

for label, lo, hi in [
    ('outside', [100, 100, 100], [110, 110, 110]),
    ('one_side', [-10, -13, 1], [10, -12, 19]),
    ('inside_void', [-10, -2, 1], [10, 2, 19]),
    ('line_contact', [25, -13, 1], [26, 13, 19]),
    ('point_contact', [25, -13, 20], [26, 13, 21]),
]:
    scoped = {**check(), 'scope': {'axis': [0, 1, 0], 'region_min_mm': lo, 'region_max_mm': hi}}
    row = measure_checks(part, [scoped])[0]
    assert row['outcome'] == 'indeterminate', row
    assert row['details']['error_code'] == 'acceptance_scope_invalid', row
    assert row['details']['reason'] == 'no_pair', row
    assert row['details']['cad_source_repair_allowed'] is False
    report['empty_crop_' + label] = row['details']
scoped = {**check(30), 'scope': {'axis': [0, 1, 0], 'region_min_mm': [60, -20, 1], 'region_max_mm': [100, 20, 19]}}
row = measure_checks(multiple, [scoped])[0]
assert row['outcome'] == 'passed' and abs(row['measured'][0] - 30) < 1e-7, row
report['earlier_empty_pair_does_not_hide_later_valid_pair'] = True

no_overlap = cq.Compound.makeCompound([
    cq.Solid.makeBox(5, 5, 5), cq.Solid.makeBox(5, 5, 5, cq.Vector(10, 15, 0))])
row = measure_checks(no_overlap, [check(axis=[0,1,0])])[0]
assert row['outcome']=='indeterminate' and row['details']['reason']=='no_pair', row
assert row['details']['empty_intersections'] > 0, row
report['projected_faces_do_not_overlap'] = True

# Failure injection at the OCCT boundary, retaining real source BReps. A failed
# builder must never be treated as the successful zero-material case above.
from unittest.mock import patch
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
class InterruptedBoolean:
    def __init__(self):
        self.builder = BRepAlgoAPI_Common()
    def __getattr__(self, name):
        return getattr(self.builder, name)
    def Build(self):
        return None  # Simulate interruption before OCCT marks its result done.

with patch('OCP.BRepAlgoAPI.BRepAlgoAPI_Common', InterruptedBoolean):
    row = measure_checks(part, [check()])[0]
assert row['outcome']=='indeterminate', row
assert row['details']['error_code']=='acceptance_measurement_failed', row
assert row['details']['cad_source_repair_allowed'] is False, row
assert row['details']['stage']=='projected_overlap', row
assert shape_digest(part) == original_shape_digest
report['native_boolean_failure_is_not_empty_and_source_is_unchanged'] = True

# Consume the actual repaired FreeCAD result, not a substitute generated in CadQuery.
with tempfile.TemporaryDirectory() as folder:
    step = str(Path(folder)/'repaired.step')
    process = subprocess.run(['/opt/freecad/bin/FreeCADCmd', '-c',
        "exec(compile(open('/tests/freecad_scoped_repair_contract.py').read(),'/tests/freecad_scoped_repair_contract.py','exec'))"],
        env={**os.environ, 'SCOPED_STEP_OUTPUT': step}, text=True, capture_output=True)
    assert process.returncode == 0 and 'CAD_SCOPED_REPAIR_CONTRACT=' in process.stdout and 'Traceback' not in process.stdout+process.stderr, (process.stdout+process.stderr)[-5000:]
    actual = cq.importers.importStep(step).val()
    ambiguous = measure_checks(actual, [check(axis=[0, 0, 1])])[0]
    assert ambiguous['outcome'] == 'indeterminate', ambiguous
    report['native_two_openings_require_explicit_scope'] = True
    payload = json.loads(Path('/sandbox/input/scoped-repair.json').read_text())
    original = deepcopy(payload['acceptance'])
    row = measure_checks(actual, original['checks'])[0]
    assert row['outcome'] == 'passed' and abs(row['measured'][0]-25) < 1e-7, row
    assert original == payload['acceptance']
    report['native_repair_saved_step_gap_mm'] = row['measured'][0]
print('CAD_PLANAR_GAP_CONTRACT=' + json.dumps(report))
