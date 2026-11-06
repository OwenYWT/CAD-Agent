"""Bounded, deterministic source binding before a new acceptance contract freezes.

The parser recognizes explicit engineering assertions, not arbitrary prose. It
returns unsupported quantities instead of treating number occurrence as coverage.
No model-supplied source kind, tolerance or coordinate frame is authoritative.
"""
from __future__ import annotations

from decimal import Decimal
import re
import math

from app.execution.canonical import canonical_sha256
from app.validation.quantities import NUMBER as _NUMBER, CHINESE_INTEGER, number
from app.contracts.acceptance_v2 import RequirementBindingError, SourceContextV1, SourceRequirementUnavailable
from app.validation.source_obligations import (
    RELATION as _RELATION, LOWER as _LOWER, STRICT as _STRICT,
    COUNT_WORDS as _COUNT_WORDS, COUNT_VALUE, CENTERED as _CENTERED,
    inventory, normalize_relation, resolve_inventory, bind_component_scopes, inventory_hole_populations,
    CUBE, source_owners, incompatible_constraints,
)


_UNIT = r'(?:mm(?:³|3|\^3)?|cm(?:³|3|\^3)?|m(?:³|3|\^3)?|立方毫米|立方厘米|立方米|毫米|厘米|米)'
_DECLARED_UNIT = re.compile(rf'(?:单位|units?)\s*[:：=]\s*(?P<unit>{_UNIT})(?![a-zA-Z])', re.I)
_COORDINATE_BOX = re.compile(
    r'(?P<label>[\w\u4e00-\u9fff]+?)坐标范围(?:为|[:：])?\s*' +
    r'\s*[、,，;；]?\s*'.join(
        rf'{axis}\s*[=：:]\s*(?P<{axis}lo>-?{_NUMBER})\s*(?:至|到|to)\s*'
        rf'(?P<{axis}hi>-?{_NUMBER})\s*(?P<{axis}unit>{_UNIT})?'
        for axis in 'XYZ'), re.I)
_PROPERTIES = {
    '夹口': ('gap', 'planar_gap'), '开口': ('gap', 'planar_gap'), '间隙': ('gap', 'planar_gap'),
    'gap': ('gap', 'planar_gap'), 'opening': ('gap', 'planar_gap'),
    '壁厚': ('wall_thickness', 'wall_thickness'), '壁的厚度': ('wall_thickness', 'wall_thickness'),
    'wall thickness': ('wall_thickness', 'wall_thickness'),
    **{name: ('side_wall_thickness', 'wall_thickness') for name in
       ('外壁厚度', '外壁的厚度', '外壁厚', '侧壁厚度', '侧壁的厚度', '侧壁厚', 'side wall thickness', 'sidewall thickness')},
    **{name: ('cavity_floor_thickness', 'wall_thickness') for name in
       ('盒体底板厚度', '盒体底面的厚度', '盒体底板厚', '盒体底厚', '盒底厚度', '盒底厚', 'cavity floor thickness')},
    '孔径': ('hole_diameter', 'hole_diameter'), '孔直径': ('hole_diameter', 'hole_diameter'),
    'hole diameter': ('hole_diameter', 'hole_diameter'), 'bore diameter': ('hole_diameter', 'hole_diameter'),
    '孔深': ('hole_depth', 'hole_depth'), 'hole depth': ('hole_depth', 'hole_depth'),
    '体积': ('volume', 'volume'), 'volume': ('volume', 'volume'),
    **{name: ('cube_edge', 'cube_edge') for name in ('边长', '棱长', 'edge length', 'side length')},
    '长度': ('length', 'overall_dimension'), '长': ('length', 'overall_dimension'), 'length': ('length', 'overall_dimension'),
    '宽度': ('width', 'overall_dimension'), '宽': ('width', 'overall_dimension'), 'width': ('width', 'overall_dimension'),
    '高度': ('height', 'overall_dimension'), '高': ('height', 'overall_dimension'), 'height': ('height', 'overall_dimension'),
    '厚度': ('thickness', 'overall_dimension'), '厚': ('thickness', 'overall_dimension'), 'thickness': ('thickness', 'overall_dimension'),
}
_PROPERTY = '|'.join(re.escape(p) for p in sorted(_PROPERTIES, key=len, reverse=True))
_DIMENSION_QUANTITY = (
    rf'(?P<value>{_NUMBER})[ \t]*(?P<unit1>{_UNIT})?[ \t]*'
    rf'(?:(?P<range>至|到|[~～–—-]|to)[ \t]*(?P<upper>{_NUMBER})[ \t]*(?P<unit2>{_UNIT})?)?[ \t]*'
    rf'(?:(?:±|\+/-)[ \t]*(?P<symmetric>{_NUMBER})|\+[ \t]*(?P<plus>{_NUMBER})[ \t]*/[ \t]*[-−][ \t]*(?P<minus>{_NUMBER}))?[ \t]*'
    rf'(?P<unit3>{_UNIT})?(?P<suffix>以上|以下)?\s*')
_SCALAR = re.compile(
    rf'(?P<prefix>{_RELATION})?\s*(?P<property>{_PROPERTY})\s*(?:改为|修改为|设为|调整为|为|是|[:：=]|of|is|to)?\s*'
    rf'(?P<relation>{_RELATION})?\s*{_DIMENSION_QUANTITY}', re.I)
# Compose noun phrases once so a circular qualifier cannot retain a diameter
# while silently dropping the count or the passage predicate. Source obligation
# discovery is deliberately broader and independent (source_obligations.py).
_HOLE_ZH = r'(?:圆形|圆)?孔'
_HOLE_EN = r'(?:(?:circular|round)[ \t]+)?holes?\b'
_PASSAGE_ZH = r'(?:贯穿|穿透|(?:两端|双端|两头)(?:均|都)?开口)(?:的)?'
_PASSAGE_EN = r'(?:through|thru)[ -]?'
_THROUGH_HOLE = re.compile(rf'通孔|{_PASSAGE_ZH}{_HOLE_ZH}|\b{_PASSAGE_EN}{_HOLE_EN}', re.I)
# A predicative sentence is a complete assertion, not an adjective hit. Never
# lower a substring of an unknown path, target, negation, or conditional clause.
_HOLE_PASSAGE_CLAUSE = re.compile(
    rf'{_HOLE_ZH}[ \t]*(?:(?:必须|务必|需要|应当|应该|须|需|要|应)[ \t]*)?'
    r'(?:(?:为|是)[ \t]*通孔|(?:打通|打穿|贯通|贯穿|穿透|穿过)'
    r'(?P<plate>[ \t]*(?:整块板|板厚))?)')
_COUNT = re.compile(rf'(?P<relation>{_RELATION})?[ \t]*(?<![a-zA-Z0-9_.零〇一二两三四五六七八九十百千万亿点])(?P<count>{COUNT_VALUE})'
    r'[ \t]*(?:(?:个|处|M\d+(?:\.\d+)?|圆形|圆|螺钉|安装|固定|线缆|接头|independent|separate|connected|centered|(?:through|thru)[- ]?|circular|round|独立|连通|居中|贯穿|穿透|(?:两端|双端|两头)(?:均|都)?开口|通|的)[ \t]*)*'
    r'(?P<kind>实体|孔|solids?\b|holes?\b)', re.I)
_LABELLED_COUNT = re.compile(rf'(?P<kind>孔|实体)(?:的)?(?:数量|数目|数)[ \t]*(?:为|[:：=])?[ \t]*'
    rf'(?P<relation>{_RELATION})?[ \t]*(?P<count>{COUNT_VALUE})[ \t]*(?:个|处)?', re.I)
