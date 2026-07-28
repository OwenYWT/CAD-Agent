import type { GenerationResult, ModelSnapshotDetail } from "../types";
import { parameterDisplayLabel } from "../utils/parameterMapping.ts";
import type {
  ChangeSet,
  GeometryMetricChange,
  ParameterChange,
} from "../types/engineering";


interface ComparableParameter {
  label: string;
  value: number;
  unit: string;
}

function parametersFromResult(
  result: GenerationResult,
): Map<string, ComparableParameter> {
  const values = new Map<string, ComparableParameter>();
  const rich = result.parameters || [];
  for (const parameter of rich) {
    values.set(parameter.name, {
      label: parameterDisplayLabel(
        parameter.name,
        parameter.comment || parameter.display_name,
      ),
      value: parameter.value,
      unit: parameter.unit || "",
    });
  }
  if (rich.length) return values;
  for (const [name, parameter] of Object.entries(result.params || {})) {
    values.set(name, {
      label: parameterDisplayLabel(name, parameter.comment),
      value: parameter.value,
      unit: "",
    });
  }
  return values;
}

function parameterChanges(
  current: GenerationResult,
  base: GenerationResult | null,
): ParameterChange[] {
  if (!base) return [];
  const before = parametersFromResult(base);
  const after = parametersFromResult(current);
  const changes: ParameterChange[] = [];
  for (const [name, next] of after) {
    const previous = before.get(name);
    if (!previous || previous.value === next.value) continue;
    changes.push({
      parameterId: name,
      label: next.label,
      before: previous.value,
      after: next.value,
      unit: next.unit || previous.unit,
    });
  }
  return changes;
}

function metric(
  label: string,
  before: number | null | undefined,
  after: number | null | undefined,
  unit: string,
): GeometryMetricChange | null {
  if (
    typeof before !== "number" ||
    !Number.isFinite(before) ||
    typeof after !== "number" ||
    !Number.isFinite(after) ||
    before === after
  ) {
    return null;
  }
  return { label, before, after, unit };
}

function geometryEvidence(
  current: GenerationResult,
  base: GenerationResult | null,
): ChangeSet["geometry"] {
  if (!base?.validation || !current.validation) {
    return { status: "unknown", metrics: [] };
  }
  const metrics = [
    metric("体积", base.validation.volume, current.validation.volume, "mm³"),
    metric(
      "X 尺寸",
      base.validation.bounding_box
        ? base.validation.bounding_box.x_max - base.validation.bounding_box.x_min
        : null,
      current.validation.bounding_box
        ? current.validation.bounding_box.x_max - current.validation.bounding_box.x_min
        : null,
      "mm",
    ),
    metric(
      "Y 尺寸",
      base.validation.bounding_box
        ? base.validation.bounding_box.y_max - base.validation.bounding_box.y_min
        : null,
      current.validation.bounding_box
        ? current.validation.bounding_box.y_max - current.validation.bounding_box.y_min
        : null,
      "mm",
    ),
    metric(
      "Z 尺寸",
      base.validation.bounding_box
        ? base.validation.bounding_box.z_max - base.validation.bounding_box.z_min
        : null,
      current.validation.bounding_box
        ? current.validation.bounding_box.z_max - current.validation.bounding_box.z_min
        : null,
      "mm",
    ),
  ].filter((item): item is GeometryMetricChange => item !== null);
  return {
    status: metrics.length ? "changed" : "unchanged",
    metrics,
  };
}

function fileEvidence(
  current: GenerationResult,
  base: GenerationResult | null,
): ChangeSet["files"] {
  if (!base) return [];
  const before = base.files || {};
  const after = current.files || {};
  const formats = new Set([...Object.keys(before), ...Object.keys(after)]);
  const changes: ChangeSet["files"] = [];
  for (const format of formats) {
    if (before[format] === after[format]) continue;
    if (!before[format] && after[format]) {
      changes.push({
        format: format.toUpperCase(),
        kind: "added",
        afterUrl: after[format],
        evidence: "reference_only",
      });
      continue;
    }
    if (before[format] && !after[format]) {
      changes.push({
        format: format.toUpperCase(),
        kind: "removed",
        beforeUrl: before[format],
        evidence: "reference_only",
      });
      continue;
    }
    changes.push({
      format: format.toUpperCase(),
      kind: "replaced",
      beforeUrl: before[format],
      afterUrl: after[format],
      evidence: "reference_only",
    });
  }
  return changes;
}

