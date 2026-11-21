"""Separate sourced physical limitations from a model's unsolicited disclaimer.

This policy applies only before a new contract freezes. Unknown limitations
remain blocking; it never removes a geometric condition from the source audit.
"""
import re


_PRINT = re.compile(r'(?<![A-Za-z0-9_])FDM(?![A-Za-z0-9_])(?:[ \t]+PLA(?![A-Za-z0-9_]))?(?:打印)?|(?<![A-Za-z0-9_])PLA(?![A-Za-z0-9_])(?:打印)?|3D[ \t]*打印|\b3D[ -]printing\b', re.I)
_WATER = re.compile(r'防水|防尘|\bwaterproof\b|\bdustproof\b|\bIP[0-9X]{2}\b', re.I)
_PHYSICAL_SPEC = re.compile(r'(?:强度|承重|承载|载荷)(?!传感器)|制造性能|材料性能|耐温|(?:工作|使用|环境|最高|最低)温度|无(?:需)?支撑|保证.*打印|\bstrength\b|\bload\b|support[- ]free|\b(?:manufacturing|printing)\s+performance\b|\bprintability\b', re.I)
# Recognize complete disclaimer sentences, not the mere occurrence of a
# physical word. Mixed or unknown limitations remain blocking. In particular,
# "waterproof performance AND lid geometry cannot be verified" is not a note.
_WATER_SUBJECT = r'(?:防水|防尘)(?:[、，,/和及与]*(?:防水|防尘))*(?:等)?(?:功能性)?(?:防护)?(?:性能|等级)?'
_WATER_LIMIT = re.compile(_WATER_SUBJECT + r'(?:'
    r'(?:无法|不能|尚不能)(?:仅|单独)?(?:通过|由|用)(?:CAD|模型|几何(?:尺寸)?)(?:确认|验证|保证)|'
    r'(?:需|需要|必须|尚需)(?:通过)?(?:实际|实物)(?:测试|试验|验证)(?:确认|验证)?)', re.I)
_PRINT_LIMIT = re.compile(r'(?:FDM)?(?:PLA)?打印(?:工艺参数|工艺|参数|过程|成品性能|性能|强度)'
    r'(?:[（(](?:温度|速度|支撑|层高|填充率)(?:[、，,/和及与]*(?:温度|速度|支撑|层高|填充率))*[）)])?'
    r'(?:(?:及|和|与)(?:成品|材料)?(?:强度|性能|打印质量))?'
    r'(?:需|需要|必须|尚需)(?:通过)?(?:实际打印|切片软件|实物试验|实际测试)'
    r'(?:(?:或|及|和)(?:实际打印|切片软件|实物试验|实际测试))*(?:验证|确认|测试)', re.I)
_MESSAGES = {
    'manufacturing_process': '打印工艺参数、材料与实际成品性能尚未通过切片或实物试验验证。',
    'water_protection': '防水、防尘性能及防护等级尚未通过实物试验验证。',
    'physical_performance': '用户要求的承载、材料或制造性能尚未通过相应工程分析或实物试验验证。',
}

# Classify complete source clauses, not keyword occurrences. A mixed clause
# containing an unknown geometric condition must still reach the coverage audit.
_CONTEXT_CLAUSE = re.compile(r'[^。；;\n:：.]+(?:\.(?=\d)[^。；;\n:：.]*)*')
_DOCUMENT_SUBJECT = r'(?:(?:the|all)\s+)?(?:linear\s+)?(?:dimensions|coordinates|measurements|values)(?:\s+and\s+(?:coordinates|dimensions|measurements|values))*'
_DIMENSION_UNITS = re.compile(
    _DOCUMENT_SUBJECT + r'(?:\s+(?:below|above))?\s+(?:are\s+)?(?:given\s+|expressed\s+)?in\s+'
    r'(?P<unit>(?:(?:source(?:\s+design)?|design|drawing)\s+)?units|mm|cm|m|millimeters|centimeters|meters)', re.I)
_DOCUMENT_CONTEXT = re.compile(
    _DOCUMENT_SUBJECT + r'\s+(?:below|above|that follow)\s+(?:describe|define|specify)\s+(?:this|the)\s+(?:model|design)|'
    r'angles\s+(?:are\s+)?in\s+(?:degrees|radians)', re.I)
_GEOMETRY_SCOPE = re.compile(
    r'(?:only\s+)?(?:evaluate|verify|check|validate|assess)\s+(?:only\s+)?(?:the\s+)?'
    r'(?:geometry|geometric\s+(?:structure|shape))(?:\s+only)?|'
    r'(?:本次|此次)?(?:仅|只)(?:验收|验证|检查)(?:概念)?几何(?:结构|形状)?', re.I)
_INPUT_RETENTION = re.compile(
    r'(?:preserve|keep|retain)\s+(?:(?:all|the)\s+)?(?:specified|explicit|provided|given|requested)\s+'
    r'(?:numeric\s+)?(?:dimensions?|diameters?|radii|coordinates?)'
    r'(?:\s*(?:and|/|,)\s*(?:(?:specified|explicit|provided|given|requested|numeric)\s+)*'
    r'(?:dimensions?|diameters?|radii|coordinates?))*', re.I)
_PHYSICAL_DISCLAIMER = re.compile(
    r'(?:manufacturing|printing|physical|waterproof|dustproof)(?:\s+performance|\s+properties|\s+rating)?|printability|strength', re.I)
