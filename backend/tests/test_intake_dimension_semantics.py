"""Expected meanings are authored independently of the source extractor."""
import pytest

from app.validation.requirement_binding import extract_requirements


@pytest.mark.parametrize('text,expected', [
    ('长方体长十厘米宽二十厘米高三十厘米', [100., 200., 300.]),
    ('长方体长100mm宽二十厘米高三十厘米', [100., 200., 300.]),
    ('板长一百零五毫米，宽两厘米，厚一点五毫米', [105., 20., 1.5]),
    ('板一百×二十×三毫米', [100., 20., 3.]),
])
def test_chinese_quantities_preserve_property_ownership_and_original_spans(text, expected):
    result = extract_requirements(text)
    assert not result['unsupported'], result
    rows = result['requirements']
    assert [r['comparison']['value'] for r in rows] == expected
    assert [r['scope']['axis'] for r in rows] == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    for row in rows:
        a, b = row['source_span']
        assert row['source_quote'] == text[a:b]


@pytest.mark.parametrize('text', ['宽一百二毫米', '宽十十毫米', '宽一百零毫米', '未支持尺寸二十五厘米'])
def test_ambiguous_or_unbound_chinese_quantities_cannot_disappear(text):
    assert extract_requirements(text)['unsupported']


def test_chinese_relations_tolerances_and_counts_use_numeric_values_without_losing_qualifiers():
    rows = extract_requirements('长度十至十二厘米，宽度二十毫米±零点五毫米，十一个孔')
    assert not rows['unsupported'], rows
    comparisons = {r['property']: r['comparison'] for r in rows['requirements']}
    assert comparisons['length'] == {'op': 'range', 'lower': 100., 'upper': 120., 'unit': 'mm'}
    assert comparisons['width']['value'] == 20. and comparisons['width']['tolerance_minus'] == .5
    assert comparisons['hole_count'] == {'op': 'eq', 'value': 11., 'unit': 'count'}


def test_chinese_number_words_in_narrative_are_not_misread_as_quantities():
    result = extract_requirements('板40×30×12mm。万一打印失败请联系我。')
    assert not result['unsupported'], result


@pytest.mark.parametrize('text', ['长方体长半厘米，宽20mm，高30mm',
                                '长方体长半米，宽20mm，高30mm',
                                '长方体长壹拾厘米，宽20mm，高30mm',
                                '板40×30×12mm，附加尺寸半厘米'])
def test_unsupported_numeric_spelling_keeps_a_quantity_obligation_even_when_number_parsing_fails(text):
    assert extract_requirements(text)['unsupported']


@pytest.mark.parametrize('omitted', ['cube_shape', 'cube_edge'])
def test_model_proposal_cannot_omit_either_implicit_shape_or_explicit_size(omitted):
    from app.contracts.acceptance_v2 import AcceptanceContractV2, RequirementBindingError
    from app.validation.requirement_binding import validate_requirement_bindings
    from tests.e2e.source_obligation_fixture import bind
    text = '构建一个十厘米的正方体'
    proposal = bind(text)
    proposal['checks'] = [c for c in proposal['checks'] if c['kind'] != omitted]
    with pytest.raises(RequirementBindingError):
        validate_requirement_bindings(text, AcceptanceContractV2.model_validate(proposal))


@pytest.mark.parametrize('text,value', [
    ('构建一个十厘米的正方体', 100.),
    ('创建一个2.5cm的立方体', 25.),
    ('正方体，边长一百毫米', 100.),
    ('立方体的棱长为二十五毫米', 25.),
    ('Create a cube with edge length 25mm', 25.),
    ('Create a 2.5cm cube', 25.),
])
def test_cube_name_and_edge_are_both_required_independent_of_wording(text, value):
    result = extract_requirements(text)
    assert not result['unsupported'], result
    assert sorted((r['kind'], r['comparison']['value']) for r in result['requirements']) == [
        ('cube_edge', value), ('cube_shape', 1.)]


def test_cube_triplet_cannot_replace_the_shape_requirement():
    result = extract_requirements('正方体100×100×100mm')
    assert not result['unsupported'], result
    assert any(r['kind'] == 'cube_shape' for r in result['requirements'])


@pytest.mark.parametrize('text', ['正方体，边长100mm，高50mm', '正方体100×50×100mm'])
def test_inconsistent_cube_dimensions_are_rejected_before_planning(text):
    assert extract_requirements(text)['unsupported']


