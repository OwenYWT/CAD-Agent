import type { Parameter } from "../types/engineering";

export function parameterValueError(parameter: Parameter, value: number) {
  if (!Number.isFinite(value)) return "请输入有效数字";
  if (parameter.min !== undefined && value < parameter.min) {
    return `不能小于 ${parameter.min}${parameter.unit}`;
  }
  if (parameter.max !== undefined && value > parameter.max) {
    return `不能大于 ${parameter.max}${parameter.unit}`;
  }
  return null;
}
