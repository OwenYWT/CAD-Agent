import type { EngineeringField } from '../types/engineeringTask';

export function validateEngineeringField(field: EngineeringField): EngineeringField {
  const nodes = field.positions_mm?.length;
  const vectors = (values: number[][]) => Array.isArray(values) && values.length === nodes && values.every(v => Array.isArray(v) && v.length === 3 && v.every(Number.isFinite));
  if (field.schema_version !== 'cad-fea-field.v1' || !nodes || nodes > 20000 || !vectors(field.positions_mm) || !vectors(field.displacements_mm)
    || !Array.isArray(field.von_mises_mpa) || field.von_mises_mpa.length !== nodes || !field.von_mises_mpa.every(v => Number.isFinite(v) && v >= 0)
    || !Array.isArray(field.triangles) || !field.triangles.length || field.triangles.length > 800000
    || !field.triangles.every(t => Array.isArray(t) && t.length === 3 && t.every(i => Number.isInteger(i) && i >= 0 && i < nodes))) {
    throw new Error('有限元场数据不完整或超过显示预算');
  }
  return field;
}
