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
from app.contracts.acceptance_v2 import RequirementBindingError


_NUMBER = r'(?:\d+(?:\.\d+)?|\.\d+)'
_UNIT = r'(?:mm(?:³|3|\^3)?|cm(?:³|3|\^3)?|m(?:³|3|\^3)?|立方毫米|立方厘米|立方米|毫米|厘米|米)'
_RELATION = r'(?:至少|不小于|不少于|不低于|不超过|至多|不大于|不高于|大于|小于|超过|最小|最大|minimum|maximum|at\s+least|at\s+most|no\s+less\s+than|no\s+more\s+than|greater\s+than|less\s+than|>=|<=|≥|≤|>|<)'
_PROPERTIES = {
    '夹口': ('gap', 'planar_gap'), '开口': ('gap', 'planar_gap'), '间隙': ('gap', 'planar_gap'),
    'gap': ('gap', 'planar_gap'), 'opening': ('gap', 'planar_gap'),
    '壁厚': ('wall_thickness', 'wall_thickness'), 'wall thickness': ('wall_thickness', 'wall_thickness'),
    '孔径': ('hole_diameter', 'hole_diameter'), '孔直径': ('hole_diameter', 'hole_diameter'),
    'hole diameter': ('hole_diameter', 'hole_diameter'), 'bore diameter': ('hole_diameter', 'hole_diameter'),
    '孔深': ('hole_depth', 'hole_depth'), 'hole depth': ('hole_depth', 'hole_depth'),
    '体积': ('volume', 'volume'), 'volume': ('volume', 'volume'),
    '长度': ('length', 'overall_dimension'), '长': ('length', 'overall_dimension'), 'length': ('length', 'overall_dimension'),
    '宽度': ('width', 'overall_dimension'), '宽': ('width', 'overall_dimension'), 'width': ('width', 'overall_dimension'),
    '高度': ('height', 'overall_dimension'), '高': ('height', 'overall_dimension'), 'height': ('height', 'overall_dimension'),
    '厚度': ('thickness', 'overall_dimension'), '厚': ('thickness', 'overall_dimension'), 'thickness': ('thickness', 'overall_dimension'),
}
_PROPERTY = '|'.join(re.escape(p) for p in sorted(_PROPERTIES, key=len, reverse=True))
_SCALAR = re.compile(
    rf'(?P<prefix>{_RELATION})?\s*(?P<property>{_PROPERTY})\s*(?:改为|修改为|设为|调整为|为|是|[:：=]|of|is|to)?\s*'
    rf'(?P<relation>{_RELATION})?\s*(?P<value>{_NUMBER})\s*(?P<unit1>{_UNIT})?\s*'
    rf'(?:(?P<range>至|到|[~～–—-]|to)\s*(?P<upper>{_NUMBER})\s*(?P<unit2>{_UNIT})?)?\s*'
    rf'(?:(?:±|\+/-)\s*(?P<symmetric>{_NUMBER})|\+\s*(?P<plus>{_NUMBER})\s*/\s*[-−]\s*(?P<minus>{_NUMBER}))?\s*'
    rf'(?P<unit3>{_UNIT})?', re.I)
_COUNT_WORDS = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6,
                '七': 7, '八': 8, '九': 9, '十': 10, 'one': 1, 'two': 2, 'three': 3,
                'four': 4, 'five': 5, 'six': 6, 'single': 1, 'a': 1, 'an': 1}
_COUNT = re.compile(rf'(?P<relation>{_RELATION})?\s*(?P<count>\d+|[一二两三四五六七八九十]|\b(?:one|two|three|four|five|six|single|an?|a)\b)'
    r'\s*(?:个|处|independent\s+|separate\s+|connected\s+|centered\s+|through\s+|独立|连通|居中|通)*\s*'
    r'(?P<kind>实体|孔|solids?\b|holes?\b)', re.I)
_EXPLICIT_QUANTITY = re.compile(rf'(?<![a-zA-Z0-9_.]){_NUMBER}\s*{_UNIT}(?![a-zA-Z])', re.I)
_OTHER_ENGINEERING_QUANTITY = re.compile(
    rf'(?<![a-zA-Z0-9_.]){_NUMBER}\s*(?:°|度|弧度|deg(?:rees?)?\b|rad(?:ians?)?\b|英寸|inches?\b)|'
    rf'(?:{_NUMBER}|[一二两三四五六七八九十]+)\s*(?:个|处|颗|条|只)\s*'
    r'(?:支脚|支柱|支撑|槽|孔|实体|齿|筋|凸台|通道|倒角|圆角)|'
    rf'\b(?:{_NUMBER}|one|two|three|four|five|six)\s+(?:[a-z-]+\s+){{0,2}}'
    r'(?:legs?|supports?|ribs?|fins?|pins?|posts?|slots?|teeth|holes?|solids?)\b', re.I)
