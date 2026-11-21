"""Source semantics: context never erases a geometric assertion or its authority."""
import pytest

from app.validation.requirement_binding import extract_source_requirements, require_bound_sources
from app.contracts.acceptance_v2 import SourceRequirementUnavailable


BASE = 'cylinder radius 75 mm, height 20.923 mm'


@pytest.mark.parametrize('declaration', [
    'All dimensions are in cm.', 'All measurements are expressed in centimeters.',
    'units: cm.', '单位：厘米。',
    'All linear dimensions and coordinates below are in source design units: 1 source unit = 50 mm.',
])
@pytest.mark.parametrize('split', [False, True])
def test_global_units_are_inherited_with_saved_source_authority(declaration, split):
    design = 'source unit' in declaration
    dimensions = 'cylinder radius 0.4, height 0.5' if design else 'cylinder radius 2, height 2.5'
    sources = ([dict(source_input_id='objective', source_kind='user', text=declaration),
                dict(source_input_id='confirmed', source_kind='confirmed', text=dimensions)] if split else
               [dict(source_input_id='objective', source_kind='user', text=declaration + '\n' + dimensions)])
    result = require_bound_sources('\n'.join(s['text'] for s in sources), requirement_sources=sources)
    assert sorted((r['kind'], r['comparison']['value']) for r in result['requirements']) == [
        ('cylinder_height', 25.), ('cylinder_radius', 20.)]
    by_id = {s['source_input_id']: s['text'] for s in sources}
    for row in result['requirements']:
        assert row['unit_sources']
        for ref in [row, *row['unit_sources']]:
            a, b = ref['source_span']
            assert ref['source_quote'] == by_id[ref['source_input_id']][a:b]


@pytest.mark.parametrize('dimensions,expected', [
    ('cylinder radius 3 mm, height 2.5', [('cylinder_height', 25.), ('cylinder_radius', 3.)]),
    ('cube edge length 2', [('cube_edge', 20.), ('cube_shape', 1.)]),
    ('plate 4×3×1.2', [('overall_dimension', 12.), ('overall_dimension', 30.), ('overall_dimension', 40.)]),
    ('plate 4 long, 3 wide, 1.2 thick', [('overall_dimension', 12.), ('overall_dimension', 30.), ('overall_dimension', 40.)]),
])
def test_global_units_apply_to_existing_dimension_grammars_and_local_units_override(dimensions, expected):
    result = require_bound_sources('All dimensions are in cm.\n' + dimensions)
    assert sorted((r['kind'], r['comparison']['value']) for r in result['requirements']) == expected


@pytest.mark.parametrize('declaration,dimensions', [
    ('All dimensions are in cm. units: mm.', 'cylinder radius 2 cm, height 2.5 cm'),
    ('All dimensions are in source design units.', 'cylinder radius 2, height 2.5'),
    ('All dimensions are in inches.', 'cylinder radius 2, height 2.5'),
    ('All dimensions are in cm.', 'cylinder radius 2, height 2.5. unknown curvature 0.3'),
])
@pytest.mark.parametrize('split', [False, True])
def test_global_unit_context_cannot_hide_conflicts_missing_scales_or_unknown_dimensions(declaration, dimensions, split):
    sources = [dict(source_input_id='objective', source_kind='user', text=declaration),
               dict(source_input_id='confirmed', source_kind='confirmed', text=dimensions)]
    with pytest.raises(SourceRequirementUnavailable):
        require_bound_sources('\n'.join(s['text'] for s in sources), requirement_sources=sources if split else None)


