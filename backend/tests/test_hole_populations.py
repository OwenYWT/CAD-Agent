"""Quantified hole groups must cover the independently measured inventory."""
from copy import deepcopy

import pytest

from app.contracts.acceptance_v2 import AcceptanceContractV2, RequirementBindingError
from app.validation.requirement_binding import extract_source_requirements, validate_requirement_bindings


MIXED = '板80×40×8mm，2个直径6mm的通孔，2个直径4mm的通孔。'


def bound_contract(text=MIXED, sources=None):
    extracted = extract_source_requirements(text, requirement_sources=sources)
    assert not extracted['unsupported'], extracted
    contract = AcceptanceContractV2.model_validate({'objective': text, 'checks': [
        {'check_id': str(i), 'binding_ref': r['requirement_id'], 'kind': r['kind'],
         'source_quote': r['source_quote'], 'description': r['property'],
         'source_kind': r['source_kind'], 'comparison': r['comparison'], 'scope': r['scope'],
         **({'hole_group': r['hole_group']} if r.get('hole_group') else {})}
        for i, r in enumerate(extracted['requirements'])]})
    validate_requirement_bindings(text, contract, requirement_sources=sources)
    return contract


@pytest.mark.parametrize('separator', ['+', '＋', ' + ', '+ ', ' +', '，', '；', '以及'])
def test_additive_count_separators_preserve_original_source_spans(separator):
    text = f'板80×40×8mm，2个直径6mm通孔{separator}2个直径4mm通孔。'
    contract = bound_contract(text)
    assert [c.comparison.value for c in contract.checks if c.kind == 'hole_count'] == [2, 2]
    extracted = extract_source_requirements(text)
    assert len({c.hole_group.group_id for c in contract.checks if c.hole_group}) == 2
    for row in extracted['requirements']:
        start, end = row['source_span']
        assert text[start:end] == row['source_quote']


@pytest.mark.parametrize('count', ['-2', '−2', '2.5', '-2.5', '+2.5'])
def test_additive_separator_fix_does_not_admit_invalid_counts(count):
    text = f'板80×40×8mm，2个直径6mm通孔，{count}个直径4mm通孔。'
    assert extract_source_requirements(text)['unsupported']


@pytest.mark.parametrize('text', [MIXED, MIXED.replace('4mm', '6mm'),
    '板80×40×8mm，2个通孔，孔径6mm；2个通孔，孔径4mm。',
    'plate 80x40x8mm, two 6mm through holes, two 4mm through holes',
    MIXED.replace('2个', '至少2个')])
def test_source_counted_groups_retain_all_atomic_requirements(text):
    contract = bound_contract(text)
    groups = {}
    for check in contract.model_dump(mode='json')['checks']:
        if check['kind'].startswith('hole_'):
            groups.setdefault(check['hole_group']['group_id'], []).append(check)
    assert len(groups) == 2
    for checks in groups.values():
        assert sorted(c['kind'] for c in checks) == ['hole_count', 'hole_depth', 'hole_diameter', 'hole_through']
    erased = contract.model_dump(mode='json')
    for check in erased['checks']:
        check.pop('hole_group', None)
    with pytest.raises(RequirementBindingError, match='group'):
        validate_requirement_bindings(text, AcceptanceContractV2.model_validate(erased))


def population_checks():
    return [c for c in bound_contract().model_dump(mode='json')['checks'] if c.get('hole_group')]