_BOX_DIMENSIONS = re.compile(rf'(?P<x>{_NUMBER})\s*[x×*]\s*(?P<y>{_NUMBER})\s*[x×*]\s*(?P<z>{_NUMBER})\s*(?P<unit>{_UNIT})', re.I)
_DIAMETER = re.compile(rf'(?:直径|diameter)\s*(?:为|of|=)?\s*(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})', re.I)
_HOLE_SIZE = re.compile(rf'(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})\s+(?:diameter\s+)?(?:through[- ]?)?holes?\b', re.I)
_SIZED_HOLE_COUNT = re.compile(rf'(?P<relation>{_RELATION})?\s*\b(?P<count>one|two|three|four|five|six|single|an?|\d+)\s+'
    rf'(?:centered\s+|central\s+)?{_NUMBER}\s*{_UNIT}\s+(?:diameter\s+)?(?:through[- ]?)?holes?\b', re.I)
_SUFFIX_DIMENSION = re.compile(rf'(?P<relation>{_RELATION})?\s*(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})\s*'
    r'(?P<property>long\b|wide\b|thick\b|high\b|[长宽厚高](?!度|\s*\d))', re.I)
_WORLD_AXIS = re.compile(r'(?:世界|world)\s*(?P<axis>[xyz])\s*(?:轴|axis)?', re.I)
_LOCAL_AXIS = re.compile(r'(?:局部|local)\s*(?P<axis>[xyz])\s*(?:轴|axis)?', re.I)
_CENTERED = re.compile(r'(?:居中|中心|centered|central)', re.I)
_LOWER = {'至少', '不小于', '不少于', '不低于', '最小', 'minimum', 'at least', 'no less than', '>=', '≥'}
_STRICT = {'大于', '小于', '超过', 'greater than', 'less than', '>', '<'}
_PRESERVATION = re.compile(
    rf'(?:(?:保持|保留)\s*(?P<leading>{_PROPERTY})(?:\s*(?:不变|原值))?|'
    rf'(?P<trailing>{_PROPERTY})\s*(?:保持)?(?:不变|原值)|'
    rf'(?:preserve|keep)\s+(?:the\s+)?(?P<english>{_PROPERTY})(?:\s+(?:unchanged|the same))?)', re.I)


def _quantity(value, unit):
    normalized = unit.lower().replace('^', '').replace('³', '3')
    cubic = '3' in normalized or normalized.startswith('立方')
    base = normalized.replace('3', '').replace('立方', '')
    factor = {'mm': 1, '毫米': 1, 'cm': 10, '厘米': 10, 'm': 1000, '米': 1000}[base]
    return float(Decimal(str(value)) * Decimal(factor) ** (3 if cubic else 1)), 'mm3' if cubic else 'mm'