_EXPLICIT_QUANTITY = re.compile(rf'(?<![a-zA-Z0-9_.]){_NUMBER}\s*{_UNIT}(?![a-zA-Z])', re.I)
_OTHER_ENGINEERING_QUANTITY = re.compile(
    rf'(?<![a-zA-Z0-9_.]){_NUMBER}\s*(?:°|度|弧度|deg(?:rees?)?\b|rad(?:ians?)?\b|英寸|inches?\b)|'
    rf'(?:{_NUMBER}|[一二两三四五六七八九十]+)[ \t]*(?:个|处|颗|条|只)[ \t]*'
    r'(?:支脚|支柱|支撑|槽|孔|实体|齿|筋|凸台|通道|倒角|圆角)|'
    rf'\b(?:{_NUMBER}|one|two|three|four|five|six)[ \t]+(?:[a-z-]+[ \t]+){{0,2}}'
    r'(?:legs?|supports?|ribs?|fins?|pins?|posts?|slots?|teeth|holes?|solids?)\b', re.I)
_BOX_DIMENSIONS = re.compile(rf'(?P<x>{_NUMBER})\s*[x×*]\s*(?P<y>{_NUMBER})\s*[x×*]\s*(?P<z>{_NUMBER})\s*(?P<unit>{_UNIT})', re.I)
_DIAMETER = re.compile(rf'(?P<prefix>{_RELATION})?\s*(?:直径|diameter)\s*(?:改为|修改为|设为|调整为|为|是|of|is|to|[:：=])?\s*'
    rf'(?P<relation>{_RELATION})?\s*{_DIMENSION_QUANTITY}', re.I)
_RADIUS = re.compile(rf'(?P<prefix>{_RELATION})?[ \t]*(?:半径|radius)[ \t]*(?:改为|修改为|设为|调整为|为|是|of|is|to|[:：=])?[ \t]*'
    rf'(?P<relation>{_RELATION})?[ \t]*{_DIMENSION_QUANTITY}', re.I)
_PRIMITIVE = re.compile(r'圆柱(?:体)?|\bcylinder\b|球(?:体)?|\bsphere\b', re.I)
_NOMINAL_CUBE = re.compile(rf'(?P<prefix>{_RELATION})?\s*{_DIMENSION_QUANTITY}(?:的\s*)?(?:{CUBE.pattern})', re.I)


def _single_cube_subject(text):
    return {m['kind'] for m in source_owners(text)} == {'cube'}


def _cube_conflicts(requirements, unsupported):
    scopes = {}
    for row in requirements:
        if row['kind'] == 'cube_edge':
            scopes.setdefault(canonical_sha256(row['scope']), []).append(row)
    for rows in scopes.values():
        conflicting = incompatible_constraints(rows, ('cube_edge',))
        unsupported.extend({'source_input_id': r['source_input_id'], 'source_span': r['source_span'],
                            'reason': 'primitive_dimension_conflict'} for r in rows
                           if r['requirement_id'] in conflicting)


def _primitive_subject(text, start, end, primitive_context=None):
    """Resolve a named material primitive, never a hole or another feature."""
    # Quantity source spans retain legacy trailing whitespace for stable IDs.
    # That whitespace must not move ownership across a newline into a hole.
    end = start + len(text[start:end].rstrip())
    start += len(text[start:end]) - len(text[start:end].lstrip())
    lo = max(text.rfind(c, 0, start) for c in ',，;；。\n') + 1
    hi = min([p for c in ',，;；。\n' if (p := text.find(c, end)) >= 0] or [len(text)])
    clause = text[lo:hi]
    if re.search(r'孔|圆角|槽|\b(?:hole|bore|fillet|slot)\b', clause, re.I):
        return None
    def connects(fragment):
        for pattern in (_SCALAR, _DIAMETER, _RADIUS, _SUFFIX_DIMENSION):
            fragment = pattern.sub('', fragment)
        return re.fullmatch(r'(?:\s|的|模型|几何模型|整体|其|它|of\b|with\b|and\b|the\b|model\b|solid\b)*', fragment, re.I)
    matches = [m for m in _PRIMITIVE.finditer(clause)
               if (m.end()+lo <= start and connects(text[m.end()+lo:start]))
               or (m.start()+lo >= end and connects(text[end:m.start()+lo]))]
    if not matches and connects(text[lo:start]) and not re.search(r'孔|板|盒|盖|支脚|安装耳|\b(?:hole|plate|box|lid|foot)\b', text, re.I):
        matches = list(_PRIMITIVE.finditer(text))
    kinds = {'cylinder' if re.search(r'圆柱|cylinder', m.group(), re.I) else 'sphere' for m in matches}
    if (not kinds and primitive_context and primitive_context['kind'] in {'cylinder', 'sphere'}
            and not source_owners(text) and connects(text[lo:start]) and connects(text[end:hi])):
        return primitive_context['kind']
    return next(iter(kinds)) if len(kinds) == 1 else None
_HOLE_SIZE = re.compile(rf'(?P<relation>{_RELATION})?\s*{_DIMENSION_QUANTITY}'
    rf'\s+(?:diameter\s+)?(?:{_PASSAGE_EN})?{_HOLE_EN}', re.I)
_SIZED_HOLE_COUNT = re.compile(rf'(?P<relation>{_RELATION})?\s*\b(?P<count>one|two|three|four|five|six|single|an?|\d+)\s+'
    rf'(?:centered\s+|central\s+)?{_NUMBER}\s*{_UNIT}\s+(?:diameter\s+)?(?:{_PASSAGE_EN})?{_HOLE_EN}', re.I)
_CHINESE_SIZED_HOLE_COUNT = re.compile(
    rf'(?P<relation>{_RELATION})?\s*(?<![a-zA-Z0-9_.零〇一二两三四五六七八九十百千万亿点])(?P<count>{COUNT_VALUE})\s*[个处]\s*'
    rf'(?:孔径|直径)\s*(?:为|[:：=])?\s*(?:{_RELATION})?\s*{_NUMBER}\s*{_UNIT}\s*(?:以上|以下)?\s*的?\s*(?:{_PASSAGE_ZH}|通)?{_HOLE_ZH}', re.I)
_SUFFIX_DIMENSION = re.compile(rf'(?P<relation>{_RELATION})?\s*(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})\s*'
    rf'(?P<property>long\b|wide\b|thick\b|high\b|[长宽厚高](?!度|\s*{_NUMBER}))', re.I)
_WORLD_AXIS = re.compile(r'(?:世界|world)\s*(?P<axis>[xyz])\s*(?:轴|axis)?', re.I)
_LOCAL_AXIS = re.compile(r'(?:局部|local)\s*(?P<axis>[xyz])\s*(?:轴|axis)?', re.I)
_PRESERVED_PROPERTY = _PROPERTY + r'|直径|半径|diameter|radius'
_PRESERVATION = re.compile(
    rf'(?:(?:保持|保留)\s*(?P<leading>{_PRESERVED_PROPERTY})(?:\s*(?:不变|原值))?|'
    rf'(?P<trailing>{_PRESERVED_PROPERTY})\s*(?:保持)?(?:不变|原值)|'
    rf'(?:preserve|keep)\s+(?:the\s+)?(?P<english>{_PRESERVED_PROPERTY})(?:\s+(?:unchanged|the same))?)', re.I)
_DESIGN_REQUEST = re.compile(
    r'^\s*(?:(?:请|帮我|请帮我|给我|麻烦)\s*)?(?:设计|创建|构建|制作|生成|做)\s*(?P<zh>一\s*个|1\s*个)|'
    r'^\s*(?:please\s+)?(?:design|create|build|make|generate)\s+(?P<en>a|an|one)\b', re.I)
_DESIGN_PRODUCT = re.compile(r'板|盒|长方体|正方体|立方体|圆柱(?:体)?|球(?:体)?|挂钩|外壳|夹具|支架|零件|模型|装配体|组件|'
    r'\b(?:plate|box|block|cube|cylinder|sphere|hook|enclosure|bracket|part|model|assembly)\b', re.I)
_CLAMP_PLATE = re.compile(
    rf'(?:可?(?:夹在|夹住|夹到)|\bclamps?\s+(?:to|onto|on)\s+(?:an?\s+)?)\s*'
    rf'(?:(?:厚度|板厚|厚)\s*(?:为|是|[:：=])?\s*)?(?P<relation>{_RELATION})?\s*{_DIMENSION_QUANTITY}'
    r'\s*(?:厚的?|的|thick\s+)?\s*(?:桌板|台面板|desktop\b|tabletop\b|table\s+top\b)', re.I)
