"""Independent source oracles: a parser cannot define its own completeness."""
import pytest

from app.contracts.acceptance_v2 import AcceptanceContractV2, RequirementBindingError
from app.validation.requirement_binding import extract_requirements, validate_requirement_bindings


def contract_for(text, rows):
    return AcceptanceContractV2.model_validate({'objective': text, 'checks': [
        {'check_id': str(index), 'binding_ref': row['requirement_id'],
         'source_quote': row['source_quote'], 'kind': row['kind'],
         'source_kind': row['source_kind'],
         'description': row['property'], 'comparison': row['comparison'], 'scope': row['scope'],
         **({'hole_group': row['hole_group']} if row.get('hole_group') else {})}
        for index, row in enumerate(rows)]})


@pytest.mark.parametrize('text,count', [
    ('4个直径6 mm的通孔', 4), ('四个直径6 mm的通孔', 4),
    ('四个直径为6毫米的通孔', 4), ('4 个孔径 6 mm 的通孔', 4),
    ('four 6 mm diameter through holes', 4),
    ('长度80mm，零个孔', 0), ('长度80mm，0个孔', 0), ('length 80 mm, zero holes', 0),
])
def test_count_is_a_separate_obligation_from_diameter_and_through(text, count):
    result = extract_requirements(text)
    assert not result['unsupported'], result
    counts = [r for r in result['requirements'] if r['kind'] == 'hole_count']
    assert len(counts) == 1
    assert counts[0]['comparison'] == {'op': 'eq', 'value': float(count), 'unit': 'count'}
    contract = contract_for(text, result['requirements'])
    assert validate_requirement_bindings(text, contract)['status'] == 'bound'
    reduced = contract.model_copy(update={'checks': tuple(c for c in contract.checks if c.kind != 'hole_count')})
    with pytest.raises(RequirementBindingError, match='coverage'):
        validate_requirement_bindings(text, reduced)


@pytest.mark.parametrize('text', [
    '长度80mm，四个沿任意螺旋线排布的通孔',
    '长度80mm，零个支脚', '长度80mm，十二个特殊加强筋',
    '长度80mm，两个实体不接触', '长度80mm，两个实体之间应有空气隔开',
    '长度80mm，不增加其他特征', '长度80mm，避免额外的孔和凸台',
    'length 80 mm, omit extra bosses', 'length 80 mm, exclude extra holes',
    '长度80mm，无额外的孔', 'length 80 mm, two connected solids',
    '孔径6mm的盲孔', '长度80mm，两个实体必须相离',
    '长度80mm，必须满足未知工程条件',
    '长度80mm，-1个孔',
    '盒体80×40×8mm，一个居中通孔，孔径6mm',
    'box 80x40x8mm, one centered 6 mm diameter hole',
])
def test_unknown_explicit_obligation_blocks_partial_contract(text):
    result = extract_requirements(text)
    assert result['unsupported'], result
    assert result['obligations']
    assert any(o['status'] == 'unresolved' for o in result['obligations'])
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable'):
        validate_requirement_bindings(text, contract_for(text, result['requirements']))


@pytest.mark.parametrize('relation', ['over', 'under', 'greater-than', 'less-than', '＞', '＜', 'above', 'below', 'exceeding'])
def test_strict_comparison_never_becomes_equality(relation):
    result = extract_requirements(f'{relation} 6 mm diameter hole')
    assert any(u['reason'] == 'strict_boundary_unsupported' for u in result['unsupported']), result
    assert not any(r['kind'] == 'hole_diameter' for r in result['requirements'])


@pytest.mark.parametrize('text,expected', [
    ('孔径6mm以上', {'op': 'ge', 'lower': 6., 'unit': 'mm'}),
    ('孔径6mm以下', {'op': 'le', 'upper': 6., 'unit': 'mm'}),
    ('直径6mm以上的孔', {'op': 'ge', 'lower': 6., 'unit': 'mm'}),
    ('at-least 6 mm diameter hole', {'op': 'ge', 'lower': 6., 'unit': 'mm'}),
    ('孔径 ≥ 6mm', {'op': 'ge', 'lower': 6., 'unit': 'mm'}),
])
def test_every_quantity_word_order_uses_the_same_comparison_policy(text, expected):
    result = extract_requirements(text)
    assert not result['unsupported'], result
    rows = [r for r in result['requirements'] if r['kind'] == 'hole_diameter']
    assert len(rows) == 1 and rows[0]['comparison'] == expected


