import { authFetch } from "../../auth";
import type { DesignAnalysis } from "../../types";
import { API_BASE, readJson } from "./http";
import { engineeringCheckResponse, validAnalysis } from '../../adapters/engineeringCheck';
import type { EngineeringCheck } from '../../adapters/engineeringCheck';
export type { EngineeringCheck } from '../../adapters/engineeringCheck';

export interface EngineeringCheckStatus { workflow_run_id: string; task_status: string; error?: string | null; analysis: DesignAnalysis | null; source_revision_id?: string }
export async function analyzeEngineeringResult(requestId: string, code: string, description: string, options: { revisionId?: string; process?: string | null; material?: string | null; idempotencyKey?: string } = {}): Promise<EngineeringCheck> {
  const response = await authFetch(`${API_BASE}/api/analyze/${encodeURIComponent(requestId)}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, description, source_revision_id: options.revisionId, process: options.process, material: options.material, asynchronous: true, idempotency_key: options.idempotencyKey || crypto.randomUUID() }),
  });
  if (response.status === 202 || (response.status === 504 && response.headers.get("X-Workflow-Run-ID"))) {
    return engineeringCheckResponse(response.status,await response.json().catch(() => ({})),response.headers.get("X-Workflow-Run-ID"));
  }
  return engineeringCheckResponse(response.status,await readJson<DesignAnalysis>(response,"工程检查失败"));
}
export async function readEngineeringCheck(workflowId: string, signal?: AbortSignal): Promise<EngineeringCheckStatus> {
  const result = await readJson<EngineeringCheckStatus>(await authFetch(`${API_BASE}/api/analyze/tasks/${encodeURIComponent(workflowId)}`, { signal }), "检查状态读取失败");
  if (result.workflow_run_id !== workflowId || typeof result.task_status !== "string") throw new Error("检查任务身份不匹配");
  if (result.analysis) validAnalysis(result.analysis);
  return result;
}
