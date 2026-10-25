"""Freeze source-bound contracts for the independent, real CAD worker test."""
import json
from pathlib import Path
import sys
from copy import deepcopy

from app.contracts.acceptance_v2 import AcceptanceContractV2, RequirementBindingError
from app.contracts.acceptance import AcceptanceContract
from app.validation.requirement_binding import extract_requirements, validate_requirement_bindings


def bind(text):
    result = extract_requirements(text)
    checks = [{'check_id': str(index), 'binding_ref': r['requirement_id'], 'kind': r['kind'],
               'description': r['property'], 'source_quote': r['source_quote'],
               'comparison': r['comparison'], 'scope': r['scope'],
               **({'hole_group': r['hole_group']} if r.get('hole_group') else {})}
              for index, r in enumerate(result['requirements'])]
    contract = AcceptanceContractV2.model_validate({'objective': text, 'checks': checks,
        'verification_limits': [u['reason'] for u in result['unsupported']]})
    validate_requirement_bindings(text, contract)
    return contract.model_dump(mode='json')


def fixtures():
    cases = []
    for text, count in [('4个直径6 mm的通孔', 4), ('四个直径6 mm的通孔', 4),
                        ('一个直径6 mm的通孔', 1), ('长度80mm，零个孔', 0)]:
        contract = bind(text)
        assert [c['comparison']['value'] for c in contract['checks'] if c['kind'] == 'hole_count'] == [count]
        cases.append({'model': 'one_hole', 'contract': contract, 'expected': 'passed' if count == 1 else 'failed'})
    for text, expected in [('孔径6mm以上', 'passed'), ('孔径7mm以上', 'failed'),
                           ('孔径6mm以下', 'passed'), ('孔径5mm以下', 'failed')]:
        cases.append({'model': 'one_hole', 'contract': bind(text), 'expected': expected})
    text = ('盒体外部长方体坐标范围为 X=0 至 80 mm、Y=0 至 55 mm、Z=0 至 32 mm。'
            '4个直径4.5mm的通孔。'
            '上盖坐标范围为 X=90 至 170 mm、Y=0 至 55 mm、Z=0 至 3 mm。'
            '4个直径3.4mm的通孔。两个独立实体。')
    contract = bind(text)
    assert [c['comparison']['value'] for c in contract['checks'] if c['kind'] == 'hole_count'] == [4, 4]
    assert len([c for c in contract['checks'] if c['kind'] == 'hole_through']) == 2
    for model, expected in [('components', 'passed'), ('oversize', 'failed'), ('wrong_lid', 'failed')]:
        cases.append({'model': model, 'contract': contract, 'expected': expected})
    bounds = ('盒体坐标范围为 X=0 至 80 mm、Y=0 至 55 mm、Z=0 至 32 mm。'
              '上盖坐标范围为 X=90 至 170 mm、Y=0 至 55 mm、Z=0 至 3 mm。')
    for phrase in ('各有', '均有'):
        contract = bind(bounds + f'盒体和上盖{phrase}4个直径6mm的通孔。两个独立实体。')
        for kind in ('hole_count', 'hole_diameter', 'hole_through'):
            checks = [c for c in contract['checks'] if c['kind'] == kind]
            assert len(checks) == 2
            assert {tuple(c['scope']['component_region']['min_mm']) for c in checks} == {(0, 0, 0), (90, 0, 0)}
        for model in ('distributed_correct', 'distributed_box_missing', 'distributed_lid_missing',
                      'distributed_box_three', 'distributed_lid_three',
                      'distributed_box_wrong_diameter', 'distributed_lid_wrong_diameter'):
            cases.append({'model': model, 'contract': contract,
                          'expected': 'passed' if model == 'distributed_correct' else 'failed'})
    for text, correct in [
            ('板80×40×8mm，2个直径6mm的通孔，2个直径4mm的通孔。', 'mixed_correct'),
            ('板80×40×8mm，2个直径6mm的通孔，2个直径6mm的通孔。', 'mixed_same_diameter')]:
        contract = bind(text)
        for model in ('mixed_correct', 'mixed_same_diameter', 'mixed_missing', 'mixed_extra',
                      'mixed_unmatched_extra', 'mixed_wrong_count', 'mixed_wrong_diameter', 'mixed_blind', 'mixed_zero'):
            cases.append({'model': model, 'contract': contract, 'expected': 'passed' if model == correct else 'failed'})
    contract = bind('板80×40×8mm，2个直径6mm通孔+2个直径4mm通孔。')
    cases.append({'model': 'mixed_correct', 'contract': contract, 'expected': 'passed'})
    for model in ('mixed_extra_conical', 'mixed_extra_square', 'mixed_extra_spline', 'mixed_extra_conical_blind',
                  'mixed_extra_side_square'):
        cases.append({'model': model, 'contract': contract, 'expected': 'indeterminate'})
        # The ordinary (ungrouped) count also must not trust a cylindrical subset.
        cases.append({'model': model, 'contract': bind('板80×40×8mm，4个通孔。'), 'expected': 'indeterminate'})
    for representation in ('analytic', 'nurbs'):
        for size in (('micro', 'small', 'large') if representation == 'analytic' else
                     ('micro', 'small', 'large', 'split4', 'split8')):
            model = f'mixed_extra_{representation}_blind_{size}'
            for requirement in (contract, bind('板80×40×8mm，4个孔。'),
                                bind('孔径4mm以上'), bind('孔深8mm'), bind('通孔')):
                cases.append({'model': model, 'contract': requirement,
                              'expected': 'failed' if representation == 'analytic' else 'indeterminate'})
    for requirement in (contract, bind('板80×40×8mm，4个孔。'), bind('孔径4mm以上'), bind('孔深8mm'), bind('通孔')):
        cases.append({'model': 'mixed_extra_sphere_blind', 'contract': requirement, 'expected': 'indeterminate'})
    cases.append({'model': 'mixed_four_blind', 'contract': bind('板80×40×8mm，4个孔。'), 'expected': 'passed'})
    legacy = AcceptanceContract.model_validate({'objective': '4个孔', 'checks': [
        {'check_id': 'holes', 'kind': 'hole_count', 'description': '孔数', 'source_quote': '4个孔', 'nominal': 4}]}).model_dump(mode='json')
    for model in ('mixed_correct', 'mixed_extra_nurbs_blind_micro', 'mixed_extra_nurbs_blind_split4'):
        cases.append({'model': model, 'contract': legacy,
                      'expected': 'passed' if model == 'mixed_correct' else 'indeterminate'})
    grouped_components = bind(bounds + '盒体和上盖各有2个直径6mm的通孔，2个直径4mm的通孔。两个独立实体。')
    for model in ('grouped_components_correct', 'grouped_components_box_wrong', 'grouped_components_lid_wrong'):
        cases.append({'model': model, 'contract': grouped_components,
                      'expected': 'passed' if model == 'grouped_components_correct' else 'failed'})
    rejected = []
    for text in ['长度80mm，两个实体不接触', '长度80mm，两个实体之间应有空气隔开',
                 '长度80mm，不增加其他特征', '长度80mm，避免额外的孔和凸台',
                 'over 6 mm diameter hole', 'under 6 mm diameter hole',
                 'greater-than 6 mm diameter hole', '＞ 6 mm diameter hole',
                 '盒体80×55×32mm，内部空腔74×49×29mm', '长度80mm，盖板厚度3mm',
                 '板80×40×8mm，孔径6mm，孔径4mm。',
                 '板80×40×8mm，直径6mm的通孔，直径4mm的通孔。',
                 bounds + '盒体或上盖各有4个直径6mm的通孔。两个独立实体。']:
        try:
            bind(text)
        except RequirementBindingError as exc:
            assert exc.code == 'engineering_verification_unavailable'
            rejected.append(text)
        else:
            raise AssertionError(f'partial contract incorrectly bound: {text}')
    return {'cases': cases, 'rejected_before_dispatch': rejected}


