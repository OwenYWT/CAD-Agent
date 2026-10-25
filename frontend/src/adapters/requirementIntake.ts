import type { RequirementBasis } from '../types/requirements';

/** Preserve source context. This is intake assistance, not engineering validation. */
export function initialRequirementBasis(objective: string): RequirementBasis {
  const affirmative = objective.replace(/(?:不包含|不创建|不添加|不需要|无需|不要|禁止|\bwithout\b|\bno\b)(?:(?!但|不过|然而|\bbut\b)[^，,。；;\n])*/gi, '');
  const physical = /适配|配合|装配|手机壳|保护壳|卡扣|夹具|夹在|套在|轴承|螺纹|phone\s*case|fit\s*(?:to|for)|mating/i.test(affirmative);
  const ambiguous = /或者|还是|待定|大约|左右|\d\s*或\s*\d|\bor\b|approximately/i.test(objective);
  const quantities = Array.from(objective.matchAll(/\d+(?:\.\d+)?(?:\s*[×xX*]\s*\d+(?:\.\d+)?){0,2}\s*(?:mm|cm|毫米|厘米)/gi));
  const declaredUnit = /(?:单位|units?)\s*[:：=]\s*(?:mm|cm|毫米|厘米)(?=$|[\s，,。；;])/i.test(objective);
  // A list of bare numbers loses which part, property and comparison each
  // value belongs to. Keep the complete source; never silently truncate it.
  const dimensions = !ambiguous && objective.length <= 1500 && (quantities.length || declaredUnit)
    ? objective.trim() : '';
  const complete = !!dimensions && (/\d+(?:\.\d+)?\s*[×xX*]\s*\d+(?:\.\d+)?\s*[×xX*]\s*\d/.test(dimensions)
    || /球|sphere/i.test(objective) && /直径|半径|diameter|radius/i.test(objective)
    || /圆柱|cylinder/i.test(objective) && /直径|半径|diameter|radius/i.test(objective) && /高|height/i.test(objective) && (declaredUnit || quantities.length >= 2)
    || /板|plate|长方体|盒|外壳|box/i.test(objective) && /长|length/i.test(objective) && /宽|width/i.test(objective) && /厚|高|thickness|height/i.test(objective) && (declaredUnit || quantities.length >= 3));
  return {schema_version:'requirement-basis.v1', target:objective.slice(0,1000), purpose:'',
    design_scope:physical ? 'physical_fit' : 'geometry',
    source_kind: !physical && complete ? 'user_specification' : 'none',
    source_reference: !physical && complete ? '用户需求中指定的设计尺寸' : '',
    dimensions, fit_notes:'', concept_acknowledged:false};
}

export function requirementNotice(basis?: RequirementBasis | null): string {
  if (basis?.design_scope === 'geometry') return basis.source_kind === 'none'
    ? '尺寸尚未明确；概念模型中的默认尺寸需要后续确认。' : '按用户指定尺寸建模；几何与制造检查分别记录。';
  return !basis || basis.source_kind === 'none' ? '概念外形，适配未验证。' : '依据已记录，适配未验证。';
}
