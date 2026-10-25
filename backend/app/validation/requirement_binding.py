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
from app.validation.source_obligations import (
    RELATION as _RELATION, LOWER as _LOWER, STRICT as _STRICT,
    COUNT_WORDS as _COUNT_WORDS, COUNT_VALUE, CENTERED as _CENTERED,
    inventory, normalize_relation, resolve_inventory, bind_component_scopes, inventory_hole_populations,
)


_NUMBER = r'(?:\d+(?:\.\d+)?|\.\d+)'
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
_DIMENSION_QUANTITY = (
    rf'(?P<value>{_NUMBER})\s*(?P<unit1>{_UNIT})?\s*'
    rf'(?:(?P<range>至|到|[~～–—-]|to)\s*(?P<upper>{_NUMBER})\s*(?P<unit2>{_UNIT})?)?\s*'
    rf'(?:(?:±|\+/-)\s*(?P<symmetric>{_NUMBER})|\+\s*(?P<plus>{_NUMBER})\s*/\s*[-−]\s*(?P<minus>{_NUMBER}))?\s*'
    rf'(?P<unit3>{_UNIT})?(?P<suffix>以上|以下)?')
_SCALAR = re.compile(
    rf'(?P<prefix>{_RELATION})?\s*(?P<property>{_PROPERTY})\s*(?:改为|修改为|设为|调整为|为|是|[:：=]|of|is|to)?\s*'
    rf'(?P<relation>{_RELATION})?\s*{_DIMENSION_QUANTITY}', re.I)
_COUNT = re.compile(rf'(?P<relation>{_RELATION})?\s*(?<![a-zA-Z0-9_.零一二两三四五六七八九十])(?P<count>{COUNT_VALUE})'
    r'\s*(?:(?:个|处|M\d+(?:\.\d+)?|圆形|螺钉|安装|固定|线缆|接头|independent|separate|connected|centered|through[- ]?|独立|连通|居中|贯穿|通)\s*)*'
    r'(?P<kind>实体|孔|solids?\b|holes?\b)', re.I)
_EXPLICIT_QUANTITY = re.compile(rf'(?<![a-zA-Z0-9_.]){_NUMBER}\s*{_UNIT}(?![a-zA-Z])', re.I)
_OTHER_ENGINEERING_QUANTITY = re.compile(
    rf'(?<![a-zA-Z0-9_.]){_NUMBER}\s*(?:°|度|弧度|deg(?:rees?)?\b|rad(?:ians?)?\b|英寸|inches?\b)|'
    rf'(?:{_NUMBER}|[一二两三四五六七八九十]+)\s*(?:个|处|颗|条|只)\s*'
    r'(?:支脚|支柱|支撑|槽|孔|实体|齿|筋|凸台|通道|倒角|圆角)|'
    rf'\b(?:{_NUMBER}|one|two|three|four|five|six)\s+(?:[a-z-]+\s+){{0,2}}'
    r'(?:legs?|supports?|ribs?|fins?|pins?|posts?|slots?|teeth|holes?|solids?)\b', re.I)
_BOX_DIMENSIONS = re.compile(rf'(?P<x>{_NUMBER})\s*[x×*]\s*(?P<y>{_NUMBER})\s*[x×*]\s*(?P<z>{_NUMBER})\s*(?P<unit>{_UNIT})', re.I)
_DIAMETER = re.compile(rf'(?P<prefix>{_RELATION})?\s*(?:直径|diameter)\s*(?:为|of|=)?\s*'
    rf'(?P<relation>{_RELATION})?\s*{_DIMENSION_QUANTITY}', re.I)
_HOLE_SIZE = re.compile(rf'(?P<relation>{_RELATION})?\s*{_DIMENSION_QUANTITY}'
    rf'\s+(?:diameter\s+)?(?:through[- ]?)?holes?\b', re.I)
_SIZED_HOLE_COUNT = re.compile(rf'(?P<relation>{_RELATION})?\s*\b(?P<count>one|two|three|four|five|six|single|an?|\d+)\s+'
    rf'(?:centered\s+|central\s+)?{_NUMBER}\s*{_UNIT}\s+(?:diameter\s+)?(?:through[- ]?)?holes?\b', re.I)
_CHINESE_SIZED_HOLE_COUNT = re.compile(
    rf'(?P<relation>{_RELATION})?\s*(?<![a-zA-Z0-9_.零一二两三四五六七八九十])(?P<count>{COUNT_VALUE})\s*[个处]\s*'
    rf'(?:孔径|直径)\s*(?:为|[:：=])?\s*(?:{_RELATION})?\s*{_NUMBER}\s*{_UNIT}\s*(?:以上|以下)?\s*的?\s*(?:贯穿|通)?孔', re.I)