def verify_reports(log):
    from app.contracts.geometry_request import geometry_request_digest
    from app.validation.durable_geometry import DurableGeometryReport, verify_geometry_evidence
    marker, = [line for line in Path(log).read_text().splitlines() if line.startswith('CAD_SOURCE_OBLIGATION_CONTRACT=')]
    observed = json.loads(marker.split('=', 1)[1])['cases']
    expected = fixtures()['cases']
    assert len(observed) == len(expected)
    rejected = 0
    for case, row in zip(expected, observed):
        version = AcceptanceContractV2 if case['contract']['schema_version'] == 'engineering-acceptance.v2' else AcceptanceContract
        contract = version.model_validate(case['contract'])
        assert row['model'] == case['model'] and row['outcome'] == case['expected']
        solids = 1 if case['model'] == 'one_hole' or case['model'].startswith('mixed_') else 2
        context = row['measurement_context']
        request = geometry_request_digest(expected_dimensions_mm={}, dimension_tolerance=.05,
            expected_solid_count=solids, acceptance=case['contract'], measurement_context=context)
        args = dict(request_sha256=request, acceptance=contract, expected_solid_count=solids, measurement_context=context)
        verify_geometry_evidence(DurableGeometryReport.model_validate(row['report']), **args)
        if not case['model'].startswith('mixed_'):
            continue
        from app.contracts.hole_populations import verify_inventory_coverage
        for evidence in row['report']['acceptance']['evidence']:
            inventory = evidence['details'].get('hole_inventory')
            if not inventory or not inventory['complete']:
                continue
            # These are measured certificates from saved STEP, not fabricated
            # topology. The receiver must reject a missing or misowned face even
            # if a caller recomputes the envelope hash and complete flag.
            for mutation in ('missing-face', 'duplicate-face', 'unknown-surface', 'wrong-owner', 'legacy'):
                forged = deepcopy(inventory)
                proof = forged['coverage']
                faces = proof['boundary_faces']
                if mutation == 'missing-face':
                    faces.pop()
                elif mutation == 'duplicate-face':
                    faces[-1] = deepcopy(faces[0])
                elif mutation == 'unknown-surface':
                    faces[0]['surface_type'] = 'BSPLINE'
                elif mutation == 'wrong-owner':
                    wall = next((f for f in faces if f['classification']=='passage_wall'), faces[0])
                    wall.update(classification='passage_wall', passage_index=-1)
                else:
                    proof.update(schema_version='hole-coverage.v1', method='closed_brep_after_measured_passage_fill')
                    proof.pop('boundary_faces')
                try:
                    verify_inventory_coverage(forged, evidence['details']['numerical_tolerance_mm'])
                except ValueError:
                    rejected += 1
                else:
                    raise AssertionError((case['model'], mutation, 'uncovered boundary accepted'))
        if row['outcome'] == 'indeterminate':
            incomplete = [r for r in row['report']['acceptance']['evidence']
                          if r['details'].get('reason') == 'hole_inventory_incomplete']
            assert incomplete and all(r['details']['hole_inventory']['complete'] is False for r in incomplete)
            # Even recomputing the claimed completeness and digest cannot make
            # the underlying incomplete geometry certificate valid.
            from app.contracts.hole_populations import verify_inventory_coverage
            for evidence in incomplete:
                forged = deepcopy(evidence['details']['hole_inventory'])
                forged['complete'] = True
                try:
                    verify_inventory_coverage(forged, evidence['details']['numerical_tolerance_mm'])
                except ValueError:
                    rejected += 1
                else:
                    raise AssertionError('incomplete geometry accepted after changing complete flag')
            continue
        if not any(getattr(c, 'hole_group', None) for c in contract.checks):
            continue
        for mutation in ('outcome', 'drop-hole', 'assignment', 'filtered-values', 'artifact', 'incomplete'):
            forged = deepcopy(row['report'])
            diameter_checks = {c.check_id for c in contract.checks if c.kind == 'hole_diameter'}
            evidence = next(r for r in forged['acceptance']['evidence']
                            if r['check_id'] in diameter_checks and 'population_evaluation' in r['details'])
            details = evidence['details']
            if mutation == 'outcome':
                evidence['outcome'] = 'failed' if evidence['outcome'] == 'passed' else 'passed'
            elif mutation == 'drop-hole':
                if details['hole_inventory']['passages']:
                    details['hole_inventory']['passages'].pop()
                else:
                    details['feature_count'] = 1
            elif mutation == 'assignment':
                details['population_evaluation']['assignment'] = {'forged-group': [0, 0]}
            elif mutation == 'filtered-values':
                evidence['measured'] = [2]
            elif mutation == 'artifact':
                forged['artifacts'][0]['sha256'] = 'e' * 64
            else:
                details['hole_inventory']['complete'] = False
            try:
                verify_geometry_evidence(DurableGeometryReport.model_validate(forged), **args)
            except ValueError:
                rejected += 1
            else:
                raise AssertionError((case['model'], mutation, 'tampered evidence accepted'))
    print('CAD_SOURCE_RECEIVER_CONTRACT=' + json.dumps({'reports': len(observed), 'tamper_rejections': rejected}))


if __name__ == '__main__':
    if sys.argv[1] == '--verify':
        verify_reports(sys.argv[2])
    else:
        Path(sys.argv[1]).write_text(json.dumps(fixtures(), ensure_ascii=False, indent=2))
