import { authFetch } from "../../auth";
import type { DesignAnalysis } from "../../types";
import { API_BASE } from "./http";
import { readJson } from "./http";

export async function analyzeEngineeringResult(requestId: string, code: string, description: string): Promise<DesignAnalysis> {
  const response = await authFetch(`${API_BASE}/api/analyze/${encodeURIComponent(requestId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, description }),
  });
  return readJson<DesignAnalysis>(response, "工程检查失败");
}
