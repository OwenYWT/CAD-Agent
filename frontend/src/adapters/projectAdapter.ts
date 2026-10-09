import { taskState } from "./taskState.ts";
import type { PanelState } from "../stores/sessionStore";
import type { CloudDocument } from "../types/document";
import type { EngineeringTaskSummary } from "../types/engineeringTask";
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
      status: gate.outcome === "passed" ? "pass" : gate.outcome === "failed" ? "fail" : "unknown",
      outcome:gate.outcome,evidenceId:gate.evidence_id,revisionId:result?.revision_id,
      description: [outcomes[gate.outcome] || "尚无确定结果", ...(gate.issues || [])].join("；"),
      object: "当前模型",
    });
  }
  analysis?.dfm_issues?.forEach((issue, index) => checks.push({
    id: `analysis-${index}`,
    domain: "DFM",
    title: issue.category,
    status: issue.severity === "critical" ? "fail" : "warning",
    description: issue.description,
    object: issue.location || "当前模型",
    suggestion: issue.suggestion,
  }));
  if (analysis) checks.push({ id: "analysis-conclusion", domain: "DFM", title: "制造检查结论",
    status: analysis.evaluation_status === "passed" ? "pass" : analysis.evaluation_status === "failed" ? "fail" : analysis.evaluation_status === "warning" ? "warning" : "unknown",
    description: [analysis.design_summary, ...(analysis.analysis_errors || [])].join("；"), object: "当前查看版本" });
  return checks;
}