def inventory(diameters, *, blind=None, depth=8):
    # Synthetic evidence for the pure partition/receiver unit tests. Native
    # tests separately obtain every certificate field from actual STEP BReps.
    passages = [
        {'diameter': d, 'largest_section_diameter': d, 'depth': depth, 'shaft_length': depth,
         'through': int(i != blind)} for i, d in enumerate(diameters)]
    through = sum(p['through'] for p in passages)
    def topology(holes, blind=0):
        wires = [1]*4 + [1+holes+blind, 1+holes] + [1]*(holes+2*blind)
        return [{'shells': [{'closed': True, 'vertices': 8+2*holes+2*blind,
            'edges': 12+3*holes+3*blind, 'face_wire_counts': wires,
            'face_indices': list(range(len(wires)))}]}]
    for i, passage in enumerate(passages):
        passage['face_indices'] = [6+i]
    faces = [{'face_index': i, 'surface_type': 'PLANE', 'classification': 'planar',
              'wire_count': n} for i, n in enumerate(topology(through, len(passages)-through)[0]['shells'][0]['face_wire_counts'])]
    for i, passage in enumerate(passages):
        faces[6+i].update(surface_type='CYLINDER', classification='passage_wall',
                          normal_radial_dot=-passage['diameter']/2, passage_index=i)
    coverage = {'schema_version': 'hole-coverage.v2', 'method': 'whole_boundary_classification_and_passage_fill',
        'passages': passages, 'source_topology': topology(through, len(passages)-through), 'filled_topology': topology(0),
        'boundary_faces': faces,
        'unrepresented_analytic_faces': [], 'incomplete_passages': [],
        'source_volume_mm3': 90., 'filled_volume_mm3': 100., 'fill_volume_mm3': 10.,
        'material_overlap_mm3': 0., 'surface_area_mm2': 60.}
    return {'schema_version': 'hole-inventory.v2', 'complete': True, 'passages': passages,
        'selected_indices': list(range(len(passages))), 'coverage': coverage}


@pytest.mark.parametrize('mutation', ['no-proof', 'extra-handle', 'residual-handle', 'unrepresented-cone',
    'incomplete-cylinder', 'inner-shell', 'fill-overlap', 'fill-volume', 'drop-scope-hole', 'v1'])
def test_complete_flag_cannot_replace_independent_geometric_coverage(mutation):
    from app.contracts.hole_populations import compare_hole_populations
    data = inventory([6, 6, 4, 4])
    proof = data['coverage']
    if mutation == 'no-proof':
        data.pop('coverage')
    elif mutation in {'extra-handle', 'residual-handle'}:
        topology = proof['source_topology' if mutation == 'extra-handle' else 'filled_topology']
        topology[0]['shells'][0]['edges'] += 2
    elif mutation == 'unrepresented-cone':
        proof['unrepresented_analytic_faces'] = [10]
    elif mutation == 'incomplete-cylinder':
        proof['incomplete_passages'] = [3]
    elif mutation == 'inner-shell':
        proof['source_topology'][0]['shells'] *= 2
    elif mutation == 'fill-overlap':
        proof['material_overlap_mm3'] = 1.
    elif mutation == 'fill-volume':
        proof['fill_volume_mm3'] = 11.
    elif mutation == 'drop-scope-hole':
        data['selected_indices'].pop()
    else:
        data['schema_version'] = 'hole-inventory.v1'
    with pytest.raises(ValueError):
        compare_hole_populations(population_checks(), data, 1e-7)


@pytest.mark.parametrize('mutation', ['missing-boundary', 'dropped-face', 'duplicate-face',
    'unknown-surface', 'unknown-as-planar', 'unowned-wall', 'double-owned-wall',
    'missing-passage-face', 'outward-as-wall', 'wrong-wire-count', 'missing-topology-face', 'old-proof'])
def test_every_original_face_requires_a_unique_supported_classification(mutation):
    from app.contracts.hole_populations import verify_inventory_coverage
    data = inventory([6, 6, 4, 4])
    coverage = data['coverage']
    faces = coverage['boundary_faces']
    if mutation == 'missing-boundary':
        coverage.pop('boundary_faces')
    elif mutation == 'dropped-face':
        faces.pop()
    elif mutation == 'duplicate-face':
        faces[-1] = deepcopy(faces[-2])
    elif mutation in {'unknown-surface', 'unknown-as-planar'}:
        faces[0].update(surface_type='BSPLINE', classification='unresolved' if mutation == 'unknown-surface' else 'planar')
    elif mutation == 'unowned-wall':
        faces[-1].pop('passage_index')
    elif mutation == 'double-owned-wall':
        coverage['passages'][0]['face_indices'].append(faces[-1]['face_index'])
    elif mutation == 'missing-passage-face':
        coverage['passages'][-1]['face_indices'] = []
    elif mutation == 'outward-as-wall':
        faces[-1]['normal_radial_dot'] = 2.
    elif mutation == 'wrong-wire-count':
        faces[0]['wire_count'] += 1
    elif mutation == 'missing-topology-face':
        coverage['source_topology'][0]['shells'][0].pop('face_indices')
    else:
        coverage['schema_version'] = 'hole-coverage.v1'
        coverage['method'] = 'closed_brep_after_measured_passage_fill'
        coverage.pop('boundary_faces')
    with pytest.raises(ValueError):
        verify_inventory_coverage(data, 1e-7)


