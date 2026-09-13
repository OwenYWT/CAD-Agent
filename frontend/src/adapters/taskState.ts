import type { PanelState } from "../stores/sessionStore";
import type { CloudDocument } from "../types/document";

export type TaskPhase = "needs_input" | "running" | "failed" | "candidate" | "saved";
export const TASK_PHASE_LABELS: Record<TaskPhase,string> = {
  needs_input:"等待补充", running:"执行中", failed:"失败", candidate:"候选待确认", saved:"已保存",
};
export const isActiveTask = (status?: string | null) => ["pending","planning","running","cancelling"].includes(status || "");

/** One projection for the active task. Document sync and the viewed revision are separate facts. */
export function taskState(panel: PanelState, document?: CloudDocument | null) {
  const durable=panel.durable;
  const rejected = !panel.submissionPending && panel.submissionFailure;
  const unconfirmed = rejected && rejected.submission_outcome === 'unknown';
  const snapshot=!panel.submissionPending && !rejected && durable?.snapshot?.id===durable?.workflowRunId ? durable?.snapshot : null;
  const taskId=panel.submissionPending || rejected ? null : durable?.workflowRunId || null;
  const status=panel.submissionPending ? "submitting" : unconfirmed ? "unconfirmed" : rejected ? "rejected" : durable?.taskStatus;
  const result=rejected || (panel.result?.workflow_run_id===taskId ? panel.result : null);
  const hasArtifacts = Boolean(panel.result?.success && Object.values(panel.result.files || {}).some(Boolean));
  const hasSaved = Boolean(document?.fcstd || hasArtifacts && (
    document?.view_mode === 'committed' || document?.view_mode === 'history'
    || !panel.result?.change_set_id && !durable?.changeSetId
  ));
  const error=panel.submissionPending || isActiveTask(status) ? null : snapshot?.error || result?.error || null;
  const errorCode=panel.submissionPending || isActiveTask(status) ? "" : error && ('code' in error ? error.code : error.type) || snapshot?.error_code || "";
  const errorMessage=panel.submissionPending || isActiveTask(status) ? "" : error?.message || snapshot?.error_message || panel.lastError || "";
  const quota=/ProviderQuotaError|insufficient_quota|额度不足/i.test(errorCode+errorMessage);
  const changeStatus=durable?.changeSetStatus || snapshot?.change_set?.status;
  const operation=String(snapshot?.request_payload.operation || (snapshot?.kind.endsWith('.modify') ? 'modify' : 'generate'));
  const operationLabel=operation==='modify' ? '修改模型' : operation==='inspect' ? '校验模型' : '首次生成';
  const recovery = !taskId ? 'request' : snapshot?.request_payload.structured_modification ? 'properties'
    : snapshot?.request_payload.revision_restore ? 'versions'
    : snapshot && !['mcad.agent.v2.generate','mcad.agent.v2.modify'].includes(snapshot.kind) ? 'request' : 'retry';
  let phase: TaskPhase;
  if (panel.submissionPending || isActiveTask(status) || (!taskId && panel.isGenerating)) phase='running';
  else if (unconfirmed) phase='needs_input';
  else if (['failed','timed_out','cancelled'].includes(status || '') || errorMessage) phase='failed';
  else if (status==='waiting_confirmation' || status!=='succeeded' && result?.needs_confirmation) phase='needs_input';
  else if (status==='succeeded' && durable?.changeSetId && !['committed','rejected','rolled_back'].includes(changeStatus || '')) phase='candidate';
  else if (hasSaved || changeStatus==='committed') phase='saved';
  else phase='needs_input';
  const title=phase==='failed' ? status==='cancelled' ? '任务已取消' : quota ? '模型服务额度不足，本次未生成模型' : '本次任务失败'
    : phase==='running' ? panel.submissionPending ? '正在提交需求' : status==='cancelling' ? '正在取消任务' : operationLabel
    : phase==='candidate' ? '模型已生成，候选待确认'
    : phase==='saved' ? '模型已保存为当前版本' : unconfirmed ? '等待确认任务是否受理' : snapshot?.confirmation ? '执行计划等待确认' : '等待补充需求';
  return {phase,label:TASK_PHASE_LABELS[phase],title,taskId,status,operationLabel,hasSaved,quota,errorCode,errorMessage,error,snapshot,recovery,
    running:phase==='running',changeSetId:durable?.changeSetId || null,changeStatus,
    objective:typeof snapshot?.request_payload.objective==='string' ? snapshot.request_payload.objective
      : panel.messages.filter(m=>m.role==='user').at(-1)?.content || ''};
}