_EXTERNAL_PLATE_CLAUSE = re.compile(
    r'(?:可?(?:夹在|夹住|夹到)|用于|适配|\bfor\b|\bclamps?\b)[^，,。；;\n]*'
    r'(?:桌板|台面板|desktop\b|tabletop\b|table\s+top\b)[^，,。；;\n]*', re.I)


def _preceding_hole_subject(text, start, end):
    """Resolve only an adjacent, complete hole noun phrase, never a product.

    A comma/newline does not change "through circular hole, diameter 6mm".
    An intervening owner, modifier or ambiguous multi-object clause does. Keep
    original offsets so the inherited subject remains auditable independently
    of the dimension's own source quote.
    """
    start += len(text[start:end]) - len(text[start:end].lstrip())
    lo = max(text.rfind(c, 0, start) for c in ',，;；。\n') + 1
    if not lo or text[lo:start].strip():
        return None
    before = text[:lo-1]
    previous_start = max(before.rfind(c) for c in ',，;；。\n') + 1
    phrase = before[previous_start:].strip()
    counted = _COUNT.fullmatch(phrase)
    if (not _THROUGH_HOLE.fullmatch(phrase) and not re.fullmatch(_HOLE_ZH, phrase)
            and not re.fullmatch(_HOLE_EN, phrase, re.I)
            and not (counted and counted['kind'].lower().startswith(('孔', 'hole')))):
        return None
    a = previous_start + len(before[previous_start:]) - len(before[previous_start:].lstrip())
    return [a, a + len(phrase)]


def _quantity(value, unit):
    normalized = unit.lower().replace('^', '').replace('³', '3')
    cubic = '3' in normalized or normalized.startswith('立方')
    base = normalized.replace('3', '').replace('立方', '')
    factor = {'mm': 1, '毫米': 1, 'cm': 10, '厘米': 10, 'm': 1000, '米': 1000}[base]
    return float(number(value) * Decimal(factor) ** (3 if cubic else 1)), 'mm3' if cubic else 'mm'