@pytest.mark.parametrize('quantity,quote', [
    ('0.3.', '0.3'), ('.3.', '.3'), ('3.', '3'), ('3.125.', '3.125'),
    ('三点三.', '三点三'), ('0.3..', '0.3'), ('0.3', '0.3'),
    ('0.3\n', '0.3'), ('0.3。', '0.3'), ('0.3;', '0.3'), ('0.3 cm.', '0.3 cm'),
])
@pytest.mark.parametrize('feature', ['Chamfer', 'Unknown curvature'])
@pytest.mark.parametrize('split', [False, True], ids=['objective', 'confirmed'])
def test_unbound_quantities_keep_their_complete_source_across_sentence_endings(quantity, quote, feature, split):
    base = 'All dimensions are in cm. cylinder radius 2, height 2.5. '
    clause = feature + ' ' + quantity
    sources = ([dict(source_input_id='objective', source_kind='user', text=base),
                dict(source_input_id='confirmed-dimensions', source_kind='confirmed', text=clause)] if split else
               [dict(source_input_id='objective', source_kind='user', text=base + clause)])
    with pytest.raises(SourceRequirementUnavailable) as error:
        require_bound_sources('\n'.join(s['text'] for s in sources), requirement_sources=sources)
    assert error.value.code == 'engineering_verification_unavailable'
    source = sources[-1]
    assert any(d['reason'] == 'quantity_property_unbound' and d['source_quote'] == quote
               and d['source_input_id'] == source['source_input_id']
               and source['text'][slice(*d['source_span'])] == quote
               for d in error.value.details['diagnostics'])


@pytest.mark.parametrize('radius,height', [('2', '2.5'), ('.2', '.25'), ('2.125', '3.375')])
def test_supported_decimal_dimensions_keep_their_full_value_before_a_period(radius, height):
    text = f'All dimensions are in cm. cylinder radius {radius}, height {height}.'
    result = require_bound_sources(text)
    assert sorted((r['kind'], r['comparison']['value']) for r in result['requirements']) == [
        ('cylinder_height', float(height) * 10), ('cylinder_radius', float(radius) * 10)]


@pytest.mark.parametrize('tail', ['PG7. FDM 3D. M4. part_7. v1.2.3.', '4个M3安装孔。'])
def test_sentence_boundaries_do_not_turn_identifier_digits_into_dimensions(tail):
    text = 'All dimensions are in cm. plate 4×3×1.2. ' + tail
    result = require_bound_sources(text)
    assert sorted(r['comparison']['value'] for r in result['requirements'] if r['kind'] == 'overall_dimension') == [12., 30., 40.]
    assert not any(r['source_quote'].strip() in {'3', '4', '7', '1.2', '1.2.3'} for r in result['requirements'])


@pytest.mark.parametrize('unit,factor', [('mm', 1), ('cm', 10), ('m', 1000)])
def test_inherited_units_preserve_comparisons_and_tolerances(unit, factor):
    result = require_bound_sources(f'All dimensions are in {unit}.\ncylinder radius 2±0.1, height 3 to 4')
    dimensions = {r['kind']: r['comparison'] for r in result['requirements']}
    assert dimensions['cylinder_radius']['value'] == 2 * factor
    assert dimensions['cylinder_radius']['tolerance_minus'] == pytest.approx(.1 * factor)
    assert dimensions['cylinder_radius']['tolerance_plus'] == pytest.approx(.1 * factor)
    assert dimensions['cylinder_height'] == {'op': 'range', 'lower': 3 * factor, 'upper': 4 * factor, 'unit': 'mm'}


@pytest.mark.parametrize('units,valid', [('厘米', True), ('mm', False)])
def test_unit_declarations_in_separate_fields_must_agree(units, valid):
    sources = [dict(source_input_id='objective', source_kind='user', text='All dimensions are in cm. cylinder radius 2 cm.'),
               dict(source_input_id='confirmed', source_kind='confirmed', text=f'units: {units}. height 25 mm')]
    objective = '\n'.join(s['text'] for s in sources)
    if valid:
        assert len(require_bound_sources(objective, requirement_sources=sources)['requirements']) == 2
    else:
        with pytest.raises(SourceRequirementUnavailable):
            require_bound_sources(objective, requirement_sources=sources)


