import { useState } from "react";
import type {
  GenerationResult,
  InspectReport,
  RepairStep,
  RunCreatedEvent,
  StepStatus,
  StepUpdate,
} from "../types";
import type {
  ArtifactHistoryEntry,
  StepHistoryEntry,
} from "../stores/sessionStore";
import {
  engineeringSourceLabel,
  engineeringStatusLabel,
  engineeringTaskEventLabel,
} from "../utils/engineeringLabels";

interface AgentRunTimelineProps {
  steps: StepHistoryEntry[];
  isGenerating: boolean;
  result: GenerationResult | null;
  repairHistory?: RepairStep[] | null;
  inspectReport?: InspectReport | null;
  activeRun?: RunCreatedEvent | null;
  artifacts?: ArtifactHistoryEntry[];
  durableAgent?: import("../types").DurableAgentSnapshotProjection | null;
  onRetryPrompt?: () => void;
  onRerunCode?: () => void;
  onResumeRun?: (runId: string) => void;
}

const STATUS_STYLES: Record<StepStatus, string> = {
  queued: "bg-[var(--subtle)] text-[var(--muted)] border-[var(--line)]",
  running: "bg-[var(--agent-soft)] text-[var(--agent)] border-[var(--agent-border)]",
  success: "bg-emerald-50 text-emerald-700 border-emerald-200",
  warn: "bg-amber-50 text-amber-700 border-amber-200",
  failed: "bg-red-50 text-red-700 border-red-200",
  skipped: "bg-[var(--surface-soft)] text-[var(--faint)] border-[var(--line)]",
};

const STATUS_ICON: Record<StepStatus, string> = {
  queued: "...",
  running: "*",
  success: "\u2713",
  warn: "!",
  failed: "\u00d7",
  skipped: "-",
};

const STATUS_LABEL: Record<StepStatus, string> = {
  queued: "\u6392\u961f\u4e2d",
  running: "\u8fd0\u884c\u4e2d",
  success: "\u6210\u529f",
  warn: "\u9700\u6ce8\u610f",
  failed: "\u5931\u8d25",
  skipped: "\u5df2\u8df3\u8fc7",
};

const STEP_LABELS: Record<string, string> = {
  plan_design: "\u9700\u6c42\u89c4\u5212",
  generate_cad_code: "\u751f\u6210 CAD \u4ee3\u7801",
  executing_code: "\u6267\u884c\u5efa\u6a21",
  execute_code: "\u6267\u884c\u4ee3\u7801",
  execute_cad_code: "\u6267\u884c CAD \u4ee3\u7801",
  repair_code: "\u81ea\u52a8\u4fee\u590d\u4ee3\u7801",
  resume_available: "\u53ef\u7ee7\u7eed\u4efb\u52a1",
  finalize_result: "\u6574\u7406\u7ed3\u679c",
  intent_detection: "\u610f\u56fe\u8bc6\u522b",
  planning: "\u9700\u6c42\u89c4\u5212",
  retrieving_examples: "\u68c0\u7d22\u6848\u4f8b",
  generating_code: "\u751f\u6210\u4ee3\u7801",
  executing: "\u6267\u884c\u5efa\u6a21",
  validating_geometry: "\u51e0\u4f55\u6821\u9a8c",
  validating_vision: "\u89c6\u89c9\u68c0\u67e5",
  repairing_code: "\u81ea\u52a8\u4fee\u590d",
  fixing_error: "\u81ea\u52a8\u4fee\u590d",
  rendering_preview: "\u6e32\u67d3\u9884\u89c8",
  exporting_files: "\u5bfc\u51fa\u6587\u4ef6",
  snapshotting_version: "\u4fdd\u5b58\u7248\u672c",
  assembly_part: "\u88c5\u914d\u96f6\u4ef6",
  complete: "\u5b8c\u6210",
  failed: "\u5931\u8d25",
};

const RUN_STATUS_LABELS: Record<string, string> = {
  running: "\u8fd0\u884c\u4e2d",
  succeeded: "\u5df2\u6210\u529f",
  failed: "\u5df2\u5931\u8d25",
  blocked: "\u5f85\u5904\u7406",
  cancelled: "\u5df2\u53d6\u6d88",
  observed: "\u5df2\u89c2\u6d4b",
};

function normalizeStatus(step: StepUpdate, isLast: boolean, isGenerating: boolean): StepStatus {
  if (step.status) return step.status;
  if (step.step === "complete") return "success";
  if (step.step === "failed") return "failed";
  if (step.step === "fixing_error" || step.step === "repairing_code") return "warn";
  if (!isGenerating && !isLast) return "success";
  return "running";
}

function formatDuration(durationMs?: number | null) {
  if (durationMs === undefined || durationMs === null) return "";
  return durationMs < 1000 ? `${durationMs}ms` : `${(durationMs / 1000).toFixed(1)}s`;
}

function stageLabel(step: StepUpdate) {
  return STEP_LABELS[step.step] || engineeringTaskEventLabel(step.step);
}

function runStatusLabel(status?: string | null) {
  if (!status) return RUN_STATUS_LABELS.observed;
  return RUN_STATUS_LABELS[status] || status;
}