def test_comparison_in_a_counted_phrase_cannot_hide_in_its_wider_source_span():
    text = '4个直径大于6mm的孔'
    result = extract_requirements(text)
    assert result['unsupported']
    assert not any(r['kind'] == 'hole_diameter' for r in result['requirements'])


COMPONENT_TEXT = ('盒体坐标范围为 X=0 至 80 mm、Y=0 至 55 mm、Z=0 至 32 mm。'
    '上盖坐标范围为 X=90 至 170 mm、Y=0 至 55 mm、Z=0 至 3 mm。'
    '盒体4个直径4.5mm的通孔；上盖4个直径3.4mm的通孔。')


def test_explicit_component_identity_authorizes_every_hole_obligation():
    result = extract_requirements(COMPONENT_TEXT)
    assert not result['unsupported'], result
    # Independent expectation includes two THROUGH obligations, not just the first word.
    for kind in ('hole_count', 'hole_diameter', 'hole_through'):
        rows = sorted([r for r in result['requirements'] if r['kind'] == kind], key=lambda r: r['source_span'])
        assert len(rows) == 2
        assert rows[0]['scope']['component_region'] == {'min_mm': [0, 0, 0], 'max_mm': [80, 55, 32]}
        assert rows[1]['scope']['component_region'] == {'min_mm': [90, 0, 0], 'max_mm': [170, 55, 3]}
        assert all(r['scope_source_refs'] for r in rows)
    contract = contract_for(COMPONENT_TEXT, result['requirements'])
    assert validate_requirement_bindings(COMPONENT_TEXT, contract)['status'] == 'bound'
    wrong = contract.model_copy(update={'checks': tuple(c.model_copy(update={'scope': c.scope.model_copy(
        update={'component_region': None})}) if c.kind == 'hole_count' else c for c in contract.checks)})
    with pytest.raises(RequirementBindingError, match='scope'):
        validate_requirement_bindings(COMPONENT_TEXT, wrong)


@pytest.mark.parametrize('text', [
    '盒体80×55×32mm，内部空腔74×49×29mm',
    '盒体长80mm，内部空腔长74mm', '长度80mm，盖板厚度3mm',
    '长度80mm，盖板3mm厚', '长度80mm，支脚3×4×5mm',
    '盒体4个通孔，孔径4.5mm；上盖4个通孔，孔径3.4mm',
])
def test_local_scope_cannot_be_inferred_from_whole_model_or_target_value(text):
    result = extract_requirements(text)
    assert result['unsupported'], result
    assert any(u['reason'] == 'feature_dimension_scope_unbound' or u['reason'] == 'component_scope_unbound'
               for u in result['unsupported'])


def test_explicit_component_scalar_suffix_and_triplet_use_one_scope_policy():
    prefix = '盖板坐标范围为 X=90 至 170 mm、Y=0 至 55 mm、Z=0 至 3 mm。'
    for suffix in ('盖板厚度3mm', '盖板3mm厚', '盖板80×55×3mm'):
        result = extract_requirements(prefix + suffix)
        assert not result['unsupported'], result
        rows = [r for r in result['requirements'] if r['kind'] == 'overall_dimension']
        assert rows and all(r['scope']['component_region']['min_mm'] == [90, 0, 0] for r in rows)


def test_declared_envelope_label_is_one_identity_when_following_clauses_omit_the_name():
    text = ('盒体外部长方体坐标范围为 X=0 至 80 mm、Y=0 至 55 mm、Z=0 至 32 mm。'
            '4个直径4.5mm的通孔。上盖坐标范围为 X=90 至 170 mm、Y=0 至 55 mm、Z=0 至 3 mm。'
            '4个直径3.4mm的通孔。')
    result = extract_requirements(text)
    assert not result['unsupported'], result
    holes = sorted([r for r in result['requirements'] if r['kind'] == 'hole_count'], key=lambda r: r['source_span'])
    assert [r['scope']['component_region']['min_mm'] for r in holes] == [[0, 0, 0], [90, 0, 0]]