def extract_requirements(objective: str, *, source_input_id: str = 'objective', source_kind: str = 'user',
                         verified_state=None, execution_scope='cad_geometry', plate_context=None, primitive_context=None) -> dict:
    requirements, unsupported, covered = [], [], []
    obligations = inventory(objective, source_input_id=source_input_id)
    # Validate whole Chinese tokens before any shorter pattern can consume a
    # suffix of an ambiguous number. Original quotes/offsets remain untouched.
    numeric_spans = [m.span() for pattern in (_EXPLICIT_QUANTITY, _SCALAR, _BOX_DIMENSIONS,
                    _COORDINATE_BOX, _COUNT, _LABELLED_COUNT) for m in pattern.finditer(objective)]
    for token in re.finditer(rf'{CHINESE_INTEGER}(?:点[零〇一二两三四五六七八九]+)?', objective):
        if not any(a <= token.start() and token.end() <= b for a, b in numeric_spans):
            continue
        try:
            number(token.group())
        except ValueError:
            unsupported.append({'source_span': list(token.span()), 'reason': 'quantity_number_ambiguous'})
    if unsupported:
        return {'schema_version': 'requirement-extraction.v1', 'requirements': [],
                'unsupported': unsupported, 'obligations': obligations}
    comparison_spans = []
    declarations = list(_DECLARED_UNIT.finditer(objective))
    declared = declarations[0] if declarations and len({m['unit'].lower() for m in declarations}) == 1 else None
    design_requests, external_references, external_spans = [], [], []

    def reference(start, end, label):
        return {'reference_id': f's-{source_input_id}-{start}-{end}-{label}',
                'source_input_id': source_input_id, 'source_kind': source_kind,
                'source_span': [start, end], 'source_quote': objective[start:end]}

    def append(start, end, prop, kind, comparison, scope=None, *, identity=None):
        identity = identity or f'r-{source_input_id}-{start}-{end}-{prop}'
        if comparison.get('tolerance_minus') or comparison.get('tolerance_plus'):
            comparison['tolerance_source_refs'] = [identity + '-tolerance']
        requirements.append({'requirement_id': identity, 'source_input_id': source_input_id,
            'source_kind': source_kind, 'source_span': [start, end], 'source_quote': objective[start:end],
            'property': prop, 'kind': kind, 'comparison': comparison, 'scope': scope or {},
            'tolerance_source_refs': comparison.get('tolerance_source_refs', [])})
        if kind.startswith(('cylinder_', 'sphere_')) and primitive_context and not source_owners(objective):
            requirements[-1]['derivation'] = {'rule': 'saved_primitive_subject.v1',
                                             'subject_sources': primitive_context['subjects']}
        if declared and not _EXPLICIT_QUANTITY.search(objective[start:end]) and comparison['unit'] != 'count':
            requirements[-1]['unit_source_span'] = list(declared.span())
        covered.append((start, end))

    def relation_comparison(match, value, unit):
        # Every word order goes through the same boundary policy. In
        # particular, a diameter shortcut cannot turn a rejected > into =.
        groups = match.groupdict()
        keys = [key for key in ('prefix', 'relation', 'suffix') if groups.get(key)]
        relations = [normalize_relation(groups[key]) for key in keys]
        reason = ('multiple_comparison_relations' if len(relations) > 1 else
                  'strict_boundary_unsupported' if any(r in _STRICT for r in relations) else None)
        if reason:
            unsupported.append({'source_span': list(match.span()), 'reason': reason})
            covered.append(match.span())
            return None
        relation = next(iter(relations), '')
        comparison_spans.extend(match.span(key) for key in keys)
        op = 'ge' if relation in _LOWER else 'le' if relation else 'eq'
        return {'op': op, {'eq': 'value', 'ge': 'lower', 'le': 'upper'}[op]: value, 'unit': unit}

    def dimension_comparison(match):
        unit = match['unit1'] or match['unit2'] or match['unit3'] or (declared['unit'] if declared else None)
        if unit is None:
            unsupported.append({'source_span': list(match.span()), 'reason': 'quantity_unit_unbound'})
            return None
        value, canonical_unit = _quantity(match['value'], unit)
        comparison = relation_comparison(match, value, canonical_unit)
        if comparison is None:
            return None
        if match['range']:
            upper, upper_unit = _quantity(match['upper'], match['unit2'] or match['unit3'] or unit)
            if comparison['op'] != 'eq' or upper_unit != canonical_unit or upper < value:
                unsupported.append({'source_span': list(match.span()), 'reason': 'range_relation_conflict'})
                return None
            comparison = {'op': 'range', 'lower': value, 'upper': upper, 'unit': canonical_unit}
        if match['symmetric'] or match['plus']:
            if comparison['op'] != 'eq':
                unsupported.append({'source_span': list(match.span()), 'reason': 'range_tolerance_conflict'})
                return None
            minus, _ = _quantity(match['symmetric'] or match['minus'], match['unit3'] or unit)
            plus, _ = _quantity(match['symmetric'] or match['plus'], match['unit3'] or unit)
            comparison.update(tolerance_minus=minus, tolerance_plus=plus)
        return comparison

    for match in CUBE.finditer(objective):
        append(*match.span(), 'cube_shape', 'cube_shape', {'op': 'eq', 'value': 1., 'unit': 'count'})
    cube_refs = [r['requirement_id'] for r in requirements if r['kind'] == 'cube_shape']
    cube_refs = (cube_refs if _single_cube_subject(objective) else
                 (primitive_context or {}).get('cube_shape_refs', []) if not source_owners(objective) else [])

    def append_cube_edge(match, comparison):
        if not cube_refs:
            unsupported.append({'source_span': list(match.span()), 'reason': 'primitive_subject_unbound'})
            return
        append(*match.span(), 'cube_edge', 'cube_edge', comparison)
        requirements[-1]['derivation'] = {'rule': 'cube_equal_edges.v1', 'source_refs': cube_refs}

    for match in _NOMINAL_CUBE.finditer(objective):
        if comparison := dimension_comparison(match):
            append_cube_edge(match, comparison)

    # The receiving plate is an input to the design, not a component whose
    # thickness may be measured on the generated hook. A confirmed concept can
    # check necessary clearance; geometry alone never establishes clamping.
    for match in _CLAMP_PLATE.finditer(objective):
        external_spans.append(match.span())
        covered.append(match.span())
        comparison = dimension_comparison(match)
        if comparison is None:
            continue
        if execution_scope != 'concept_geometry':
            unsupported.append({'source_span': list(match.span()), 'reason': 'external_fit_confirmation_required'})
            continue
        upper = (comparison.get('value', 0) + comparison.get('tolerance_plus', 0)
                 if comparison['op'] == 'eq' else comparison.get('upper'))
        if comparison['unit'] != 'mm' or upper is None or upper <= 0:
            unsupported.append({'source_span': list(match.span()), 'reason': 'external_dimension_bound_required'})
            continue
        ref = {**reference(*match.span(), 'external-plate'), 'property': 'plate_thickness',
               'comparison': comparison}
        if comparison.get('tolerance_minus') or comparison.get('tolerance_plus'):
            comparison['tolerance_source_refs'] = [ref['reference_id'] + '-tolerance']
        external_references.append(ref)
        append(*match.span(), 'gap', 'planar_gap', {'op': 'ge', 'lower': upper, 'unit': 'mm'})
        requirements[-1]['derivation'] = {'rule': 'external_plate_clearance.v1', 'source_refs': [ref['reference_id']]}

    # Do not let a different word order fall back to an overall part dimension.
    # Unrecognized receiving-object assertions stay unverified, even when a
    # labelled thickness inside that phrase is recognizable by the scalar parser.
    for clause in _EXTERNAL_PLATE_CLAUSE.finditer(objective):
        if _EXPLICIT_QUANTITY.search(clause.group()) and not any(
                clause.start() <= a < b <= clause.end() for a, b in external_spans):
            unsupported.append({'source_span': list(clause.span()), 'reason': 'external_reference_property_unbound'})
            external_spans.append(clause.span())
            covered.append(clause.span())

    for match in _COORDINATE_BOX.finditer(objective):
        units = [match[axis+'unit'] for axis in 'XYZ' if match[axis+'unit']]
        unit = units[0] if units and len(set(units)) == 1 else declared['unit'] if not units and declared else None
        if unit is None or re.search(r'空腔|腔体|内部|cavity|void', match['label'], re.I):
            unsupported.append({'source_span':list(match.span()), 'reason':
                'void_component_scope_unbound' if unit else 'quantity_unit_unbound'})
            covered.append(match.span())
            continue
        lo = [_quantity(match[axis+'lo'],unit)[0] for axis in 'XYZ']
        hi = [_quantity(match[axis+'hi'],unit)[0] for axis in 'XYZ']
        if any(a >= b for a,b in zip(lo,hi)) or _quantity(1,unit)[1] != 'mm':
            unsupported.append({'source_span':list(match.span()),'reason':'component_bounds_invalid'})
            covered.append(match.span())
            continue
        append(*match.span(), 'component_bounds', 'bounding_box', {'op':'eq','value':0.,'unit':'mm'},
            {'frame':'world','component_region':{'min_mm':lo,'max_mm':hi}})
        requirements[-1]['derivation'] = {'rule':'explicit_component_bounds_residual.v1',
                                           'component_label':match['label']}

    for match in _SUFFIX_DIMENSION.finditer(objective):
        if any(a <= match.start('value') < b for a, b in external_spans):
            continue
        raw=match['property'].lower()
        prop={'long':'length','wide':'width','thick':'thickness','high':'height',
              '长':'length','宽':'width','厚':'thickness','高':'height'}[raw]
        value,unit=_quantity(match['value'],match['unit'])
        comparison = relation_comparison(match, value, unit)
        if comparison is None:
            continue
        if cube_refs:
            append_cube_edge(match, comparison)
        elif prop == 'height' and _primitive_subject(objective, *match.span(), primitive_context) == 'cylinder':
            append(*match.span(),'cylinder_height','cylinder_height',comparison)
        else:
            append(*match.span(),prop,'overall_dimension',comparison,
                {'axis':{'length':[1,0,0],'width':[0,1,0],'height':[0,0,1],'thickness':[0,0,1]}[prop]})

    preservation_spans = []
    baseline = (verified_state or {}).get('verified_acceptance')
    for match in _PRESERVATION.finditer(objective):
        preservation_spans.append(match.span())
        raw = (match['leading'] or match['trailing'] or match['english']).lower()
        primitive = _primitive_subject(objective, *match.span(), primitive_context)
        prior_measurements = (baseline or {}).get('measurements', [])
        radial = {'直径':'diameter','diameter':'diameter','半径':'radius','radius':'radius'}.get(raw)
        if radial:
            if primitive is None:
                measured_primitives = {m['kind'].split('_')[0] for m in prior_measurements
                    if m['kind'] in {'cylinder_diameter','cylinder_radius','sphere_diameter','sphere_radius'}}
                primitive = next(iter(measured_primitives)) if len(measured_primitives) == 1 else None
            prop = kind = (primitive or 'unbound_material') + '_' + radial
        else:
            prop, kind = _PROPERTIES[raw]
            if prop == 'height' and primitive == 'cylinder':
                prop = kind = 'cylinder_height'
        scale = 1.
        candidates = [m for m in prior_measurements if m['kind'] == kind]
        if radial and primitive and not candidates:
            alternate = primitive + ('_diameter' if radial == 'radius' else '_radius')
            candidates = [m for m in prior_measurements if m['kind'] == alternate]
            scale = .5 if radial == 'radius' else 2.
        if kind == 'overall_dimension':
            axis = {'length':[1,0,0], 'width':[0,1,0], 'height':[0,0,1], 'thickness':[0,0,1]}[prop]
            candidates = [m for m in candidates if m['scope'].get('axis') == axis]
        # Repeated objective/basis assertions can produce identical sealed
        # measurements. Their duplication is not a second geometric target.
        if len(candidates) > 1 and all(m['kind'] == candidates[0]['kind'] and m['scope'] == candidates[0]['scope']
                and m['values'] == candidates[0]['values'] for m in candidates):
            candidates = candidates[:1]
        if baseline and baseline.get('revision_id') == (verified_state or {}).get('revision_id') and len(candidates) == 1:
            prior = candidates[0]
            values = prior['values']
            # Unequal holes or local probes cannot stand in for a whole-feature preservation requirement.
            scope = prior['scope']
            if (values and all(isinstance(v, (int,float)) and not isinstance(v,bool) and math.isfinite(v) and v >= 0 for v in values)
                    and max(values) - min(values) <= 1e-7 and not scope.get('region_min_mm')
                    and not scope.get('object_ref') and not scope.get('component_region') and scope.get('frame')!='selected_object'
                    and scope.get('point_mm') is None and not scope.get('centers_mm')):
                append(*match.span(), prop, kind, {'op':'eq','value':values[0]*scale, 'unit':'mm3' if kind=='volume' else 'mm'}, scope)
                requirements[-1]['derivation'] = {'rule':'sealed_independent_measurement.v1',
                    **{k:baseline[k] for k in ('revision_id','evidence_id','evidence_sha256','acceptance_digest','artifact_sha256s')},
                    'check_id': prior['check_id']}
                if scale != 1.:
                    requirements[-1]['derivation'].update(rule='sealed_material_radius_diameter.v1',
                        source_kind=prior['kind'], scale=scale)
                continue
        unsupported.append({'source_span':list(match.span()), 'reason':'preservation_baseline_required'})
    for match in re.finditer(r'不变|保留|保持|preserv\w*|unchanged|remain\s+unchanged|keep\s+.*same', objective, re.I):
        if not any(a <= match.start() and match.end() <= b for a,b in preservation_spans):
            unsupported.append({'source_span':list(match.span()), 'reason':'preservation_baseline_required'})

    for match in _BOX_DIMENSIONS.finditer(objective):
        # A triplet is only interpreted as L/W/T for an explicitly named plate
        # or box. A product/model code is not dimensional authority.
        prefix = re.split(r'[，,。；;\n]', objective[:match.start()])[-1]
        if (not cube_refs and not re.search(r'板|长方体|盒|支脚|安装柱|凸台|密封槽|腔体|空腔|内部|plate|box|block|cavity|lid|cover|foot|boss', prefix, re.I)
                and not re.match(r'\s*(?:(?:plate|box|block)\b|的?(?:板|盒|长方体))', objective[match.end():], re.I)):
            continue
        if re.search(_RELATION, prefix, re.I):
            unsupported.append({'source_span': list(match.span()), 'reason': 'triplet_relation_unbound'})
            covered.append(match.span())
            continue
        for component, prop, axis in [('x', 'length', [1, 0, 0]), ('y', 'width', [0, 1, 0]), ('z', 'thickness', [0, 0, 1])]:
            value, unit = _quantity(match[component], match['unit'])
            if cube_refs:
                append(*match.span(), prop, 'cube_edge', {'op': 'eq', 'value': value, 'unit': unit})
                requirements[-1]['derivation'] = {'rule': 'cube_equal_edges.v1', 'source_refs': cube_refs}
            else:
                append(*match.span(), prop, 'overall_dimension', {'op': 'eq', 'value': value, 'unit': unit}, {'axis': axis})

    for match in _SCALAR.finditer(objective):
        if any(a <= match.start('value') < b for a,b in covered):
            continue
        prop, kind = _PROPERTIES[match['property'].lower()]
        comparison = dimension_comparison(match)
        if comparison is None:
            continue
        if kind == 'cube_edge' or cube_refs and kind == 'overall_dimension':
            append_cube_edge(match, comparison)
            continue
        scope = {}
        if prop == 'side_wall_thickness':
            scope = {'wall_mode': 'side_walls'}
        elif prop == 'cavity_floor_thickness':
            scope = {'wall_mode': 'cavity_floor'}
        elif prop == 'height' and _primitive_subject(objective, *match.span(), primitive_context) == 'cylinder':
            prop = kind = 'cylinder_height'
        if kind == 'overall_dimension':
            scope = {'axis': {'length': [1, 0, 0], 'width': [0, 1, 0],
                              'height': [0, 0, 1], 'thickness': [0, 0, 1]}[prop]}
        append(*match.span(), prop, kind, comparison, scope)
    for match in [*_LABELLED_COUNT.finditer(objective), *_COUNT.finditer(objective)]:
        if any(a <= match.start('count') < b for a, b in covered):
            continue  # A preceding labelled dimension is not the next line's hole count.
        raw = match['count'].lower()
        count = _COUNT_WORDS[raw] if raw in _COUNT_WORDS else int(number(raw))
        kind = 'hole_count' if match['kind'].lower().startswith(('孔', 'hole')) else 'solid_count'
        comparison = relation_comparison(match, float(count), 'count')
        if comparison is not None:
            append(*match.span(), kind, kind, comparison)
    for match in list(_SIZED_HOLE_COUNT.finditer(objective)) + list(_CHINESE_SIZED_HOLE_COUNT.finditer(objective)):
        if any(r['kind'] == 'hole_count' and r['source_span'][0] <= match.start('count') < r['source_span'][1]
               for r in requirements):
            continue
        raw=match['count'].lower()
        comparison = relation_comparison(match, float(_COUNT_WORDS[raw] if raw in _COUNT_WORDS else number(raw)), 'count')
        if comparison is not None:
            append(*match.span(), 'hole_count', 'hole_count', comparison)
    for match in list(_DIAMETER.finditer(objective)) + list(_HOLE_SIZE.finditer(objective)):
        # The same quantity can match several word-order patterns. Once bound
        # or rejected, it must not be reinterpreted by a shorter fallback.
        if any(r['source_span'][0] <= match.start('value') < r['source_span'][1]
               for r in requirements + unsupported
               if r.get('property') == 'hole_diameter' or 'reason' in r):
            covered.append(match.span())
            continue
        # Source IDs retain legacy whitespace, but ownership must not cross a
        # line simply because a quantity regex consumed its leading whitespace.
        start = match.start() + len(match.group()) - len(match.group().lstrip())
        end = match.start() + len(match.group().rstrip())
        lo = max(objective.rfind(c, 0, start) for c in ',，;；。\n') + 1
        hi = min([p for c in ',，;；。\n' if (p := objective.find(c, end)) >= 0] or [len(objective)])
        clause = objective[lo:hi]
        primitive = _primitive_subject(objective, *match.span(), primitive_context)
        inherited_subject = None
        if not primitive and not re.search(r'孔|hole|bore', clause, re.I):
            inherited_subject = _preceding_hole_subject(objective, *match.span())
            if inherited_subject is None:
                continue
        comparison = dimension_comparison(match)
        if comparison is not None:
            kind = primitive + '_diameter' if primitive else 'hole_diameter'
            append(*match.span(), kind, kind, comparison)
            if inherited_subject is not None:
                requirements[-1]['derivation'] = {'rule': 'preceding_hole_subject.v1',
                                                 'subject_source_span': inherited_subject}
    for match in _RADIUS.finditer(objective):
        primitive = _primitive_subject(objective, *match.span(), primitive_context)
        if primitive and (comparison := dimension_comparison(match)) is not None:
            append(*match.span(), primitive + '_radius', primitive + '_radius', comparison)
    for match in _CENTERED.finditer(objective):
        context = objective[max(0, match.start()-20):match.end()+30]
        # An explicit local landmark cannot inherit a plate's global centre.
        local_owner = re.search(r'(?:安装耳|支脚|凸台|安装柱|密封槽|底座|\b(?:ear|lug|foot|boss|post|groove)\b)[^，,。；;\n]{0,12}$',
                                objective[:match.start()], re.I)
        if not local_owner and re.search(r'孔|hole|bore', context, re.I) and re.search(r'板|plate', objective, re.I):
            append(*match.span(), 'hole_position', 'hole_position', {'op': 'eq', 'value': 0., 'unit': 'mm'},
                {'axis': [0, 0, 1], 'frame': 'bounds_center', 'centers_mm': [[0, 0, 0]]})
    through_spans = []
    for clause in re.finditer(r'[^，,。；;\n]+', objective):
        phrase = clause.group().strip()
        predicate = _HOLE_PASSAGE_CLAUSE.fullmatch(phrase)
        if predicate is None:
            continue
        start = clause.start() + len(clause.group()) - len(clause.group().lstrip())
        span = (start, start + len(phrase))
        # A target plate must be part of the source, not introduced solely by
        # this assertion. Component identity is checked by the shared scope
        # binder below; it cannot be borrowed from a generated model.
        if predicate['plate']:
            thicknesses = [r for r in requirements if r['property'] == 'thickness'
                           and r['comparison']['op'] == 'eq']
            components = [r for r in requirements if r['kind'] == 'bounding_box']
            local_target = (re.search(r'板|\bplate\b', objective[:clause.start()], re.I)
                            and len(thicknesses) == 1 and len(components) <= 1)
            if not local_target and span not in (plate_context or {}):
                continue
        through_spans.append(span)
    through_spans.extend(m.span() for m in _THROUGH_HOLE.finditer(objective)
        if not any(a <= m.start() and m.end() <= b for a, b in through_spans))
    for span in through_spans:
        append(*span, 'hole_through', 'hole_through', {'op': 'eq', 'value': 1., 'unit': 'count'})
    request = _DESIGN_REQUEST.match(objective)
    if request:
        span = request.span('zh' if request['zh'] else 'en')
        subject = re.split(r'[，,。；;\n]', objective[request.end():])[0]
        # Only the requested product can authorize a delivery count. A plate
        # in "板上的孔" or "rib on a plate" is a modifier/location, not the
        # counted subject. Unknown feature counts stay in the inventory.
        if request['zh']:
            subject = subject.rsplit('的', 1)[-1]
            subject = re.split(r'放在|放到|放置|安装|固定在|位于|用于|在', subject, maxsplit=1)[0]
        else:
            subject = re.split(r'\b(?:with|on|in|onto|for|that|which|to)\b', subject, maxsplit=1, flags=re.I)[0]
        products = list(_DESIGN_PRODUCT.finditer(subject))
        # A substring such as 板 in 板状加强筋 cannot establish the head noun.
        # Permit a product followed by the existing dimensional grammar too.
        product_head = any(not (tail := subject[m.end():].strip(' \t.:：')) or any(
            pattern.match(tail) for pattern in (_SCALAR, _BOX_DIMENSIONS, _SUFFIX_DIMENSION, _DIAMETER, _RADIUS))
            for m in products)
        if product_head and not any(
                r['kind'] in {'hole_count', 'solid_count'} and r['source_span'][0] <= span[0] < r['source_span'][1]
                for r in requirements):
            design_requests.append({**reference(*span, 'design'), 'count': 1, 'delivery_unit': 'cad_document'})
            covered.append(span)
    quantity_patterns = [_EXPLICIT_QUANTITY, _OTHER_ENGINEERING_QUANTITY]
    if declarations:
        # A global unit authorizes bare values, not the omission of values
        # whose property or scope the parser cannot recognize. Identifiers
        # such as M4, PG7 and FDM 3D are not dimensional assertions.
        quantity_patterns.append(re.compile(rf'(?<![a-zA-Z0-9_.]){_NUMBER}(?![a-zA-Z0-9_.])'))
    for pattern in quantity_patterns:
        for quantity in pattern.finditer(objective):
            if not any(a <= quantity.start() and quantity.end() <= b for a, b in covered):
                unsupported.append({'source_span': list(quantity.span()), 'reason': 'quantity_property_unbound'})
    # Unit discovery must survive a number grammar miss (e.g. 半厘米). Otherwise
    # successful extraction and its coverage audit can omit the same quantity.
    # The single-character 米 also occurs inside product names. Require a
    # phrase boundary for an unparsed value rather than splitting such words.
    quantity_spans = covered + [tuple(r['source_span']) for r in unsupported] + [m.span() for m in declarations]
    for unit in re.finditer(rf'(?<![a-zA-Z]){_UNIT}(?![a-zA-Z0-9])', objective, re.I):
        if unit.group() == '米' and not re.match(r'$|[\s，,。；;:：]|的|[长宽高厚]', objective[unit.end():]):
            continue
        if not any(a <= unit.start() and unit.end() <= b for a, b in quantity_spans):
            unsupported.append({'source_span': list(unit.span()), 'reason': 'quantity_property_unbound'})
    axes=[(match,frame) for pattern,frame in ((_WORLD_AXIS,'world'),(_LOCAL_AXIS,'selected_object'))
          for match in pattern.finditer(objective)]
    for axis,frame in axes:
        # Bind only a single measurement in the same clause; never delete an
        # explicit world requirement when a rotated model would otherwise pass.
        lo=max([objective.rfind(c,0,axis.start()) for c in ',，;；。\n'])+1
        ends=[objective.find(c,axis.end()) for c in ',，;；。\n']
        hi=min([x for x in ends if x>=0] or [len(objective)])
        selected=[r for r in requirements if lo<=r['source_span'][0] and r['source_span'][1]<=hi
                  and r['kind'] in {'planar_gap','overall_dimension','hole_diameter','hole_depth','hole_count','wall_thickness'}]
        if len(selected)==1 and sum(lo<=other.start() and other.end()<=hi for other,_ in axes)==1:
            selected[0]['scope']={**selected[0]['scope'],'frame':frame,'axis':{'x':[1,0,0],'y':[0,1,0],'z':[0,0,1]}[axis['axis'].lower()]}
            coordinate_start = axis.start()
            if direction := re.search(r'(?:沿|\balong)[ \t]*$', objective[lo:axis.start()], re.I):
                coordinate_start = lo + direction.start()
            selected[0]['coordinate_source_span']=[coordinate_start, axis.end()]
            if frame=='selected_object':
                selection=(verified_state or {}).get('selection_context') or {}
                features=selection.get('features') or []
                if (len(features)!=1 or features[0].get('type') not in {'PartDesign::Body','App::Link'}
                        or selection.get('topology_selector') or not selection.get('parameter_state_sha256')
                        or selection.get('revision_id')!=(verified_state or {}).get('revision_id')):
                    unsupported.append({'source_span':list(axis.span()),'reason':'selected_object_authority_required'})
                else:
                    feature=features[0]
                    selected[0]['scope']['object_ref']={'revision_id':selection['revision_id'],
                        'feature_id':feature['feature_id'],'kernel_name':feature['kernel_name'],'type_id':feature['type'],
                        'parameter_state_sha256':selection['parameter_state_sha256']}
        else:
            unsupported.append({'source_span':list(axis.span()),'reason':'coordinate_authority_ambiguous'})
    for marker in re.finditer(r'世界|world|局部|local\s+(?:frame|coordinate)|坐标|区域|region|(?:[xyz]\s*轴)|\baxis\b',objective,re.I):
        if any(r['kind']=='bounding_box' and r['source_span'][0] <= marker.start()
               and marker.end() <= r['source_span'][1] for r in requirements):
            continue
        if not any(a.start()<=marker.start() and marker.end()<=a.end() for a,_ in axes):
            unsupported.append({'source_span':list(marker.span()),'reason':'coordinate_authority_unbound'})
    for marker in re.finditer(r'所选(?:对象|实体|零件|部件)?|选中(?:的)?(?:对象|实体|零件|部件)|\bselected\s+(?:object|body|component|part)\b',objective,re.I):
        lo=max([objective.rfind(c,0,marker.start()) for c in ',，;；。\n'])+1
        ends=[objective.find(c,marker.end()) for c in ',，;；。\n']
        hi=min([x for x in ends if x>=0] or [len(objective)])
        scoped=[r for r in requirements if lo<=r['source_span'][0] and r['source_span'][1]<=hi]
        if not scoped or any(not r['scope'].get('object_ref') for r in scoped):
            unsupported.append({'source_span':list(marker.span()),'reason':'selected_object_scope_unbound'})
        else:
            for record in scoped:
                record['selection_source_span'] = list(marker.span())
    for owner in source_owners(objective):
        primitive = owner['kind']
        if primitive in {'cylinder', 'sphere'} and not any(r['kind'].startswith(primitive+'_') for r in requirements):
            append(*owner['source_span'], primitive+'_shape', primitive+'_shape',
                   {'op': 'eq', 'value': 1., 'unit': 'count'})
    bind_component_scopes(objective, requirements, unsupported, obligations, source_input_id=source_input_id,
                          external_spans=external_spans)
    for through in [r for r in requirements if r['kind'] == 'hole_through']:
        thickness = [r for r in requirements if r['property'] == 'thickness' and r['comparison']['op'] == 'eq'
                     and r['scope'].get('component_region') == through['scope'].get('component_region')]
        inherited = (plate_context or {}).get(tuple(through['source_span']))
        if not thickness and inherited:
            thickness = [inherited]
        if len(thickness) == 1 and (inherited or re.search(r'板|plate', objective, re.I)):
            append(*through['source_span'], 'hole_depth', 'hole_depth', dict(thickness[0]['comparison']), dict(through['scope']),
                   identity=through['requirement_id'] + '-depth' if through.get('distribution_ref') else None)
            requirements[-1]['derivation'] = {'rule': 'through_plate_thickness.v1',
                                             'source_refs': [thickness[0]['requirement_id']]}
    inventory_hole_populations(objective, requirements, obligations, source_input_id=source_input_id)
    _cube_conflicts(requirements, unsupported)
    resolve_inventory(obligations, requirements, comparison_spans, unsupported, design_requests=design_requests)
    return {'schema_version': 'requirement-extraction.v1', 'requirements': requirements,
            'unsupported': unsupported, 'obligations': obligations,
            **({'source_context': {'schema_version': 'source-context.v1', 'execution_scope': execution_scope,
                'design_requests': design_requests, 'external_references': external_references,
                'unverified_aspects': ['physical_fit', 'clamping_force'] if external_references else []}}
               if design_requests or external_references else {})}