function validationEvidence(
  result: GenerationResult,
): ChangeSet["validation"] {
  const verdict = result.inspect_report?.verdict;
  if (verdict) {
    const verdictLabel = {
      pass: "通过",
      warn: "有警告",
      fail: "未通过",
    }[verdict];
    const sourceLabel = {
      geometry_validator: "几何验证器",
    }[result.inspect_report?.source || ""]
      || result.inspect_report?.source
      || "未记录";
    return {
      status: verdict === "warn" ? "warning" : verdict,
      summary: `检查报告：${verdictLabel}，来源 ${sourceLabel}`,
    };
  }
  if (result.validation) {
    return {
      status: result.validation.is_watertight ? "pass" : "fail",
      summary: result.validation.is_watertight
        ? "几何验证记录为闭合网格。"
        : "几何验证记录为非闭合网格。",
    };
  }
  if (!result.success && result.error) {
    return {
      status: "fail",
      summary: `${result.error.type}：${result.error.message}`,
    };
  }
  return { status: "unknown", summary: "当前版本没有可用的验证证据。" };
}

function riskEvidence(
  result: GenerationResult,
  validation: ChangeSet["validation"],
): ChangeSet["risk"] {
  const reasons: string[] = [];
  if (result.error) reasons.push(`${result.error.type}：${result.error.message}`);
  const repairs = result.repair_history || [];
  if (repairs.length) reasons.push(`本次执行包含 ${repairs.length} 条真实修复记录。`);
  if (validation.status === "fail") {
    reasons.push("当前验证结果存在失败项。");
    return { level: "high", reasons };
  }
  if (validation.status === "warning" || repairs.length) {
    reasons.push("需要人工审查警告或自动修复影响。");
    return { level: "medium", reasons };
  }
  if (validation.status === "pass") {
    return {
      level: "low",
      reasons: reasons.length ? reasons : ["现有验证证据未报告问题。"],
    };
  }
  return {
    level: "unknown",
    reasons: reasons.length ? reasons : ["缺少足够验证证据，无法评估风险。"],
  };
}

function changedAssemblyObjectCount(
  current: GenerationResult,
  base: GenerationResult | null,
): number | null {
  if (!base?.assembly_parts || !current.assembly_parts) return null;
  const before = new Map(
    base.assembly_parts.map((part) => [part.name, JSON.stringify(part)]),
  );
  const after = new Map(
    current.assembly_parts.map((part) => [part.name, JSON.stringify(part)]),
  );
  const names = new Set([...before.keys(), ...after.keys()]);
  return [...names].filter((name) => before.get(name) !== after.get(name)).length;
}

function repairLogs(result: GenerationResult): string[] {
  return (result.repair_history || []).map((repair) => (
    `尝试 ${repair.attempt} · ${repair.stage} · ${repair.status}：${repair.message}`
  ));
}

function objectiveLabel(prompt: string) {
  const normalized = prompt.trim();
  if (normalized === "manual code execution") return "手动执行参数化建模代码";
  return normalized || null;
}

export function buildChangeSet(
  current: ModelSnapshotDetail,
  base: ModelSnapshotDetail | null,
): ChangeSet {
  const result = current.result;
  const baseResult = base?.result || null;
  const validation = validationEvidence(result);
  const hasComparableCode = Boolean(base?.code && current.code);
  return {
    id: `changeset:${current.id}`,
    panelId: current.panel_id,
    objective: objectiveLabel(current.prompt),
    baseRevisionId: current.parent_snapshot_id || null,
    targetRevisionId: current.id,
    baseVersion: base?.version ?? null,
    targetVersion: current.version,
    requestId: result.request_id || null,
    taskId: result.task_id || null,
    modifiedObjectCount: changedAssemblyObjectCount(result, baseResult),
    parameterChanges: parameterChanges(result, baseResult),
    code: {
      status: hasComparableCode
        ? base?.code === current.code ? "unchanged" : "changed"
        : "unknown",
      beforeLines: base?.code ? base.code.split(/\r?\n/).length : null,
      afterLines: current.code ? current.code.split(/\r?\n/).length : null,
    },
    geometry: geometryEvidence(result, baseResult),
    files: fileEvidence(result, baseResult),
    validation,
    risk: riskEvidence(result, validation),
    agentLogs: repairLogs(result),
    createdAt: current.created_at,
  };
}