def test_duplicate_component_labels_do_not_authorize_arbitrary_selection():
    text = COMPONENT_TEXT.replace('上盖坐标', '盒体坐标')
    result = extract_requirements(text)
    assert any(u['reason'] == 'component_scope_ambiguous' for u in result['unsupported'])


def test_feature_groups_inside_one_component_do_not_become_all_holes():
    text = ('盒体坐标范围为 X=0 至 80 mm、Y=0 至 55 mm、Z=0 至 32 mm。'
            '盒体底部4个通孔，孔径4.5mm；盒体右侧1个通孔，孔径12.5mm。')
    result = extract_requirements(text)
    assert any(u['reason'] == 'feature_group_scope_unbound' for u in result['unsupported'])
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable'):
        validate_requirement_bindings(text, contract_for(text, result['requirements']))


def test_explicit_directive_can_only_be_discharged_by_its_own_predicate():
    for text in ['必须孔径6mm', 'must have hole diameter 6 mm']:
        result = extract_requirements(text)
        assert not result['unsupported'], result
        assert any(o['category'] == 'directive' for o in result['obligations'])
        assert all(o['status'] == 'bound' for o in result['obligations'])


def test_centered_hole_obligation_requires_a_position_predicate():
    text = '板80×40×8mm，一个居中通孔，孔径6mm'
    result = extract_requirements(text)
    assert not result['unsupported'], result
    position = [o for o in result['obligations'] if o['category'] == 'feature_position']
    assert len(position) == 1 and position[0]['status'] == 'bound'
    refs = {r['requirement_id']: r['kind'] for r in result['requirements']}
    assert all(refs[ref] == 'hole_position' for ref in position[0]['requirement_refs'])


COMPONENT_BOUNDS = COMPONENT_TEXT.split('盒体4个')[0]


@pytest.mark.parametrize('subject', [
    '盒体和上盖各有', '盒体和上盖均有', '盒体与上盖各自有',
    '盒体、上盖各有', '盒体及上盖 each have ',
])
def test_distributed_component_requirements_cover_every_named_subject(subject):
    text = COMPONENT_BOUNDS + subject + '4个直径6mm的通孔。'
    result = extract_requirements(text)
    assert not result['unsupported'], result
    assert len({r['requirement_id'] for r in result['requirements']}) == len(result['requirements'])
    for kind in ('hole_count', 'hole_diameter', 'hole_through'):
        rows = [r for r in result['requirements'] if r['kind'] == kind]
        assert len(rows) == 2, (kind, result)
        assert {tuple(r['scope']['component_region']['min_mm']) for r in rows} == {(0, 0, 0), (90, 0, 0)}
    quantities = [o for o in result['obligations'] if o['category'] == 'count']
    assert len(quantities) == 1 and len(quantities[0]['requirement_refs']) == 2
    distributions = [o for o in result['obligations'] if o['category'] == 'component_distribution']
    assert len(distributions) == 1 and distributions[0]['status'] == 'bound'
    assert len(distributions[0]['component_refs']) == 2
    contract = contract_for(text, result['requirements'])
    assert validate_requirement_bindings(text, contract)['status'] == 'bound'
    partial = contract.model_copy(update={'checks': tuple(c for c in contract.checks
        if not (c.kind.startswith('hole_') and c.scope.component_region.min_mm[0] == 0))})
    with pytest.raises(RequirementBindingError, match='coverage'):
        validate_requirement_bindings(text, partial)


@pytest.mark.parametrize('subject', ['盒体或上盖有', '盒体或者上盖有', '盒体和上盖有', '盒体和上盖分别有'])
def test_unresolved_component_choice_cannot_select_the_last_subject(subject):
    text = COMPONENT_BOUNDS + subject + '4个直径6mm的通孔。'
    result = extract_requirements(text)
    assert any(u['reason'] == 'component_scope_ambiguous' for u in result['unsupported']), result
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable'):
        validate_requirement_bindings(text, contract_for(text, result['requirements']))