def extract_requirements(objective: str, *, source_input_id: str = 'objective', source_kind: str = 'user',
                         verified_state=None) -> dict:
    requirements, unsupported, covered = [], [], []
    # These semantic obligations require a separate proven selector/baseline;
    # recognizing another number in the sentence is not coverage for them.
    for pattern, reason in [
        (r'不要|禁止|不得|不能|并非|不是|not\s+(?:a|an|the)|without|except|excluding', 'negative_requirement_unbound'),
        (r'均匀|等距|同轴|对称|连通|uniform\w*|equally\s+spaced|coaxial|symmetric', 'relationship_coverage_unbound'),
    ]:
        for match in re.finditer(pattern, objective, re.I):
            unsupported.append({'source_span': list(match.span()), 'reason': reason})

    def append(start, end, prop, kind, comparison, scope=None):
        identity = f'r-{source_input_id}-{start}-{end}-{prop}'
        if comparison.get('tolerance_minus') or comparison.get('tolerance_plus'):
            comparison['tolerance_source_refs'] = [identity + '-tolerance']
        requirements.append({'requirement_id': identity, 'source_input_id': source_input_id,
            'source_kind': source_kind, 'source_span': [start, end], 'source_quote': objective[start:end],
            'property': prop, 'kind': kind, 'comparison': comparison, 'scope': scope or {},
            'tolerance_source_refs': comparison.get('tolerance_source_refs', [])})
        covered.append((start, end))

    for match in _SUFFIX_DIMENSION.finditer(objective):
        raw=match['property'].lower()
        prop={'long':'length','wide':'width','thick':'thickness','high':'height',
              '长':'length','宽':'width','厚':'thickness','高':'height'}[raw]
        relation=re.sub(r'\s+',' ',(match['relation'] or '').lower())
        if relation in _STRICT:
            unsupported.append({'source_span':list(match.span()),'reason':'strict_boundary_unsupported'})
            continue
        op='ge' if relation in _LOWER else 'le' if relation else 'eq'
        value,unit=_quantity(match['value'],match['unit'])
        append(*match.span(),prop,'overall_dimension',{'op':op,{'eq':'value','ge':'lower','le':'upper'}[op]:value,'unit':unit},
            {'axis':{'length':[1,0,0],'width':[0,1,0],'height':[0,0,1],'thickness':[0,0,1]}[prop]})

    preservation_spans = []
    baseline = (verified_state or {}).get('verified_acceptance')
    for match in _PRESERVATION.finditer(objective):
        preservation_spans.append(match.span())
        prop, kind = _PROPERTIES[(match['leading'] or match['trailing'] or match['english']).lower()]
        candidates = [m for m in (baseline or {}).get('measurements', []) if m['kind'] == kind]
        if kind == 'overall_dimension':
            axis = {'length':[1,0,0], 'width':[0,1,0], 'height':[0,0,1], 'thickness':[0,0,1]}[prop]
            candidates = [m for m in candidates if m['scope'].get('axis') == axis]
        if baseline and baseline.get('revision_id') == (verified_state or {}).get('revision_id') and len(candidates) == 1:
            prior = candidates[0]
            values = prior['values']
            # Unequal holes or local probes cannot stand in for a whole-feature preservation requirement.
            scope = prior['scope']
            if (values and all(isinstance(v, (int,float)) and not isinstance(v,bool) and math.isfinite(v) and v >= 0 for v in values)
                    and max(values) - min(values) <= 1e-7 and not scope.get('region_min_mm')
                    and not scope.get('object_ref') and scope.get('frame')!='selected_object'
                    and scope.get('point_mm') is None and not scope.get('centers_mm')):
                append(*match.span(), prop, kind, {'op':'eq','value':values[0], 'unit':'mm3' if kind=='volume' else 'mm'}, scope)
                requirements[-1]['derivation'] = {'rule':'sealed_independent_measurement.v1',
                    **{k:baseline[k] for k in ('revision_id','evidence_id','evidence_sha256','acceptance_digest','artifact_sha256s')},
                    'check_id': prior['check_id']}
                continue
        unsupported.append({'source_span':list(match.span()), 'reason':'preservation_baseline_required'})
    for match in re.finditer(r'不变|保留|保持|preserv\w*|unchanged|remain\s+unchanged|keep\s+.*same', objective, re.I):
        if not any(a <= match.start() and match.end() <= b for a,b in preservation_spans):
            unsupported.append({'source_span':list(match.span()), 'reason':'preservation_baseline_required'})

    for match in _BOX_DIMENSIONS.finditer(objective):
        # A triplet is only interpreted as L/W/T for an explicitly named plate
        # or box. A product/model code is not dimensional authority.
        if not re.search(r'板|长方体|盒|plate|box|block', objective, re.I):
            continue
        if re.search(_RELATION, re.split(r'[，,。；;\n]', objective[:match.start()])[-1], re.I):
            unsupported.append({'source_span': list(match.span()), 'reason': 'triplet_relation_unbound'})
            covered.append(match.span())
            continue
        for component, prop, axis in [('x', 'length', [1, 0, 0]), ('y', 'width', [0, 1, 0]), ('z', 'thickness', [0, 0, 1])]:
            value, unit = _quantity(match[component], match['unit'])
            append(*match.span(), prop, 'overall_dimension', {'op': 'eq', 'value': value, 'unit': unit}, {'axis': axis})

    for match in _SCALAR.finditer(objective):
        prop, kind = _PROPERTIES[match['property'].lower()]
        unit = match['unit1'] or match['unit2'] or match['unit3']
        if unit is None:
            unsupported.append({'source_span': list(match.span()), 'reason': 'quantity_unit_unbound'})
            continue
        value, canonical_unit = _quantity(match['value'], unit)
        relation = re.sub(r'\s+', ' ', (match['relation'] or match['prefix'] or '').lower())
        if match['relation'] and match['prefix']:
            unsupported.append({'source_span': list(match.span()), 'reason': 'multiple_comparison_relations'})
            continue
        if relation in _STRICT:
            unsupported.append({'source_span': list(match.span()), 'reason': 'strict_boundary_unsupported'})
            covered.append(match.span())
            continue
        op = ('ge' if relation in _LOWER
              else 'le' if relation else 'eq')
        comparison = {'op': op, {'eq': 'value', 'ge': 'lower', 'le': 'upper'}[op]: value, 'unit': canonical_unit}
        if match['range']:
            upper, upper_unit = _quantity(match['upper'], match['unit2'] or match['unit3'] or unit)
            if relation or upper_unit != canonical_unit or upper < value:
                unsupported.append({'source_span': list(match.span()), 'reason': 'range_relation_conflict'})
                continue
            comparison = {'op': 'range', 'lower': value, 'upper': upper, 'unit': canonical_unit}
        if match['symmetric'] or match['plus']:
            if comparison['op'] != 'eq':
                unsupported.append({'source_span': list(match.span()), 'reason': 'range_tolerance_conflict'})
                continue
            minus, _ = _quantity(match['symmetric'] or match['minus'], match['unit3'] or unit)
            plus, _ = _quantity(match['symmetric'] or match['plus'], match['unit3'] or unit)
            comparison.update(tolerance_minus=minus, tolerance_plus=plus)
        scope = {}
        if kind == 'overall_dimension':
            scope = {'axis': {'length': [1, 0, 0], 'width': [0, 1, 0],
                              'height': [0, 0, 1], 'thickness': [0, 0, 1]}[prop]}
        append(*match.span(), prop, kind, comparison, scope)
    for match in _COUNT.finditer(objective):
        raw = match['count'].lower()
        count = int(raw) if raw.isdecimal() else _COUNT_WORDS[raw]
        kind = 'hole_count' if match['kind'].lower().startswith(('孔', 'hole')) else 'solid_count'
        relation = re.sub(r'\s+', ' ', (match['relation'] or '').lower())
        if relation in _STRICT:
            unsupported.append({'source_span': list(match.span()), 'reason': 'strict_boundary_unsupported'})
            continue
        op = 'ge' if relation in _LOWER else 'le' if relation else 'eq'
        append(*match.span(), kind, kind, {'op': op, {'eq':'value','ge':'lower','le':'upper'}[op]: float(count), 'unit': 'count'})
    for match in _SIZED_HOLE_COUNT.finditer(objective):
        raw=match['count'].lower()
        relation=re.sub(r'\s+',' ',(match['relation'] or '').lower())
        if relation in _STRICT:
            unsupported.append({'source_span':list(match.span()),'reason':'strict_boundary_unsupported'})
            continue
        op='ge' if relation in _LOWER else 'le' if relation else 'eq'
        append(*match.span(), 'hole_count','hole_count',{'op':op,{'eq':'value','ge':'lower','le':'upper'}[op]:float(int(raw) if raw.isdecimal() else _COUNT_WORDS[raw]),'unit':'count'})
    for match in list(_DIAMETER.finditer(objective)) + list(_HOLE_SIZE.finditer(objective)):
        if any(a <= match.start() and match.end() <= b for a, b in covered):
            # Count phrases can contain a distinct diameter obligation.
            if any(r['property']=='hole_diameter' and r['source_span'][0]<=match.start() and match.end()<=r['source_span'][1] for r in requirements):
                continue
        clause = re.split(r'[，,。；;\n]', objective[:match.start()])[-1] + objective[match.start():]
        clause = re.split(r'[，,。；;\n]', clause)[0]
        if not re.search(r'孔|hole|bore', clause, re.I):
            continue
        value, unit = _quantity(match['value'], match['unit'])
        append(*match.span(), 'hole_diameter', 'hole_diameter', {'op': 'eq', 'value': value, 'unit': unit})
    for match in _CENTERED.finditer(objective):
        context = objective[max(0, match.start()-20):match.end()+30]
        if re.search(r'孔|hole|bore', context, re.I) and re.search(r'板|plate', objective, re.I):
            append(*match.span(), 'hole_position', 'hole_position', {'op': 'eq', 'value': 0., 'unit': 'mm'},
                {'axis': [0, 0, 1], 'frame': 'bounds_center', 'centers_mm': [[0, 0, 0]]})
    thickness = [r for r in requirements if r['property'] == 'thickness' and r['comparison']['op'] == 'eq']
    through = re.search(r'通孔|贯穿孔|through[- ]hole', objective, re.I)
    if through and len(thickness) == 1 and re.search(r'板|plate', objective, re.I):
        append(*through.span(), 'hole_depth', 'hole_depth', dict(thickness[0]['comparison']))
        requirements[-1]['derivation'] = {'rule': 'through_plate_thickness.v1',
                                         'source_refs': [thickness[0]['requirement_id']]}
    for pattern in (_EXPLICIT_QUANTITY,_OTHER_ENGINEERING_QUANTITY):
        for quantity in pattern.finditer(objective):
            if not any(a <= quantity.start() and quantity.end() <= b for a, b in covered):
                unsupported.append({'source_span': list(quantity.span()), 'reason': 'quantity_property_unbound'})
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
            selected[0]['coordinate_source_span']=list(axis.span())
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
        if not any(a.start()<=marker.start() and marker.end()<=a.end() for a,_ in axes):
            unsupported.append({'source_span':list(marker.span()),'reason':'coordinate_authority_unbound'})
    for marker in re.finditer(r'所选(?:对象|实体|零件|部件)?|选中(?:的)?(?:对象|实体|零件|部件)|\bselected\s+(?:object|body|component|part)\b',objective,re.I):
        lo=max([objective.rfind(c,0,marker.start()) for c in ',，;；。\n'])+1
        ends=[objective.find(c,marker.end()) for c in ',，;；。\n']
        hi=min([x for x in ends if x>=0] or [len(objective)])
        scoped=[r for r in requirements if lo<=r['source_span'][0] and r['source_span'][1]<=hi]
        if not scoped or any(not r['scope'].get('object_ref') for r in scoped):
            unsupported.append({'source_span':list(marker.span()),'reason':'selected_object_scope_unbound'})
    return {'schema_version': 'requirement-extraction.v1', 'requirements': requirements, 'unsupported': unsupported}


