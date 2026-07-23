import { authFetch } from "../auth";
import type { DesignAnalysis, GenerationResult } from "../types";
import type { HistoryProject, OnshapeConfig, OnshapeLink } from "../types/engineering";

const API_BASE = import.meta.env.VITE_API_BASE || "";

interface PanelSummary {
  id: string;
  title: string;
  current_code: string | null;
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
  }>;
}

function apiErrorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    if (typeof record.message === "string" && record.message.trim()) return record.message;
    if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
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
  const rows = await readJson<Array<{ id: string; title: string; updated_at: string }>>(response, "历史项目加载失败");
  return rows.map((row) => ({ id: row.id, name: row.title || `项目 ${row.id.slice(0, 8)}`, updatedAt: row.updated_at }));
}

export async function restoreHistoryProject(sessionId: string): Promise<RestoredProject> {
  const panelResponse = await authFetch(`${API_BASE}/api/history/sessions/${sessionId}/panels`);
  const panels = await readJson<PanelSummary[]>(panelResponse, "项目面板加载失败");
  const hydrated = await Promise.all(panels.map(async (panel) => {
    const response = await authFetch(`${API_BASE}/api/history/panels/${panel.id}/messages`);
    const messages = await readJson<MessageData[]>(response, "项目消息加载失败");
    return { id: panel.id, title: panel.title || "工程任务", messages, currentCode: panel.current_code };
  }));
  return { sessionId, panels: hydrated };
}

export async function deleteHistoryProject(sessionId: string): Promise<void> {
  const response = await authFetch(`${API_BASE}/api/history/sessions/${sessionId}`, { method: "DELETE" });
  if (!response.ok) throw new Error("删除项目失败，请重试");
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
  const target = /^https?:\/\//.test(url) ? url : `${API_BASE}${url}`;
  const response = await authFetch(target);
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