def test_distributed_thickness_and_derived_depth_keep_distinct_component_identities():
    text = ('板坐标范围为 X=0 至 80 mm、Y=0 至 40 mm、Z=0 至 8 mm。'
            '盖板坐标范围为 X=90 至 170 mm、Y=0 至 40 mm、Z=0 至 8 mm。'
            '板和盖板均厚8mm，各有2个直径6mm的通孔。')
    result = extract_requirements(text)
    assert not result['unsupported'], result
    rows = [r for r in result['requirements'] if r['kind'] == 'hole_depth']
    assert len(rows) == 2 and len({r['requirement_id'] for r in rows}) == 2
    assert {tuple(r['scope']['component_region']['min_mm']) for r in rows} == {(0, 0, 0), (90, 0, 0)}
    records = {r['requirement_id']: r for r in result['requirements']}
    for depth in rows:
        thickness = records[depth['derivation']['source_refs'][0]]
        assert thickness['scope']['component_region'] == depth['scope']['component_region']
    assert validate_requirement_bindings(text, contract_for(text, result['requirements']))['status'] == 'bound'


@pytest.mark.parametrize('text', [
    '板80×40×8mm，直径6mm的通孔，直径4mm的通孔。',
    '板80×40×8mm，孔径6mm，孔径4mm。',
])
def test_distinct_hole_populations_cannot_share_an_unqualified_measurement_scope(text):
    result = extract_requirements(text)
    assert any(u['reason'] == 'feature_group_scope_unbound' for u in result['unsupported']), result
    assert any(o['category'] == 'hole_population' and o['status'] == 'unresolved' for o in result['obligations'])
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable'):
        validate_requirement_bindings(text, contract_for(text, result['requirements']))


def test_shared_subject_expansion_does_not_hide_multiple_groups_per_component():
    text = COMPONENT_BOUNDS + '盒体和上盖各有2个直径6mm的通孔，2个直径4mm的通孔。'
    result = extract_requirements(text)
    assert not result['unsupported'], result
    checks = contract_for(text, result['requirements'])
    assert validate_requirement_bindings(text, checks)['status'] == 'bound'
    groups = {c.hole_group.group_id for c in checks.checks if c.hole_group}
    assert len(groups) == 4


def test_compatible_diameter_bounds_remain_constraints_on_one_hole_population():
    text = '板80×40×8mm，两个通孔，孔径至少4mm，孔径不超过6mm。'
    result = extract_requirements(text)
    assert not result['unsupported'], result
    assert validate_requirement_bindings(text, contract_for(text, result['requirements']))['status'] == 'bound'


@pytest.mark.parametrize('dimensions', ['2个直径4mm的通孔。', '孔径4mm。'])
def test_user_and_confirmed_sources_cannot_freeze_conflicting_hole_populations(dimensions):
    from types import SimpleNamespace
    from app.domain.requirement_basis import RequirementBasisV1
    from app.workflows.handlers.planning import requirements_objective, requirements_sources
    from app.validation.requirement_binding import extract_source_requirements
    request = SimpleNamespace(objective='板80×40×8mm，2个直径6mm的通孔。',
        operation_context=SimpleNamespace(requirement_basis=RequirementBasisV1(
            target='板', design_scope='geometry', source_kind='user_specification',
            source_reference='用户确认', dimensions=dimensions)))
    text, sources = requirements_objective(request), requirements_sources(request)
    result = extract_source_requirements(text, requirement_sources=sources)
    assert any(u['reason'] == 'feature_group_scope_unbound' for u in result['unsupported']), result
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable'):
        validate_requirement_bindings(text, contract_for(text, result['requirements']), requirement_sources=sources)


def test_confirmed_compatible_diameter_bound_does_not_create_another_population():
    from app.validation.requirement_binding import extract_source_requirements
    sources = [{'source_input_id': 'objective', 'source_kind': 'user', 'text': '2个直径6mm的通孔。'},
               {'source_input_id': 'basis-dimensions', 'source_kind': 'confirmed', 'text': '孔径不超过6mm。'}]
    text = '\n'.join(s['text'] for s in sources)
    result = extract_source_requirements(text, requirement_sources=sources)
    assert not result['unsupported'], result
    assert validate_requirement_bindings(text, contract_for(text, result['requirements']), requirement_sources=sources)['status'] == 'bound'
