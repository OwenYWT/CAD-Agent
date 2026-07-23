import type { RepairStep } from "../types";

interface RepairHistoryProps {
  steps?: RepairStep[] | null;
  attempts?: number;
}

const STAGE_LABEL: Record<string, string> = {
  validation: "\u6821\u9a8c\u9636\u6bb5",
  static_analysis: "\u9759\u6001\u5206\u6790",
  execution: "\u4ee3\u7801\u6267\u884c",
  geometry: "\u51e0\u4f55\u68c0\u67e5",
  vision: "\u89c6\u89c9\u68c0\u67e5",
};

const ACTION_LABEL: Record<string, string> = {
  fix_error: "\u4fee\u590d\u9519\u8bef",
  fix_visual_issues: "\u4fee\u590d\u89c6\u89c9\u95ee\u9898",
};

const STATUS_LABEL: Record<string, string> = {
  repaired: "\u5df2\u4fee\u590d",
  failed: "\u5931\u8d25",
  skipped: "\u5df2\u8df3\u8fc7",
};

export default function RepairHistory({ steps, attempts }: RepairHistoryProps) {
  if (!steps || steps.length === 0) return null;

  return (
    <div className="rounded-xl border border-amber-200 bg-amber-50/70 p-3 text-xs text-amber-900">
      <div className="flex items-center justify-between gap-2">
        <div className="font-semibold">{"\u81ea\u52a8\u4fee\u590d\u8bb0\u5f55"}</div>
        {attempts && attempts > 1 && (
          <div className="text-[11px] text-amber-700">{"\u5171\u5c1d\u8bd5"} {attempts} {"\u6b21"}</div>
        )}
      </div>
      <div className="mt-2 space-y-2">
        {steps.map((step, index) => (
          <div key={`${step.attempt}-${step.stage}-${index}`} className="flex gap-2">
            <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-amber-200 text-[10px] font-semibold text-amber-800">
              {step.attempt}
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium">{STAGE_LABEL[step.stage] || step.stage}</span>
                <span className="rounded bg-white/70 px-1.5 py-0.5 text-[10px] text-amber-700">
                  {ACTION_LABEL[step.action] || step.action}
                </span>
                <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-700">
                  {STATUS_LABEL[step.status] || step.status}
                </span>
              </div>
              <div className="mt-0.5 truncate text-amber-700" title={step.message}>
                {step.error_type}: {step.message}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