_DISCLAIMER_PREDICATE = re.compile(
    r'(?:(?:is|are|has|have)\s+)?(?:not\s+(?:been\s+)?(?:certified|verified|validated|tested)|unverified|uncertified)|'
    r'(?:requires?|needs?|awaits?)\s+(?:physical|real[- ]world)\s+(?:testing|tests|validation)|'
    r'(?:is|are)\s+left\s+for\s+(?:physical|real[- ]world)\s+(?:testing|tests|validation)', re.I)

# Parse a complete physical subject and predicate in Chinese as well as English.
# Parenthetical lists are physical subjects too; unknown/geometric terms cannot
# be swallowed by a wildcard and converted into a nonblocking notice.
_PHYSICAL_SUBJECT_ZH = r'(?:(?:制造|打印|物理|材料|成品)(?:性能|强度)|承载(?:能力)?|承重(?:能力)?|材料|工艺(?:可行性)?)'
_PHYSICAL_LIST_ZH = _PHYSICAL_SUBJECT_ZH + r'(?:[、，,/和及与]+' + _PHYSICAL_SUBJECT_ZH + r')*'
_PHYSICAL_SCOPE_ZH = r'不在(?:本次|此次|当前)?(?:CAD)?几何(?:验证|验收)(?:范围)?内'
_PHYSICAL_TEST_ZH = r'(?:工程分析|仿真分析|实物试验|实物测试|实际测试|实际打印)'
_PHYSICAL_UNVERIFIED_ZH = r'(?:尚|还)?未(?:通过' + _PHYSICAL_TEST_ZH + r'(?:[、，,/或和及与]+' + _PHYSICAL_TEST_ZH + r')*)?(?:验证|确认|认证|测试)'
_PHYSICAL_DISCLAIMER_ZH = re.compile(
    _PHYSICAL_LIST_ZH + r'(?:[（(]' + _PHYSICAL_LIST_ZH + r'[）)])?'
    r'(?:' + _PHYSICAL_UNVERIFIED_ZH + r'(?:[，,]' + _PHYSICAL_SCOPE_ZH + r')?|' + _PHYSICAL_SCOPE_ZH + r')', re.I)


def non_geometric_clauses(text):
    """Return source spans whose complete predicate describes input or scope."""
    result = []
    for match in _CONTEXT_CLAUSE.finditer(text):
        raw = match.group()
        clause = raw.strip()
        start = match.start() + len(raw) - len(raw.lstrip())
        end = start + len(clause)
        subject = _PHYSICAL_DISCLAIMER.match(clause)
        physical = bool(subject and _DISCLAIMER_PREDICATE.fullmatch(clause[subject.end():].strip())
                        or _PHYSICAL_DISCLAIMER_ZH.fullmatch(re.sub(r'\s+', '', clause)))
        units = _DIMENSION_UNITS.fullmatch(clause)
        if units or physical or any(pattern.fullmatch(clause) for pattern in
                           (_DOCUMENT_CONTEXT, _GEOMETRY_SCOPE, _INPUT_RETENTION)):
            result.append({'source_span': [start, end], 'physical_disclaimer': physical,
                           **({'declared_unit': units['unit']} if units else {})})
    return result


def verification_intent(sources):
    grouped, required, diagnostics = {}, [], []
    for source in sources:
        disclaimers = [item['source_span'] for item in non_geometric_clauses(source['text'])
                       if item['physical_disclaimer']]
        for category, pattern in [('manufacturing_process', _PRINT), ('water_protection', _WATER),
                                  ('physical_performance', _PHYSICAL_SPEC)]:
            for match in pattern.finditer(source['text']):
                ref = {'reference_id':f"v-{source['source_input_id']}-{match.start()}-{match.end()}-{category}",
                    'source_input_id':source['source_input_id'], 'source_kind':source['source_kind'],
                    'source_span':list(match.span()), 'source_quote':match.group()}
                grouped.setdefault(category, []).append(ref)
                deferred = any(a <= match.start() and match.end() <= b for a, b in disclaimers)
                if category in {'water_protection','physical_performance'} and source.get('execution_scope') != 'concept_geometry' and not deferred:
                    required.append(_MESSAGES[category])
                    diagnostics.append({'source_input_id':source['source_input_id'], 'source_span':list(match.span()),
                        'reason':'physical_performance_not_verified'})
    return {'notices':[{'schema_version':'verification-notice.v1','category':category,
                       'source_refs':refs,'message':_MESSAGES[category],'status':'unverified'}
                      for category, refs in grouped.items()],
            'required_limits':list(dict.fromkeys(required)), 'unsupported':diagnostics}


def bind_verification_intent(proposal, sources):
    intent = verification_intent(sources)
    limits = []
    text = '\n'.join(s['text'] for s in sources)
    for limit in proposal.get('verification_limits', []):
        # Only recognized statements about physical certification are eligible.
        # A groove, wall or hole limitation cannot become an informational note.
        sentence = re.sub(r'\s+', '', limit).strip('。.!！')
        water_notice = _WATER_LIMIT.fullmatch(sentence)
        print_notice = _PRINT_LIMIT.fullmatch(sentence)
        clause = limit.strip().rstrip('。.!！')
        contexts = non_geometric_clauses(clause)
        physical_notice = (len(contexts) == 1 and contexts[0]['physical_disclaimer']
                           and contexts[0]['source_span'] == [0, len(clause)])
        if physical_notice or water_notice or (print_notice and _PRINT.search(text) and not _PHYSICAL_SPEC.search(text)):
            continue  # Replaced by the server's sourced notices/required limits below.
        limits.append(limit)
    proposal['verification_limits'] = list(dict.fromkeys([*limits, *intent['required_limits']]))
    if intent['notices']:
        proposal['verification_notices'] = intent['notices']
    else:
        proposal.pop('verification_notices', None)
