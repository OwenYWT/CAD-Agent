"""Reopen STEP, measure complete features, and validate the artifact-bound evidence on the host."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile


def fixtures():
    from app.contracts.acceptance_v2 import AcceptanceContractV2
    axis = {'axis': [0, 0, 1]}
    slot = {**axis, 'region_min_mm': [3, 4, 0], 'region_max_mm': [9, 8, 6]}
    bore = [('notched_tube_outer_diameter', axis, 14), ('notched_tube_bore_diameter', axis, 10),
            ('notched_tube_wall_thickness', axis, 2), ('semicircular_cut_diameter', {'axis': [1, 0, 0]}, 6),
            ('semicircular_cut_position', {'axis': [1, 0, 0], 'point_mm': [0, 0, 15]}, 0)]
    notch = [('semicircular_cut_diameter', axis, 6), ('semicircular_cut_position', {**axis, 'point_mm': [0, 11, 0]}, 0)]
    step = [('solid_segment_diameter', {**axis, 'point_mm': [0, 0, 7]}, 10),
            ('solid_segment_height', {**axis, 'point_mm': [0, 0, 7]}, 6)]
    groups = [
        ('fillet', [('fillet_radius', {}, 2)], {'complete': 'passed', 'one_edge': 'indeterminate',
            'wrong_radius': 'failed', 'square': 'indeterminate', 'cavity': 'indeterminate'}),
        ('parallel', [('fillet_radius', axis, 3)], {'translated': 'passed', 'wrong_family': 'indeterminate'}),
        ('slot', [('rectangular_opening_count', axis, 2), ('rectangular_opening_bounds', slot, 0)],
         {'two': 'passed', 'one': 'failed', 'shifted': 'failed', 'blind': 'indeterminate',
          'edge_open': 'indeterminate', 'round_hole': 'indeterminate'}),
        ('notch', notch, {'edge': 'passed', 'shifted': 'failed', 'blind': 'indeterminate',
                          'partial': 'indeterminate', 'cavity': 'indeterminate'}),
        ('tube', bore, {'transverse': 'passed', 'wrong_bore': 'failed', 'wrong_cut': 'failed',
                        'end_cut': 'indeterminate', 'extra_hole': 'indeterminate', 'blocked_bore': 'indeterminate'}),
        ('rotated_tube', [('notched_tube_wall_thickness', {'axis': [-1, 0, 0]}, 2),
             ('semicircular_cut_position', {**axis, 'point_mm': [8, 6, 2]}, 0)], {'translated': 'passed'}),
        ('step', step, {'solid': 'passed', 'short': 'failed', 'tiny_cavity': 'indeterminate', 'bored': 'indeterminate'}),
        ('prism', [('bounding_box', {'component_region': {'min_mm': [-20, -12, -3], 'max_mm': [20, 12, 3]}}, 0)],
         {'centered': 'passed', 'shifted': 'failed'}),
    ]
    rows = []
    for group, measurements, variants in groups:
        objective = 'Measure the specified ' + group
        checks = [dict(check_id=kind.replace('_', '-'), kind=kind, source_quote=objective, description=kind,
                       binding_ref='objective', scope=scope, comparison={'op': 'eq', 'value': value,
                       'unit': 'count' if kind.endswith('_count') else 'mm'}) for kind, scope, value in measurements]
        contract = AcceptanceContractV2(objective=objective, checks=checks).model_dump(mode='json')
        rows.extend(dict(group=group, name=name, expected=outcome, contract=contract)
                    for name, outcome in variants.items())
    return rows


def make_part(group, name):
    import cadquery as cq
    v = cq.Vector
    box = cq.Solid.makeBox
    cyl = cq.Solid.makeCylinder
    blank = box(40, 24, 6)
    if group == 'prism':
        return blank.translate((-17 if name == 'shifted' else -20, -12, -3))
    if group in {'fillet', 'parallel'}:
        if name == 'square':
            return blank
        edges = blank.Edges()
        if group == 'parallel':
            edges = [e for e in edges if abs(e.tangentAt().toTuple()[0 if name == 'wrong_family' else 2]) > .99]
            return blank.fillet(2 if name == 'wrong_family' else 3, edges).translate((4, -9, 2))
        shape = blank.fillet(1 if name == 'wrong_radius' else 2, edges[:1] if name == 'one_edge' else edges)
        return shape.cut(cq.Solid.makeSphere(.2, v(13, 12, 3))) if name == 'cavity' else shape
    if group == 'slot':
        x = 4 if name == 'shifted' else 3
        part = blank.cut(box(6, 4, 3 if name == 'blind' else 6, v(x, 4, 0)))
        if name != 'one':
            part = part.cut(box(5, 8, 6, v(0 if name == 'edge_open' else 20, 12, 0)))
        return part.cut(cyl(1, 6, v(15, 5, 0))) if name == 'round_hole' else part
    if group == 'notch':
        center = v(1 if name == 'partial' else 0, 13 if name == 'shifted' else 11, 0)
        part = blank.cut(cyl(3, 3 if name == 'blind' else 6, center))
        return part.cut(cq.Solid.makeSphere(.2, v(23, 12, 3))) if name == 'cavity' else part
    if group == 'step':
        part = cyl(8, 4).fuse(cyl(5, 5 if name == 'short' else 6, v(0, 0, 4))).clean()
        if name == 'bored':
            return part.cut(cyl(1, 10))
        return part.cut(cq.Solid.makeSphere(.01, v(2, 2, 7))) if name == 'tiny_cavity' else part
    tube = cyl(7, 30).cut(cyl(4 if name == 'wrong_bore' else 5, 29 if name == 'blocked_bore' else 30))
    z = 2 if name == 'end_cut' else 15
    transverse = cyl(4 if name == 'wrong_cut' else 3, 18, v(-9, 0, z), v(1, 0, 0))
    half = box(18, 10, 12, v(-9, 0, z-6))
    part = tube.cut(transverse.intersect(half)).clean()
    if name == 'extra_hole':
        part = part.cut(cyl(1, 18, v(-9, 0, 21), v(1, 0, 0)))
    if group == 'rotated_tube':
        return part.rotate((0, 0, 0), (0, 1, 0), 90).translate((-7, 6, 2))
    return part


def native(cases):
    import cadquery as cq
    from geometry_validation import validate_geometry_files
    with tempfile.TemporaryDirectory() as directory:
        for case in cases:
            name = case['group'] + '-' + case['name']
            shape = make_part(case['group'], case['name'])
            assert shape.isValid() and len(shape.Solids()) == 1, name
            path = Path(directory) / (name + '.step')
            cq.exporters.export(shape, str(path))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            context = dict(schema_version='measurement-context.v1', base_revision_id='machined-blank',
                           checkpoint_manifest_id=name, artifact_sha256s=[digest])
            report = validate_geometry_files([dict(role='model', format='step', path=str(path))],
                acceptance=case['contract'], measurement_context=context)
            assert report['outcome'] == case['expected'], (name, report['acceptance']['evidence'])
            print(json.dumps(dict(name=name, context=context, contract=case['contract'], report=report)))
    print('CAD_MACHINED_FEATURES=' + str(len(cases)))


def verify(path):
    from app.contracts.acceptance_v2 import decode_acceptance
    from app.contracts.measurement_binding import binding_digest
    from app.validation.durable_geometry import DurableGeometryReport, verify_geometry_evidence
    count = mutations = 0
    for line in Path(path).read_text().splitlines():
        if not line.startswith('{'):
            continue
        row = json.loads(line)
        report = row['report']
        args = dict(request_sha256=report['request_sha256'], acceptance=decode_acceptance(row['contract']),
                    expected_solid_count=None, measurement_context=row['context'])
        verify_geometry_evidence(DurableGeometryReport.model_validate(report), **args)
        count += 1
        for index, evidence in enumerate(report['acceptance']['evidence']):
            key = next((key for key in ('machined_feature', 'solid_segment') if key in evidence['details']), None)
            if evidence['outcome'] == 'indeterminate' or key is None:
                continue
            for field in ('method', 'measured', 'difference', 'artifact'):
                tampered = deepcopy(report)
                item = tampered['acceptance']['evidence'][index]
                binding = item['details']['measurement_binding']
                if field == 'method':
                    item['method'] = 'unmeasured'
                elif field == 'measured':
                    item['measured'][0] += 1
                elif field == 'artifact':
                    binding['artifact_sha256'] = '0' * 64
                else:
                    cert = item['details'][key]
                    if key == 'solid_segment':
                        cert['missing_material_mm3'] = 1.e-12
                    else:
                        cert['symmetric_difference_solids'][0] = 1
                    binding['proof']['selection'][key] = deepcopy(cert)
                binding['binding_digest'] = binding_digest({k:v for k,v in binding.items() if k != 'binding_digest'})
                try:
                    verify_geometry_evidence(DurableGeometryReport.model_validate(tampered), **args)
                except ValueError:
                    mutations += 1
                else:
                    raise AssertionError((row['name'], 'accepted altered evidence', field))
    assert count == len(fixtures()) and mutations > 0
    print('CAD_MACHINED_RECEIVER=' + json.dumps(dict(cases=count, rejected_mutations=mutations)))


if __name__ == '__main__':
    if sys.argv[1] == '--fixtures':
        Path(sys.argv[2]).write_text(json.dumps(fixtures()))
    elif sys.argv[1] == '--verify':
        verify(sys.argv[2])
    else:
        native(json.loads(Path(sys.argv[1]).read_text()))