def test_enclosed_passage_properties_require_every_internal_face_and_never_authorize_counts():
    from types import SimpleNamespace
    from app.contracts.acceptance import AcceptanceMeasurements, MeasurementEvidence
    from app.contracts.acceptance_comparison import compare_feature_measurement
    from app.contracts.hole_populations import verify_inventory_coverage, inventory_values, digest
    from app.validation.durable_geometry import verify_geometry_evidence
    data = inventory([6], blind=0)
    coverage = data['coverage']
    outer = deepcopy(coverage['filled_topology'][0]['shells'][0])
    inner = {'closed': True, 'vertices': 2, 'edges': 3,
             'face_indices': [6, 7, 8], 'face_wire_counts': [1, 1, 1]}
    coverage['source_topology'][0]['shells'] = [outer, inner]
    coverage['boundary_faces'] = [
        {'face_index': i, 'surface_type': 'PLANE', 'classification': 'planar', 'wire_count': 1}
        for i in range(9)]
    coverage['boundary_faces'][6].update(surface_type='CYLINDER', classification='passage_wall',
                                        normal_radial_dot=-3., passage_index=0)
    for kind in ('hole_diameter', 'hole_depth', 'hole_through'):
        verify_inventory_coverage(data, 1e-7, check={'kind': kind})
        value = {'hole_diameter': 6., 'hole_depth': 8., 'hole_through': 1.}[kind]
        contract = AcceptanceContractV2.model_validate({'schema_version': 'engineering-acceptance.v2',
            'objective': 'round passage property', 'checks': [{'check_id': 'property', 'kind': kind,
                'description': 'property', 'source_quote': 'round passage property', 'binding_ref': 'r-0',
                'comparison': {'op': 'eq', 'value': value, 'unit': 'count' if kind == 'hole_through' else 'mm'}}]})
        check = contract.checks[0].model_dump(mode='json')
        values = inventory_values(check, data)
        details = compare_feature_measurement(check, values, 1e-7, feature_count=1)
        outcome = details.pop('outcome')
        assert outcome == ('failed' if kind == 'hole_through' else 'passed')
        details.update(hole_inventory=data, hole_inventory_sha256=digest(data), numerical_tolerance_mm=1e-7)
        row = MeasurementEvidence(check_id='property', outcome=outcome, measured=values,
                                  method='native_inventory', details=details)
        report = SimpleNamespace(request_sha256='a'*64, acceptance_contract=contract, outcome=outcome, artifacts=[],
            acceptance=AcceptanceMeasurements(contract_sha256=contract.digest(), evidence=(row,)))
        verify_geometry_evidence(report, request_sha256='a'*64, acceptance=contract, expected_solid_count=None)
    for check in (None, {'kind': 'hole_count'}, {'kind': 'hole_position'},
                  {'kind': 'hole_diameter', 'hole_group': {'group_id': 'group'}}):
        with pytest.raises(ValueError, match='internal shells'):
            verify_inventory_coverage(data, 1e-7, check=check)
    for mutation in ('drop', 'duplicate', 'unknown'):
        bad = deepcopy(data)
        faces = bad['coverage']['boundary_faces']
        if mutation == 'drop':
            faces.pop()
        elif mutation == 'duplicate':
            faces[-1] = deepcopy(faces[-2])
        else:
            faces[-1].update(surface_type='BSPLINE', classification='unresolved')
        with pytest.raises(ValueError):
            verify_inventory_coverage(bad, 1e-7, check={'kind': 'hole_diameter'})