@pytest.mark.parametrize('property,kind', [('半径', 'cylinder_radius'), ('直径', 'cylinder_diameter'), ('高度', 'cylinder_height')])
def test_preserving_an_explicit_value_freezes_that_value_without_inventing_a_baseline(property, kind):
    result = require_bound_sources(f'圆柱{property}保持20mm。')
    assert [(r['kind'], r['comparison']) for r in result['requirements']] == [
        (kind, {'op': 'eq', 'value': 20., 'unit': 'mm'})]
    with pytest.raises(SourceRequirementUnavailable):
        require_bound_sources(f'圆柱{property}保持不变。')


@pytest.mark.parametrize('coordinates', [
    'X=0 至 1、Y=0 至 2、Z=0 至 3',
    'X=0 至 10 mm、Y=0 至 2、Z=0 至 3',
    'X=0 至 10 mm、Y=0 至 2 cm、Z=0 至 3',
])
def test_coordinate_units_are_resolved_for_each_axis_with_global_source_authority(coordinates):
    result = require_bound_sources('All dimensions and coordinates are in cm. 盖板坐标范围为 ' + coordinates)
    row, = result['requirements']
    assert row['scope']['component_region'] == {'min_mm': [0., 0., 0.], 'max_mm': [10., 20., 30.]}
    assert row['unit_sources'][0]['source_quote'] == 'All dimensions and coordinates are in cm'


@pytest.mark.parametrize('unit,expected', [('mm3', 2.), ('cm3', 2000.), ('立方厘米', 2000.)])
def test_existing_declared_volume_units_remain_supported(unit, expected):
    result = require_bound_sources(f'units: {unit}. volume 2')
    row, = result['requirements']
    assert row['kind'] == 'volume'
    assert row['comparison'] == {'op': 'eq', 'value': expected, 'unit': 'mm3'}


@pytest.mark.parametrize('context', [
    'The dimensions below describe this model.',
    'The coordinates above describe the model.',
    'Evaluate geometry only.', 'Only verify the geometry.',
    '本次仅验收几何结构。',
    'Manufacturing performance is not certified.',
    'Printing performance has not been verified.',
    'Waterproof performance requires physical testing.',
    'Preserve all specified numeric dimensions.',
    'Keep the provided diameters and dimensions.',
])
@pytest.mark.parametrize('context_first', [False, True])
def test_document_context_does_not_become_a_geometry_obligation(context, context_first):
    text = (context + '\n' + BASE) if context_first else (BASE + '. ' + context)
    result = require_bound_sources(text)
    assert sorted((r['kind'], r['comparison']['value']) for r in result['requirements']) == [
        ('cylinder_height', 20.923), ('cylinder_radius', 75.)]
    for row in result['requirements']:
        a, b = row['source_span']
        assert row['source_quote'] == text[a:b]


@pytest.mark.parametrize('tail', [
    'The height must be below 10 mm.',
    'The hole must not pass through the plate.',
    'Manufacturing performance is not certified and the hole must follow an unknown path.',
    'Evaluate geometry only and omit the mounting holes.',
    'Preserve the existing diameter.',
    'The dimensions below 10 mm are required.',
    'The wall shall follow an unknown curvature.',
])
def test_context_words_cannot_hide_mixed_or_unknown_geometric_conditions(tail):
    with pytest.raises(SourceRequirementUnavailable):
        require_bound_sources(BASE + '. ' + tail)


@pytest.mark.parametrize('declaration,radius,height', [
    ('1 source unit = exactly 100 mm', 75., 20.),
    ('2 drawing units = 1 cm', 3.75, 1.),
    ('1 design unit = 0.025 m', 18.75, 5.),
])
def test_explicit_design_unit_definitions_scale_measurements_without_changing_source_quotes(declaration, radius, height):
    text = ('All linear dimensions and coordinates below are in source design units: '
            + declaration + '. Angles are in degrees.\n'
            'cylinder radius 0.75 units, height 0.2 units')
    result = require_bound_sources(text)
    assert sorted((r['kind'], r['comparison']['value']) for r in result['requirements']) == [
        ('cylinder_height', height), ('cylinder_radius', radius)]
    for row in result['requirements']:
        a, b = row['source_span']
        assert row['source_quote'] == text[a:b]
        assert row['comparison']['unit'] == 'mm'


