import { authFetch } from "../auth";
import type {
  DesignAnalysis,
  DurableTaskEvent,
  DurableTaskSnapshot,
  GenerationResult,
  ModelSnapshotDetail,
  ModelSnapshotSummary,
} from "../types";
import type {
  ChangeSetActionResult,
  DurableChangeSetDetail,
  HistoryProject,
  OnshapeConfig,
  OnshapeLink,
} from "../types/engineering";

const API_BASE = import.meta.env.VITE_API_BASE || "";

interface PanelSummary {
  id: string;
  title: string;
  current_code: string | null;
  project_id?: string | null;
  branch_id?: string | null;
  current_revision_id?: string | null;
  active_workflow_run_id?: string | null;
  active_workflow_status?: string | null;
}

interface MessageData {
  role: "user" | "assistant";
  content: string;
  result?: GenerationResult;
}

export interface RestoredProject {
  sessionId: string;
  panels: Array<{
    id: string;
    title: string;
    messages: MessageData[];
    currentCode: string | null;
    projectId: string | null;
    branchId: string | null;
    currentRevisionId: string | null;
    workflowRunId: string | null;
    workflowStatus: string | null;
  }>;
}

function apiErrorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    if (typeof record.message === "string" && record.message.trim()) return record.message;
    if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
    if (record.detail) return apiErrorMessage(record.detail, fallback);
  }
  return fallback;
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: unknown };
    throw new Error(apiErrorMessage(body.detail, fallback));
  }
  return response.json() as Promise<T>;
}

export async function listHistoryProjects(): Promise<HistoryProject[]> {
  const response = await authFetch(`${API_BASE}/api/history/sessions`);
  const rows = await readJson<Array<{
    id: string;
    project_id?: string | null;
    title: string;
    updated_at: string;
  }>>(response, "历史项目加载失败");
  return rows.map((row) => ({
    id: row.id,
    projectId: row.project_id || null,
    name: row.title || `项目 ${row.id.slice(0, 8)}`,
    updatedAt: row.updated_at,
  }));
}

export async function restoreHistoryProject(sessionId: string): Promise<RestoredProject> {
  const panelResponse = await authFetch(`${API_BASE}/api/history/sessions/${sessionId}/panels`);
  const panels = await readJson<PanelSummary[]>(panelResponse, "项目面板加载失败");
  const hydrated = await Promise.all(panels.map(async (panel) => {
    const response = await authFetch(`${API_BASE}/api/history/panels/${panel.id}/messages`);
    const messages = await readJson<MessageData[]>(response, "项目消息加载失败");
    return {
      id: panel.id,
      title: panel.title || "工程任务",
      messages,
      currentCode: panel.current_code,
      projectId: panel.project_id || null,
      branchId: panel.branch_id || null,
      currentRevisionId: panel.current_revision_id || null,
      workflowRunId: panel.active_workflow_run_id || null,
      workflowStatus: panel.active_workflow_status || null,
    };
  }));
  return { sessionId, panels: hydrated };
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

export async function getDurableChangeSet(
  changeSetId: string,
): Promise<DurableChangeSetDetail> {
  const response = await authFetch(
    `${API_BASE}/api/change-sets/${encodeURIComponent(changeSetId)}`,
  );
  return readJson<DurableChangeSetDetail>(response, "变更审查加载失败");
}

async function changeSetAction(
  changeSetId: string,
  action: "accept" | "reject" | "request-change" | "commit" | "rollback",
  note?: string,
): Promise<ChangeSetActionResult> {
  const response = await authFetch(
    `${API_BASE}/api/change-sets/${encodeURIComponent(changeSetId)}/${action}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: action === "commit" ? undefined : JSON.stringify({ note: note || "" }),
    },
  );
  return readJson<ChangeSetActionResult>(response, "变更操作失败");
}

export const acceptDurableChangeSet = (id: string, note = "") =>
  changeSetAction(id, "accept", note);
export const rejectDurableChangeSet = (id: string, note: string) =>
  changeSetAction(id, "reject", note);
export const requestDurableChangeSetModification = (id: string, note: string) =>
  changeSetAction(id, "request-change", note);
export const commitDurableChangeSet = (id: string) =>
  changeSetAction(id, "commit");
export const rollbackDurableChangeSet = (id: string, note = "") =>
  changeSetAction(id, "rollback", note);

export async function deleteHistoryProject(sessionId: string): Promise<void> {
  const response = await authFetch(`${API_BASE}/api/history/sessions/${sessionId}`, { method: "DELETE" });
  if (!response.ok) throw new Error("删除项目失败，请重试");
}

export async function listModelSnapshots(
  panelId: string,
): Promise<ModelSnapshotSummary[]> {
  const response = await authFetch(
    `${API_BASE}/api/history/panels/${encodeURIComponent(panelId)}/snapshots`,
  );
  return readJson<ModelSnapshotSummary[]>(response, "版本列表加载失败");
}

export async function getModelSnapshot(
  snapshotId: string,
): Promise<ModelSnapshotDetail> {
  const response = await authFetch(
    `${API_BASE}/api/history/snapshots/${encodeURIComponent(snapshotId)}`,
  );
  return readJson<ModelSnapshotDetail>(response, "版本证据加载失败");
}

export async function analyzeEngineeringResult(requestId: string, code: string, description: string): Promise<DesignAnalysis> {
  const response = await authFetch(`${API_BASE}/api/analyze/${encodeURIComponent(requestId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, description }),
  });
  return readJson<DesignAnalysis>(response, "工程检查失败");
}

export async function downloadEngineeringArtifact(url: string, filename: string): Promise<void> {
  const external = /^https?:\/\//.test(url);
  const target = external ? url : `${API_BASE}${url}`;
  // S3-compatible presigned URLs already carry short-lived authorization in
  // their query string. Sending the WordsWave bearer token to that host both
  // leaks credentials and can invalidate the object-store signature.
  const response = external ? await fetch(target) : await authFetch(target);
  if (!response.ok) throw new Error(`文件下载失败（HTTP ${response.status}）`);
  const objectUrl = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

export async function getOnshapeConfig(): Promise<OnshapeConfig> {
  const response = await authFetch(`${API_BASE}/api/onshape/config`);
  return readJson<OnshapeConfig>(response, "Onshape 配置状态加载失败");
}

export async function listOnshapeLinks(requestId: string): Promise<OnshapeLink[]> {
  const response = await authFetch(`${API_BASE}/api/onshape/links/${encodeURIComponent(requestId)}`);
  return readJson<OnshapeLink[]>(response, "Onshape 发布记录加载失败");
}

export async function publishToOnshape(requestId: string, stepFilename: string): Promise<OnshapeLink> {
  const response = await authFetch(`${API_BASE}/api/onshape/publish`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      request_id: requestId,
      step_filename: stepFilename,
      wait_for_completion: false,
    }),
  });
  return readJson<OnshapeLink>(response, "发布到 Onshape 失败");
}

export async function refreshOnshapeLink(requestId: string): Promise<OnshapeLink> {
  const response = await authFetch(`${API_BASE}/api/onshape/links/${encodeURIComponent(requestId)}/refresh`, {
    method: "POST",
  });
  return readJson<OnshapeLink>(response, "Onshape 导入状态刷新失败");
}
