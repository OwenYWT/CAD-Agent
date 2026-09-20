import { authFetch } from "../../auth";
import type { DurableTaskSnapshot, GenerationResult } from "../../types";
import type { HistoryProject } from "../../types/engineering";
import { API_BASE } from "./http";
import { readJson } from "./http";
import { getDurableTaskSnapshot } from "./tasks";

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
    changeSetId: string | null;
    taskSnapshot: DurableTaskSnapshot | null;
  }>;
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
    const workflowRunId = panel.active_workflow_run_id || null;
    const [messages, taskSnapshot] = await Promise.all([
      authFetch(`${API_BASE}/api/history/panels/${panel.id}/messages`).then(
        (response) => readJson<MessageData[]>(response, "项目消息加载失败"),
      ),
      workflowRunId
        ? getDurableTaskSnapshot(workflowRunId)
        : Promise.resolve(null),
    ]);
    return {
      id: panel.id,
      title: panel.title || "工程任务",
      messages,
      currentCode: panel.current_code,
      projectId: panel.project_id || null,
      branchId: panel.branch_id || null,
      currentRevisionId: panel.current_revision_id || null,
      workflowRunId,
      workflowStatus: taskSnapshot?.status
        || panel.active_workflow_status
        || null,
      changeSetId: taskSnapshot?.change_set?.id || null,
      taskSnapshot,
    };
  }));
  return { sessionId, panels: hydrated };
}