def _cross_source_plate_context(index, sources, extractions, verified_state):
    """Prove a passage against saved fields, then retain their separate authority.

    The joined parse is only a scope/derivation proof. It cannot supply new
    dimensions, units or tolerances: its thickness must already exist in one
    individually parsed source with the same scope and comparison.
    """
    source = sources[index]
    others = [i for i in range(len(sources)) if i != index]
    if (not any(r['property'] == 'thickness' for i in others for r in extractions[i]['requirements'])
            or any(r['property'] == 'thickness' for r in extractions[index]['requirements'])
            or not (_HOLE_PASSAGE_CLAUSE.search(source['text']) or _THROUGH_HOLE.search(source['text']))):
        return {}
    offsets, text = {}, ''
    for i in [*others, index]:
        offsets[i] = len(text)
        text += sources[i]['text'] + '\n'
    proof = extract_requirements(text, verified_state=verified_state,
                                 execution_scope=source.get('execution_scope', 'cad_geometry'))
    if proof['unsupported']:
        return {}
    records = {r['requirement_id']: r for r in proof['requirements']}
    context = {}
    for depth in proof['requirements']:
        derivation = depth.get('derivation', {})
        start, end = depth['source_span']
        if (derivation.get('rule') != 'through_plate_thickness.v1'
                or not offsets[index] <= start < end <= offsets[index] + len(source['text'])
                or depth['scope'].get('component_region') or depth['scope'].get('object_ref')):
            continue
        thickness = records[derivation['source_refs'][0]]
        # Component selectors require a complete cross-field scope resolver;
        # this rule only proves the same single, unselected plate.
        if thickness['scope'].get('component_region') or thickness['scope'].get('object_ref'):
            continue
        for i in others:
            for original in extractions[i]['requirements']:
                if (original['property'] == 'thickness'
                        and [offsets[i] + v for v in original['source_span']] == thickness['source_span']
                        and original['scope'] == thickness['scope']
                        and {k: v for k, v in original['comparison'].items() if k != 'tolerance_source_refs'}
                        == {k: v for k, v in thickness['comparison'].items() if k != 'tolerance_source_refs'}):
                    context[(start - offsets[index], end - offsets[index])] = original
    return context


