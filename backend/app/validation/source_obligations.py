"""Source-first coverage, independent of successful measurement extraction.

This is a bounded engineering grammar, not a claim to understand arbitrary
prose. Count tokens, comparison qualifiers and restrictions survive even when
the measurement parser cannot lower their surrounding noun phrase. A diameter
span cannot discharge a count, nor can a count discharge a restriction.
"""
import re
import json


RELATION = (r'(?:至少|不小于|不少于|不低于|不超过|至多|不大于|不高于|大于|小于|超过|最小|最大|以上|以下|'
            r'\b(?:minimum|maximum|over|under|above|below|exceeding)\b|\b(?:at[\s-]+least|at[\s-]+most|'
            r'no[\s-]+less[\s-]+than|no[\s-]+more[\s-]+than|greater[\s-]+than|'
            r'more[\s-]+than|less[\s-]+than)\b|>=|<=|≥|≤|＞|＜|>|<)')
LOWER = {'至少', '不小于', '不少于', '不低于', '最小', 'minimum', 'at least', 'no less than', '>=', '≥', '以上'}
STRICT = {'大于', '小于', '超过', 'greater than', 'more than', 'less than', '>', '<', 'over', 'under', 'above', 'below', 'exceeding'}
COUNT_WORDS = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6,
               '七': 7, '八': 8, '九': 9, '十': 10, 'zero': 0, 'one': 1, 'two': 2, 'three': 3,
               'four': 4, 'five': 5, 'six': 6, 'single': 1, 'a': 1, 'an': 1}
COUNT_VALUE = r'(?:\d+|[零一二两三四五六七八九十]|\b(?:zero|one|two|three|four|five|six|single|an?)\b)'
_COUNT_TOKEN = re.compile(r'(?<![a-zA-Z0-9_.零一二两三四五六七八九十])'
    # A positive sign also separates additive groups. Like COUNT_VALUE, start
    # at the number so the original source span agrees with the binder. Retain
    # minus signs and fractional tokens: neither may bind as a positive integer.
    r'(?:[−-]?\d+(?:\.\d+)?|[零一二两三四五六七八九十百千]+)\s*(?:个|处|颗|条|只)|'
    r'(?<![a-zA-Z0-9_.])(?:zero|one|two|three|four|five|six|single|[−-]?\d+(?:\.\d+)?)\b(?=\s+(?:(?!mm\b|cm\b|m\b)[a-z-]+\s+){0,6}'
    r'(?:holes?|solids?|legs?|supports?|ribs?|fins?|pins?|posts?|slots?|teeth)\b)', re.I)
_RESTRICTION = re.compile(r'不(?!变)|无|[非未](?=[ \t]*(?:贯穿|穿透|贯通|通孔|打穿))|禁止|避免|排除|仅(?:有|限|允许)?|只(?:有|能|允许)|'
    r'\b(?:not|no|non[- ]?(?:through|thru|penetrat\w*)|without|except|exclud\w*|omit\w*|avoid\w*|only)\b', re.I)
_RELATIONSHIP = re.compile(r'均匀|等距|同轴|对称|连通|接触|相交|隔开|分离|'
    r'\b(?:uniform\w*|equally[ -]+spaced|coaxial|symmetric|touch\w*|connected|separated|disjoint)\b', re.I)
_FEATURE_GROUP = re.compile(r'(?:底部|底面|侧壁|侧面|右侧|左侧|前侧|后侧|顶面|顶板|底板|单侧壁|'
    r'\b(?:bottom|top|left|right|side)[ -]+(?:face|wall)\b)(?=[^，,。；;\n]{0,50}(?:孔|\bhole))', re.I)
