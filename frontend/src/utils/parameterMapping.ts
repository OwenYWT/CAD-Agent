import type { CADParameter, CriticalDimension, DesignBrief } from "../types";

export interface CriticalParameterMatch {
  parameter: CADParameter;
  dimension: CriticalDimension | null;
}

export interface EngineeringParameterGroups {
  critical: CriticalParameterMatch[];
  standard: CADParameter[];
}

const PARAMETER_LABELS: Record<string, string> = {
  width: "宽度",
  height: "高度",
  depth: "深度",
  length: "长度",
  thickness: "厚度",
  radius: "半径",
  diameter: "直径",
};

export function parameterDisplayLabel(
  name: string,
  supplied?: string | null,
) {
  const normalized = name.trim().toLowerCase();
  const candidate = supplied?.trim();
  if (
    candidate
    && candidate.toLowerCase() !== normalized
    && !Object.prototype.hasOwnProperty.call(
      PARAMETER_LABELS,
      candidate.toLowerCase(),
    )
  ) {
    return candidate;
  }
  return PARAMETER_LABELS[normalized] || candidate || name;
}

function normalizeTokenText(value: string) {
  return value
    .toLowerCase()
    .replace(/[_-]+/g, " ")
    .replace(/\b(mm|cm|m|deg|rad|count|number|param|parameter)\b/g, " ")
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function tokens(value: string) {
  return normalizeTokenText(value)
    .split(" ")
    .filter((token) => token.length >= 2);
}

function scoreMatch(parameter: CADParameter, dimension: CriticalDimension) {
  const parameterText = normalizeTokenText(
    [parameter.name, parameter.display_name, parameter.comment || ""].join(" "),
  );
  const dimensionText = normalizeTokenText(dimension.name);
  if (!parameterText || !dimensionText) return 0;
  if (parameterText.includes(dimensionText) || dimensionText.includes(parameterText)) return 100;

  const parameterTokens = new Set(tokens(parameterText));
  const dimensionTokens = tokens(dimensionText);
  if (!dimensionTokens.length) return 0;
  const matched = dimensionTokens.filter((token) => parameterTokens.has(token)).length;
  return matched / dimensionTokens.length;
}

function findCriticalDimension(parameter: CADParameter, brief: DesignBrief | null) {
  if (!brief?.critical_dimensions?.length) return null;
  const matches = brief.critical_dimensions
    .map((dimension) => ({ dimension, score: scoreMatch(parameter, dimension) }))
    .filter((match) => match.score >= 0.5)
    .sort((a, b) => b.score - a.score);
  return matches[0]?.dimension || null;
}

export function splitEngineeringParameters(
  parameters: CADParameter[],
  brief: DesignBrief | null,
): EngineeringParameterGroups {
  const critical: CriticalParameterMatch[] = [];
  const standard: CADParameter[] = [];

  for (const parameter of parameters) {
    const dimension = findCriticalDimension(parameter, brief);
    if (dimension) {
      critical.push({ parameter, dimension });
    } else {
      standard.push(parameter);
    }
  }

  return { critical, standard };
}