def extract_source_requirements(objective, *, verified_state=None, requirement_sources=None) -> dict:
    """Structured sources are supplied by the server, never the tool response."""
    sources = requirement_sources if requirement_sources is not None else [
        {'text':objective, 'source_input_id':'objective', 'source_kind':'user'}]
    from app.validation.rectangular_assembly_requirements import extract_rectangular_assembly_sources
    assembly = extract_rectangular_assembly_sources(objective, sources)
    if assembly is not None:
        from app.validation.verification_intent import verification_intent
        intent = verification_intent(sources)
        assembly['unsupported'].extend(intent['unsupported'])
        if intent['notices']:
            assembly['verification_notices'] = intent['notices']
        return assembly
    result = {'schema_version':'requirement-extraction.v1', 'requirements':[], 'unsupported':[], 'obligations':[]}
    submitted_rows = []
    extractions = []
    for source in sources:
        if source['source_kind'] not in {'user','confirmed'} or source['text'] not in objective:
            raise ValueError('requirement source is not part of the frozen submitted input')
        extracted = extract_requirements(source['text'], source_input_id=source['source_input_id'],
                                        source_kind=source['source_kind'], verified_state=verified_state,
                                        execution_scope=source.get('execution_scope', 'cad_geometry'))
        extractions.append(extracted)
    subjects = [{**mention, 'source_input_id': s['source_input_id']}
                for s in sources for mention in source_owners(s['text'])]
    owners = {s['kind'] for s in subjects}
    primitive_context = None
    if len(owners) == 1 and owners <= {'cube', 'cylinder', 'sphere'}:
        primitive_context = {'kind': next(iter(owners)), 'subjects': subjects,
            'cube_shape_refs': [r['requirement_id'] for parsed in extractions for r in parsed['requirements']
                                if r['kind'] == 'cube_shape' and not r['scope']]}
    for index, (source, extracted) in enumerate(zip(sources, extractions)):
        plate_context = (_cross_source_plate_context(index, sources, extractions, verified_state)
                         if len(sources) > 1 else {})
        if plate_context or primitive_context and not source_owners(source['text']):
            extracted = extract_requirements(source['text'], source_input_id=source['source_input_id'],
                source_kind=source['source_kind'], verified_state=verified_state,
                execution_scope=source.get('execution_scope', 'cad_geometry'), plate_context=plate_context,
                primitive_context=primitive_context)
        if context := extracted.get('source_context'):
            merged = result.setdefault('source_context', {'schema_version': 'source-context.v1',
                'execution_scope': 'cad_geometry', 'design_requests': [], 'external_references': [], 'unverified_aspects': []})
            if context['execution_scope'] == 'concept_geometry':
                merged['execution_scope'] = 'concept_geometry'
            for key in ('design_requests', 'external_references', 'unverified_aspects'):
                merged[key].extend(item for item in context[key] if item not in merged[key])
        result['requirements'].extend(extracted['requirements'])
        result['unsupported'].extend({**item, 'source_input_id': source['source_input_id']}
                                     for item in extracted['unsupported'])
        result['obligations'].extend(extracted['obligations'])
        offset = objective.index(source['text'])
        submitted_rows.extend({**r, 'source_span': [offset + value for value in r['source_span']]}
                              for r in extracted['requirements'])
    # Individually valid target and confirmed dimensions can describe
    # incompatible populations. Audit the complete submitted input while
    # preserving each requirement's original authority and local source span.
    cross_source = []
    inventory_hole_populations(objective, submitted_rows, cross_source,
        source_input_id='submitted-objective', cross_source_only=True)
    grouped = {r['requirement_id']: r['hole_group'] for r in submitted_rows if r.get('hole_group')}
    for record in result['requirements']:
        if record['requirement_id'] in grouped:
            record['hole_group'] = grouped[record['requirement_id']]
    resolve_inventory(cross_source, result['requirements'], [], result['unsupported'])
    result['obligations'].extend(cross_source)
    _cube_conflicts(result['requirements'], result['unsupported'])
    if len(sources) > 1 and len(owners) > 1 and owners & {'cube', 'cylinder', 'sphere'}:
        result['unsupported'].extend({'source_input_id': r['source_input_id'], 'source_span': r['source_span'],
            'reason': 'component_scope_unbound'} for r in result['requirements']
            if r['kind'] not in {'solid_count', 'bounding_box'}
            and not r['scope'].get('component_region') and not r['scope'].get('object_ref'))
    from app.validation.verification_intent import verification_intent
    intent = verification_intent(sources)
    result['unsupported'].extend(intent['unsupported'])
    if intent['notices']:
        result['verification_notices'] = intent['notices']
    return result


