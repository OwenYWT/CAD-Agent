import { authFetch } from "../../auth";
import type { OnshapeConfig, OnshapeLink } from "../../types/engineering";
import { API_BASE } from "./http";
import { readJson } from "./http";

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