def test_saved_fields_share_explicit_unit_authority_without_rewriting_inputs():
    sources = [dict(source_input_id='objective', source_kind='user', text='cylinder. 1 source unit = 25 mm.'),
               dict(source_input_id='basis-dimensions', source_kind='confirmed', text='radius 2 units, height 3 units')]
    text = '\n'.join(s['text'] for s in sources)
    result = require_bound_sources(text, requirement_sources=sources)
    dimensions = [r for r in result['requirements'] if r['comparison']['unit'] == 'mm']
    assert sorted((r['kind'], r['comparison']['value']) for r in dimensions) == [
        ('cylinder_height', 75.), ('cylinder_radius', 50.)]
    assert all(r['source_input_id'] == 'basis-dimensions' for r in dimensions)
    assert all(r['unit_sources'][0]['source_input_id'] == 'objective' for r in dimensions)


@pytest.mark.parametrize('definition', [
    '', '0 source units = 100 mm.', '1 source unit = 0 mm.',
    '-1 source unit = 100 mm.', '1 source unit = -100 mm.',
    '1 source unit = 10 mm. 1 source unit = 20 mm.',
    '一百二 source units = 100 mm.', '1 source unit = 一百二 mm.',
])
def test_missing_invalid_or_conflicting_design_units_remain_blocking(definition):
    with pytest.raises(SourceRequirementUnavailable):
        require_bound_sources(definition + ' cylinder radius 2 units, height 3 units')


def test_unit_quantity_near_a_hole_is_not_invented_as_a_fractional_hole_count():
    text = '1 source unit = 100 mm. hole diameter 0.75 units to create a hole.'
    result = extract_source_requirements(text)
    assert not any(o['category'] == 'count' and '0.75' in o['source_quote'] for o in result['obligations'])
    assert not any(r['kind'] == 'hole_count' and r['comparison']['value'] == .75 for r in result['requirements'])


def test_physical_disclaimer_is_saved_unverified_without_waiving_an_explicit_rating():
    result = require_bound_sources(BASE + '. Manufacturing performance is not certified.')
    assert result['verification_notices'][0]['status'] == 'unverified'
    with pytest.raises(SourceRequirementUnavailable):
        require_bound_sources(BASE + '. Waterproof performance is required. Manufacturing performance is not certified.')


@pytest.mark.parametrize('mutation', ['scale', 'omitted_check', 'assumed_source', 'tolerance'])
def test_unit_conversion_never_authorizes_provider_changes_to_frozen_requirements(mutation):
    from app.contracts.acceptance_v2 import AcceptanceContractV2, RequirementBindingError
    from app.validation.requirement_binding import validate_requirement_bindings
    from tests.e2e.source_obligation_fixture import bind
    text = '1 design unit = 25 mm. cylinder radius 2 units, height 3 units'
    proposal = bind(text)
    assert validate_requirement_bindings(text, AcceptanceContractV2.model_validate(proposal))['status'] == 'bound'
    if mutation == 'scale':
        proposal['checks'][0]['comparison']['value'] *= 2
    elif mutation == 'omitted_check':
        proposal['checks'].pop()
    elif mutation == 'assumed_source':
        proposal['checks'][0]['source_kind'] = 'assumption'
        proposal['checks'][0]['required'] = False
    else:
        proposal['checks'][0]['comparison']['tolerance_plus'] = 1
        proposal['checks'][0]['comparison']['tolerance_source_refs'] = ['provider-invented-tolerance']
    with pytest.raises(RequirementBindingError):
        validate_requirement_bindings(text, AcceptanceContractV2.model_validate(proposal))