def preview_requirements(objective):
    """Intake assistance from the same source semantics used before freezing.

    This is neither a frozen contract nor evidence of physical fit. Submission
    re-extracts the saved fields and validates every proposed acceptance check.
    """
    parsed = extract_source_requirements(objective)
    rows = parsed['requirements']
    dimensions = objective.strip() if len(objective) <= 1500 and any(
        r['comparison']['unit'] in {'mm', 'mm3'} for r in rows) else ''
    kinds = {r['kind'] for r in rows}
    axes = {tuple(r['scope']['axis']) for r in rows if r['kind'] == 'overall_dimension'}
    assembly = {r['scope']['assembly_measurement'] for r in rows if r['kind'] == 'assembly_dimension'}
    complete = bool(dimensions and not parsed['unsupported'] and (
        {'cube_shape', 'cube_edge'} <= kinds or kinds & {'sphere_radius', 'sphere_diameter'}
        or 'cylinder_height' in kinds and kinds & {'cylinder_radius', 'cylinder_diameter'}
        or {(1, 0, 0), (0, 1, 0), (0, 0, 1)} <= axes
        or {'envelope_length', 'envelope_width', 'envelope_height'} <= assembly))
    return {'dimensions': dimensions, 'complete': complete}


def bind_source_quotes(objective, acceptance, *, verified_state=None, requirement_sources=None):
    """Restore exact text for known references without changing their meaning."""
    extracted = extract_source_requirements(objective, verified_state=verified_state,
                                            requirement_sources=requirement_sources)
    records = {r['requirement_id']: r for r in extracted['requirements']}
    for check in acceptance.get('checks', []):
        record = records.get(check.get('binding_ref')) if isinstance(check, dict) else None
        if record and check.get('source_kind', 'user') == record['source_kind']:
            check['source_quote'] = record['source_quote']
    return extracted


