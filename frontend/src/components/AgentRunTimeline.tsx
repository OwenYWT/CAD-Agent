import type { GenerationResult, InspectReport, RepairStep, StepStatus, StepUpdate } from "../types";
import type { StepHistoryEntry } from "../stores/sessionStore";

interface AgentRunTimelineProps {
  steps: StepHistoryEntry[];
  isGenerating: boolean;
  result: GenerationResult | null;
  repairHistory?: RepairStep[] | null;
  inspectReport?: InspectReport | null;
  onRetryPrompt?: () => void;
  onRerunCode?: () => void;
}

const STATUS_STYLES: Record<StepStatus, string> = {
  queued: "bg-gray-100 text-gray-600 border-gray-200",
  running: "bg-indigo-50 text-indigo-700 border-indigo-200",
  success: "bg-emerald-50 text-emerald-700 border-emerald-200",
  warn: "bg-amber-50 text-amber-700 border-amber-200",
  failed: "bg-red-50 text-red-700 border-red-200",
  skipped: "bg-gray-50 text-gray-500 border-gray-200",
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
  return STEP_LABELS[step.step] || step.step.replace(/_/g, " ");
}

function detailText(step: StepUpdate) {
  if (!step.detail) return "";
  const source = typeof step.detail.source === "string" ? step.detail.source : "";
  const errorType = typeof step.detail.error_type === "string" ? step.detail.error_type : "";
  return [source, errorType].filter(Boolean).join(" - ");
}

function printableLabel(inspectReport: InspectReport) {
  if (inspectReport.printable === false) return "\u5426";
  if (inspectReport.printable === true) return "\u662f";
  return "\u672a\u77e5";
}

export default function AgentRunTimeline({ steps, isGenerating, result, repairHistory, inspectReport, onRetryPrompt, onRerunCode }: AgentRunTimelineProps) {
  if (steps.length === 0 && !repairHistory?.length && !inspectReport) return null;

  const hasFailed = result?.success === false || steps.some((step) => normalizeStatus(step, false, false) === "failed");
  const hasCode = Boolean(result?.code);

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">{"Agent \u8fd0\u884c\u65f6\u95f4\u7ebf"}</h3>
          <p className="text-xs text-gray-500 mt-0.5">{"\u5c55\u793a CADAM \u98ce\u683c\u7684\u8fc7\u7a0b\u8fdb\u5ea6\uff0c\u5e76\u4fdd\u7559 ForgeCAD \u98ce\u683c\u7684\u68c0\u67e5\u4e0e\u4fee\u590d\u8bc1\u636e\u3002"}</p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {hasFailed && onRetryPrompt && (
            <button type="button" onClick={onRetryPrompt} disabled={isGenerating} className="px-2.5 py-1 text-xs rounded-md border border-indigo-200 text-indigo-700 hover:bg-indigo-50 disabled:opacity-50">
              {"\u91cd\u8bd5\u63d0\u793a\u8bcd"}
            </button>
          )}
          {hasCode && onRerunCode && (
            <button type="button" onClick={onRerunCode} disabled={isGenerating} className="px-2.5 py-1 text-xs rounded-md border border-gray-200 text-gray-700 hover:bg-gray-50 disabled:opacity-50">
              {"\u91cd\u65b0\u8fd0\u884c\u5f53\u524d\u4ee3\u7801"}
            </button>
          )}
        </div>
      </div>

      <div className="p-4 space-y-3">
        {steps.map((step, index) => {
          const isLast = index === steps.length - 1;
          const status = normalizeStatus(step, isLast, isGenerating);
          const duration = formatDuration(step.duration_ms);
          const detail = detailText(step);
          return (
            <div key={`${step.stage_id || step.step}-${index}`} className="flex gap-3">
              <div className="flex flex-col items-center">
                <span className={`w-6 h-6 rounded-full border text-xs flex items-center justify-center ${STATUS_STYLES[status]}`}>{STATUS_ICON[status]}</span>
                {index < steps.length - 1 && <span className="w-px flex-1 bg-gray-200 mt-1" />}
              </div>
              <div className="min-w-0 flex-1 pb-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium text-gray-900">{stageLabel(step)}</span>
                  <span className={`text-[10px] border rounded-full px-1.5 py-0.5 ${STATUS_STYLES[status]}`}>{STATUS_LABEL[status]}</span>
                  {step.attempt && <span className="text-[10px] text-gray-400">{"\u7b2c"} {step.attempt} {"\u6b21"}</span>}
                  {duration && <span className="text-[10px] text-gray-400">{duration}</span>}
                </div>
                <p className="text-xs text-gray-600 mt-0.5">{step.message}</p>
                {detail && <p className="text-[10px] text-gray-400 mt-1">{detail}</p>}
                {step.part_name && <p className="text-[10px] text-gray-400 mt-1">{"\u96f6\u4ef6"} {step.part_index || "?"}/{step.total_parts || "?"}: {step.part_name}</p>}
              </div>
            </div>
          );
        })}

        {(repairHistory?.length || inspectReport) && (
          <div className="rounded-lg bg-gray-50 border border-gray-100 p-3 space-y-1">
            {repairHistory?.length ? <p className="text-xs text-gray-600">{"\u4fee\u590d\u8bc1\u636e\uff1a\u5df2\u8bb0\u5f55"} {repairHistory.length} {"\u4e2a\u81ea\u52a8\u4fee\u590d\u6b65\u9aa4\u3002"}</p> : null}
            {inspectReport ? <p className="text-xs text-gray-600">{"\u68c0\u67e5\u8bc1\u636e\uff1a\u7ed3\u8bba"} {inspectReport.verdict}{"\uff0c\u53ef\u6253\u5370\u6027"} {printableLabel(inspectReport)}{"\u3002"}</p> : null}
          </div>
        )}
      </div>
    </div>
  );
}
