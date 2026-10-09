import { useEffect, useEffectEvent, useState } from "react";
import { adaptValidation } from "../../adapters/projectAdapter";
import { analyzeEngineeringResult, readEngineeringCheck } from "../../services/clients/checks";
import type { ArtifactHistoryEntry, StepHistoryEntry } from "../../stores/sessionStore";
import type { DesignAnalysis, GenerationResult, ModelSnapshotDetail, RunCreatedEvent } from "../../types";
import type { ValidationResult } from "../../types/engineering";
import AgentRunTimeline from "../AgentRunTimeline";
import DesignBriefPanel from "../DesignBriefPanel";
import InspectReportPanel from "../InspectReportPanel";
import RepairHistory from "../RepairHistory";
import VersionHistoryPanel from "../VersionHistoryPanel";
import { WorkspaceDialog, InlineState } from "../common/WorkspaceOverlay";
import { Icon } from '../ui/Icon';
import { getAuthUser } from '../../auth';

const GROUPS: ValidationResult["domain"][] = ["几何", "装配", "DFM", "ERC / DRC", "仿真", "固件", "文件导出"];

interface ValidationDialogProps {
  open: boolean;
  embedded?: boolean;
  result: GenerationResult | null;
  analysis?: DesignAnalysis | null;
  description: string;
  panelId: string;
  steps: StepHistoryEntry[];
  activeRun?: RunCreatedEvent | null;
  artifacts?: ArtifactHistoryEntry[];
  durableAgent?: import("../../types").DurableAgentSnapshotProjection | null;
  isGenerating: boolean;
  activeSnapshotId?: string | null;
  currentParts?: GenerationResult["assembly_parts"];
  onModifyPart?: (partName: string, instruction: string, partId?: string | null) => boolean | void;
  refreshKey?: string | number | null;
  onClose: () => void;
  onAnalysis?: (analysis: DesignAnalysis) => void;
  onAskAgent: (prompt: string) => void;
  onRestore: (snapshot: ModelSnapshotDetail) => boolean | void | Promise<boolean | void>;
  onRetryPrompt?: () => unknown;
  onRerunCode?: () => unknown;
  onResumeRun?: (runId: string) => unknown;
  viewLabel?: string;
}

function checkIconClass(status: ValidationResult["status"]) {
  if (status === "pass") return "bg-emerald-50 text-emerald-700";
  if (status === "fail") return "bg-red-50 text-red-700";
  return "bg-amber-50 text-amber-800";
}