def bind_acceptance_proposal(objective, acceptance, *, verified_state=None, requirement_sources=None):
    """Attach server authority to a NEW, unfrozen model proposal.

    Source context and quotes are derived from saved inputs, not provider output.
    Property, comparison, units, scope and coverage still undergo independent
    validation. Never call this on a persisted acceptance contract or evidence.
    Old provider receipts may contain source_context; it has no authority here.
    """
    extracted = bind_source_quotes(objective, acceptance, verified_state=verified_state,
                                   requirement_sources=requirement_sources)
    if extracted.get('source_context'):
        acceptance['source_context'] = extracted['source_context']
    else:
        acceptance.pop('source_context', None)
    from app.validation.verification_intent import bind_verification_intent
    bind_verification_intent(acceptance, requirement_sources if requirement_sources is not None else [
        {'source_input_id':'objective','source_kind':'user','text':objective}])


def require_bound_sources(objective, *, verified_state=None, requirement_sources=None):
    extracted = extract_source_requirements(objective, verified_state=verified_state, requirement_sources=requirement_sources)
    if extracted['unsupported']:
        sources = {s['source_input_id']: s['text'] for s in requirement_sources or [
            {'source_input_id': 'objective', 'text': objective}]}
        diagnostics = []
        for item in extracted['unsupported']:
            source_id = next((o['source_input_id'] for o in extracted['obligations']
                              if o['obligation_id'] == item.get('obligation_id')), item.get('source_input_id', 'objective'))
            text = sources.get(source_id, objective)
            start, end = item['source_span']
            before = max(text.rfind(c, 0, start) for c in ',，;；。\n') + 1
            after = min([pos for c in ',，;；。\n' if (pos := text.find(c, end)) >= 0] or [len(text)])
            diagnostics.append({**item, 'source_quote': text[start:end], 'source_clause': text[before:after]})
        clauses = list(dict.fromkeys(d['source_clause'] for d in diagnostics))
        reasons = list(dict.fromkeys(d['reason'] for d in diagnostics))
        message = '当前无法完整核验这些条件，尚未执行建模：' + '；'.join(c[:120] for c in clauses[:5])
        message += '。原因：' + '、'.join(reasons[:8])
        if len(clauses) > 5:
            message += f'；另有 {len(clauses)-5} 处条件，完整诊断已保存。'
        # Model jobs persist ApplicationError.details separately from their
        # 4,000-character public message, including after worker recovery.
        raise SourceRequirementUnavailable('engineering_verification_unavailable', message,
            details={'schema_version':'source-diagnostics.v1', 'diagnostics':diagnostics})
    return extracted


def validate_requirement_bindings(objective, contract, verified_state=None, *, confirmed_sources=(), requirement_sources=None) -> dict:
    """Bind every recognized explicit condition before CAD dispatch, or reject.

    Preservation requires independent evidence sealed into the exact revision;
    parameter labels and nominal values never substitute for measured geometry.
    """
    extracted = require_bound_sources(objective, verified_state=verified_state, requirement_sources=requirement_sources)
    expected_context = extracted.get('source_context')
    if contract.source_context != (SourceContextV1.model_validate(expected_context) if expected_context else None):
        raise RequirementBindingError('requirement_binding_mismatch', 'source context differs from saved user input')
    if [n.model_dump(mode='json') for n in contract.verification_notices] != extracted.get('verification_notices', []):
        raise RequirementBindingError('requirement_binding_mismatch', 'verification notices differ from saved user input')
    records = {r['requirement_id']: r for r in extracted['requirements']}
    for source in confirmed_sources:
        if source['source_kind'] != 'confirmed':
            raise ValueError('confirmed source must come from a saved server confirmation')
        rows = extract_requirements(source['text'], source_input_id=source['source_input_id'], source_kind='confirmed')
        if rows['unsupported']:
            raise RequirementBindingError('engineering_verification_unavailable', 'confirmed source contains unbound conditions')
        records.update((r['requirement_id'], r) for r in rows['requirements'])
    bindings, covered = [], set()
    for check in contract.checks:
        if check.source_kind == 'assumption':
            if check.binding_ref in records:
                raise RequirementBindingError('requirement_binding_mismatch', 'explicit requirement cannot become an assumption')
            if check.comparison.op == 'eq' and (check.comparison.tolerance_minus or check.comparison.tolerance_plus):
                raise RequirementBindingError('requirement_tolerance_unbound', 'assumption cannot authorize engineering tolerance')
            continue
        record = records.get(check.binding_ref)
        if record is None:
            raise RequirementBindingError('engineering_verification_unavailable', f'{check.check_id}: no recognized property source')
        if record['source_kind'] != check.source_kind:
            raise RequirementBindingError('requirement_binding_mismatch', f'{check.check_id}: confirmed source is not server verified')
        if record['source_quote'] not in check.source_quote or check.kind != record['kind']:
            alternatives = [r['requirement_id'] for r in records.values() if r['kind'] == check.kind]
            raise RequirementBindingError('requirement_binding_mismatch',
                f"{check.check_id}: property/source differs: binding_ref {check.binding_ref} "
                f"expected {record['kind']} quoting {record['source_quote']!r}, got {check.kind}. "
                f"Source references for {check.kind}: {alternatives}. Retain every required source "
                'and its exact comparison and scope; correct the proposal, do not remove a check.')
        actual, expected = check.comparison.model_dump(mode='json'), record['comparison']
        if actual['op'] != expected['op']:
            raise RequirementBindingError('requirement_comparison_mismatch', f'{check.check_id}: comparison differs from source')
        if any(actual.get(key) != expected.get(key) for key in ('value', 'lower', 'upper', 'unit')):
            raise RequirementBindingError('requirement_binding_mismatch', f'{check.check_id}: quantity/unit differs from source')
        if any(actual.get(key, 0) != expected.get(key, 0) for key in ('tolerance_minus', 'tolerance_plus')) or (
                actual.get('tolerance_source_refs', []) != expected.get('tolerance_source_refs', [])):
            raise RequirementBindingError('requirement_tolerance_unbound', f'{check.check_id}: engineering tolerance lacks source')
        from app.contracts.acceptance_v2 import MeasurementScopeV2
        if check.scope != MeasurementScopeV2.model_validate(record['scope']):
            raise RequirementBindingError('requirement_binding_mismatch', f'{check.check_id}: measurement scope differs from its verified property')
        if (check.hole_group.model_dump(mode='json') if check.hole_group else None) != record.get('hole_group'):
            raise RequirementBindingError('requirement_binding_mismatch', f'{check.check_id}: hole group differs from its counted source phrase')
        if check.kind == 'wall_thickness' and check.scope.wall_mode not in {'continuous_normal', 'side_walls', 'cavity_floor'}:
            raise RequirementBindingError('requirement_binding_mismatch', 'whole wall requirement cannot be replaced by local coverage')
        covered.add(record['requirement_id'])
        bindings.append({**record, 'check_ids': [check.check_id], 'status': 'bound'})
    if covered != set(records):
        raise RequirementBindingError('requirement_binding_mismatch', 'coverage missing: ' + ','.join(sorted(set(records) - covered)))
    return {'schema_version': 'requirement-bindings.v1', 'status': 'bound',
            'requirements_digest': canonical_sha256({'objective': objective}),
            'acceptance_digest': contract.digest(), 'bindings': bindings,
            'source_obligations': extracted['obligations'],
            **({'source_context': expected_context} if expected_context else {}),
            'comparison_policy_id': contract.comparison_policy_id}
