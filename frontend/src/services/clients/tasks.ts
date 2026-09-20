import { authFetch } from "../../auth";
import type { DurableTaskEvent, DurableTaskSnapshot } from "../../types";
import { API_BASE } from "./http";
import { readJson } from "./http";

export async function cancelEngineeringTask(taskId: string): Promise<void> {
  await readJson(await authFetch(`${API_BASE}/api/tasks/${taskId}/cancel`, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({reason:'用户取消工程计算'})}), '取消工程任务失败');
}

export interface DurableTaskEventPage {
  workflow_run_id: string;
  events: DurableTaskEvent[];
  after_sequence: number;
  next_cursor: number;
  earliest_sequence: number;
  current_sequence: number;
  has_more: boolean;
}

export async function getDurableTaskSnapshot(
  workflowRunId: string,
): Promise<DurableTaskSnapshot> {
  const response = await authFetch(
    `${API_BASE}/api/tasks/${encodeURIComponent(workflowRunId)}/snapshot`,
  );
  return readJson<DurableTaskSnapshot>(response, "任务状态加载失败");
}

export async function listDurableTaskEvents(
  workflowRunId: string,
  afterSequence: number,
): Promise<DurableTaskEventPage> {
  const query = new URLSearchParams({
    after_sequence: String(afterSequence),
    limit: "100",
  });
  const response = await authFetch(
    `${API_BASE}/api/tasks/${encodeURIComponent(workflowRunId)}/events?${query}`,
  );
  return readJson<DurableTaskEventPage>(response, "任务事件加载失败");
}

export interface DurableConfirmationResult {
  workflow_run_id: string;
  accepted: boolean;
  status: "signal_delivered";
}

export async function confirmDurableTask(
  workflowRunId: string,
  accepted: boolean,
  note = "",
): Promise<DurableConfirmationResult> {
  const response = await authFetch(
    `${API_BASE}/api/tasks/${encodeURIComponent(workflowRunId)}/confirmation`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accepted, note }),
    },
  );
  return readJson<DurableConfirmationResult>(response, "任务确认失败");
}

export async function retryDurableTask(workflowRunId:string,idempotencyKey:string) {
  const response=await authFetch(`${API_BASE}/api/tasks/${encodeURIComponent(workflowRunId)}/retry`,{
    method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({idempotency_key:idempotencyKey}),
  });
  return readJson<{workflow_run_id:string;project_id:string;branch_id:string;expected_base_revision_id:string;status:string}>(response,"重试提交失败");
}

export interface TaskValidationEvidence {
  evidence_id:string; evidence_hash:string; workflow_run_id:string;
  staging_manifest_id:string; revision_id:string | null; selected_for_revision:boolean;
  gate:string; mode:string; outcome:string; report:Record<string,unknown>;
}

export async function getTaskValidationEvidence(taskId:string,evidenceId:string) {
  return readJson<TaskValidationEvidence>(await authFetch(
    `${API_BASE}/api/tasks/${encodeURIComponent(taskId)}/validations/${encodeURIComponent(evidenceId)}`,
  ),'检查证据读取失败');
}