_FEATURE_SEMANTIC = re.compile(r'盲孔|螺纹|沉孔|沉头孔|\b(?:blind|threaded|counterbored?|countersunk)\b', re.I)
# Inventory the engineering predicate independently of the noun grammar that
# can lower it into a measurement. Unknown paths/targets must remain unresolved;
# finding a diameter, count, or depth in the same clause is not coverage.
_PASSAGE = re.compile(r'通孔|贯穿|穿透|贯通|打穿|打通|穿过|(?:两端|双端|两头)(?:均|都)?开口|'
                      r'\b(?:through|thru)(?:[- ]?holes?)?\b|\bpenetrat\w*\b', re.I)
_PASSAGE_SUBJECT = re.compile(r'孔|槽|腔|\b(?:holes?|bores?|slots?|grooves?|channels?)\b', re.I)
CENTERED = re.compile(r'(?:居中|中心|centered|central)', re.I)
_DIRECTIVE = re.compile(r'(?:必须|务必|需满足|\bmust(?:\s+have)?\b|\bshall\b)\s*'
                        r'(?P<predicate>[^，,。；;\n]+)', re.I)
# Discover assertion structure without needing to recognize its action. A new
# verb after a feature/property must not become invisible just because neither
# the measurement parser nor _PASSAGE has that verb in its vocabulary.
_ASSERTION_SUBJECT = re.compile(
    r'(?:该|此|当前|这个)?(?:(?:M\d+(?:\.\d+)?[ \t]*)?(?:安装|固定|螺钉|线缆|接头)?(?:圆形孔|圆孔|孔)|槽|腔|壁|'
    r'(?:底板|盖板|上盖|盒体|板)(?:(?:固定|安装)?孔(?:数量|数目|数)?[ \t]*(?:为|[:：=])?)?|'
    r'圆柱体|圆柱|球体|球|'
    r'(?:模型|外壳)?(?:外部)?(?:总体|整体|总)?(?:长度|宽度|高度|厚度|壁厚|直径|半径|间隙|体积))|'
    r'(?:the\s+)?\b(?:holes?|bores?|slots?|grooves?|channels?|cavit(?:y|ies)|walls?|'
    r'plates?|lids?|boxes?|cylinders?|spheres?|length|width|height|thickness|diameter|radius|gap|volume)\b', re.I)
_CLAUSE_DIRECTIVE = re.compile(r'(?:必须|务必|需要|需满足|须|需|要|应当|应该|应|'
                              r'\b(?:must(?:\s+have)?|shall)\b)\s*(?P<predicate>.+)', re.I)
_LIST_PREFIX = re.compile(r'(?:[-*•][ \t]+|\d+[、)）][ \t]*|\d+\.(?=\D)[ \t]*)')
_REQUEST_PREFIX = re.compile(
    r'(?:(?:请(?:帮我|为我)?|帮我|为我)[ \t]*)?(?:设计|创建|生成|制作|做|绘制|添加|增加|开|设置)'
    r'[ \t]*(?:一个|一块|一种|一些|个)?[ \t]*|'
    r'(?:please\s+)?(?:create|design|generate|make|add|draw)\s+(?:(?:a|an|the)\s+)?|'
    r'(?:将|把)(?:当前|这个|该|此)?[ \t]*', re.I)
_MATERIAL_NOUN = re.compile(r'(?:底板|盖板|上盖|盒体|板|圆柱(?:体)?|球(?:体)?)(?:几何模型|模型)?|'
    r'(?:the\s+)?(?:plates?|lids?|boxes?|cylinders?|spheres?)(?:\s+model)?', re.I)
_CLAUSE_LINK = re.compile(r'修改为|改为|设为|调整为|另有|带(?:有)?|的|以及|[+＋、]|'
                          r'\b(?:with|of|and|is|are|the)\b', re.I)


