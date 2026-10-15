/** Project recorded engineering evidence; never infer a pass or a missing value. */
type RecordValue = Record<string, unknown>;
function record(value: unknown): RecordValue {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {};
}

export function engineeringMeasurements(report: RecordValue) {
  const acceptance = record(report.acceptance);
  const contract = record(report.acceptance_contract);
  const checks = Array.isArray(contract.checks) ? contract.checks.map(record) : [];
  return (Array.isArray(acceptance.evidence) ? acceptance.evidence : []).map(record).map(item => {
    const check = checks.find(c => c.check_id === item.check_id);
    const kind = check?.kind;
    const unit = kind === 'volume' ? 'mm³' : ['solid_count', 'hole_count', 'void_connected'].includes(String(kind)) ? '' : kind ? 'mm' : '';
    const scope = record(check?.scope);
    const expected = kind === 'hole_position' ? scope.centers_mm : check?.nominal;
    const comparison = record(check?.comparison);
    const v2 = contract.schema_version === 'engineering-acceptance.v2';
    const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
    let expectedText = expected === undefined || expected === null ? '未记录' : `${JSON.stringify(expected)} ${unit}`.trim();
    if (v2 && kind !== 'hole_position') {
      if (comparison.op === 'eq' && finite(comparison.value)) expectedText = `${comparison.value} ${unit}`.trim();
      else if (comparison.op === 'ge' && finite(comparison.lower)) expectedText = `≥ ${comparison.lower} ${unit}`.trim();
      else if (comparison.op === 'le' && finite(comparison.upper)) expectedText = `≤ ${comparison.upper} ${unit}`.trim();
      else if (comparison.op === 'range' && finite(comparison.lower) && finite(comparison.upper)) expectedText = `[${comparison.lower}, ${comparison.upper}] ${unit}`.trim();
    }
    const measured = Array.isArray(item.measured) ? item.measured.filter(v => typeof v === 'number' && Number.isFinite(v)) : [];
    return {
      id: String(item.check_id ?? ''),
      description: typeof check?.description === 'string' ? check.description : String(item.check_id ?? '未命名检查'),
      expected: expectedText,
      measured: measured.length ? `${measured.join('、')} ${unit}`.trim() : '无量测值',
      measuredLabel: kind === 'hole_position' ? '孔轴位置偏差' : '实测',
      tolerance: v2 ? comparison.op === 'eq' && finite(comparison.tolerance_minus) && finite(comparison.tolerance_plus)
        ? `−${comparison.tolerance_minus} / +${comparison.tolerance_plus}` : undefined
        : kind === 'volume' ? check?.tolerance_mm3 : check?.tolerance_mm,
      method: typeof item.method === 'string' ? item.method : '未记录',
      outcome: item.outcome === 'passed' ? '通过' : item.outcome === 'failed' ? '未通过' : '未验证',
    };
  });
}