def test_confirmed_edge_uses_the_same_saved_cube_subject_without_rewriting_either_source():
    from app.validation.requirement_binding import extract_source_requirements
    sources = [dict(source_input_id='objective', source_kind='user', text='构建一个正方体'),
               dict(source_input_id='dimensions', source_kind='confirmed', text='边长十厘米')]
    result = extract_source_requirements('\n'.join(s['text'] for s in sources), requirement_sources=sources)
    assert not result['unsupported'], result
    edge = next(r for r in result['requirements'] if r['kind'] == 'cube_edge')
    assert edge['comparison']['value'] == 100.
    assert edge['source_input_id'] == 'dimensions' and edge['source_quote'] == '边长十厘米'


@pytest.mark.parametrize('target,dimensions,expected', [
    ('构建一个球体', '半径两厘米', [('sphere_radius', 20.)]),
    ('构建一个圆柱体', '直径四厘米，高两厘米', [('cylinder_diameter', 40.), ('cylinder_height', 20.)]),
])
def test_saved_primitive_subject_is_shared_across_fields_without_borrowing_values(target, dimensions, expected):
    from app.validation.requirement_binding import extract_source_requirements
    sources = [dict(source_input_id='objective', source_kind='user', text=target),
               dict(source_input_id='dimensions', source_kind='confirmed', text=dimensions)]
    result = extract_source_requirements(target+'\n'+dimensions, requirement_sources=sources)
    assert not result['unsupported'], result
    dimensions_rows = [r for r in result['requirements'] if not r['kind'].endswith('_shape')]
    shapes = [r for r in result['requirements'] if r['kind'].endswith('_shape')]
    assert sorted((r['kind'], r['comparison']['value']) for r in dimensions_rows) == expected
    assert len(shapes) == 1 and shapes[0]['source_input_id'] == 'objective'
    assert shapes[0]['comparison'] == {'op': 'eq', 'value': 1., 'unit': 'count'}
    assert all(r['source_input_id'] == 'dimensions' and r['source_kind'] == 'confirmed'
               for r in dimensions_rows)


@pytest.mark.parametrize('target,dimensions', [('正方体', '板40×30×12mm'), ('球体', '板半径20mm'),
                                             ('球体', '板40×30×12mm'), ('圆柱体', '板40×30×12mm'),
                                             ('圆柱体和球体', '半径20mm')])
def test_saved_fields_never_share_dimensions_between_different_primitive_owners(target, dimensions):
    from app.validation.requirement_binding import extract_source_requirements
    sources = [dict(source_input_id='objective', source_kind='user', text=target),
               dict(source_input_id='dimensions', source_kind='confirmed', text=dimensions)]
    assert extract_source_requirements(target+'\n'+dimensions, requirement_sources=sources)['unsupported']


@pytest.mark.parametrize('text', ['正方体，边长100mm，边缘特殊处理', '正方体板的边长100mm',
                                '正方体和球体，边长100mm'])
def test_cube_context_does_not_hide_unknown_features_or_other_owners(text):
    assert extract_requirements(text)['unsupported']


@pytest.mark.parametrize('text,kind', [('球体长40mm宽40mm高40mm', 'sphere_shape'),
                                     ('圆柱体长40mm宽40mm厚25mm', 'cylinder_shape'),
                                     ('创建一个球体', 'sphere_shape'), ('创建一个圆柱体', 'cylinder_shape')])
def test_named_material_shape_survives_without_a_radial_dimension(text, kind):
    result = extract_requirements(text)
    assert not result['unsupported'], result
    assert any(r['kind'] == kind and r['comparison'] == {'op': 'eq', 'value': 1., 'unit': 'count'}
               for r in result['requirements']), result


@pytest.mark.parametrize('text,mode', [
    ('盒体外壁厚度：3 mm', 'side_walls'), ('盒体侧壁厚度3mm', 'side_walls'),
    ('盒体外壁厚3mm', 'side_walls'), ('盒体底板厚度：3 mm', 'cavity_floor'),
    ('盒体底厚3mm', 'cavity_floor'),
])
def test_wall_qualifiers_cannot_fall_back_to_the_whole_model_height(text, mode):
    result = extract_requirements(text)
    assert not result['unsupported'], result
    assert len(result['requirements']) == 1
    row = result['requirements'][0]
    assert row['kind'] == 'wall_thickness'
    assert row['scope'] == {'wall_mode': mode}
    assert row['comparison'] == {'op':'eq', 'value':3., 'unit':'mm'}


