"""Source-owned requirements for rectangular body/lid/tab/groove assemblies.

This bounded capability inventories every clause of an explicitly declared
assembly. Unsupported wording stays unresolved; a model cannot assign roles,
authorize an envelope convention, or hide a condition in a dimension label.
"""
import re
from app.validation.quantities import NUMBER as _NUMBER, number

from app.contracts.assembly_measurements import assembly_kind

_DECLARATION = r'(?:外壳|壳体)(?:由|包括)盒体(?:和|与|及)上盖(?:组成)?'
_ENVELOPE = r'(?:总体|整体)尺寸包含装配后的盒体和上盖[，,]不包含安装耳'
_DIMENSIONS = {
    r'(?:外壳|壳体)(?:总体|整体|总)?长度':'envelope_length',
    r'(?:外壳|壳体)(?:总体|整体|总)?宽度':'envelope_width',
    r'(?:外壳|壳体)(?:总体|整体|总)?高度':'envelope_height',
    r'盒体(?:外壁|侧壁)(?:厚度|厚)':'side_wall_thickness',
    r'盒体(?:底板|底面)(?:厚度|厚)':'floor_thickness',
    r'上盖(?:厚度|厚)':'lid_thickness',
    r'(?:外部)?安装耳长度':'tab_length', r'(?:外部)?安装耳宽度':'tab_width',
    r'(?:外部)?安装耳(?:厚度|厚)':'tab_thickness',
    r'(?:M\d+(?:\.\d+)?)?安装孔(?:直径|孔径)':'mounting_hole_diameter',
    r'密封槽宽度':'groove_width',r'密封槽深度':'groove_depth',
    r'(?:线缆)?接头孔(?:直径|孔径)':'connector_diameter',
}
_RELATIONSHIPS = [
    (_DECLARATION,['body_lid_fit']),
    (r'(?:M\d+(?:\.\d+)?)?安装孔(?:设置在|位于)(?:外壳|壳体)四角的(?:外部)?安装耳上',
     ['tabs_at_corners','mounting_holes_on_tabs']),
    (r'(?:避免|禁止|不得|不能)安装孔(?:贯穿|连通)(?:内部)?(?:防水)?(?:腔|内腔|腔体)',
     ['mounting_holes_isolated']),
    (r'盒体(?:与|和)上盖之间(?:设置|设有)密封槽',['groove_at_interface']),
    (r'侧面(?:预留|设置)(?:线缆)?接头孔',['connector_through']),
    (r'(?:线缆)?接头孔位于(?:外壳|壳体)短侧面(?:中央|中心)',['connector_short_side_centered']),
]
_INTRO = re.compile(r'(?:\d+[.、]?)?(?:请)?(?:设计|创建|生成)(?:一|1)个带M\d+安装孔的矩形(?:防水)?(?:传感器)?外壳')
_MANUFACTURING = re.compile(r'适合(?:FDM\s*PLA|FDM|PLA)打印',re.I)


