import type { RequirementBasis, RequirementIntakePreview } from '../types/requirements';

/** Preserve source context. This is intake assistance, not engineering validation. */
export function initialRequirementBasis(objective: string, preview?: RequirementIntakePreview): RequirementBasis {
  const affirmative = objective.replace(/(?:不包含|不创建|不添加|不需要|无需|不要|禁止|\bwithout\b|\bno\b)(?:(?!但|不过|然而|\bbut\b)[^，,。；;\n])*/gi, '');
  const physical = /适配|配合|装配|手机壳|保护壳|卡扣|夹具|夹在|套在|轴承|螺纹|phone\s*case|fit\s*(?:to|for)|mating/i.test(affirmative);
  // Numerical meaning and completeness come from the server's source binder.
  // No second grammar here may erase Chinese units or implicit shape rules.
  const dimensions = preview?.dimensions ?? '';
  const complete = preview?.complete ?? false;
  return {schema_version:'requirement-basis.v1', target:objective.slice(0,1000), purpose:'',
    design_scope:physical ? 'physical_fit' : 'geometry',
    source_kind: !physical && complete ? 'user_specification' : 'none',
    source_reference: !physical && complete ? '用户需求中指定的设计尺寸' : '',
    dimensions, fit_notes:'', concept_acknowledged:false};
}

export function requirementNotice(basis?: RequirementBasis | null): string {
  if (basis?.concept_acknowledged) return basis.source_kind === 'none'
    ? '概念几何；未提供的尺寸、实物适配及性能均未验证。'
    : '按指定尺寸生成概念几何；实物适配及性能未验证。';
  if (basis?.design_scope === 'geometry') return basis.source_kind === 'none'
    ? '尺寸尚未明确；概念模型中的默认尺寸需要后续确认。' : '按用户指定尺寸建模；几何与制造检查分别记录。';
  return !basis || basis.source_kind === 'none' ? '概念外形，适配未验证。' : '依据已记录，适配未验证。';
}