def test_box_wall_and_floor_clauses_share_the_box_owner():
    result = extract_requirements('盒体外壁厚度3mm，盒体底板厚度3mm')
    assert not result['unsupported'], result
    assert {r['scope'].get('wall_mode') for r in result['requirements']} == {'side_walls','cavity_floor'}


def test_a_qualified_box_floor_does_not_inherit_the_preceding_lid_owner():
    result = extract_requirements('上盖厚度3mm，盒体底板厚度3mm')
    assert result['unsupported'], result
    floor = next(r for r in result['requirements'] if r['scope'].get('wall_mode') == 'cavity_floor')
    assert any(d['reason'] == 'component_scope_unbound' and d['source_span'] == floor['source_span']
               for d in result['unsupported']), result


def test_box_walls_in_an_unlocated_assembly_cannot_be_whole_model_measurements():
    result = extract_requirements('盒体外壁厚度3mm，上盖厚度3mm')
    assert all(any(d['reason'] == 'component_scope_unbound' and d['source_span'][1] == r['source_span'][1]
                   for d in result['unsupported']) for r in result['requirements']), result


@pytest.mark.parametrize('text', ['圆柱体直径40mm，盒体高25mm', '球体半径20mm，板厚3mm'])
def test_a_material_primitive_and_another_part_need_independent_component_authority(text):
    result = extract_requirements(text)
    assert result['unsupported'], result
    assert all(any(d['reason'] == 'component_scope_unbound' and d['source_span'][1] == r['source_span'][1]
                   for d in result['unsupported']) for r in result['requirements']), result


@pytest.mark.parametrize('label,kind,value', [('直径','cylinder_diameter',40.), ('半径','cylinder_radius',20.)])
def test_cylinder_radius_and_diameter_are_material_geometry_not_holes(label, kind, value):
    text = f'生成圆柱体几何模型。圆柱体{label}为{value:g} mm，圆柱体高度为25 mm。'
    result = extract_requirements(text)
    assert not result['unsupported'], result
    rows = {r['kind']:r for r in result['requirements']}
    assert set(rows) == {kind, 'cylinder_height'}
    assert rows[kind]['comparison'] == {'op':'eq','value':value,'unit':'mm'}
    assert rows['cylinder_height']['comparison']['value'] == 25.


@pytest.mark.parametrize('text,kinds', [
    ('设计一个圆柱体，直径40mm，高25mm', {'cylinder_diameter','cylinder_height'}),
    ('Create a cylinder, diameter 40mm, height 25mm', {'cylinder_diameter','cylinder_height'}),
    ('圆柱体直径40mm，25mm高', {'cylinder_diameter','cylinder_height'}),
    ('圆柱直径40mm，圆柱体高度25mm', {'cylinder_diameter','cylinder_height'}),
    ('生成一个球，半径20mm', {'sphere_radius'}),
    ('Create a sphere with diameter 40mm', {'sphere_diameter'}),
], ids=['zh-cylinder','en-cylinder','suffix-height','cylinder-aliases','zh-sphere','en-sphere'])
def test_material_primitive_word_orders_and_delivery_article_keep_the_same_measurement(text,kinds):
    result = extract_requirements(text)
    assert not result['unsupported'], result
    assert {r['kind'] for r in result['requirements']} == kinds


def test_primitive_context_is_not_lost_to_a_following_newline_feature_clause():
    result = extract_requirements('圆柱体直径40mm\n孔径6mm')
    assert any(r['kind']=='cylinder_diameter' for r in result['requirements'])
    assert any(r['kind']=='hole_diameter' for r in result['requirements'])


@pytest.mark.parametrize('text', ['盒体壁的厚度3mm','盒体底面的厚度3mm','板上加强筋宽度3mm','外部安装耳的宽度10mm'])
def test_unknown_feature_qualifier_cannot_be_accepted_as_a_global_dimension(text):
    result=extract_requirements(text)
    assert result['unsupported'] or all(r['kind']!='overall_dimension' for r in result['requirements']),result


@pytest.mark.parametrize('text', ['圆柱体上法兰的直径6mm','圆柱体直径40mm，法兰半径6mm'])
def test_primitive_name_does_not_authorize_a_different_features_diameter(text):
    result=extract_requirements(text)
    assert result['unsupported'],result
    assert not any(r['kind'].startswith(('cylinder_','sphere_')) and r['comparison'].get('value')==6
                   for r in result['requirements'])