def test_legacy_hole_evidence_cannot_bypass_boundary_coverage():
    from types import SimpleNamespace
    from app.contracts.acceptance import AcceptanceContract, AcceptanceMeasurements, MeasurementEvidence
    from app.contracts.hole_populations import digest
    from app.validation.durable_geometry import verify_geometry_evidence
    contract = AcceptanceContract.model_validate({'objective': '一个孔', 'checks': [
        {'check_id': 'count', 'kind': 'hole_count', 'source_quote': '一个孔', 'description': '孔数', 'nominal': 1}]})
    measured = inventory([6])
    details = {'hole_inventory': measured, 'hole_inventory_sha256': digest(measured), 'numerical_tolerance_mm': 1e-7}
    def report():
        return SimpleNamespace(request_sha256='a'*64, acceptance_contract=contract, outcome='passed', artifacts=[],
            acceptance=AcceptanceMeasurements(contract_sha256=contract.digest(), evidence=(
                MeasurementEvidence(check_id='count', outcome='passed', method='native_inventory',
                                    measured=(1,), details=details),)))
    verify_geometry_evidence(report(), request_sha256='a'*64, acceptance=contract, expected_solid_count=None)
    measured['coverage'].pop('boundary_faces')
    details['hole_inventory_sha256'] = digest(measured)
    with pytest.raises(ValueError):
        verify_geometry_evidence(report(), request_sha256='a'*64, acceptance=contract, expected_solid_count=None)


@pytest.mark.parametrize('diameters,blind,expected', [
    ([6, 6, 4, 4], None, 'passed'), ([4, 6, 4, 6], None, 'passed'),
    ([6, 6, 6, 4], None, 'failed'), ([6, 4], None, 'failed'),
    ([6, 6, 4, 4, 3], None, 'failed'), ([6, 6, 4, 4, 6], None, 'failed'),
    ([6, 6, 4, 5], None, 'failed'), ([6, 6, 4, 4], 2, 'failed'),
    ([], None, 'failed'),
])
def test_partition_counts_every_hole_once(diameters, blind, expected):
    from app.contracts.hole_populations import compare_hole_populations
    result = compare_hole_populations(population_checks(), inventory(diameters, blind=blind), 1e-7)
    assert result['outcome'] == expected
    if expected == 'passed':
        assigned = [i for indices in result['assignment'].values() for i in indices]
        assert sorted(assigned) == list(range(len(diameters)))
        assert len(assigned) == len(set(assigned))
    else:
        assert result['assignment'] is None


def test_overlapping_predicates_use_a_complete_partition_not_greedy_filtering():
    from app.contracts.hole_populations import compare_hole_populations
    checks = population_checks()
    diameters = [c for c in checks if c['kind'] == 'hole_diameter']
    diameters[0]['comparison'] = {'op': 'range', 'lower': 4., 'upper': 6., 'unit': 'mm'}
    result = compare_hole_populations(checks, inventory([4, 6, 4, 6]), 1e-7)
    assert result['outcome'] == 'passed'
    assert result['assignment'][diameters[0]['hole_group']['group_id']] == [1, 3]
    assert compare_hole_populations(checks, inventory([6, 6, 6, 4]), 1e-7)['outcome'] == 'failed'


def test_compatible_counts_accept_extra_matching_holes_but_never_unmatched_holes():
    from app.contracts.hole_populations import compare_hole_populations
    checks = population_checks()
    for check in checks:
        if check['kind'] == 'hole_count':
            check['comparison'] = {'op': 'ge', 'lower': 2., 'unit': 'count'}
    assert compare_hole_populations(checks, inventory([6, 6, 4, 4, 6]), 1e-7)['outcome'] == 'passed'
    assert compare_hole_populations(checks, inventory([6, 6, 4, 4, 3]), 1e-7)['outcome'] == 'failed'


