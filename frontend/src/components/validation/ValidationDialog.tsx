import { useState } from "react";
import { adaptValidation } from "../../adapters/projectAdapter";
import { analyzeEngineeringResult } from "../../services/engineeringService";
import type { DesignAnalysis, GenerationResult } from "../../types";
import type { ValidationResult } from "../../types/engineering";
import { WorkspaceDialog, InlineState } from "../common/WorkspaceOverlay";

const GROUPS: ValidationResult["domain"][] = ["几何", "装配", "DFM", "ERC / DRC", "仿真", "固件", "文件导出"];

interface ValidationDialogProps {
  open: boolean;
  result: GenerationResult | null;
  description: string;
  onClose: () => void;
  onAskAgent: (prompt: string) => void;
}

export default function ValidationDialog({ open, result, description, onClose, onAskAgent }: ValidationDialogProps) {
  const [analysis, setAnalysis] = useState<DesignAnalysis | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "error" | "success">("idle");
  const [error, setError] = useState("");
  const checks = adaptValidation(result, analysis);

  const run = async () => {
    if (!result?.request_id) { setStatus("error"); setError("当前结果没有 request_id，无法调用分析接口。"); return; }
    setStatus("loading");
    setError("");
    try {
      setAnalysis(await analyzeEngineeringResult(result.request_id, result.code || "", description));
      setStatus("success");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "工程检查失败");
      setStatus("error");
    }
  };

  return (
    <WorkspaceDialog description="结果来自当前生成验证、inspect report 和按需调用的现有分析接口。未接入领域不会显示伪造通过项。" footer={<div className="flex justify-end gap-2"><button className="workspace-button" onClick={onClose} type="button">关闭</button><button className="workspace-button workspace-button--primary" disabled={!result?.success || status === "loading"} onClick={() => void run()} type="button">{status === "loading" ? "正在检查" : "运行工程检查"}</button></div>} onClose={onClose} open={open} title="工程检查">
      <div className="space-y-5 p-5">
        {!result?.success ? <InlineState detail="先完成一次成功的 CAD 生成，再运行几何和 DFM 检查。" title="暂无可检查结果" /> : null}
        {status === "loading" ? <InlineState detail="正在调用现有分析接口并整理几何、DFM 和制造风险。" title="正在运行工程检查" tone="info" /> : null}
        {status === "error" ? <InlineState actionLabel="重试" detail={error} onAction={() => void run()} title="工程检查失败" tone="error" /> : null}
        {checks.length === 0 && result?.success && status !== "loading" ? <InlineState detail="当前生成结果没有 inspect report。点击“运行工程检查”获取真实分析。" title="尚未运行检查" /> : GROUPS.map((group) => {
          const items = checks.filter((check) => check.domain === group);
          if (!items.length) return null;
          return <section key={group}><h3 className="mb-2 text-xs font-semibold text-[var(--ink)]">{group}</h3><div className="space-y-2">{items.map((check) => <article className="grid gap-3 rounded-lg border border-[var(--line)] p-3 sm:grid-cols-[24px_minmax(0,1fr)_auto]" key={check.id}><span className={`mt-0.5 grid h-6 w-6 place-items-center rounded-full text-xs ${check.status === "pass" ? "bg-emerald-50 text-emerald-700" : check.status === "fail" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-800"}`}>{check.status === "pass" ? "✓" : "!"}</span><div><h4 className="text-xs font-semibold text-[var(--ink)]">{check.title}</h4><p className="mt-1 text-xs leading-5 text-[var(--muted)]">{check.description}</p><dl className="mt-2 grid gap-1 text-[11px] text-[var(--faint)] sm:grid-cols-2">{check.impact ? <div><dt className="inline font-medium">影响：</dt><dd className="inline">{check.impact}</dd></div> : null}<div><dt className="inline font-medium">对象：</dt><dd className="inline">{check.object}</dd></div>{check.suggestion ? <div className="sm:col-span-2"><dt className="inline font-medium">建议：</dt><dd className="inline">{check.suggestion}</dd></div> : null}</dl></div>{check.status !== "pass" ? <button className="workspace-button self-start" onClick={() => onAskAgent(`请修复检查项“${check.title}”：${check.description}${check.suggestion ? `。建议：${check.suggestion}` : ""}`)} type="button">让 Agent 修复</button> : null}</article>)}</div></section>;
        })}
        {GROUPS.filter((group) => !checks.some((check) => check.domain === group)).length ? <p className="text-[11px] text-[var(--faint)]">未显示的领域当前没有真实检查接口或结果，不以演示数据代替。</p> : null}
      </div>
    </WorkspaceDialog>
  );
}
