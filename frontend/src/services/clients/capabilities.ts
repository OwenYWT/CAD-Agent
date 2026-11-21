import { API_BASE, authFetch, readJson } from "./http";

export interface RunResult {
  status: "succeeded" | "blocked" | "failed" | "dry_run";
  capability: string;
  action: string;
  request_id: string;
  data?: unknown;
  files?: Array<{ name: string; url?: string; sha256?: string; size_bytes?: number }>;
  checks?: Array<Record<string, unknown>>;
  command_preview?: string[];
  blocked_reasons?: string[];
  error?: string;
}
export async function uploadCapabilityFile(file: File): Promise<{ input: string }> {
  const form = new FormData();
  form.append("file", file);
  return readJson(await authFetch(`${API_BASE}/api/capability-artifacts`, { method: "POST", body: form }), "上传失败");
}

export async function executeCapability(capability: string, action: string, params: Record<string, unknown>): Promise<RunResult> {
  return readJson(await authFetch(`${API_BASE}/api/capability-actions/${encodeURIComponent(capability)}/${encodeURIComponent(action)}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ params }),
  }), "运行失败");
}