@pytest.mark.parametrize('separator', ['', '\n孔径4.6mm', '，孔径4.6mm', '；孔径4.6mm', '\n\n孔径4.6mm'])
def test_labelled_hole_count_does_not_borrow_a_noun_from_the_next_line(separator):
    text = 'M4安装孔数量：4个' + separator
    result = extract_requirements(text)
    assert not result['unsupported'], result
    counts = [r for r in result['requirements'] if r['kind'] == 'hole_count']
    assert len(counts) == 1 and counts[0]['comparison']['value'] == 4
    assert counts[0]['source_span'][1] <= len('M4安装孔数量：4个')


def test_following_fastener_name_cannot_be_consumed_as_a_metre_unit():
    text = '厚度3mm\nM4安装孔直径4.6mm\nM4安装孔数量4个'
    result = extract_requirements(text)
    for row in result['requirements']:
        assert not row['source_quote'].rstrip().endswith('M')


@pytest.mark.parametrize('text', ['板长80mm，孔位于安装耳中心', '板长80mm，孔位于支脚中心'])
def test_center_requires_its_own_reference_not_a_plate_somewhere_in_the_document(text):
    result = extract_requirements(text)
    assert not any(r['kind'] == 'hole_position' and r['scope'].get('frame') == 'bounds_center'
                   for r in result['requirements'])
    assert result['unsupported']


def test_long_unbound_input_retains_every_diagnostic_outside_the_bounded_error_message():
    from app.validation.requirement_binding import require_bound_sources, SourceRequirementUnavailable
    from app.workflows.errors import planning_error
    source = '\n'.join(f'未支持尺寸{i}mm' for i in range(1,101))
    with pytest.raises(SourceRequirementUnavailable) as caught:
        require_bound_sources(source)
    error = planning_error(caught.value)
    assert len(error.message) < 2000
    diagnostics = error.details[0]['diagnostics']
    assert len(diagnostics) == 100
    assert diagnostics[-1]['source_quote'] == '100mm'
    assert diagnostics[-1]['source_span'][1] == len(source)
    assert error.type == 'engineering_verification_unavailable' and error.non_retryable


@pytest.mark.parametrize('text,kind,value', [
    ('圆柱体高度改为30mm，保持直径不变','cylinder_diameter',40.),
    ('圆柱体高度改为30mm，保持半径不变','cylinder_radius',20.),
    ('圆柱体直径改为42mm，保持高度不变','cylinder_height',25.),
])
def test_material_preservation_uses_the_exact_sealed_measurement(text,kind,value):
    from tests.test_requirement_binding import baseline
    state=baseline()
    state['verified_acceptance']['measurements']=[
        {'kind':'cylinder_diameter','scope':{},'check_id':'d','values':[40.]},
        {'kind':'cylinder_height','scope':{},'check_id':'h','values':[25.]},
    ]
    result=extract_requirements(text,verified_state=state)
    assert not result['unsupported'],result
    preserved=[r for r in result['requirements'] if r.get('derivation')]
    assert len(preserved)==1 and preserved[0]['kind']==kind
    assert preserved[0]['comparison']['value']==value
    assert preserved[0]['derivation']['revision_id']=='current-revision'
    state['revision_id']='different'
    assert extract_requirements(text,verified_state=state)['unsupported']


@pytest.mark.parametrize('scope,values,accepted', [
    ({},[40.],True), ({},[41.],False),
    ({'component_region':{'min_mm':[0,0,0],'max_mm':[40,40,25]}},[40.],False),
    ({'object_ref':'part-A'},[40.],False), ({'point_mm':[0,0,0]},[40.],False),
])
def test_preservation_accepts_duplicate_evidence_only_for_the_same_complete_material(scope,values,accepted):
    from tests.test_requirement_binding import baseline
    state = baseline()
    state['verified_acceptance']['measurements'] = [
        {'kind':'cylinder_diameter','scope':scope,'check_id':'a','values':[40.]},
        {'kind':'cylinder_diameter','scope':scope,'check_id':'b','values':values},
    ]
    result = extract_requirements('圆柱体高度改为30mm，保持半径不变',verified_state=state)
    assert (not result['unsupported']) is accepted, result