@pytest.mark.parametrize('mutation', ['incomplete', 'missing_through', 'nonfinite', 'negative', 'bool'])
def test_unmeasured_or_invalid_inventory_never_passes(mutation):
    from app.contracts.hole_populations import compare_hole_populations
    data = inventory([6, 6, 4, 4])
    if mutation == 'incomplete':
        data['complete'] = False
    elif mutation == 'missing_through':
        data['passages'][0].pop('through')
    else:
        data['passages'][0]['diameter'] = {'nonfinite': float('nan'), 'negative': -6, 'bool': True}[mutation]
    with pytest.raises(ValueError):
        compare_hole_populations(population_checks(), data, 1e-7)


def test_cross_source_additive_groups_preserve_authority():
    sources = [dict(text='板80×40×8mm，2个直径6mm的通孔。', source_input_id='objective', source_kind='user'),
               dict(text='另有2个直径4mm的通孔。', source_input_id='dimensions', source_kind='confirmed')]
    contract = bound_contract('\n'.join(s['text'] for s in sources), sources)
    groups = {c.hole_group.group_id for c in contract.checks if c.hole_group}
    assert len(groups) == 2
    assert {c.source_kind for c in contract.checks if c.hole_group} == {'user', 'confirmed'}


def test_group_identity_cannot_be_swapped_or_property_omitted():
    contract = bound_contract()
    data = contract.model_dump(mode='json')
    diameter = [c for c in data['checks'] if c['kind'] == 'hole_diameter']
    diameter[0]['hole_group'], diameter[1]['hole_group'] = diameter[1]['hole_group'], diameter[0]['hole_group']
    with pytest.raises(RequirementBindingError, match='group'):
        validate_requirement_bindings(MIXED, AcceptanceContractV2.model_validate(data))
    data = contract.model_dump(mode='json')
    data['checks'] = [c for c in data['checks'] if c['kind'] != 'hole_through']
    with pytest.raises(RequirementBindingError, match='coverage'):
        validate_requirement_bindings(MIXED, AcceptanceContractV2.model_validate(data))


def test_legacy_v2_wire_does_not_acquire_a_new_default_field():
    contract = bound_contract('板80×40×8mm，2个直径6mm的通孔。')
    assert all('hole_group' not in c for c in contract.model_dump(mode='json')['checks'])
    assert AcceptanceContractV2.model_validate(deepcopy(contract.model_dump(mode='json'))).digest() == contract.digest()


def test_capacitated_partition_matches_exhaustive_small_population_oracle():
    from itertools import product
    from random import Random
    from app.contracts.hole_populations import _partition
    random = Random(20261024)
    for size in range(7):
        for _ in range(40):
            groups = ['a', 'b', 'c']
            eligible = [[g for g in groups if random.choice([True, False])] for _ in range(size)]
            bounds = {g: sorted([random.randrange(4), random.randrange(4)]) for g in groups}
            feasible = any(all(lo <= assignment.count(g) <= hi for g, (lo, hi) in bounds.items())
                           for assignment in product(*eligible))
            result = _partition(eligible, bounds)
            assert (result is not None) == feasible, (eligible, bounds, result)
            if result is not None:
                assert sorted(i for indices in result.values() for i in indices) == list(range(size))
                assert all(all(g in eligible[i] for i in indices) for g, indices in result.items())


def test_group_cannot_change_measurement_scope_or_become_an_assumption():
    data = bound_contract().model_dump(mode='json')
    check = next(c for c in data['checks'] if c['kind'] == 'hole_diameter')
    check['scope']['axis'] = [0, 0, 1]
    with pytest.raises(ValueError, match='different measurement scopes'):
        AcceptanceContractV2.model_validate(data)
    check['scope']['axis'] = None
    check.update(source_kind='assumption', required=False)
    with pytest.raises(ValueError, match='authority'):
        AcceptanceContractV2.model_validate(data)


@pytest.mark.parametrize('text', [
    'plate 80x40x8mm, one centered 6mm through hole, two 4mm through holes',
    '板80×40×8mm，2个直径10mm的通孔，2个直径4mm的通孔，所有孔的孔径不超过8mm。',
])
def test_grouping_does_not_move_position_or_global_qualifier_to_the_last_group(text):
    result = extract_source_requirements(text)
    assert result['unsupported'], result