def _assertion_clauses(text, source_input_id):
    for clause in re.finditer(r'[^，,。；;\n]+', text):
        raw = clause.group()
        start = clause.start() + len(raw) - len(raw.lstrip())
        end = clause.end() - len(raw) + len(raw.rstrip())
        if marker := _LIST_PREFIX.match(text[start:end]):
            start += marker.end()
        if introduction := _REQUEST_PREFIX.match(text[start:end]):
            start += introduction.end()
        phrase = text[start:end]
        subject = _ASSERTION_SUBJECT.search(phrase)
        directive = _CLAUSE_DIRECTIVE.fullmatch(phrase)
        if directive:
            # This is independent of subject discovery: recognizing a later
            # dimension must not hide an earlier, unlowered required action.
            a, b = (start + p for p in directive.span('predicate'))
            yield {'obligation_id': f'o-{source_input_id}-{a}-{b}-directive',
                'source_input_id': source_input_id, 'source_span': [a, b],
                'source_quote': text[a:b], 'category': 'directive'}
        if subject:
            # Nominal qualifiers before a recognized subject carry meaning
            # too. Preserve the entire assertion, apart from an exact request
            # introduction; a later diameter must not hide an unknown prefix.
            subject_start = start + subject.start()
            subject_end = start + subject.end()
            if directive:
                start += directive.start('predicate')
            # Naming the requested product is not itself a dimensional
            # assertion. This exception is an exact noun phrase; any verb,
            # qualifier, or unknown tail still creates a coverage obligation.
            if _MATERIAL_NOUN.fullmatch(text[start:end]):
                continue
            yield {'obligation_id': f'o-{source_input_id}-{start}-{end}-assertion_clause',
                'source_input_id': source_input_id, 'source_span': [start, end],
                'source_quote': text[start:end], 'category': 'assertion_clause',
                'subject_span': [subject_start, subject_end]}


def normalize_relation(text):
    return re.sub(r'[\s-]+', ' ', text.lower()).translate(str.maketrans('＞＜', '><'))


def inventory(text, *, source_input_id):
    """Scan obligations before binding; unknown qualifiers retain their spans."""
    relations = list(re.finditer(RELATION, text, re.I))
    positions = [m for m in CENTERED.finditer(text)
                 if re.search(r'孔|hole|bore', text[max(0, m.start()-20):m.end()+30], re.I)]
    items = list(_assertion_clauses(text, source_input_id))
    for category, matches in [('count', _COUNT_TOKEN.finditer(text)), ('comparison', relations),
                               ('restriction', _RESTRICTION.finditer(text)),
                               ('relationship', _RELATIONSHIP.finditer(text)),
                               ('feature_group', _FEATURE_GROUP.finditer(text)),
                               ('feature_semantic', _FEATURE_SEMANTIC.finditer(text)),
                               ('feature_position', positions),
                               ('directive', _DIRECTIVE.finditer(text))]:
        for match in matches:
            if category == 'restriction' and any(r.start() <= match.start() and match.end() <= r.end() for r in relations):
                continue  # "不小于" and "no less than" are positive comparisons.
            start, end = match.span('predicate') if category == 'directive' else match.span()
            items.append({'obligation_id': f'o-{source_input_id}-{start}-{end}-{category}',
                'source_input_id': source_input_id, 'source_span': [start, end],
                'source_quote': text[start:end], 'category': category})
    for match in _PASSAGE.finditer(text):
        start, end = match.span()
        lo = max(text.rfind(c, 0, start) for c in ',，;；。\n') + 1
        hi = min([p for c in ',，;；。\n' if (p := text.find(c, end)) >= 0] or [len(text)])
        # Chinese passage verbs retain an obligation even with an omitted
        # subject ("孔径6mm，打穿全部厚度"). English "through" also describes
        # a process or transport, so require a feature in that clause.
        if match.group().lower() in {'through', 'thru'} and not _PASSAGE_SUBJECT.search(text[lo:hi]):
            # A standalone predicate line may refer to a nearby feature. It
            # remains unresolved until its subject/path can actually be bound.
            if text[lo:hi].strip().lower() not in {'through', 'thru'} or not _PASSAGE_SUBJECT.search(text):
                continue
        items.append({'obligation_id': f'o-{source_input_id}-{start}-{end}-feature_semantic',
            'source_input_id': source_input_id, 'source_span': [start, end],
            'source_quote': text[start:end], 'category': 'feature_semantic',
            'predicate': 'through_passage'})
    # A strong modal can also be found by the legacy directive scanner. One
    # original source assertion has one stable identity, whichever discovers it.
    return list({item['obligation_id']: item for item in items}.values())