_SUFFIX_DIMENSION = re.compile(rf'(?P<relation>{_RELATION})?\s*(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})\s*'
    r'(?P<property>long\b|wide\b|thick\b|high\b|[长宽厚高](?!度|\s*\d))', re.I)
_WORLD_AXIS = re.compile(r'(?:世界|world)\s*(?P<axis>[xyz])\s*(?:轴|axis)?', re.I)
_LOCAL_AXIS = re.compile(r'(?:局部|local)\s*(?P<axis>[xyz])\s*(?:轴|axis)?', re.I)
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
    obligations = inventory(objective, source_input_id=source_input_id)
    comparison_spans = []
    declarations = list(_DECLARED_UNIT.finditer(objective))
    declared = declarations[0] if declarations and len({m['unit'].lower() for m in declarations}) == 1 else None

    def append(start, end, prop, kind, comparison, scope=None, *, identity=None):
        identity = identity or f'r-{source_input_id}-{start}-{end}-{prop}'
        if comparison.get('tolerance_minus') or comparison.get('tolerance_plus'):
            comparison['tolerance_source_refs'] = [identity + '-tolerance']
        requirements.append({'requirement_id': identity, 'source_input_id': source_input_id,
            'source_kind': source_kind, 'source_span': [start, end], 'source_quote': objective[start:end],
            'property': prop, 'kind': kind, 'comparison': comparison, 'scope': scope or {},
            'tolerance_source_refs': comparison.get('tolerance_source_refs', [])})
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
        raw=match['property'].lower()
        prop={'long':'length','wide':'width','thick':'thickness','high':'height',
              '长':'length','宽':'width','厚':'thickness','高':'height'}[raw]
        value,unit=_quantity(match['value'],match['unit'])
        comparison = relation_comparison(match, value, unit)
        if comparison is None:
            continue
        append(*match.span(),prop,'overall_dimension',comparison,
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
                    and not scope.get('object_ref') and not scope.get('component_region') and scope.get('frame')!='selected_object'
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
        prefix = re.split(r'[，,。；;\n]', objective[:match.start()])[-1]
        if (not re.search(r'板|长方体|盒|支脚|安装柱|凸台|密封槽|腔体|空腔|内部|plate|box|block|cavity|lid|cover|foot|boss', prefix, re.I)
                and not re.match(r'\s*(?:(?:plate|box|block)\b|的?(?:板|盒|长方体))', objective[match.end():], re.I)):
            continue
        if re.search(_RELATION, prefix, re.I):
            unsupported.append({'source_span': list(match.span()), 'reason': 'triplet_relation_unbound'})
            covered.append(match.span())
            continue
        for component, prop, axis in [('x', 'length', [1, 0, 0]), ('y', 'width', [0, 1, 0]), ('z', 'thickness', [0, 0, 1])]:
            value, unit = _quantity(match[component], match['unit'])
            append(*match.span(), prop, 'overall_dimension', {'op': 'eq', 'value': value, 'unit': unit}, {'axis': axis})

    for match in _SCALAR.finditer(objective):
        if any(a <= match.start('value') < b for a,b in covered):
            continue
        prop, kind = _PROPERTIES[match['property'].lower()]
        comparison = dimension_comparison(match)
        if comparison is None:
            continue
        scope = {}
        if kind == 'overall_dimension':
            scope = {'axis': {'length': [1, 0, 0], 'width': [0, 1, 0],
                              'height': [0, 0, 1], 'thickness': [0, 0, 1]}[prop]}
        append(*match.span(), prop, kind, comparison, scope)
    for match in _COUNT.finditer(objective):
        if any(a <= match.start('count') < b for a, b in covered):
            continue  # A preceding labelled dimension is not the next line's hole count.
        raw = match['count'].lower()
        count = int(raw) if raw.isdecimal() else _COUNT_WORDS[raw]
        kind = 'hole_count' if match['kind'].lower().startswith(('孔', 'hole')) else 'solid_count'
        comparison = relation_comparison(match, float(count), 'count')
        if comparison is not None:
            append(*match.span(), kind, kind, comparison)
    for match in list(_SIZED_HOLE_COUNT.finditer(objective)) + list(_CHINESE_SIZED_HOLE_COUNT.finditer(objective)):
        if any(r['kind'] == 'hole_count' and r['source_span'][0] <= match.start('count') < r['source_span'][1]
               for r in requirements):
            continue
        raw=match['count'].lower()
        comparison = relation_comparison(match, float(int(raw) if raw.isdecimal() else _COUNT_WORDS[raw]), 'count')
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
        clause = re.split(r'[，,。；;\n]', objective[:match.start()])[-1] + objective[match.start():]
        clause = re.split(r'[，,。；;\n]', clause)[0]
        if not re.search(r'孔|hole|bore', clause, re.I):
            continue
        comparison = dimension_comparison(match)
        if comparison is not None:
            append(*match.span(), 'hole_diameter', 'hole_diameter', comparison)
    for match in _CENTERED.finditer(objective):
        context = objective[max(0, match.start()-20):match.end()+30]
        if re.search(r'孔|hole|bore', context, re.I) and re.search(r'板|plate', objective, re.I):
            append(*match.span(), 'hole_position', 'hole_position', {'op': 'eq', 'value': 0., 'unit': 'mm'},
                {'axis': [0, 0, 1], 'frame': 'bounds_center', 'centers_mm': [[0, 0, 0]]})
    throughs = list(re.finditer(r'通孔|贯穿孔|\bthrough[- ]?holes?\b', objective, re.I))
    for through in throughs:
        append(*through.span(), 'hole_through', 'hole_through', {'op': 'eq', 'value': 1., 'unit': 'count'})
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
    bind_component_scopes(objective, requirements, unsupported, obligations, source_input_id=source_input_id)
    for through in [r for r in requirements if r['kind'] == 'hole_through']:
        thickness = [r for r in requirements if r['property'] == 'thickness' and r['comparison']['op'] == 'eq'
                     and r['scope'].get('component_region') == through['scope'].get('component_region')]
        if len(thickness) == 1 and re.search(r'板|plate', objective, re.I):
            append(*through['source_span'], 'hole_depth', 'hole_depth', dict(thickness[0]['comparison']), dict(through['scope']),
                   identity=through['requirement_id'] + '-depth' if through.get('distribution_ref') else None)
            requirements[-1]['derivation'] = {'rule': 'through_plate_thickness.v1',
                                             'source_refs': [thickness[0]['requirement_id']]}
    inventory_hole_populations(objective, requirements, obligations, source_input_id=source_input_id)
    resolve_inventory(obligations, requirements, comparison_spans, unsupported)
    return {'schema_version': 'requirement-extraction.v1', 'requirements': requirements,
            'unsupported': unsupported, 'obligations': obligations}


def extract_source_requirements(objective, *, verified_state=None, requirement_sources=None) -> dict:
    """Structured sources are supplied by the server, never the tool response."""
    sources = requirement_sources if requirement_sources is not None else [
        {'text':objective, 'source_input_id':'objective', 'source_kind':'user'}]
    result = {'schema_version':'requirement-extraction.v1', 'requirements':[], 'unsupported':[], 'obligations':[]}
    submitted_rows = []
    for source in sources:
        if source['source_kind'] not in {'user','confirmed'} or source['text'] not in objective:
            raise ValueError('requirement source is not part of the frozen submitted input')
        extracted = extract_requirements(source['text'], source_input_id=source['source_input_id'],
                                        source_kind=source['source_kind'], verified_state=verified_state)
        result['requirements'].extend(extracted['requirements'])
        result['unsupported'].extend(extracted['unsupported'])
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
    return result


def bind_source_quotes(objective, acceptance, *, verified_state=None, requirement_sources=None):
    """The server owns quotes for known v2 references, not model paraphrases.

    Only restore the exact source text. Property, comparison, units, scope and
    coverage still undergo independent validation and are never repaired here.
    """
    extracted = extract_source_requirements(objective, verified_state=verified_state,
                                            requirement_sources=requirement_sources)
    records = {r['requirement_id']: r for r in extracted['requirements']}
    for check in acceptance.get('checks', []):
        record = records.get(check.get('binding_ref')) if isinstance(check, dict) else None
        if record and check.get('source_kind', 'user') == record['source_kind']:
            check['source_quote'] = record['source_quote']


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
        if check.kind == 'wall_thickness' and check.scope.wall_mode != 'continuous_normal':
            raise RequirementBindingError('requirement_binding_mismatch', 'whole wall requirement cannot be replaced by local coverage')
        covered.add(record['requirement_id'])
        bindings.append({**record, 'check_ids': [check.check_id], 'status': 'bound'})
    if covered != set(records):
        raise RequirementBindingError('requirement_binding_mismatch', 'coverage missing: ' + ','.join(sorted(set(records) - covered)))
    return {'schema_version': 'requirement-bindings.v1', 'status': 'bound',
            'requirements_digest': canonical_sha256({'objective': objective}),
            'acceptance_digest': contract.digest(), 'bindings': bindings,
            'source_obligations': extracted['obligations'],
            'comparison_policy_id': contract.comparison_policy_id}
