import type { PanelState } from "../stores/sessionStore";
import type { DesignAnalysis, GenerationResult, InspectCheck } from "../types";
import { engineeringTaskEventLabel } from "../utils/engineeringLabels.ts";
import {
  parameterDisplayLabel,
  splitEngineeringParameters,
} from "../utils/parameterMapping.ts";
import type {
  Artifact,
  EngineeringProjectModel,
  EngineeringStage,
  ExportJob,
  Parameter,
  Project,
  ValidationResult,
} from "../types/engineering";

const PARAMETER_GROUPS = new Set<Parameter["group"]>([
  "基础尺寸",
  "材料",
  "装配参数",
  "制造参数",
  "高级参数",
]);

function parameterGroup(group: string | null | undefined, name: string, critical: boolean): Parameter["group"] {
  if (group && PARAMETER_GROUPS.has(group as Parameter["group"])) return group as Parameter["group"];
  if (/material|材质|材料/i.test(name)) return "材料";
  if (/fit|clearance|mount|assembly|配合|装配|安装/i.test(name)) return "装配参数";
  if (/wall|nozzle|layer|draft|tolerance|壁厚|喷嘴|层高|公差/i.test(name)) return "制造参数";
  return critical ? "基础尺寸" : "高级参数";
}

export function adaptParameters(result?: GenerationResult | null): Parameter[] {
  const richParameters = result?.parameters || [];
  if (richParameters.length) {
    const brief = result?.design_brief || result?.plan?.design_brief || null;
    const criticalNames = new Set(
      splitEngineeringParameters(richParameters, brief).critical.map(({ parameter }) => parameter.name),
    );
    return richParameters.map((parameter) => ({
      id: parameter.name,
      label: parameterDisplayLabel(
        parameter.name,
        parameter.comment || parameter.display_name,
      ),
      group: parameterGroup(parameter.group, parameter.name, criticalNames.has(parameter.name)),
      value: parameter.value,
      defaultValue: parameter.default_value,
      unit: parameter.unit || "",
      comment: parameter.comment || parameter.name,
      min: parameter.min ?? undefined,
      max: parameter.max ?? undefined,
      step: parameter.step ?? undefined,
    }));
  }

  return Object.entries(result?.params || {}).map(([name, param]) => ({
    id: name,
    label: parameterDisplayLabel(name, param.comment),
    group: "高级参数",
    value: param.value,
    defaultValue: param.value,
    unit: "",
    comment: param.comment || name,
  }));
}

function statusFromCheck(check: InspectCheck): ValidationResult["status"] {
  return check.status === "warn" ? "warning" : check.status;
}

export function adaptValidation(result: GenerationResult | null, analysis?: DesignAnalysis | null): ValidationResult[] {
  const checks: ValidationResult[] = (result?.inspect_report?.checks || []).map((check, index) => ({
    id: `inspect-${index}`,
    domain: check.source === "dfm" ? "DFM" : "几何",
    title: check.name,
    status: statusFromCheck(check),
    description: check.message,
    object: "当前模型",
  }));
  if (typeof result?.validation?.is_watertight === "boolean") {
    checks.unshift({
      id: "mesh-watertight",
      domain: "几何",
      title: "网格闭合",
      status: result.validation.is_watertight ? "pass" : "fail",
      description: result.validation.is_watertight ? "模型网格闭合。" : "模型存在非闭合边界。",
      object: "当前模型",
    });
  }
  for (const gate of result?.validation?.gates || []) {
    const names: Record<string, string> = { geometry: "几何门禁", visual: "视觉一致性", dfm: "制造检查", artifact_integrity: "产物完整性", bom: "物料清单" };
    const outcomes: Record<string, string> = { passed: "通过", failed: "未通过", indeterminate: "无法确认", disabled: "未启用", skipped: "未执行" };
    checks.push({
      id: `gate-${gate.gate}`,
      domain: gate.gate === "dfm" ? "DFM" : gate.gate === "bom" ? "装配" : "几何",
      title: `${names[gate.gate] || gate.gate}（${gate.mode === "required" ? "必需" : "参考"}）`,
      status: gate.outcome === "passed" ? "pass" : gate.outcome === "failed" ? "fail" : "warning",
      description: [outcomes[gate.outcome] || "尚无确定结果", ...(gate.issues || [])].join("；"),
      object: "当前模型",
    });
  }
  analysis?.dfm_issues.forEach((issue, index) => checks.push({
    id: `analysis-${index}`,
    domain: "DFM",
    title: issue.category,
    status: issue.severity === "critical" ? "fail" : "warning",
    description: issue.description,
    object: issue.location || "当前模型",
    suggestion: issue.suggestion,
  }));
  return checks;
}

export function adaptArtifacts(result: GenerationResult | null): Artifact[] {
  return Object.entries(result?.files || {})
    .filter(([, url]) => (
      /^\/api\/files\/[A-Za-z0-9_-]+\/[A-Za-z0-9._-]+(?:\?.*)?$/.test(url)
    ))
    .map(([format, url]) => ({
    id: `${result?.request_id || "result"}-${format}`,
    name: url.split("/").pop() || `model.${format}`,
    format: format.toUpperCase(),
    url,
    status: "ready",
    }));
}