def resolve_inventory(items, requirements, comparison_spans, unsupported, *, design_requests=()):
    # Clause coverage may use an already validated coordinated subject. Its
    # presence alone never authorizes distribution or hides an unknown tail.
    for item in sorted(items, key=lambda item: item['category'] == 'assertion_clause'):
        start, end = item['source_span']
        refs = []
        if item['category'] == 'count':
            refs = [r['requirement_id'] for r in requirements if r['kind'] in {'hole_count', 'solid_count'}
                    and r['source_span'][0] <= start and end <= r['source_span'][1]]
            if not refs:
                refs = [r['reference_id'] for r in design_requests
                        if r['source_span'] == [start, end]]
                if refs:
                    item['verification_domain'] = 'cad_document_delivery'
        elif item['category'] == 'feature_position':
            refs = [r['requirement_id'] for r in requirements if r['kind'] == 'hole_position'
                    and r['source_span'][0] <= start and end <= r['source_span'][1]]
        elif item['category'] == 'feature_semantic' and item.get('predicate') == 'through_passage':
            refs = [r['requirement_id'] for r in requirements if r['kind'] == 'hole_through'
                    and r['comparison'] == {'op': 'eq', 'value': 1., 'unit': 'count'}
                    and r['source_span'][0] <= start and end <= r['source_span'][1]]
        elif item['category'] == 'comparison':
            refs = [r['requirement_id'] for r in requirements
                    if any(a <= start and end <= b and r['source_span'][0] <= a and b <= r['source_span'][1]
                           for a, b in comparison_spans)]
        elif item['category'] == 'directive':
            relevant = [r for r in requirements if r['source_span'][0] < end and start < r['source_span'][1]]
            if all(character.isspace() or any(r['source_span'][0] <= start + i < r['source_span'][1] for r in relevant)
                   for i, character in enumerate(item['source_quote'])):
                refs = [r['requirement_id'] for r in relevant]
        elif item['category'] == 'assertion_clause':
            relevant = [r for r in requirements if r['source_span'][0] < end and start < r['source_span'][1]]
            # A bare subject may be completed by an explicitly bound adjacent
            # dimension; preserve that parser's original subject reference.
            relevant += [r for r in requirements if r.get('derivation', {}).get('subject_source_span') == [start, end]]
            spans = [item['subject_span'], *(r['source_span'] for r in relevant)]
            spans += [o['source_span'] for o in items if o['category'] in {'component_distribution', 'count'}
                      and o.get('status') == 'bound']
            if any(r.get('distribution_ref') for r in relevant):
                if marker := _DISTRIBUTIVE.match(item['source_quote']):
                    spans.append((start + marker.start(), start + marker.end()))
            spans += [(start + m.start(), start + m.end()) for m in _CLAUSE_LINK.finditer(item['source_quote'])]
            # Nominal word order ("diameter 6 mm hole", "10mm厚的板")
            # retains its subject without treating arbitrary suffix prose as
            # decoration. The suffix must be the complete terminal noun phrase
            # and its type must agree with a measured property in this clause.
            tail_start = max((min(end, r['source_span'][1]) for r in relevant), default=start)
            tail = item['source_quote'][tail_start-start:].strip()
            if ((re.fullmatch(r'(?:的)?(?:圆形|圆)?孔|holes?', tail, re.I)
                    and any(r['kind'].startswith('hole_') for r in relevant))
                    or (re.fullmatch(r'(?:的)?板|plate', tail, re.I)
                        and relevant)):
                spans.append((tail_start, end))
            if (re.fullmatch(r'上(?:的(?:耳机)?挂钩)?', tail)
                    and any(r.get('derivation', {}).get('rule') == 'external_plate_clearance.v1' for r in relevant)):
                spans.append((tail_start, end))
            if relevant and all(character.isspace() or character in ':：=' or (character == '.' and start+i == end-1)
                    or any(a <= start + i < b for a, b in spans)
                    for i, character in enumerate(item['source_quote'])):
                refs = [r['requirement_id'] for r in relevant]
        elif item['category'] == 'component_distribution':
            relevant = [r for r in requirements if r.get('distribution_ref') == item['obligation_id']]
            targets = set(item['component_refs'])
            if len(targets) > 1 and item['property_refs'] and all(
                    {ref for r in relevant if r['distribution_origin'] == origin
                     for ref in r['scope_source_refs']} == targets for origin in item['property_refs']):
                refs = [r['requirement_id'] for r in relevant]
        elif item['category'] == 'hole_population' and not item['selector_required']:
            refs = item['population_refs']
        item.update(status='bound' if refs else 'unresolved', requirement_refs=refs)
        if not refs:
            reason = {'count': 'count_property_unbound', 'comparison': 'comparison_relation_unbound',
                      'restriction': 'negative_requirement_unbound',
                      'relationship': 'relationship_coverage_unbound',
                      'feature_group': 'feature_group_scope_unbound',
                      'feature_semantic': 'feature_semantics_unbound',
                      'feature_position': 'feature_position_scope_unbound',
                      'component_distribution': 'component_scope_ambiguous',
                      'hole_population': 'feature_group_scope_unbound',
                      'directive': 'required_clause_unbound',
                      'assertion_clause': 'required_clause_unbound'}[item['category']]
            if item['category'] == 'comparison' and normalize_relation(item['source_quote']) in STRICT:
                reason = 'strict_boundary_unsupported'
            unsupported.append({'source_span': [start, end], 'reason': reason,
                                'obligation_id': item['obligation_id']})