export function adaptArtifacts(result: GenerationResult | null): Artifact[] {
  return Object.entries(result?.files || {})
    .filter(([, url]) => (
      /^\/api\/files\/[A-Za-z0-9_-]+\/[A-Za-z0-9._-]+(?:\?.*)?$/.test(url)
      || /^\/api\/documents\/[a-f0-9-]{36}\/artifacts\/[a-f0-9-]{36}$/.test(url)
    ))
    .map(([format, url]) => ({
    id: `${result?.request_id || "result"}-${format}`,
    name: url.startsWith("/api/documents/") ? format.split(":")[1] || `model.${format}` : url.split("/").pop() || `model.${format}`,
    format: format.split(":")[0].toUpperCase(),
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
  document: CloudDocument | null = null,
  engineeringTasks: EngineeringTaskSummary[] = [],
  engineeringLoad?: {pending:boolean;error?:string},
  activeTask?: ReturnType<typeof taskState>,
): EngineeringProjectModel {
  const task=activeTask || taskState(panel,document);
  const result = panel.result;
  const hasPrompt = panel.messages.some((message) => message.role === "user");
  const needsConfirmation = task.phase==="needs_input";
  const artifacts = adaptArtifacts(result);
  const hasSuccessfulResult = Boolean(result?.success && artifacts.length > 0);
  const resultStatus = document?.view_mode === "committed" || document?.view_mode === "history" ? "completed" : "awaiting_confirmation";
  const missingArtifacts = Boolean(result?.success && artifacts.length === 0);
  const parameters = adaptParameters(result);
  const validation = adaptValidation(result, analysis);
  const hasIssue = task.phase==="failed" || missingArtifacts
    || result?.inspect_report?.verdict === "fail" || validation.some(check => check.status === "fail");
  const hasWarning = validation.some(check => check.status === "warning" || check.status === "unknown");
  const nativeDocument = Boolean(document?.fcstd && document.head_revision_id);
  const currentAnalysis = engineeringTasks.find(task => task.task_kind === "linear_static" && task.source_revision_id === document?.revision_id);
  const simulationStatus: EngineeringStage["status"] = engineeringLoad?.error ? "issue" : !currentAnalysis ? "not_started"
    : currentAnalysis.status === "succeeded" ? "completed"
      : ["failed", "timed_out", "cancelled"].includes(currentAnalysis.status) ? "issue" : "in_progress";
  const currentTaskLabel = engineeringTaskEventLabel(panel.currentStep?.message || "正在处理工程任务");
  const requirementSteps = task.snapshot?.steps?.filter(step => ['agent_requirements', 'agent_decompose', 'agent_plan'].includes(step.kind)) || [];
  const requirementsStatus: EngineeringStage["status"] = task.phase === 'failed' && !requirementSteps.every(step => step.status === 'succeeded') ? 'issue'
    : needsConfirmation ? 'awaiting_confirmation'
    : requirementSteps.length && requirementSteps.every(step => step.status === 'succeeded') ? 'completed'
    : task.running ? 'in_progress' : hasSuccessfulResult ? 'completed' : 'not_started';
  const project: Project = {
    id: sessionId,
    name: deriveName(panel),
    description: hasPrompt ? "由当前工程会话和生成结果构建。" : "描述需求后开始创建工程。",
    branch: "当前设计",
    status: hasIssue ? "issue" : task.running ? "in_progress" : needsConfirmation ? "awaiting_confirmation" : hasSuccessfulResult ? resultStatus : hasPrompt ? "in_progress" : "not_started",
  };
  const stages: EngineeringStage[] = [
    { id: "requirements", index: 1, title: "需求与方案", summary: task.phase === 'failed' ? "本次任务已停止，原需求已保留，可在 Agent 中查看详情和重试。" : needsConfirmation ? "补充建模依据或确认执行计划后继续。" : hasPrompt ? "需求已进入当前工程会话。" : "等待输入工程需求。", status: requirementsStatus, actionLabel: needsConfirmation ? "确认需求依据" : "查看需求", domain: "overview", available: true },
    { id: "mechanical", index: 2, title: "机械设计", summary: task.running ? currentTaskLabel : needsConfirmation ? "等待设计简报确认，尚未生成 CAD 模型。" : missingArtifacts ? "后端未返回可下载工程产物，本次结果不能视为完成。" : hasIssue ? panel.lastError || "生成存在问题，需要修改需求。" : hasSuccessfulResult ? "CAD 结果已生成，可查看模型和参数。" : "等待机械生成结果。", status: hasIssue ? "issue" : task.running ? "in_progress" : needsConfirmation ? "not_started" : hasSuccessfulResult ? resultStatus : "not_started", actionLabel: hasSuccessfulResult ? "查看机械设计" : "打开机械设计", domain: "mechanical", available: !needsConfirmation && hasSuccessfulResult },
    { id: "electronics", index: 3, title: "电子设计", summary: "当前后端未提供 ECAD 数据或 ERC / DRC 接口。", status: "not_started", actionLabel: "不可用", domain: "electronics", available: false, limitation: "当前后端尚无 ECAD 数据接口。" },
    { id: "simulation", index: 4, title: "仿真验证", summary: !nativeDocument ? "提交原生 CAD 模型后可运行结构静力分析。"
      : engineeringLoad?.error ? `无法读取计算状态：${engineeringLoad.error}`
      : engineeringLoad?.pending ? "正在读取当前修订的计算记录。"
      : simulationStatus === "completed" ? "当前修订的有限元计算已完成，可查看位移、应力和求解证据。"
        : simulationStatus === "in_progress" ? "当前修订的有限元任务正在处理。"
          : simulationStatus === "issue" ? currentAnalysis?.error_message || "当前修订的有限元任务未完成，请查看任务记录。"
            : "当前修订尚未计算。可配置材料、载荷与边界条件，运行单实体线弹性静力分析。",
      status: simulationStatus, actionLabel: "结构仿真", domain: "simulation", available: nativeDocument },
    { id: "firmware", index: 5, title: "固件", summary: "当前后端未提供固件仓库、构建任务或日志接口。", status: "not_started", actionLabel: "不可用", domain: "firmware", available: false, limitation: "当前后端没有固件仓库和构建日志接口。" },
    { id: "manufacturing", index: 6, title: "制造检查", summary: validation.length ? `已有 ${validation.length} 项真实检查结果。${hasIssue ? "存在未通过项。" : hasWarning ? "仍有风险或未能判定项。" : ""}` : "等待模型结果后运行工程检查。", status: hasIssue ? "issue" : hasWarning ? "awaiting_confirmation" : validation.length ? "completed" : "not_started", actionLabel: "工程检查", available: hasSuccessfulResult || validation.length > 0 },
    { id: "release", index: 7, title: "发布", summary: document?.can_export === false ? "当前账号可查看模型，没有导出权限。" : artifacts.length ? `已有 ${artifacts.length} 个可下载产物。` : "生成工程产物后可导出。", status: artifacts.length ? "awaiting_confirmation" : "not_started", actionLabel: "导出", available: document?.can_export !== false && artifacts.length > 0 },
  ];
  return { project, stages, task: task.running ? { id: `task-${panel.id}`, title: currentTaskLabel, detail: engineeringTaskEventLabel(panel.currentStep?.step || "planning"), status: "in_progress", startedAt: panel.generationStartTime } : null, artifacts, parameters, validation, exports: document?.can_export === false ? [] : exportJobs(artifacts), result };
}

export function patchCodeParameters(code: string, changes: Record<string, number>): string {
  return Object.entries(changes).reduce((nextCode, [name, value]) => {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return nextCode.replace(new RegExp(`(${escaped}\\s*=\\s*)(-?[0-9]+(?:\\.[0-9]+)?)`, "m"), `$1${value}`);
  }, code);
}