def extract_source_requirements(objective, *, verified_state=None, requirement_sources=None) -> dict:
    """Structured sources are supplied by the server, never the tool response."""
    sources = requirement_sources if requirement_sources is not None else [
        {'text':objective, 'source_input_id':'objective', 'source_kind':'user'}]
    result = {'schema_version':'requirement-extraction.v1', 'requirements':[], 'unsupported':[]}
    for source in sources:
        if source['source_kind'] not in {'user','confirmed'} or source['text'] not in objective:
            raise ValueError('requirement source is not part of the frozen submitted input')
        extracted = extract_requirements(source['text'], source_input_id=source['source_input_id'],
                                        source_kind=source['source_kind'], verified_state=verified_state)
        result['requirements'].extend(extracted['requirements'])
        result['unsupported'].extend(extracted['unsupported'])
    return result


def validate_requirement_bindings(objective, contract, verified_state=None, *, confirmed_sources=(), requirement_sources=None) -> dict:
    """Bind every recognized explicit condition before CAD dispatch, or reject.

    Preservation requires independent evidence sealed into the exact revision;
    parameter labels and nominal values never substitute for measured geometry.
    """
    extracted = extract_source_requirements(objective, verified_state=verified_state, requirement_sources=requirement_sources)
    if extracted['unsupported']:
        raise RequirementBindingError('engineering_verification_unavailable',
            'source quantities cannot yet be bound reliably: ' + str(extracted['unsupported']))
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
            raise RequirementBindingError('requirement_binding_mismatch', f'{check.check_id}: property/source differs')
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
        if check.kind == 'wall_thickness' and check.scope.wall_mode != 'continuous_normal':
            raise RequirementBindingError('requirement_binding_mismatch', 'whole wall requirement cannot be replaced by local coverage')
        covered.add(record['requirement_id'])
        bindings.append({**record, 'check_ids': [check.check_id], 'status': 'bound'})
    if covered != set(records):
        raise RequirementBindingError('requirement_binding_mismatch', 'coverage missing: ' + ','.join(sorted(set(records) - covered)))
    return {'schema_version': 'requirement-bindings.v1', 'status': 'bound',
            'requirements_digest': canonical_sha256({'objective': objective}),
            'acceptance_digest': contract.digest(), 'bindings': bindings,
            'comparison_policy_id': contract.comparison_policy_id}