_OWNER = re.compile(r'内部有效空间|内部空腔|空腔|腔体|盒体(?:底板|底面)?|上盖|盖板|底板|底座|支脚|安装柱|安装耳|加强筋|法兰|凸台|密封槽|'
    r'长方体|圆柱体|圆柱(?![形面孔])|球体|球(?![形面孔])|盒|板|(?:零件|部件|组件)[A-Za-z0-9一二三四]+|'
    r'\b(?:cavity|void|lid|cover|base|foot|boss|groove|plate|box|block|cylinder|sphere)\b', re.I)
_ROOT_OWNERS = {'盒体', '盒', '板', '长方体', 'plate', 'box', 'block', 'cylinder', 'sphere'}
_VOID_OWNER = re.compile(r'空腔|腔体|内部|cavity|void', re.I)
_SUBJECT_JOIN = re.compile(r'\s*(?:和|与|以及|及|、|，|,|&|\band\b|或者|或|\bor\b)\s*', re.I)
_SUBJECT_CHOICE = re.compile(r'或者|或|\bor\b', re.I)
_DISTRIBUTIVE = re.compile(r'\s*(?:各(?:自)?(?:都)?(?:有)?|均(?:有)?|都(?:有)?|\beach\b(?:\s+(?:have|has))?)', re.I)


def _owner_identity(phrase):
    if re.fullmatch(r'盒体(?:底板|底面)', phrase):
        return '盒体'
    if re.fullmatch(r'圆柱(?:体)?|cylinder', phrase, re.I):
        return 'cylinder'
    if re.fullmatch(r'球(?:体)?|sphere', phrase, re.I):
        return 'sphere'
    return phrase.lower()