def extract_rectangular_assembly_sources(objective, sources):
    """Return None for other capabilities; never reinterpret frozen contracts."""
    if (not any(re.search(_DECLARATION,s['text']) for s in sources)
            or not any(re.search(_ENVELOPE,s['text']) for s in sources)):
        return None
    requirements, obligations, unsupported = [], [], []
    envelope_refs = []

    def add(source,start,end,measurement,value):
        identity=f"r-{source['source_input_id']}-{start}-{end}-assembly-{measurement}"
        kind=assembly_kind(measurement)
        requirements.append({'requirement_id':identity,'source_input_id':source['source_input_id'],
            'source_kind':source['source_kind'],'source_span':[start,end],
            'source_quote':source['text'][start:end],'property':measurement,'kind':kind,
            'comparison':{'op':'eq','value':float(value),'unit':'mm' if kind=='assembly_dimension' else 'count'},
            'scope':{'assembly_measurement':measurement},
            'derivation':{'rule':'rectangular_assembly_roles.v1'}})
        return identity

    for source in sources:
        text=source['text']
        if source['source_kind'] not in {'user','confirmed'} or text not in objective:
            raise ValueError('assembly source is not saved submitted input')
        # The inclusion convention is a compound clause: retain both halves.
        envelope=list(re.finditer(_ENVELOPE,text))
        spans=[(m.start(),m.end()) for m in envelope]
        for m in envelope:
            ref=add(source,*m.span(),'envelope_excludes_tabs',1)
            envelope_refs.append(ref)
        for clause in re.finditer(r'[^，,。；;\n]+',text):
            start,end=clause.span()
            if any(a<=start and end<=b for a,b in spans):
                continue
            raw=clause.group()
            start+=len(raw)-len(raw.lstrip());end-=len(raw)-len(raw.rstrip())
            phrase=text[start:end]
            refs=[]
            if not phrase:
                continue
            for label,measurement in _DIMENSIONS.items():
                match=re.fullmatch(rf'{label}\s*(?:为|是|[:：=])?\s*(?P<value>{_NUMBER})\s*(?P<unit>mm|毫米|cm|厘米)',phrase,re.I)
                if match:
                    try:
                        value=number(match['value'])*(10 if match['unit'].lower() in {'cm','厘米'} else 1)
                    except ValueError:
                        continue  # Keep the complete original clause unresolved.
                    refs=[add(source,start,end,measurement,value)]
                    break
            if not refs:
                count=re.fullmatch(rf'(?:M\d+(?:\.\d+)?)?安装孔(?:数量|数目|数)\s*(?:为|[:：=])?\s*(?P<n>\d+)\s*个?',phrase)
                centered=re.fullmatch(r'(?:M\d+(?:\.\d+)?)?安装孔位于(?P<n>四|\d+)个(?:外部)?安装耳(?:中心|中央)',phrase)
                if count:
                    refs=[add(source,start,end,'mounting_hole_count',int(count['n']))]
                elif centered:
                    n=4 if centered['n']=='四' else int(centered['n'])
                    refs=[add(source,start,end,'tab_count',n),add(source,start,end,'mounting_holes_centered',1),
                          add(source,start,end,'mounting_holes_on_tabs',1)]
            if not refs:
                for pattern,measurements in _RELATIONSHIPS:
                    if re.fullmatch(pattern,phrase):
                        refs=[add(source,start,end,m,1) for m in measurements]
                        break
            context_only=bool(_INTRO.fullmatch(phrase) or _MANUFACTURING.fullmatch(phrase))
            item={'obligation_id':f"o-{source['source_input_id']}-{start}-{end}-assembly-clause",
                  'source_input_id':source['source_input_id'],'source_span':[start,end],
                  'source_quote':phrase,'category':'assembly_clause',
                  'requirement_refs':refs,'status':'bound' if refs or context_only else 'unresolved'}
            obligations.append(item)
            if not refs and not context_only:
                unsupported.append({'source_input_id':source['source_input_id'],'source_span':[start,end],
                    'reason':'assembly_clause_unbound','obligation_id':item['obligation_id']})
    if not envelope_refs:
        source=sources[0]
        unsupported.append({'source_input_id':source['source_input_id'],'source_span':[0,len(source['text'])],
                            'reason':'assembly_envelope_scope_unconfirmed'})
    for record in requirements:
        if record['property'].startswith('envelope_'):
            record['scope_source_refs']=envelope_refs
    # Contradictory duplicate labels cannot become selective geometry filters.
    for key in {r['property'] for r in requirements}:
        rows=[r for r in requirements if r['property']==key]
        if len({r['comparison']['value'] for r in rows})>1:
            unsupported.extend({'source_input_id':r['source_input_id'],'source_span':r['source_span'],
                'reason':'assembly_quantity_conflict'} for r in rows)
    return {'schema_version':'requirement-extraction.v1','requirements':requirements,
            'unsupported':unsupported,'obligations':obligations}