function exportJobs(artifacts: Artifact[]): ExportJob[] {
  return artifacts.map((artifact) => ({
    id: `export-${artifact.id}`,
    label: `${artifact.format} 文件`,
    format: artifact.format as ExportJob["format"],
    status: "available",
    artifact,
  }));
}

function deriveName(panel: PanelState): string {
  const firstPrompt = panel.messages.find((message) => message.role === "user")?.content.trim();
  if (!firstPrompt) return panel.title || "新硬件项目";
  return firstPrompt.length > 24 ? `${firstPrompt.slice(0, 24)}…` : firstPrompt;
}

export function adaptEngineeringProject(
  sessionId: string,
  panel: PanelState,
  analysis: DesignAnalysis | null = null,
): EngineeringProjectModel {
  const result = panel.result;
  const hasPrompt = panel.messages.some((message) => message.role === "user");
  const needsConfirmation = Boolean(result?.needs_confirmation);
  const artifacts = adaptArtifacts(result);
  const hasSuccessfulResult = Boolean(result?.success && artifacts.length > 0);
  const missingArtifacts = Boolean(result?.success && artifacts.length === 0);
  const hasIssue = Boolean(panel.lastError) || missingArtifacts || Boolean(result && !result.success && !needsConfirmation) || result?.inspect_report?.verdict === "fail";
  const parameters = adaptParameters(result);
  const validation = adaptValidation(result, analysis);
  const currentTaskLabel = engineeringTaskEventLabel(panel.currentStep?.message || "正在处理工程任务");
  const project: Project = {
    id: sessionId,
    name: deriveName(panel),
    description: hasPrompt ? "由当前工程会话和生成结果构建。" : "描述需求后开始创建工程。",
    branch: `会话 ${panel.id.slice(0, 8)}`,
    status: hasIssue ? "issue" : panel.isGenerating ? "in_progress" : needsConfirmation ? "awaiting_confirmation" : hasSuccessfulResult ? "awaiting_confirmation" : hasPrompt ? "in_progress" : "not_started",
  };
  const stages: EngineeringStage[] = [
    { id: "requirements", index: 1, title: "需求与方案", summary: needsConfirmation ? "设计简报包含必须确认的问题，补充信息后再继续建模。" : hasPrompt ? "需求已进入当前工程会话。" : "等待输入工程需求。", status: needsConfirmation ? "awaiting_confirmation" : hasPrompt ? "completed" : "not_started", actionLabel: needsConfirmation ? "确认设计简报" : "查看需求", domain: "overview", available: true },
    { id: "mechanical", index: 2, title: "机械设计", summary: panel.isGenerating ? currentTaskLabel : needsConfirmation ? "等待设计简报确认，尚未生成 CAD 模型。" : missingArtifacts ? "后端未返回可下载工程产物，本次结果不能视为完成。" : hasIssue ? panel.lastError || "生成存在问题，需要修改需求。" : hasSuccessfulResult ? "CAD 结果已生成，可查看模型和参数。" : "等待机械生成结果。", status: hasIssue ? "issue" : panel.isGenerating ? "in_progress" : needsConfirmation ? "not_started" : hasSuccessfulResult ? "awaiting_confirmation" : "not_started", actionLabel: hasSuccessfulResult ? "查看机械设计" : "打开机械设计", domain: "mechanical", available: !needsConfirmation && hasSuccessfulResult },
    { id: "electronics", index: 3, title: "电子设计", summary: "当前后端未提供 ECAD 数据或 ERC / DRC 接口。", status: "not_started", actionLabel: "不可用", domain: "electronics", available: false, limitation: "当前后端尚无 ECAD 数据接口。" },
    { id: "simulation", index: 4, title: "仿真验证", summary: "当前后端未提供热、结构或运动求解任务接口。", status: "not_started", actionLabel: "不可用", domain: "simulation", available: false, limitation: "当前仅有 CAD/DFM 分析，不含热、结构或运动求解器。" },
    { id: "firmware", index: 5, title: "固件", summary: "当前后端未提供固件仓库、构建任务或日志接口。", status: "not_started", actionLabel: "不可用", domain: "firmware", available: false, limitation: "当前后端没有固件仓库和构建日志接口。" },
    { id: "manufacturing", index: 6, title: "制造检查", summary: validation.length ? `已有 ${validation.length} 项真实检查结果。` : "等待模型结果后运行工程检查。", status: hasIssue ? "issue" : validation.length ? "completed" : "not_started", actionLabel: "工程检查", available: hasSuccessfulResult },
    { id: "release", index: 7, title: "发布", summary: artifacts.length ? `已有 ${artifacts.length} 个可下载产物。` : "生成工程产物后可导出。", status: artifacts.length ? "awaiting_confirmation" : "not_started", actionLabel: "导出", available: artifacts.length > 0 },
  ];
  return { project, stages, task: panel.isGenerating ? { id: `task-${panel.id}`, title: currentTaskLabel, detail: engineeringTaskEventLabel(panel.currentStep?.step || "planning"), status: "in_progress", startedAt: panel.generationStartTime } : null, artifacts, parameters, validation, exports: exportJobs(artifacts), result };
}

export function patchCodeParameters(code: string, changes: Record<string, number>): string {
  return Object.entries(changes).reduce((nextCode, [name, value]) => {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return nextCode.replace(new RegExp(`(${escaped}\\s*=\\s*)(-?[0-9]+(?:\\.[0-9]+)?)`, "m"), `$1${value}`);
  }, code);
}
