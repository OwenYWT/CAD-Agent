import { authFetch, API_BASE } from "./http";
import type { EngineeringResult } from "../../types/engineeringTask";

export async function readVerifiedEngineeringBytes(ref: EngineeringResult['artifacts']['engineering_field'], signal?: AbortSignal): Promise<ArrayBuffer> {
  const response = await authFetch(`${API_BASE}${ref.url}`, {signal});
  if (!response.ok) throw new Error(`工程场数据读取失败（HTTP ${response.status}）`);
  const bytes = await response.arrayBuffer();
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2,'0')).join('');
  if (bytes.byteLength !== ref.size_bytes || hash !== ref.sha256) throw new Error('工程场数据完整性校验失败');
  return bytes;
}

export async function readEngineeringFieldData(ref: EngineeringResult['artifacts']['engineering_field'], signal?: AbortSignal): Promise<unknown> {
  return JSON.parse(new TextDecoder().decode(await readVerifiedEngineeringBytes(ref,signal)));
}