def _component_subjects(text, mentions, declarations):
    """Resolve coordinated subjects before assigning any property to an owner.

    Coordinate declarations are atomic authorities, not items in a noun list.
    A bare conjunction or a choice is unresolved; only an explicit distributive
    phrase authorizes copying a requirement to every named component.
    """
    declared = {(start, end) for start, end, _ in declarations}
    groups = []
    for start, end, owner in mentions:
        previous = groups[-1] if groups else None
        join = text[previous['end']:start] if previous else ''
        if (previous and not previous['declaration'] and (start, end) not in declared
                and _SUBJECT_JOIN.fullmatch(join)):
            previous['owners'].append(owner)
            previous['end'] = end
            previous['choice'] |= bool(_SUBJECT_CHOICE.search(join))
        else:
            groups.append({'start': start, 'end': end, 'owners': [owner], 'choice': False,
                           'declaration': (start, end) in declared})
    for group in groups:
        marker = _DISTRIBUTIVE.match(text, group['end'])
        group['distributive'] = bool(marker) and not group['choice']
        group['quote_end'] = marker.end() if marker else group['end']
    return groups


def bind_component_scopes(text, requirements, unsupported, obligations, *, source_input_id,
                          external_spans=()):
    """Apply one ownership policy to every property and every extraction path.

    Only explicit source bounds authorize a material component selector. Names
    are references to that authority, never identities inferred from a nominal
    dimension or a generated model label. Unnamed assembly counts stay global.
    """
    authorities = {}
    declarations = []
    for record in requirements:
        if record['kind'] != 'bounding_box':
            continue
        label = record['derivation']['component_label']
        # "盒体外部长方体" is the envelope declaration of "盒体".
        label = re.sub(r'(?:外部|外形|外轮廓)(?:长方体)?$', '', label).lower()
        authorities.setdefault(label, []).append(record)
        declarations.append((*record['source_span'], label))
    names = sorted(authorities, key=len, reverse=True)
    pattern = re.compile('|'.join([*(re.escape(name) for name in names), _OWNER.pattern]), re.I)
    # A declaration is one identity. Its descriptive "长方体" must not
    # replace "盒体" as the owner inherited by the following hole clause.
    # A named box's floor is a property of that box, not a second
    # independently positioned component. Keep the entire owner phrase atomic.
    mentions = sorted([*declarations, *((m.start(), m.end(), _owner_identity(m.group()))
        for m in pattern.finditer(text)
        if not any(a <= m.start() and m.end() <= b for a, b, _ in declarations)
        and not any(a <= m.start() and m.end() <= b for a, b in external_spans))])
    owners = {name for _, _, name in mentions}
    subjects = _component_subjects(text, mentions, declarations)
    for subject in subjects:
        if len(subject['owners']) < 2:
            continue
        start, end = subject['start'], subject['quote_end']
        targets = [authorities[name][0]['requirement_id'] for name in subject['owners']
                   if len(authorities.get(name, [])) == 1]
        obligation = {'obligation_id': f'o-{source_input_id}-{start}-{end}-component_distribution',
            'source_input_id': source_input_id, 'source_span': [start, end],
            'source_quote': text[start:end], 'category': 'component_distribution',
            'component_refs': targets, 'property_refs': []}
        subject['obligation'] = obligation
        obligations.append(obligation)
    expanded = []
    for record in requirements:
        expanded.append(record)
        if record['kind'] in {'bounding_box', 'solid_count'}:
            if record['kind'] == 'solid_count':
                previous = [s for s in subjects if s['end'] <= record['source_span'][0]]
                if previous and len(previous[-1]['owners']) > 1 and not re.search(
                        r'[，,。；;\n]', text[previous[-1]['end']:record['source_span'][0]]):
                    unsupported.append({'source_span': [previous[-1]['start'], record['source_span'][1]],
                                        'reason': 'component_solid_count_unbound'})
            continue
        start, end = record['source_span']
        previous = [s for s in subjects if s['end'] <= start
                    or (s['start'] == start and s['end'] <= end)]
        if not previous:
            if authorities:
                unsupported.append({'source_span': [start, end], 'reason': 'component_scope_unbound'})
            continue
        subject = previous[-1]
        owner_start, owner_end = subject['start'], subject['end']
        if re.search(r'整体|总共|全模型|\b(?:overall|global|total)\b', text[owner_end:start], re.I):
            continue
        if len(subject['owners']) > 1:
            obligation = subject['obligation']
            obligation['property_refs'].append(record['requirement_id'])
            if (not subject['distributive'] or len(set(subject['owners'])) != len(subject['owners'])
                    or any(len(authorities.get(name, [])) != 1 for name in subject['owners'])):
                unsupported.append({'source_span': [owner_start, end], 'reason': 'component_scope_ambiguous'})
                continue
            if (record['scope'].get('frame', 'world') != 'world' or record['scope'].get('object_ref')
                    or any(_VOID_OWNER.search(name) for name in subject['owners'])):
                unsupported.append({'source_span': [owner_start, end], 'reason': 'component_frame_unbound'})
                continue
            expanded.pop()
            for owner in subject['owners']:
                authority = authorities[owner][0]
                expanded.append({**record,
                    'requirement_id': record['requirement_id'] + '-component-' + authority['requirement_id'],
                    'scope': {**record['scope'], 'frame': 'world',
                              'component_region': authority['scope']['component_region']},
                    'scope_source_refs': [authority['requirement_id']], 'component_label': owner,
                    'distribution_ref': obligation['obligation_id'],
                    'distribution_origin': record['requirement_id']})
            continue
        owner = subject['owners'][0]
        candidates = authorities.get(owner, [])
        reason = None
        if _VOID_OWNER.search(owner):
            reason = 'feature_dimension_scope_unbound'
        elif len(candidates) > 1:
            reason = 'component_scope_ambiguous'
        elif len(candidates) == 1:
            authority = candidates[0]
            if record['scope'].get('frame', 'world') != 'world' or record['scope'].get('object_ref'):
                reason = 'component_frame_unbound'
            else:
                record['scope'] = {**record['scope'], 'frame': 'world',
                    'component_region': authority['scope']['component_region']}
                record['scope_source_refs'] = [authority['requirement_id']]
                record['component_label'] = owner
        elif (owner not in _ROOT_OWNERS and not (
                owner in {'上盖', '盖板', 'lid', 'cover'} and len(owners) == 1
                and all(r['source_span'][0] >= mentions[0][0] for r in requirements))) or len(owners) > 1 or authorities:
            reason = 'component_scope_unbound'
        if reason:
            unsupported.append({'source_span': [owner_start, end], 'reason': reason})
    requirements[:] = expanded


