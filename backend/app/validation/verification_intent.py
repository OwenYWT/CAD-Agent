"""Separate sourced physical limitations from a model's unsolicited disclaimer.

This policy applies only before a new contract freezes. Unknown limitations
remain blocking; it never removes a geometric condition from the source audit.
"""
import re


_PRINT = re.compile(r'(?<![A-Za-z0-9_])FDM(?![A-Za-z0-9_])(?:[ \t]+PLA(?![A-Za-z0-9_]))?(?:打印)?|(?<![A-Za-z0-9_])PLA(?![A-Za-z0-9_])(?:打印)?|3D[ \t]*打印|\b3D[ -]printing\b', re.I)
_WATER = re.compile(r'防水|防尘|\bwaterproof\b|\bdustproof\b|\bIP[0-9X]{2}\b', re.I)
_PHYSICAL_SPEC = re.compile(r'(?:强度|承重|载荷)(?!传感器)|耐温|(?:工作|使用|环境|最高|最低)温度|无(?:需)?支撑|保证.*打印|\bstrength\b|\bload\b|support[- ]free', re.I)
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


def verification_intent(sources):
    grouped, required, diagnostics = {}, [], []
    for source in sources:
        for category, pattern in [('manufacturing_process', _PRINT), ('water_protection', _WATER),
                                  ('physical_performance', _PHYSICAL_SPEC)]:
            for match in pattern.finditer(source['text']):
                ref = {'reference_id':f"v-{source['source_input_id']}-{match.start()}-{match.end()}-{category}",
                    'source_input_id':source['source_input_id'], 'source_kind':source['source_kind'],
                    'source_span':list(match.span()), 'source_quote':match.group()}
                grouped.setdefault(category, []).append(ref)
                if category in {'water_protection','physical_performance'} and source.get('execution_scope') != 'concept_geometry':
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
        if water_notice or (print_notice and _PRINT.search(text) and not _PHYSICAL_SPEC.search(text)):
            continue  # Replaced by the server's sourced notices/required limits below.
        limits.append(limit)
    proposal['verification_limits'] = list(dict.fromkeys([*limits, *intent['required_limits']]))
    if intent['notices']:
        proposal['verification_notices'] = intent['notices']
    else:
        proposal.pop('verification_notices', None)
