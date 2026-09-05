import type { GenerationResult, ModelSnapshotDetail } from "../types";
import { parameterDisplayLabel } from "../utils/parameterMapping.ts";
import type {
  ChangeSet,
  DurableArtifact,
  DurableChangeSetDetail,
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
  if (typeof result.validation?.is_watertight === "boolean") {
    return {
      status: result.validation.is_watertight ? "pass" : "fail",
      summary: result.validation.is_watertight
        ? "几何验证记录为闭合网格。"
        : "几何验证记录为非闭合网格。",
    };
  }
  if (result.validation?.gates?.length) {
    const gates = result.validation.gates.filter((gate) => gate.mode !== "disabled");
    if (!gates.length) return { status: "unknown", summary: "所有门禁均未启用，没有已执行的验证证据。" };
    const failedRequired = gates.some((gate) => gate.mode === "required" && gate.outcome === "failed");
    const incomplete = gates.some((gate) => gate.outcome !== "passed");
    return {
      status: failedRequired ? "fail" : incomplete ? "warning" : "pass",
      summary: failedRequired ? "必需门禁未通过。" : incomplete ? "仍有未通过或无法确认的检查，请查看各项证据。" : "已记录的工程门禁通过，不代表未执行的检查已通过。",
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
    source: "snapshot",
  };
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function durableParameterChanges(
  summary: Record<string, unknown>,
): ParameterChange[] {
  const rows = Array.isArray(summary.parameter_changes)
    ? summary.parameter_changes
    : [];
  return rows.flatMap((value, index) => {
    const item = record(value);
    if (!item) return [];
    const before = item.before;
    const after = item.after;
    if (
      !["number", "string"].includes(typeof before)
      || !["number", "string"].includes(typeof after)
    ) {
      return [];
    }
    return [{
      parameterId: typeof item.parameter_id === "string"
        ? item.parameter_id
        : `parameter-${index}`,
      label: typeof item.label === "string"
        ? item.label
        : typeof item.parameter_id === "string"
          ? parameterDisplayLabel(item.parameter_id)
          : `参数 ${index + 1}`,
      before: before as number | string,
      after: after as number | string,
      unit: typeof item.unit === "string" ? item.unit : undefined,
    }];
  });
}

function executionHashes(manifest: Record<string, unknown>): Map<string, string> {
  const executions = Array.isArray(manifest.executions)
    ? manifest.executions
    : [];
  const hashes = new Map<string, string>();
  for (const value of executions) {
    const item = record(value);
    if (
      item
      && typeof item.step_key === "string"
      && typeof item.source_sha256 === "string"
    ) {
      hashes.set(item.step_key, item.source_sha256);
    }
  }
  return hashes;
}

function durableCodeEvidence(
  detail: DurableChangeSetDetail,
): ChangeSet["code"] {
  const before = executionHashes(detail.base_manifest);
  const after = executionHashes(detail.candidate_manifest);
  if (!before.size || !after.size) {
    return { status: "unknown", beforeLines: null, afterLines: null };
  }
  const keys = new Set([...before.keys(), ...after.keys()]);
  return {
    status: [...keys].some((key) => before.get(key) !== after.get(key))
      ? "changed"
      : "unchanged",
    beforeLines: null,
    afterLines: null,
  };
}

function durableGeometryEvidence(
  summary: Record<string, unknown>,
): ChangeSet["geometry"] {
  const geometry = record(summary.geometry);
  if (!geometry) return { status: "unknown", metrics: [] };
  const rawStatus = geometry.status;
  const status = rawStatus === "changed"
    || rawStatus === "unchanged"
    || rawStatus === "unknown"
    ? rawStatus
    : "unknown";
  const rawMetrics = Array.isArray(geometry.metrics) ? geometry.metrics : [];
  const metrics = rawMetrics.flatMap((value) => {
    const item = record(value);
    if (!item) return [];
    const before = finiteNumber(item.before);
    const after = finiteNumber(item.after);
    if (
      before === null
      || after === null
      || typeof item.label !== "string"
    ) {
      return [];
    }
    return [{
      label: item.label,
      before,
      after,
      unit: typeof item.unit === "string" ? item.unit : "",
    }];
  });
  return { status, metrics };
}

function artifactMap(items: DurableArtifact[]) {
  return new Map(items.map((artifact) => [
    `${artifact.artifact_kind}:${artifact.filename}`,
    artifact,
  ]));
}

function durableFileChanges(
  detail: DurableChangeSetDetail,
): ChangeSet["files"] {
  const before = artifactMap(detail.base_artifacts);
  const after = artifactMap(detail.candidate_artifacts);
  const keys = new Set([...before.keys(), ...after.keys()]);
  const changes: ChangeSet["files"] = [];
  for (const key of keys) {
    const previous = before.get(key);
    const current = after.get(key);
    if (previous?.sha256 === current?.sha256) continue;
    const format = (current?.artifact_kind || previous?.artifact_kind || "file")
      .toUpperCase();
    changes.push({
      format,
      kind: previous ? current ? "replaced" : "removed" : "added",
      beforeUrl: previous?.download_url,
      afterUrl: current?.download_url,
      evidence: "sha256",
    });
  }
  return changes;
}

function durableValidation(
  summary: Record<string, unknown>,
): ChangeSet["validation"] {
  const raw = typeof summary.status === "string"
    ? summary.status.toLowerCase()
    : "";
  const status = raw === "passed" || raw === "success"
    ? "pass"
    : raw === "failed" || raw === "fail"
      ? "fail"
      : raw === "warning" || raw === "warn"
        ? "warning"
        : "unknown";
  const issueCount = finiteNumber(summary.issue_count);
  const summaryText = typeof summary.summary === "string"
    ? summary.summary
    : status === "unknown"
      ? "后端未记录可判定的验证结论。"
      : `验证状态：${raw}${issueCount === null ? "" : `，问题 ${issueCount} 项`}`;
  const gates = (Array.isArray(summary.gates) ? summary.gates : []).flatMap(
    (value) => {
      const item = record(value);
      if (
        !item
        || typeof item.gate !== "string"
        || typeof item.mode !== "string"
        || typeof item.outcome !== "string"
      ) return [];
      return [{
        gate: item.gate,
        mode: item.mode,
        outcome: item.outcome,
        evidenceId: typeof item.evidence_id === "string"
          ? item.evidence_id
          : undefined,
        evidenceHash: typeof item.evidence_hash === "string"
          ? item.evidence_hash
          : undefined,
      }];
    },
  );
  return { status, summary: summaryText, gates };
}

function durableRisk(
  summary: Record<string, unknown>,
): ChangeSet["risk"] {
  const raw = typeof summary.level === "string"
    ? summary.level.toLowerCase()
    : typeof summary.status === "string"
      ? summary.status.toLowerCase()
      : "";
  const level = raw === "low" || raw === "medium" || raw === "high"
    ? raw
    : raw === "clear"
      ? "low"
      : raw === "attention_required"
        ? "medium"
        : "unknown";
  const reasons = Array.isArray(summary.reasons)
    ? summary.reasons.filter((value): value is string => typeof value === "string")
    : [];
  const itemReasons = (Array.isArray(summary.items) ? summary.items : [])
    .flatMap((value) => {
      const item = record(value);
      if (!item) return [];
      const gate = typeof item.gate === "string" ? item.gate.toUpperCase() : "检查";
      const issues = Array.isArray(item.issues)
        ? item.issues.filter((issue): issue is string => typeof issue === "string")
        : [];
      const violations = Array.isArray(item.violations) ? item.violations.length : 0;
      if (issues.length) return issues.map((issue) => `${gate}：${issue}`);
      return violations ? [`${gate}：记录 ${violations} 项规则风险`] : [];
    });
  return {
    level,
    reasons: reasons.length || itemReasons.length
      ? [...reasons, ...itemReasons]
      : raw === "clear"
        ? ["持久化验证未记录建议风险。"]
        : raw === "attention_required"
          ? ["后端记录了需关注风险，但未提供具体说明。"]
          : ["后端未记录可判定的风险依据。"],
  };
}

export function adaptDurableChangeSet(
  detail: DurableChangeSetDetail,
  panelId: string,
): ChangeSet {
  const operationCount = finiteNumber(detail.change_summary.modified_object_count);
  return {
    id: detail.id,
    panelId,
    objective: detail.objective || null,
    baseRevisionId: detail.base_revision_id,
    targetRevisionId: detail.candidate_revision_id,
    baseVersion: detail.base_revision_number,
    targetVersion: detail.candidate_revision_number,
    requestId: null,
    taskId: detail.source_workflow_run_id || null,
    modifiedObjectCount: operationCount,
    parameterChanges: durableParameterChanges(detail.change_summary),
    code: durableCodeEvidence(detail),
    geometry: durableGeometryEvidence(detail.change_summary),
    files: durableFileChanges(detail),
    validation: durableValidation(detail.validation_summary),
    risk: durableRisk(detail.risk_summary),
    agentLogs: [
      ...(detail.agent_events || []).flatMap((entry) => (
        entry.event_type.startsWith("agent.") && entry.projection?.message
          ? [`#${entry.sequence} ${entry.projection.message}`]
          : []
      )),
      ...detail.audit_log.map((entry) => {
      const note = typeof entry.payload.note === "string"
        ? `：${entry.payload.note}`
        : "";
      return `${entry.action}${note}`;
      }),
    ],
    createdAt: detail.created_at,
    source: "durable",
    reviewStatus: detail.status,
  };
}