def _group_counted_phrases(text, rows, counts):
    """Bind predicates to counted phrases, not to expected geometric matches.

    A subsequent counted phrase starts a new group. Its following property
    clauses end at the next counted phrase. A confirmed source starts another
    group only when it explicitly says it is additional; otherwise a repeated
    count may be a conflicting restatement of the original requirement.
    """
    ordered = sorted(counts, key=lambda r: r['source_span'])
    first_source = ordered[0]['source_input_id']
    sources = {r['source_input_id'] for r in rows}
    if len(sources) > 1:
        for source in sources - {first_source}:
            source_counts = [r for r in ordered if r['source_input_id'] == source]
            if not source_counts:
                return None
            start = source_counts[0]['source_span'][0]
            prefix = re.split(r'[。；;\n]', text[:start])[-1]
            if not re.search(r'另(?:外)?(?:有|增加|还有)?|额外|\b(?:additional|another|also)\b', prefix, re.I):
                return None
    groups = {c['requirement_id']: [c] for c in ordered}
    for row in rows:
        if row['kind'] == 'hole_count':
            continue
        candidates = [c for c in ordered if c['source_input_id'] == row['source_input_id']
                      and c['source_span'][0] <= row['source_span'][0]]
        if not candidates:
            return None
        count = candidates[-1]
        if not (count['source_span'][0] <= row['source_span'][0]
                and row['source_span'][1] <= count['source_span'][1]):
            clause_start = max(text.rfind(mark, 0, row['source_span'][0]) for mark in '，,。；;\n') + 1
            qualifier = text[max(clause_start, count['source_span'][1]):row['source_span'][0]]
            if not re.fullmatch(r'\s*(?:(?:的|of|with|and)\s*)?', qualifier, re.I):
                return None  # A global/other owner cannot be silently attached to the last group.
        # A property spanning another counted phrase is not group-local.
        if any(count['source_span'][0] < c['source_span'][0] < row['source_span'][1]
               for c in ordered if c['source_input_id'] == row['source_input_id']):
            return None
        groups[count['requirement_id']].append(row)
    if any(not any(r['kind'] == 'hole_diameter' for r in members) for members in groups.values()):
        return None
    return groups


