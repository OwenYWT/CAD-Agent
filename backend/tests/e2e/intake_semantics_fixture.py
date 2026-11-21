"""Source-bound fixtures plus receiver verification for real material geometry."""
import json
import sys
from copy import deepcopy
from pathlib import Path

from tests.e2e.source_obligation_fixture import bind
from app.contracts.acceptance_v2 import decode_acceptance
from app.validation.durable_geometry import DurableGeometryReport, verify_geometry_evidence


def fixtures():
    cases = []
    for text in ('构建一个十厘米的正方体', '正方体，边长100mm', 'Create a 10cm cube'):
        contract = bind(text)
        assert sorted((c['kind'], c['comparison']['value']) for c in contract['checks']) == [
            ('cube_edge', 100.), ('cube_shape', 1.)]
        for model, expected in [('cube_good', 'passed'), ('cube_rotated', 'passed'),
                ('cube_wrong_size', 'failed'), ('cube_rectangular', 'indeterminate'),
                ('cube_sheared', 'indeterminate'), ('cube_hollow', 'indeterminate'),
                ('cube_blind_micro', 'indeterminate'), ('cube_blind_nurbs', 'indeterminate'),
                ('cube_notch', 'indeterminate')]:
            cases.append({'model': model, 'expected': expected, 'contract': contract})
    cases.append({'model': 'cube_small', 'expected': 'passed', 'contract': bind('立方体棱长二十五毫米')})
    for text, primitive in [('球体长40mm宽40mm高40mm', 'sphere'), ('圆柱体长40mm宽40mm厚25mm', 'cylinder')]:
        contract = bind(text)
        assert primitive+'_shape' in {c['kind'] for c in contract['checks']}
        for suffix, expected in [('good', 'passed'), ('box', 'indeterminate')]:
            cases.append({'model': primitive+'_'+suffix, 'expected': expected, 'contract': contract})
    for text, kind, models in [
        ('盒体外壁厚度3mm', 'wall_thickness', [('box_good','passed'),('box_wrong_wall','failed'),
            ('box_one_thin_wall','failed'),('box_wrong_floor','passed'),('box_rotated','passed'),
            ('box_solid','indeterminate'),('box_closed','indeterminate'),('box_two_cavities','indeterminate'),
            ('box_blind_micro','indeterminate'),('box_blind_nurbs','indeterminate'),
            ('box_inner_blind_micro','indeterminate'),('box_inner_blind_nurbs','indeterminate'),
            ('box_upper_wall_recess','indeterminate'),('box_stepped_cavity','indeterminate')]),
        ('盒体底板厚度3mm', 'wall_thickness', [('box_good','passed'),('box_wrong_floor','failed'),('box_rotated','passed'),
            ('box_floor_blind_micro','indeterminate'),('box_floor_blind_nurbs','indeterminate'),('box_floor_through','indeterminate')]),
        ('盒体外壁厚度3mm，盒体底板厚度3mm', 'wall_thickness', [('box_good','passed'),
            ('box_wrong_floor','failed'),('box_wrong_wall','failed'),('box_rotated','passed')]),
        ('圆柱体直径40mm，圆柱体高度25mm', 'cylinder_diameter', [('cylinder_good','passed'),
            ('cylinder_rotated','passed'),('cylinder_wrong_radius','failed'),('cylinder_wrong_height','failed'),
            ('cylinder_box','indeterminate'),('cylinder_step','indeterminate'),('cylinder_bore','indeterminate'),
            ('cylinder_blind_nurbs','indeterminate')]),
        ('圆柱体半径2cm，圆柱体高度25mm', 'cylinder_radius', [('cylinder_good','passed'),('cylinder_wrong_radius','failed')]),
        ('球体直径40mm', 'sphere_diameter', [('sphere_good','passed'),('sphere_wrong','failed')]),
    ]:
        contract = bind(text)
        assert kind in [c['kind'] for c in contract['checks']]
        if text.startswith('盒体外壁'):
            assert contract['checks'][0]['scope']['wall_mode'] == 'side_walls'
        for model, expected in models:
            cases.append({'model':model,'expected':expected,'contract':contract})
    return cases


def verify(path):
    line = next(s for s in Path(path).read_text().splitlines() if s.startswith('CAD_INTAKE_SEMANTICS='))
    rows = json.loads(line.split('=', 1)[1])
    mutations = 0
    for row in rows:
        report = DurableGeometryReport.model_validate(row['report'])
        contract = decode_acceptance(row['contract'])
        kwargs = dict(request_sha256=row['report']['request_sha256'], acceptance=contract,
                      expected_solid_count=None, measurement_context=row['context'])
        verify_geometry_evidence(report, **kwargs)
        assert report.outcome == row['expected'], row['model']
        for field in ('complete', 'measured', 'coverage', 'inner_boundary', 'face_complete', 'cavity_height',
                      'cube_axes', 'cube_lengths', 'cube_inner_boundary', 'cube_pairs', 'cube_tolerance'):
            tampered = deepcopy(row['report'])
            evidence = next((e for e in tampered['acceptance']['evidence'] if e['outcome'] != 'indeterminate'
                             and ('material_primitive' in e['details'] or 'cavity_walls' in e['details'])), None)
            if evidence is None:
                continue
            details = evidence['details']
            key = 'material_primitive' if 'material_primitive' in details else 'cavity_walls'
            if field.startswith('cube_') and details.get('material_primitive', {}).get('primitive') != 'cube':
                continue
            if key == 'material_primitive' and field in {'inner_boundary','face_complete','cavity_height'}:
                continue
            if field == 'cube_axes':
                details[key]['geometry']['axes'][1] = details[key]['geometry']['axes'][0]
            elif field == 'cube_lengths':
                details[key]['geometry']['intervals_mm'][0][1] += 1
            elif field == 'cube_inner_boundary':
                details[key]['geometry']['inner_boundary_counts'][0] = 1
            elif field == 'cube_pairs':
                details[key]['geometry']['face_axis_pairs'][1] = details[key]['geometry']['face_axis_pairs'][0]
            elif field == 'cube_tolerance':
                details['numerical_tolerance_mm'] = 1
            elif field == 'complete':
                details[key]['complete'] = False
            elif field == 'measured':
                evidence['measured'][0] += 1
            elif field == 'inner_boundary':
                details[key]['layers'][0]['inner_boundary_count'] = 1
            elif field == 'face_complete':
                details[key]['layers'][0]['complete_face'] = False
            elif field == 'cavity_height':
                details[key]['cavity_height_mm'] = 0
            elif key == 'material_primitive':
                details[key]['covered_face_indices'].pop()
            else:
                details[key]['layers'].pop()
            # Keep the duplicated proof consistent so the receiver must reject
            # the certificate's semantics, not only a mismatch between copies.
            binding = details.get('measurement_binding')
            if binding:
                binding['proof']['selection'][key] = deepcopy(details[key])
            try:
                verify_geometry_evidence(DurableGeometryReport.model_validate(tampered), **kwargs)
            except ValueError:
                mutations += 1
            else:
                raise AssertionError('tampered material evidence accepted: '+field)
    print('CAD_INTAKE_RECEIVER='+json.dumps({'cases':len(rows),'rejected_mutations':mutations}))


if __name__ == '__main__':
    if sys.argv[1] == '--verify':
        verify(sys.argv[2])
    else:
        Path(sys.argv[1]).write_text(json.dumps(fixtures(), ensure_ascii=False))