export default function ValidationDialog({
  open, embedded = false,
  result,
  analysis: providedAnalysis = null,
  description,
  panelId,
  steps,
  activeRun,
  artifacts = [],
  durableAgent,
  isGenerating,
  activeSnapshotId,
  currentParts,
  onModifyPart,
  refreshKey,
  onClose,
  onAnalysis,
  onAskAgent,
  onRestore,
  onRetryPrompt,
  onRerunCode,
  onResumeRun,
  viewLabel,
}: ValidationDialogProps) {
  const [localAnalysis, setLocalAnalysis] = useState<DesignAnalysis | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "error" | "success">("idle");
  const [error, setError] = useState("");
  const sourceKey = `cad-check:${getAuthUser()?.id || 'local'}:${result?.request_id || ""}:${result?.revision_id || ""}`;
  const [pendingTask, setPendingTask] = useState<{ source: string; workflowId: string } | null>(() => {
    const saved = sessionStorage.getItem(sourceKey);
    return saved ? { source: sourceKey, workflowId: saved } : null;
  });
  // The immutable profile may arrive after the first result. Store only an
  // explicit user override, so a provisional empty result cannot mask it.
  const [processChoice, setProcessChoice] = useState<{source:string;value:string} | null>(null);
  const process=processChoice?.source===sourceKey ? processChoice.value : result?.manufacturing_profile?.process.toLowerCase() || "";
  const receive = useEffectEvent((value: DesignAnalysis) => onAnalysis?.(value));
  const [localSource, setLocalSource] = useState("");
  const [pollAttempt, setPollAttempt] = useState(0);
  const activeTask = pendingTask?.source === sourceKey ? pendingTask.workflowId : sessionStorage.getItem(sourceKey);
  useEffect(() => {
    if (!open || !activeTask) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await readEngineeringCheck(activeTask, controller.signal);
        if (controller.signal.aborted) return;
        if (next.task_status === "succeeded") {
          if (!next.analysis || result?.revision_id && next.analysis.source_revision_id !== result.revision_id) throw new Error("检查报告与当前查看版本不一致");
          setLocalAnalysis(next.analysis); setLocalSource(sourceKey); receive(next.analysis); setStatus("success");
          sessionStorage.removeItem(sourceKey); setPendingTask(null); return;
        }
        if (["failed", "cancelled", "timed_out"].includes(next.task_status)) {
          sessionStorage.removeItem(sourceKey); setPendingTask(null); throw new Error(next.error || "工程检查未完成");
        }
        timer = setTimeout(() => void poll(), 2000);
      } catch (reason) {
        if (!controller.signal.aborted) { setError(reason instanceof Error ? reason.message : "检查状态读取失败"); setStatus("error"); }
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [open, activeTask, sourceKey, result?.revision_id, pollAttempt]);
  const analysis = providedAnalysis && (result?.revision_id ? providedAnalysis.source_revision_id === result.revision_id : !providedAnalysis.source_revision_id) ? providedAnalysis : localSource === sourceKey ? localAnalysis : null;

  const checks = adaptValidation(result, analysis);
  const brief = result?.design_brief || result?.plan?.design_brief || null;
  const canAnalyzeDesign = Boolean(
    result?.success &&
      result.request_id &&
      result.files?.stl &&
      !result.needs_confirmation,
  );

  const currentVersionSummary = result ? {
    version: result.version ?? null,
    snapshotId: result.snapshot_id || activeSnapshotId || null,
    revisionId: result.revision_id || result.expected_base_revision_id || null,
  } : null;

  const run = async () => {
    if (!canAnalyzeDesign || !result?.request_id) {
      setStatus("error");
      setError("需要先完成包含 STL 的 CAD 生成，才能运行工程检查。");
      return;
    }
    setStatus("loading");
    setError("");
    try {
      const input = JSON.stringify([result.request_id, result.code || "", description,
        result.revision_id, process || null, result.manufacturing_profile?.material]);
      const inputHash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(input))), byte => byte.toString(16).padStart(2, '0')).join('');
      const receiptKey = `${sourceKey}:submission`;
      let receipt: { inputHash: string; id: string } | null = null;
      try { receipt = JSON.parse(sessionStorage.getItem(receiptKey) || 'null'); } catch { /* No valid prior submission. */ }
      if (receipt?.inputHash !== inputHash || !receipt.id) receipt = { inputHash, id: crypto.randomUUID() };
      sessionStorage.setItem(receiptKey, JSON.stringify(receipt));
      const nextAnalysis = await analyzeEngineeringResult(
        result.request_id,
        result.code || "",
        description,
        { revisionId: result.revision_id || undefined, process: process || null, material: result.manufacturing_profile?.material, idempotencyKey: receipt.id },
      );
      if (nextAnalysis.kind === "pending") {
        sessionStorage.setItem(sourceKey, nextAnalysis.workflowId);
        setPendingTask({ source: sourceKey, workflowId: nextAnalysis.workflowId });
      } else {
        if (result.revision_id && nextAnalysis.analysis.source_revision_id !== result.revision_id) throw new Error("检查报告与当前查看版本不一致");
        setLocalAnalysis(nextAnalysis.analysis); setLocalSource(sourceKey); onAnalysis?.(nextAnalysis.analysis); setStatus("success");
      }
      sessionStorage.removeItem(receiptKey);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "工程检查失败");
      setStatus("error");
    }
  };

  const clarificationPrompt = brief?.open_questions?.length
    ? `请逐项补充设计简报中的待确认问题：${brief.open_questions.join("；")}。我的回答是：`
    : "请帮助我确认当前工程设计简报后继续生成。";

  return (
    <WorkspaceDialog embedded={embedded}
      description={viewLabel || "汇总当前任务的设计简报、执行过程、几何检查、修复记录和历史版本。"}
      footer={
        <div className="flex justify-end gap-2">
          <button className="workspace-button" onClick={onClose} type="button">
            关闭
          </button>
          <button
            className="workspace-button workspace-button--primary"
            disabled={!canAnalyzeDesign || status === "loading" || Boolean(activeTask)}
            onClick={() => void run()}
            type="button"
          >
            {status === "loading" || activeTask ? "正在检查" : "运行工程检查"}
          </button>
        </div>
      }
      onClose={onClose}
      open={open}
      title="工程检查与证据"
    >
      <div className="space-y-5 p-5">
        <DesignBriefPanel brief={brief} />
        {canAnalyzeDesign ? <label className="ww-form-grid">检查工艺<select aria-label="检查工艺" value={process} disabled={status === "loading" || Boolean(activeTask)} onChange={event => setProcessChoice({source:sourceKey,value:event.target.value})}>
          <option value="">请选择此版本的制造工艺</option>{['cnc','fdm','sla','injection_mold','sheet_metal'].map(value => <option key={value} value={value}>{value.toUpperCase()}</option>)}
          {process && !['cnc','fdm','sla','injection_mold','sheet_metal'].includes(process) ? <option value={process}>{process}</option> : null}
        </select></label> : null}
        {result?.manufacturing_profile && process && process !== result.manufacturing_profile.process.toLowerCase() ? <p className="type-caption">此工艺用于参考比较，不改变当前修订已保存的制造设置。</p> : null}

        {currentVersionSummary ? (
          <div className="rounded-lg border border-[var(--line)] bg-[var(--surface)] px-3 py-2 type-caption text-[var(--ink)]">
            <div className="type-control">当前版本概览</div>
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 type-caption">
              <span>查看修订：{currentVersionSummary.revisionId || "未知"}</span>
              <span>快照：{currentVersionSummary.snapshotId || "未生成"}</span>
              <span>基线：{currentVersionSummary.revisionId || "未知"}</span>
            </div>
          </div>
        ) : null}

        {result?.needs_confirmation ? (
          <InlineState
            actionLabel="补充确认信息"
            detail="后端已暂时停止 CAD 生成。补充关键尺寸、用途或安装方式后，系统会继续沿用当前设计简报。"
            onAction={() => onAskAgent(clarificationPrompt)}
            title="设计简报等待确认"
            tone="info"
          />
        ) : null}

        <AgentRunTimeline
          activeRun={activeRun}
          artifacts={artifacts}
          durableAgent={durableAgent}
          inspectReport={result?.inspect_report}
          isGenerating={isGenerating}
          onRerunCode={result?.code ? onRerunCode : undefined}
          onResumeRun={onResumeRun}
          onRetryPrompt={!result?.needs_confirmation ? onRetryPrompt : undefined}
          repairHistory={result?.repair_history}
          result={result}
          steps={steps}
        />

        <RepairHistory attempts={result?.attempts} steps={result?.repair_history} />
        <InspectReportPanel report={result?.inspect_report} />

        {!result?.success ? (
          <InlineState
            detail="先完成一次成功的 CAD 生成，再运行几何和制造检查。"
            title="暂无可检查结果"
          />
        ) : null}

        {status === "loading" || activeTask && status !== "error" ? (
          <InlineState
            detail="正在调用工程检查接口并整理几何、DFM 与可制造性证据。"
            title="正在运行工程检查"
            tone="info"
          />
        ) : null}

        {status === "error" ? (
          <InlineState
            actionLabel="重试"
            detail={error}
            onAction={() => { if (activeTask) { setStatus("loading"); setPollAttempt(value => value + 1); } else void run(); }}
            title="工程检查失败"
            tone="error"
          />
        ) : null}

        {checks.length === 0 && result?.success && status !== "loading" ? (
          <InlineState
            detail="当前结果没有 inspect report。点击“运行工程检查”获取真实分析。"
            title="尚未运行检查"
          />
        ) : null}

        {analysis?.rule_configuration ? <details className="ww-check-configuration"><summary>本次检查规则配置</summary><p className="type-caption break-all" data-i18n-skip>SHA-256: {analysis.rule_configuration.sha256}</p><p className="type-caption">{analysis.rule_configuration.origin === "admission" ? "规则已在任务受理时冻结；后续设置修改仅用于新检查。" : "旧任务的规则在首次执行时冻结，重试沿用该配置。"}</p><ul>{analysis.rule_configuration.rules.map(rule => <li key={rule.id} className="type-caption"><span data-i18n-skip>{rule.id}</span> · {rule.enabled ? "已启用" : "未启用"} · <span data-i18n-skip>{rule.threshold_min ?? "—"} – {rule.threshold_max ?? "—"} {rule.unit}</span></li>)}</ul></details> : null}
        {GROUPS.map((group) => {
          const items = checks.filter((check) => check.domain === group);
          if (!items.length) return null;
          return (
            <section key={group}>
              <h3 className="mb-2 type-section-heading  text-[var(--ink)]">{group}</h3>
              <div className="space-y-2">
                {items.map((check) => (
                  <article
                    className="grid gap-3 rounded-lg border border-[var(--line)] p-3 sm:grid-cols-[24px_minmax(0,1fr)_auto]"
                    key={check.id}
                  >
                    <span className={`mt-0.5 grid h-6 w-6 place-items-center rounded-full type-body ${checkIconClass(check.status)}`}>
                      <Icon name={check.status === "pass" ? "shield-check" : "warning"} size={18}/>
                    </span>
                    <div>
                      <h4 className="type-section-heading  text-[var(--ink)]">{check.title}</h4>
                      <p className="mt-1 type-body  text-[var(--muted)]">{check.description}</p>
                      <dl className="mt-2 grid gap-1 type-caption text-[var(--faint)] sm:grid-cols-2">
                        {check.impact ? (
                          <div>
                            <dt className="inline ">影响：</dt>
                            <dd className="inline">{check.impact}</dd>
                          </div>
                        ) : null}
                        <div>
                          <dt className="inline ">对象：</dt>
                          <dd className="inline">{check.object}</dd>
                        </div>
                        {check.suggestion ? (
                          <div className="sm:col-span-2">
                            <dt className="inline ">建议：</dt>
                            <dd className="inline">{check.suggestion}</dd>
                          </div>
                        ) : null}
                      </dl>
                    </div>
                    {check.status === "fail" ? (
                      <button
                        className="workspace-button self-start"
                        onClick={() => onAskAgent(`请依据修订 ${check.revisionId || result?.revision_id || "未提供"}、证据 ${check.evidenceId || "尚无独立证据 ID，需要先核验"}、检查项 ${check.id}、对象 ${check.object}，先核对证据归属和实际影响范围，再修复“${check.title}”：${check.description}${check.suggestion ? `。建议：${check.suggestion}` : ""}`)}
                        type="button"
                      >
                        请求 Agent 修复
                      </button>
                    ) : check.status==='unknown' && !['disabled','skipped'].includes(check.outcome || '') ? <p className="type-caption text-[var(--muted)]">尚未证实几何有错；请补充测量或输入后重试检查。</p> : null}
                  </article>
                ))}
              </div>
            </section>
          );
        })}

        {GROUPS.filter((group) => !checks.some((check) => check.domain === group)).length ? (
          <p className="type-caption text-[var(--faint)]">
            当前没有这些领域的真实检查结果，界面仅展示已验证证据，不用推测数据代替结果。
          </p>
        ) : null}

        {!embedded ? <VersionHistoryPanel
          activeSnapshotId={activeSnapshotId}
          currentParts={currentParts || result?.assembly_parts || []}
          onModifyPart={onModifyPart}
          onRestore={onRestore}
          panelId={panelId}
          refreshKey={refreshKey}
        /> : null}
      </div>
    </WorkspaceDialog>
  );
}