def _incompatible_hole_constraints(rows):
    incompatible = []
    for kind in ('hole_count', 'hole_diameter', 'hole_depth'):
        constraints = [r for r in rows if r['kind'] == kind]
        lower, upper = float('-inf'), float('inf')
        for record in constraints:
            comparison = record['comparison']
            if comparison['op'] == 'eq':
                lo = comparison['value'] - comparison.get('tolerance_minus', 0)
                hi = comparison['value'] + comparison.get('tolerance_plus', 0)
            else:
                lo, hi = comparison.get('lower', float('-inf')), comparison.get('upper', float('inf'))
            lower, upper = max(lower, lo), min(upper, hi)
        if lower > upper:
            incompatible.extend(r['requirement_id'] for r in constraints)
    return incompatible


def inventory_hole_populations(text, requirements, obligations, *, source_input_id, cross_source_only=False):
    """Represent counted groups as quantified predicates over one whole scope.

    No new measurement selector is inferred from nominal diameter. Every
    atomic source requirement survives; the worker must partition the entire
    independent inventory. Unqualified contradictory assertions still fail
    before dispatch instead of becoming geometry repair targets.
    """
    scopes = {}
    for record in requirements:
        if record['kind'] not in {'hole_count', 'hole_diameter', 'hole_depth', 'hole_through'}:
            continue
        scope = {'frame': 'world', **record['scope']}
        key = json.dumps(scope, sort_keys=True, ensure_ascii=False)
        scopes.setdefault(key, []).append(record)
    for rows in scopes.values():
        source_refs = sorted({r['source_input_id'] for r in rows})
        if cross_source_only and len(source_refs) < 2:
            continue
        populations = [r for r in rows if r['kind'] == 'hole_count']
        groups = _group_counted_phrases(text, rows, populations) if len(populations) > 1 else None
        if groups and any(r['kind'] == 'hole_position'
                and r['scope'].get('component_region') == rows[0]['scope'].get('component_region')
                and r['scope'].get('object_ref') == rows[0]['scope'].get('object_ref') for r in requirements):
            groups = None  # Group-local positions need their own quantified coordinates, not a global position check.
        if groups:
            for group, members in groups.items():
                for record in members:
                    record['hole_group'] = {'schema_version': 'hole-group.v1', 'group_id': group}
        if not populations:
            populations = [r for r in rows if r['kind'] == 'hole_through']
        incompatible = [ref for members in (groups.values() if groups else [rows])
                        for ref in _incompatible_hole_constraints(members)]
        if not populations and not incompatible:
            continue
        start = min(r['source_span'][0] for r in rows)
        end = max(r['source_span'][1] for r in rows)
        obligations.append({'obligation_id': f'o-{source_input_id}-{start}-{end}-hole_population-'
                + rows[0]['requirement_id'],
            'source_input_id': source_input_id, 'source_span': [start, end], 'source_quote': text[start:end],
            'source_refs': source_refs,
            'category': 'hole_population', 'scope': rows[0]['scope'],
            'population_refs': [r['requirement_id'] for r in populations],
            'incompatible_constraint_refs': incompatible,
            'group_refs': list(groups or {}),
            'selector_required': (len(populations) > 1 and not groups) or bool(incompatible)})