function detailText(step: StepUpdate) {
  if (!step.detail) return "";
  const source = typeof step.detail.source === "string" ? step.detail.source : "";
  const label = typeof step.detail.label === "string" ? step.detail.label : "";
  const errorType = typeof step.detail.error_type === "string" ? step.detail.error_type : "";
  return [label, source ? engineeringSourceLabel(source) : "", errorType].filter(Boolean).join(" - ");
}

function printableLabel(inspectReport: InspectReport) {
  if (inspectReport.printable === false) return "\u5426";
  if (inspectReport.printable === true) return "\u662f";
  return "\u672a\u77e5";
}

function shortId(value?: string | null) {
  return value ? value.slice(0, 8) : "-";
}

export default function AgentRunTimeline({ steps = [], isGenerating, result, repairHistory, inspectReport, activeRun, artifacts = [], durableAgent, onRetryPrompt, onRerunCode, onResumeRun }: AgentRunTimelineProps) {
  const [collapsed, setCollapsed] = useState(true);
  const hasContent = steps.length > 0 || Boolean(repairHistory?.length) || Boolean(inspectReport) || Boolean(activeRun) || artifacts.length > 0 || Boolean(durableAgent);
  if (!hasContent) return null;

  const hasFailed = (result?.success === false && !result.needs_confirmation) || steps.some((step) => normalizeStatus(step, false, false) === "failed");
  const hasCode = Boolean(result?.code);
  const canResume = Boolean(activeRun?.run_id && steps.some((step) => step.step === "resume_available"));
  const latestStep = steps.at(-1);
  const projectedStage = typeof latestStep?.detail?.stage === "string"
    ? latestStep.detail.stage
    : durableAgent?.current_stage;
  const planSteps = Array.isArray(durableAgent?.plan?.steps)
    ? durableAgent.plan.steps.length
    : 0;
  const riskCount = typeof durableAgent?.risk_summary?.issue_count === "number"
    ? durableAgent.risk_summary.issue_count
    : 0;
  return (
    <div className="bg-[var(--surface)] rounded-xl border border-[var(--line)] shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-[var(--line)] flex items-center justify-between gap-3">
        <div>
          <h3 className="type-section-heading  text-[var(--ink)]">{"\u667a\u80fd\u4f53\u8fd0\u884c\u65f6\u95f4\u7ebf"}</h3>
          <p className="type-body text-[var(--muted)] mt-0.5">{"\u8bb0\u5f55\u672c\u6b21\u4efb\u52a1\u7684\u6267\u884c\u8fdb\u5ea6\u3001\u81ea\u52a8\u4fee\u590d\u3001\u68c0\u67e5\u8bc1\u636e\u548c\u4ea7\u7269\u4fe1\u606f\u3002"}</p>
          {(activeRun || artifacts.length > 0 || latestStep) && (
            <div className="mt-2 flex flex-wrap gap-1.5 type-caption">
              {activeRun ? <span className="rounded-full border border-[var(--agent-border)] bg-[var(--agent-soft)] px-2 py-0.5 text-[var(--agent)]">{"\u8fd0\u884c"} {shortId(activeRun.run_id)} {"\u00b7"} {runStatusLabel(activeRun.status)}</span> : null}
              {latestStep ? <span className="rounded-full border border-[var(--agent-border)] bg-[var(--agent-soft)] px-2 py-0.5 text-[var(--agent)]">{"\u6700\u65b0\u6b65\u9aa4"} {stageLabel(latestStep)}</span> : null}
              {steps.length > 0 ? <span className="rounded-full border border-[var(--line)] bg-[var(--surface-soft)] px-2 py-0.5 text-[var(--muted)]">{steps.length} {"\u4e2a\u6b65\u9aa4"}</span> : null}
              {artifacts.slice(-6).map((artifact, index) => (
                <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-emerald-700" key={`${artifact.path}:${index}`}>{"\u4ea7\u7269"} {artifact.artifact_type.toUpperCase()}</span>
              ))}
              {projectedStage ? <span className="rounded-full border border-[var(--line)] bg-[var(--surface-soft)] px-2 py-0.5 text-[var(--muted)]">当前阶段 {_STAGE_LABELS[projectedStage] || projectedStage}</span> : null}
              {planSteps ? <span className="rounded-full border border-[var(--line)] bg-[var(--surface-soft)] px-2 py-0.5 text-[var(--muted)]">计划 {planSteps} 步</span> : null}
              {durableAgent?.repair_count ? <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-amber-700">真实修复 {durableAgent.repair_count} 次</span> : null}
              {riskCount ? <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-amber-700">风险 {riskCount} 项</span> : null}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0 flex-wrap justify-end">
          <button type="button" onClick={() => setCollapsed((value) => !value)} className="px-2.5 py-1 type-control rounded-md border border-[var(--line)] text-[var(--ink)] hover:bg-[var(--surface-soft)]" aria-expanded={!collapsed}>
            {collapsed ? "\u5c55\u5f00\u8be6\u60c5" : "\u6536\u8d77\u65f6\u95f4\u7ebf"}
          </button>
          {canResume && onResumeRun && activeRun?.run_id && (
            <button type="button" onClick={() => onResumeRun(activeRun.run_id)} disabled={isGenerating} className="px-2.5 py-1 type-control rounded-md border border-emerald-200 text-emerald-700 hover:bg-emerald-50 disabled:opacity-50">
              {"\u7ee7\u7eed\u4e0a\u6b21\u4efb\u52a1"}
            </button>
          )}
          {hasFailed && onRetryPrompt && (
            <button type="button" onClick={onRetryPrompt} disabled={isGenerating} className="px-2.5 py-1 type-control rounded-md border border-[var(--agent-border)] text-[var(--agent)] hover:bg-[var(--agent-soft)] disabled:opacity-50">
              {"\u91cd\u8bd5\u63d0\u793a\u8bcd"}
            </button>
          )}
          {hasCode && onRerunCode && (
            <button type="button" onClick={onRerunCode} disabled={isGenerating} className="px-2.5 py-1 type-control rounded-md border border-[var(--line)] text-[var(--ink)] hover:bg-[var(--surface-soft)] disabled:opacity-50">
              {"\u91cd\u65b0\u8fd0\u884c\u5f53\u524d\u4ee3\u7801"}
            </button>
          )}
        </div>
      </div>

      {!collapsed && <div className="p-4 space-y-3 max-h-[42vh] overflow-y-auto">
        {steps.map((step, index) => {
          const isLast = index === steps.length - 1;
          const status = normalizeStatus(step, isLast, isGenerating);
          const duration = formatDuration(step.duration_ms);
          const detail = detailText(step);
          return (
            <div key={`${step.stage_id || step.step}-${index}`} className="flex gap-3">
              <div className="flex flex-col items-center">
                <span className={`w-6 h-6 rounded-full border type-body flex items-center justify-center ${STATUS_STYLES[status]}`}>{STATUS_ICON[status]}</span>
                {index < steps.length - 1 && <span className="w-px flex-1 bg-[var(--line)] mt-1" />}
              </div>
              <div className="min-w-0 flex-1 pb-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="type-body  text-[var(--ink)]">{stageLabel(step)}</span>
                  <span className={`type-caption border rounded-full px-1.5 py-0.5 ${STATUS_STYLES[status]}`}>{STATUS_LABEL[status]}</span>
                  {step.attempt && <span className="type-caption text-[var(--faint)]">{"\u7b2c"} {step.attempt} {"\u6b21"}</span>}
                  {duration && <span className="type-caption text-[var(--faint)]">{duration}</span>}
                </div>
                <p className="type-body text-[var(--muted)] mt-0.5">{step.message}</p>
                {detail && <p className="type-caption text-[var(--faint)] mt-1">{detail}</p>}
                {step.part_name && <p className="type-caption text-[var(--faint)] mt-1">{"\u96f6\u4ef6"} {step.part_index || "?"}/{step.total_parts || "?"}: {step.part_name}</p>}
              </div>
            </div>
          );
        })}

        {(repairHistory?.length || inspectReport) && (
          <div className="rounded-lg bg-[var(--surface-soft)] border border-[var(--line)] p-3 space-y-1">
            {repairHistory?.length ? <p className="type-body text-[var(--muted)]">{"\u4fee\u590d\u8bc1\u636e\uff1a\u5df2\u8bb0\u5f55"} {repairHistory.length} {"\u4e2a\u81ea\u52a8\u4fee\u590d\u6b65\u9aa4\u3002"}</p> : null}
            {inspectReport ? <p className="type-body text-[var(--muted)]">{"\u68c0\u67e5\u8bc1\u636e\uff1a\u7ed3\u8bba"} {engineeringStatusLabel(inspectReport.verdict)}{"\uff0c\u53ef\u6253\u5370\u6027"} {printableLabel(inspectReport)}{"\u3002"}</p> : null}
          </div>
        )}
        {durableAgent?.validations.length ? (
          <div className="rounded-lg border border-[var(--line)] bg-[var(--surface-soft)] p-3">
            <p className="type-body  text-[var(--ink)]">持久化验证证据</p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {durableAgent.validations.map((item) => (
                <span className={`rounded-full border px-2 py-0.5 type-caption ${item.outcome === "passed" ? "border-emerald-200 bg-emerald-50 text-emerald-700" : item.mode === "required" ? "border-red-200 bg-red-50 text-red-700" : "border-amber-200 bg-amber-50 text-amber-700"}`} key={item.evidence_id} title={item.evidence_hash}>
                  {item.gate.toUpperCase()} · {item.outcome === "passed" ? "通过" : item.outcome === "failed" ? "存在问题" : "未能判定"}
                </span>
              ))}
            </div>
          </div>
        ) : null}
      </div>}
    </div>
  );
}

const _STAGE_LABELS: Record<string, string> = {
  planning: "需求与方案",
  modeling: "机械设计",
  repair: "自动修复",
  validation: "工程验证",
  review: "变更审查",
  complete: "已完成",
};
