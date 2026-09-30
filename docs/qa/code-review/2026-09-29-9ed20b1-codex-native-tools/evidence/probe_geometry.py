"""Read-only review probes against real OCCT; no product/test source changes."""
import hashlib
import json
import tempfile
from pathlib import Path

import cadquery as cq
import feature_verification as fv
from geometry_validation import validate_geometry_files

print('MEASURER_SHA256=' + hashlib.sha256(Path(fv.__file__).read_bytes()).hexdigest())
tube = cq.Workplane('XY').circle(10).circle(8).extrude(20).val()
cavity = cq.Solid.makeSphere(0.5, cq.Vector(9, 0, 10), angleDegrees1=-90)
hidden = tube.cut(cavity)
cross_drill = cq.Solid.makeCylinder(1, 25, cq.Vector(-12.5, 0, 10), cq.Vector(1, 0, 0))
perforated = tube.cut(cross_drill)
contract = {'schema_version': 'engineering-acceptance.v1', 'objective': 'radial wall thickness 2 mm',
    'checks': [{'check_id': 'wall', 'kind': 'wall_thickness', 'description': 'radial wall',
        'source_quote': 'radial wall thickness 2 mm', 'source_kind': 'user', 'required': True,
        'nominal': 2, 'scope': {'wall_mode': 'radial', 'axis': [0, 0, 1]}}]}
results = []
for name, shape in [('baseline', tube), ('hidden_cavity', hidden), ('cross_drilled', perforated)]:
    evidence = fv.measure_checks(shape, contract['checks'])
    # An independent Boolean measures missing material inside the expected ring.
    missing = tube.cut(shape)
    record = {'case': name, 'valid': shape.isValid(), 'solids': len(shape.Solids()),
        'missing_material_mm3': missing.Volume(), 'wall': evidence,
        'z_holes': fv.measure_checks(shape, [{'check_id':'holes', 'kind':'hole_count',
            'nominal':1, 'scope': {'axis':[0,0,1]}}])}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'candidate.step'
        cq.exporters.export(shape, str(path))
        report = validate_geometry_files([{'role':'model', 'format':'step', 'path':str(path)}],
            expected_solid_count=1, acceptance=contract)
        record['final_step_report'] = report
    results.append(record)
print('GEOMETRY_REVIEW=' + json.dumps(results))
assert results[0]['wall'][0]['outcome'] == 'passed'
assert results[1]['missing_material_mm3'] > 0
